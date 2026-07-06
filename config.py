"""Central configuration via pydantic-settings.

Precedence (highest first): real environment variables > the config file
(`.env` format) > built-in defaults. The config file path defaults to
`~/.claude-fleet/.env` and can be overridden with the app's `--config/-c` flag
(or the `FLEET_CONFIG` environment variable).

For backwards compatibility the rest of the code reads module-level constants
(`config.FLEET_ROOT`, `config.DB_PATH`, ...). These are (re)assigned from the
active `Settings` instance by `load()`, so anything that reads them at call time
picks up the loaded config. Do NOT read them at import time in other modules.
"""
import os

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_CONFIG_PATH = os.path.expanduser("~/.claude-fleet/.env")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(case_sensitive=False, extra="ignore")

    # Paths
    root: str = Field(
        default=os.path.expanduser("~/.claude-fleet/instances"),
        validation_alias="FLEET_ROOT",
    )
    db_path: str = Field(
        default=os.path.expanduser("~/.claude-fleet/fleet.db"),
        validation_alias="FLEET_DB",
    )
    secrets_root: str = Field(
        default=os.path.expanduser("~/.claude-fleet/secrets"),
        validation_alias="FLEET_SECRETS",
    )

    # Secrets / auth
    secret_key: str | None = Field(default=None, validation_alias="FLEET_SECRET_KEY")
    auth_token: str | None = Field(default=None, validation_alias="FLEET_AUTH_TOKEN")

    # Claude / remote-control
    claude_rc_cmd: str = Field(
        default="claude remote-control", validation_alias="CLAUDE_RC_CMD"
    )
    relay_regex: str = Field(
        default=r"https://claude\.ai/code/session_\S+", validation_alias="RELAY_REGEX"
    )

    # Server
    host: str = Field(default="127.0.0.1", validation_alias="FLEET_HOST")
    port: int = Field(default=8700, validation_alias="FLEET_PORT")

    @field_validator("root", "db_path", "secrets_root")
    @classmethod
    def _absolute(cls, v: str) -> str:
        return os.path.abspath(os.path.expanduser(v))


# Active settings + backwards-compatible module constants (populated by load()).
settings: Settings
FLEET_ROOT: str
DB_PATH: str
SECRETS_ROOT: str
SECRET_KEY: str | None
AUTH_TOKEN: str | None
CLAUDE_RC_CMD: str
RELAY_REGEX: str
HOST: str
PORT: int


def load(config_path: str | None = None) -> Settings:
    """(Re)load configuration from `config_path` (or FLEET_CONFIG, or the
    default), refresh the module constants, and return the Settings."""
    global settings
    path = config_path or os.environ.get("FLEET_CONFIG") or DEFAULT_CONFIG_PATH
    env_file = path if os.path.isfile(path) else None
    settings = Settings(_env_file=env_file)

    globals().update(
        FLEET_ROOT=settings.root,
        DB_PATH=settings.db_path,
        SECRETS_ROOT=settings.secrets_root,
        SECRET_KEY=settings.secret_key,
        AUTH_TOKEN=settings.auth_token,
        CLAUDE_RC_CMD=settings.claude_rc_cmd,
        RELAY_REGEX=settings.relay_regex,
        HOST=settings.host,
        PORT=settings.port,
    )
    return settings


# Eager default load so `import config` works without an explicit load() call.
load()
