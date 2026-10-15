import json
import os
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ON_VERCEL = bool(os.environ.get("VERCEL"))
DATA_DIR = Path(os.environ.get("EYEASD_DATA", "/tmp/eyeasd" if ON_VERCEL else ROOT / "data"))
CONFIG_PATH = DATA_DIR / "config.json"


def _local_credentials() -> dict:
    cfg = json.loads(CONFIG_PATH.read_text()) if CONFIG_PATH.exists() else {}
    if "admin_password" not in cfg or "secret_key" not in cfg:
        cfg.setdefault("admin_password", secrets.token_urlsafe(12))
        cfg.setdefault("secret_key", secrets.token_hex(32))
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2) + "\n")
        CONFIG_PATH.chmod(0o600)
    return cfg


def load_config() -> dict:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    password, secret = os.environ.get("ADMIN_PASSWORD"), os.environ.get("SECRET_KEY")
    if not ON_VERCEL and not (password and secret):
        local = _local_credentials()
        password, secret = password or local["admin_password"], secret or local["secret_key"]

    database_url = os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL")
    return {
        "admin_password": password,
        "secret_key": secret or secrets.token_hex(32),
        "database_url": database_url or f"sqlite:///{DATA_DIR / 'app.db'}",
        "database_ephemeral": ON_VERCEL and not database_url,
    }
