"""Entry points for the Android app (called from Java through Chaquopy).

The app keeps everything in its private storage on the phone:
  <files>/datasell.db        the database
  <files>/settings.json      the Claude API key and other settings
  <files>/backup/            nightly backup + CSV, which the Java side copies
                             into the phone's Documents/Datasell folder
"""

from __future__ import annotations

import base64
import json
import logging
import os
import sqlite3
import threading

log = logging.getLogger("datasell.android")

_server = None
_files_dir: str | None = None


def _configure(files_dir: str) -> None:
    """Point the app at the phone's storage. Must run before other imports."""
    global _files_dir
    _files_dir = files_dir
    os.environ.setdefault("DATASELL_DB", os.path.join(files_dir, "datasell.db"))
    os.environ.setdefault("DATASELL_BACKUP_DIR", os.path.join(files_dir, "backup"))
    os.environ.setdefault("DATASELL_SETTINGS", os.path.join(files_dir, "settings.json"))
    from . import db  # in case it was imported before the paths were set

    db.DEFAULT_DB = os.environ["DATASELL_DB"]
    db.BACKUP_DIR = os.environ["DATASELL_BACKUP_DIR"]


def _identify_with_java(front_jpeg: bytes, tag_jpeg: bytes | None, hint: str = "") -> dict:
    """Same as vision.identify, but through the Java SDK (the Python SDK
    can't be installed on Android)."""
    from . import settings, vision

    key = settings.api_key()
    if not key:
        raise vision.IdentifyError("Add your Claude API key in Settings to use photo checks.")
    from java import jclass
    bridge = jclass("com.datasell.app.ClaudeBridge")
    try:
        text = bridge.identify(
            key, vision.MODEL, vision.SYSTEM, json.dumps(vision.SCHEMA),
            base64.b64encode(front_jpeg).decode(),
            base64.b64encode(tag_jpeg).decode() if tag_jpeg else None,
            vision.prompt_text(hint), None)
    except Exception as e:  # ClaudeBridge.ClaudeError carries a user-facing message
        msg = getattr(e, "getMessage", lambda: str(e))()
        raise vision.IdentifyError(str(msg)) from e
    try:
        return vision.finish(json.loads(str(text)))
    except json.JSONDecodeError as e:
        raise vision.IdentifyError("Claude returned an unreadable answer. Please try again.") from e


def start(files_dir: str, on_backup=None) -> int:
    """Start the web app on a free local port and return the port.

    `on_backup` (a Java object with a run() method) is called after each
    update, so the backup can be copied to the Documents folder.
    """
    global _server
    if _server is not None:
        return _server.server_port
    _configure(files_dir)
    logging.basicConfig(level=logging.INFO)

    from werkzeug.serving import make_server

    from . import daily, db
    from .app import create_app

    db.restore_if_missing()
    app = create_app()
    app.config["IDENTIFY"] = _identify_with_java
    app.config["SCHEDULER_ON"] = True  # the phone wakes the app up to run it
    app.config["ANDROID"] = True
    if on_backup is not None:
        daily.on_finished.append(lambda summary: on_backup.run())

    _server = make_server("127.0.0.1", 0, app, threaded=True)
    threading.Thread(target=_server.serve_forever, daemon=True, name="datasell-web").start()
    log.info("Datasell running on port %s", _server.server_port)
    return _server.server_port


def run_if_due(files_dir: str) -> bool:
    """Called by the Android background worker. Returns True if it ran."""
    _configure(files_dir)
    from . import daily

    return daily.run_if_due() is not None


def backup_now(files_dir: str) -> str | None:
    _configure(files_dir)
    from . import db

    conn = db.connect()
    try:
        return db.backup(conn)
    finally:
        conn.close()


def restore_from(files_dir: str, source_path: str) -> str:
    """Replace the database with a backup file the user picked.

    Returns "" on success, or an error message.
    """
    _configure(files_dir)
    from . import db

    try:
        src = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True)
        try:
            tables = {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "listings" not in tables:
                return "That file isn't a Datasell backup."
            dest = db.connect()
            try:
                src.backup(dest)
            finally:
                dest.close()
        finally:
            src.close()
    except sqlite3.DatabaseError:
        return "That file isn't a Datasell backup."
    db.connect().close()  # adds any columns newer versions need
    return ""
