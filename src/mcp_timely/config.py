"""Configuration loaded from environment variables."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Server transport
    transport: Literal["stdio", "http"] = "stdio"
    host: str = "127.0.0.1"
    port: int = 8000

    # Bearer token auth for the MCP endpoint
    mcp_api_key: SecretStr = SecretStr("")

    # Timely OAuth application + token persistence
    timely_client_id: str = ""
    timely_client_secret: SecretStr = SecretStr("")
    timely_token_file: Path = Path("timely_tokens.json")
    timely_api_base: str = "https://api.timelyapp.com"

    model_config = {"env_prefix": "", "case_sensitive": False}

    @model_validator(mode="after")
    def require_api_key_for_http(self) -> "Settings":
        if self.transport == "http" and not self.mcp_api_key.get_secret_value():
            raise ValueError(
                "MCP_API_KEY is required when TRANSPORT=http. "
                "Refusing to start an unauthenticated server."
            )
        return self


settings = Settings()
