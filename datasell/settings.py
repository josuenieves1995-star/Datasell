"""Settings the user can change in the app (stored next to the database).

Environment variables still win, so desktop/Termux setups keep working.
"""

from __future__ import annotations

import json
import os

from . import db


def path() -> str:
    return os.environ.get("DATASELL_SETTINGS") or os.path.join(
        os.path.dirname(os.path.abspath(db.DEFAULT_DB)), "settings.json")


def load() -> dict:
    try:
        with open(path(), encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save(**changes) -> None:
    data = load()
    for k, v in changes.items():
        if v is None:
            data.pop(k, None)
        else:
            data[k] = v
    p = path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, p)


def api_key() -> str | None:
    return os.environ.get("ANTHROPIC_API_KEY") or load().get("anthropic_api_key") or None


def api_key_source() -> str | None:
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "environment"
    if load().get("anthropic_api_key"):
        return "settings"
    return None
