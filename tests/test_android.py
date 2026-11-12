"""Tests for the Android entry points and the Settings page (no phone needed)."""

import json
import os
import sqlite3
import sys
import types
import urllib.request

import pytest

from datasell import android, daily, db, settings, vision
from datasell.app import create_app


@pytest.fixture
def phone(tmp_path, monkeypatch):
    """A fake app files directory, with env/module state restored afterwards."""
    files = tmp_path / "files"
    files.mkdir()
    for var in ("DATASELL_DB", "DATASELL_BACKUP_DIR", "DATASELL_SETTINGS", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(db, "DEFAULT_DB", db.DEFAULT_DB)
    monkeypatch.setattr(db, "BACKUP_DIR", db.BACKUP_DIR)
    monkeypatch.setattr(daily, "on_finished", [])
    monkeypatch.setattr(android, "_server", None)
    return str(files)


def fake_java(monkeypatch, result=None, error=None):
    calls = []

    class Bridge:
        @staticmethod
        def identify(*args):
            calls.append(args)
            if error:
                raise Exception(error)
            return json.dumps(result)

    mod = types.ModuleType("java")
    mod.jclass = lambda name: Bridge
    monkeypatch.setitem(sys.modules, "java", mod)
    return calls


def test_start_serves_app_and_uses_phone_storage(phone):
    port = android.start(phone)
    assert android.start(phone) == port  # second call reuses the server
    html = urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
        f"http://127.0.0.1:{port}/settings").read().decode()
    assert "Back up now" in html and "Documents/Datasell" in html
    assert db.DEFAULT_DB == os.path.join(phone, "datasell.db")
    urllib.request.build_opener(urllib.request.ProxyHandler({})).open(f"http://127.0.0.1:{port}/check").read()
    assert os.path.exists(os.path.join(phone, "datasell.db"))
    android._server.shutdown()


def test_identify_through_java(phone, monkeypatch):
    android._configure(phone)
    with pytest.raises(vision.IdentifyError, match="Settings"):
        android._identify_with_java(b"f", None)
    settings.save(anthropic_api_key="sk-ant-test")
    calls = fake_java(monkeypatch, result={"brand": "Gap", "search_query": "", "broad_query": "Gap Jacket"})
    out = android._identify_with_java(b"front", b"tag", "made in usa")
    assert out["search_query"] == "Gap Jacket"
    key, model, system, schema, front, tag, prompt, base = calls[0]
    assert key == "sk-ant-test" and model == vision.MODEL and json.loads(schema) == vision.SCHEMA
    assert front == "ZnJvbnQ=" and tag == "dGFn" and "made in usa" in prompt and base is None
    fake_java(monkeypatch, error="Your Claude API key is missing or invalid. Check it in Settings.")
    with pytest.raises(vision.IdentifyError, match="invalid"):
        android._identify_with_java(b"front", None)


def test_run_if_due_and_backup_hook(phone):
    android._configure(phone)
    ran = []
    daily.on_finished.append(lambda summary: ran.append(summary))
    assert android.run_if_due(phone) is True       # never ran before
    assert android.run_if_due(phone) is False      # already ran today
    assert len(ran) == 1
    assert os.path.exists(os.path.join(phone, "backup", "datasell.db"))
    assert os.path.exists(os.path.join(phone, "backup", "datasell_solds.csv"))


def test_restore_from_backup_file(phone, tmp_path):
    android._configure(phone)
    c = db.connect()
    db.save_many(c, [{"item_id": "1", "title": "Old item", "price": 40}])
    c.close()
    other = str(tmp_path / "picked.db")
    o = db.connect(other)
    db.save_many(o, [{"item_id": str(i), "title": f"Backup item {i}", "price": 50} for i in range(3)])
    o.close()
    assert android.restore_from(phone, other) == ""
    assert db.query(db.connect())[1] == 3
    junk = str(tmp_path / "junk.db")
    sqlite3.connect(junk).execute("CREATE TABLE x (a)").connection.commit()
    assert "isn't a Datasell backup" in android.restore_from(phone, junk)
    with open(str(tmp_path / "text.db"), "w") as f:
        f.write("hello")
    assert "isn't a Datasell backup" in android.restore_from(phone, str(tmp_path / "text.db"))


def test_settings_page_saves_key(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("DATASELL_SETTINGS", str(tmp_path / "settings.json"))
    app = create_app(str(tmp_path / "s.db"))
    app.testing = True
    c = app.test_client()
    assert b"Paste your key" in c.get("/settings").data
    assert b"Add it in Settings" in c.get("/check").data
    r = c.post("/settings", data={"api_key": "hello"}, follow_redirects=True)
    assert b"doesn" in r.data and settings.api_key() is None
    r = c.post("/settings", data={"api_key": "sk-ant-abcdefghijklmnop1234"}, follow_redirects=True)
    assert b"API key saved" in r.data and b"sk-ant-abc" in r.data and b"1234" in r.data
    assert b"abcdefghijklmnop" not in r.data  # never shown in full
    assert settings.api_key() == "sk-ant-abcdefghijklmnop1234" and vision.api_configured()
    assert b"Add it in Settings" not in c.get("/check").data
    c.post("/settings", data={"remove_key": "1"})
    assert settings.api_key() is None
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fromenv0000")
    assert b"environment variable" in c.get("/settings").data


def test_browser_made_thumbnail_is_used(tmp_path, monkeypatch):
    from test_check import IDENT, fake_ebay, jpeg
    from datasell import market
    import io
    app = create_app(str(tmp_path / "t.db"))
    app.testing = True
    app.config["IDENTIFY"] = lambda f, t, h="": dict(IDENT)
    app.config["MARKET_CHECK"] = lambda q: market.check(q, fetch_page=fake_ebay()[0])
    thumb = "data:image/jpeg;base64,/9j/THUMB"
    c = app.test_client()
    c.post("/check", data={"front": (io.BytesIO(jpeg()), "f.jpg"), "front_thumb": thumb},
           content_type="multipart/form-data")
    assert db.connect(str(tmp_path / "t.db")).execute("SELECT front_thumb FROM appraisals").fetchone()[0] == thumb
