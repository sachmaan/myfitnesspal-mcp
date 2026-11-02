import os
from pathlib import Path

import platformdirs

APP_NAME = "myfitnesspal-mcp"


def config_dir() -> Path:
    path = Path(platformdirs.user_config_dir(APP_NAME))
    path.mkdir(parents=True, exist_ok=True)
    return path


def data_dir() -> Path:
    override = os.environ.get("MFP_MCP_DATA_DIR")
    if override:
        path = Path(override)
    else:
        path = Path(platformdirs.user_data_dir(APP_NAME))
    path.mkdir(parents=True, exist_ok=True)
    return path


def cookies_path() -> Path:
    return config_dir() / "cookies.json"


def database_path() -> Path:
    return data_dir() / "data.db"


def cookie_env() -> str | None:
    return os.environ.get("MFP_COOKIE")


def username_env() -> str | None:
    return os.environ.get("MFP_USERNAME")


def impersonate() -> str:
    return os.environ.get("MFP_IMPERSONATE", "chrome")


def sync_days() -> int:
    return int(os.environ.get("MFP_SYNC_DAYS", "30"))


# --- platform mode (sachmaan/personal-cluster chat.md): cookie from the auth service, ---------
# --- one intent per write (AD-06, AD-08). All off when AUTH_URL / INTENTS_URL are unset. ------
def auth_url() -> str | None:
    """Base URL of the auth service; set, the session cookie comes from
    GET <AUTH_URL>/credentials/<AUTH_CREDENTIAL> and nothing is saved to disk."""
    return os.environ.get("AUTH_URL") or None


def auth_key() -> str | None:
    return os.environ.get("AUTH_KEY") or None


def auth_credential() -> str:
    return os.environ.get("AUTH_CREDENTIAL", "mfp")


def auth_page_hint() -> str:
    return os.environ.get(
        "AUTH_PAGE_HINT", "sign in to MyFitnessPal again on the auth service page"
    )


def intents_url() -> str | None:
    """lake-mcp's POST /actions; set, every write tool posts one intent before it returns."""
    return os.environ.get("INTENTS_URL") or None


def intents_default_actor() -> str:
    return os.environ.get("INTENTS_DEFAULT_ACTOR", "unknown")
