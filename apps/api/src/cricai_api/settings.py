"""Environment-driven settings. LAN-local defaults; no cloud egress (US-L3)."""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CRICAI_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://cricai:cricai@localhost:5432/cricai"
    storage_root: Path = Path("storage")

    # LAN role tokens (US-L3). Empty token = that role cannot authenticate.
    parent_token: str = ""
    coach_token: str = ""
    player_token: str = ""
