"""OAuth2 session against the Timely API.

Spike-verified behavior (2026-07-19): token responses carry no ``expires_in``
and refresh tokens rotate on every refresh. Strategy: use the access token
until a 401, refresh once (persisting the new pair atomically before anything
else), retry once. Token values never appear in errors or logs.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from typing import Any

import httpx
from fastmcp.exceptions import ToolError

from .config import Settings
from .config import settings as default_settings

OOB_REDIRECT = "urn:ietf:wg:oauth:2.0:oob"
REAUTH_HINT = "Run `mcp-timely auth` to (re-)authorize."


class TimelySession:
    """Authenticated Timely API access with rotating-refresh-token persistence."""

    def __init__(
        self,
        settings: Settings = default_settings,
        client: httpx.AsyncClient | None = None,
    ):
        self._settings = settings
        self._client = client or httpx.AsyncClient(
            base_url=settings.timely_api_base, timeout=30.0
        )
        self._tokens: dict[str, Any] | None = None
        self._refresh_lock = asyncio.Lock()
        self._account_id: int | None = None
        self._user_id: int | None = None

    # -- token persistence ---------------------------------------------------

    def _load_tokens(self) -> dict[str, Any]:
        if self._tokens is None:
            path = self._settings.timely_token_file
            if not path.exists():
                raise ToolError(f"No Timely token file at {path}. {REAUTH_HINT}")
            self._tokens = json.loads(path.read_text())
        return self._tokens

    def _save_tokens(self, tokens: dict[str, Any]) -> None:
        path = self._settings.timely_token_file
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tokens-")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(tokens, f)
            os.replace(tmp, path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
        self._tokens = tokens

    # -- oauth ---------------------------------------------------------------

    def authorize_url(self) -> str:
        return (
            f"{self._settings.timely_api_base}/1.1/oauth/authorize"
            f"?response_type=code&redirect_uri={OOB_REDIRECT}"
            f"&client_id={self._settings.timely_client_id}"
        )

    async def exchange_code(self, code: str) -> None:
        await self._token_request(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": OOB_REDIRECT,
            }
        )

    async def _token_request(self, grant: dict[str, str]) -> None:
        resp = await self._client.post(
            "/1.1/oauth/token",
            data={
                **grant,
                "client_id": self._settings.timely_client_id,
                "client_secret": self._settings.timely_client_secret.get_secret_value(),
            },
        )
        if resp.status_code != 200:
            # Deliberately omits the response body — it can quote token material.
            raise ToolError(
                f"Timely token request failed (HTTP {resp.status_code}). {REAUTH_HINT}"
            )
        self._save_tokens(resp.json())

    async def _refresh_once(self, seen_access_token: str) -> None:
        async with self._refresh_lock:
            if self._load_tokens().get("access_token") != seen_access_token:
                return  # a concurrent call already refreshed
            refresh_token = str(self._load_tokens().get("refresh_token", ""))
            await self._token_request(
                {"grant_type": "refresh_token", "refresh_token": refresh_token}
            )

    # -- requests ------------------------------------------------------------

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> httpx.Response:
        token = str(self._load_tokens().get("access_token", ""))
        resp = await self._authed(method, path, token, params, json_body)
        if resp.status_code == 401:
            await self._refresh_once(token)
            fresh = str(self._load_tokens().get("access_token", ""))
            resp = await self._authed(method, path, fresh, params, json_body)
        if resp.status_code == 401:
            raise ToolError(f"Timely authorization expired. {REAUTH_HINT}")
        if resp.status_code == 429:
            retry_after = resp.headers.get("retry-after", "unknown")
            raise ToolError(
                f"Timely rate limit hit (HTTP 429, retry-after: {retry_after}); "
                "try again later."
            )
        if resp.status_code >= 400:
            raise ToolError(
                f"Timely API error on {method} {path}: HTTP {resp.status_code}"
            )
        return resp

    async def _authed(
        self,
        method: str,
        path: str,
        token: str,
        params: dict[str, Any] | None,
        json_body: dict[str, Any] | None,
    ) -> httpx.Response:
        return await self._client.request(
            method,
            path,
            headers={"Authorization": f"Bearer {token}"},
            params=params,
            json=json_body,
        )

    async def get(
        self, path: str, *, params: dict[str, Any] | None = None
    ) -> httpx.Response:
        return await self.request("GET", path, params=params)

    async def post(
        self, path: str, *, json_body: dict[str, Any] | None = None
    ) -> httpx.Response:
        return await self.request("POST", path, json_body=json_body)

    # -- me-scoping ----------------------------------------------------------

    async def account_id(self) -> int:
        if self._account_id is None:
            accounts = (await self.get("/1.1/accounts")).json()
            if not accounts:
                raise ToolError("The authorized Timely user has no accounts.")
            # ponytail: first account wins; add TIMELY_ACCOUNT_ID env override if
            # anyone with multiple accounts ever needs it
            self._account_id = int(accounts[0]["id"])
        return self._account_id

    async def user_id(self) -> int:
        if self._user_id is None:
            acc = await self.account_id()
            me = (await self.get(f"/1.1/{acc}/users/current")).json()
            self._user_id = int(me["id"])
        return self._user_id
