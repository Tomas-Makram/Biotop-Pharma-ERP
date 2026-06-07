from pathlib import Path
import json
import shutil
from datetime import datetime
from sqlalchemy.orm import Session
from .db import SessionLocal, DB_PATH

BASE_DIR = Path(__file__).resolve().parents[1]
CONFIG_PATH = BASE_DIR / "config.json"
BACKUPS_DIR = BASE_DIR / "backups"

DEFAULT_SETTINGS = {
    "company_name": "شركة توزيع أدوية",
    "address": "",
    "phone": "",
    "logo_path": "",
    "print_paper_size": "A4",
}


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def load_settings():
    if not CONFIG_PATH.exists():
        save_settings(DEFAULT_SETTINGS)
        return DEFAULT_SETTINGS.copy()
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        data = json.load(f)
    merged = DEFAULT_SETTINGS.copy()
    merged.update(data or {})
    return merged


def save_settings(data):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with CONFIG_PATH.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def ensure_backups_dir():
    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)


def list_backups():
    ensure_backups_dir()
    return sorted(BACKUPS_DIR.glob("erp_*.db"), reverse=True)


def backup_db():
    ensure_backups_dir()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    target = BACKUPS_DIR / f"erp_{stamp}.db"
    shutil.copy2(DB_PATH, target)
    return target.name


def restore_db(filename):
    ensure_backups_dir()
    source = BACKUPS_DIR / filename
    if not source.exists():
        return False
    shutil.copy2(source, DB_PATH)
    return True
