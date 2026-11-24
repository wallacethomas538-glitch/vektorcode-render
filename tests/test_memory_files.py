"""Memory files, instructions, and mood (2026-11-23, Wallace's order).

Root cause under test: the memory table keys rows by (kind, key), so a
plain save with a repeated key silently REPLACED the previous memory and
the list never grew — Wallace could save one memory and then "not add
anything else". The fix: mode "new" saves never replace (auto-suffix),
the chat default-title path accumulates, and every memory also lives in
an editable MEMORY.md mirror. Instructions (WORKING.md) and mood are
loaded into the system prompt.

Lab tests only: no live Render, no live Supabase. Stores run against
real temporary SQLite files; file paths point at a temporary state dir.
"""
import io
import json
from pathlib import Path

import pytest

import app
from vektor_cloud.storage import DashboardStore


# ---------------------------------------------------------------------------
# Fake-handler harness (mirrors tests/test_web_access.py)
# ---------------------------------------------------------------------------

def make_handler(method="POST", path="/api/chat", body=None, headers=None):
    h = app.Handler.__new__(app.Handler)
    h.path = path
    h.command = method
    raw = json.dumps(body if body is not None else {}).encode()
    h.rfile = io.BytesIO(raw)
    h.headers = dict(headers or {})
    h.headers["Content-Length"] = str(len(raw))
    h.wfile = io.BytesIO()
    h._status = None
    h._resp_headers = {}

    def send_response(status, message=None):
        h._status = status

    def send_header(key, value):
        h._resp_headers[key] = value

    def end_headers():
        pass

    def send_error(code, message=None):
        h._status = code
        h.wfile.write(json.dumps({"error": message}).encode())

    h.send_response = send_response
    h.send_header = send_header
    h.end_headers = end_headers
    h.send_error = send_error
    return h


def response_json(handler):
    return json.loads(handler.wfile.getvalue().decode())


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mem_env(monkeypatch, tmp_path):
    """Temporary SQLite DB, no Supabase, temporary VEKTOR_STATE, auth open."""
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_KEY", raising=False)
    monkeypatch.delenv("RENDER", raising=False)
    monkeypatch.delenv("VEKTORCODE_TOOL_PASSWORD", raising=False)
    monkeypatch.setattr(app, "DB", tmp_path / "test.db")
    monkeypatch.setattr(app, "VEKTOR_STATE", tmp_path / "state")
    app._WEB_TOOL_COUNTS.clear()
    return tmp_path


# ---------------------------------------------------------------------------
# 1. Root cause: same key twice must not silently replace (mode "new")
# ---------------------------------------------------------------------------

def test_save_unique_same_key_creates_new_rows(mem_env):
    store = DashboardStore(mem_env / "m.db")
    r1 = store.memory_save_unique("fact", "note", "first thing")
    r2 = store.memory_save_unique("fact", "note", "second thing")
    r3 = store.memory_save_unique("fact", "note", "third thing")
    assert r1["key"] == "note"
    assert r2["key"] == "note (2)"
    assert r3["key"] == "note (3)"
    assert len(store.memory_list()) == 3
    # Same value again: no duplicate row.
    r4 = store.memory_save_unique("fact", "note", "first thing")
    assert r4["key"] == "note"
    assert len(store.memory_list()) == 3


def test_plain_save_still_replaces_for_updates(mem_env):
    """Update path is unchanged: same key = update, list stays at one."""
    store = DashboardStore(mem_env / "m.db")
    store.memory_save("fact", "note", "first")
    store.memory_save("fact", "note", "second")
    assert len(store.memory_list()) == 1
    assert store.memory_get("fact", "note")["value"] == "second"


def test_fifty_plus_memories(mem_env):
    store = DashboardStore(mem_env / "m.db")
    for i in range(55):
        store.memory_save_unique("fact", f"key-{i:02d}", f"value {i}")
    for i in range(5):
        store.memory_save_unique("fact", "repeated", f"repeat {i}")
    rows = store.memory_list()
    assert len(rows) == 60
    assert len({(r["kind"], r["key"]) for r in rows}) == 60


# ---------------------------------------------------------------------------
# 2. MEMORY.md file mirror: render / parse / edit on disk / import
# ---------------------------------------------------------------------------

def test_memory_file_roundtrip(mem_env):
    store = app.store()
    store.memory_save("fact", "wallace", "created VektorCode")
    store.memory_save("preference", "voice", "short and precise")
    store.memory_save("decision", "deploy", "manual only")
    assert app.sync_memory_file() is True
    path = app.memory_file_path()
    assert path.is_file()
    text = path.read_text(encoding="utf-8")
    assert "## [fact] wallace" in text
    assert "created VektorCode" in text
    assert "## [preference] voice" in text
    # Parse is the inverse of render.
    rows = app.parse_memory_file(text)
    assert ("fact", "wallace", "created VektorCode") in rows
    assert ("preference", "voice", "short and precise") in rows
    assert len(rows) == 3


def test_edit_file_on_disk_then_import(mem_env):
    store = app.store()
    store.memory_save("fact", "wallace", "created VektorCode")
    app.sync_memory_file()
    path = app.memory_file_path()
    # Wallace edits the file directly: change a value, add a memory.
    text = path.read_text(encoding="utf-8")
    text = text.replace("created VektorCode", "created VektorCode and VektorFlow")
    text += "\n## [lesson] testing\ntest everything before pushing\n"
    path.write_text(text, encoding="utf-8")
    imported, skipped = app.import_memory_file()
    assert imported == 2
    assert skipped == 0
    assert store.memory_get("fact", "wallace")["value"] == (
        "created VektorCode and VektorFlow")
    assert store.memory_get("lesson", "testing")["value"] == (
        "test everything before pushing")


def test_multiline_value_roundtrip(mem_env):
    store = app.store()
    store.memory_save("fact", "multi", "line one\nline two\nline three")
    app.sync_memory_file()
    imported, skipped = app.import_memory_file()
    assert imported == 1 and skipped == 0
    assert store.memory_get("fact", "multi")["value"] == (
        "line one\nline two\nline three")


# ---------------------------------------------------------------------------
# 3. HTTP endpoints: mode new/update, chat default title, file, mood
# ---------------------------------------------------------------------------

def test_memory_endpoint_mode_new_suffixes(mem_env):
    for value in ("one", "two", "three"):
        h = make_handler("POST", "/api/memory",
                         {"kind": "fact", "key": "note", "value": value,
                          "mode": "new"})
        h.do_POST()
        assert h._status == 200, response_json(h)
    h = make_handler("GET", "/api/memory")
    h.do_GET()
    assert h._status == 200
    rows = response_json(h)["memories"]
    assert len(rows) == 3
    assert sorted(r["key"] for r in rows) == ["note", "note (2)", "note (3)"]


def test_memory_endpoint_mode_update_replaces(mem_env):
    h = make_handler("POST", "/api/memory",
                     {"kind": "fact", "key": "note", "value": "one",
                      "mode": "update"})
    h.do_POST()
    assert h._status == 200
    h = make_handler("POST", "/api/memory",
                     {"kind": "fact", "key": "note", "value": "two",
                      "mode": "update"})
    h.do_POST()
    assert h._status == 200
    h = make_handler("GET", "/api/memory")
    h.do_GET()
    rows = response_json(h)["memories"]
    assert len(rows) == 1
    assert rows[0]["value"] == "two"


def test_memory_endpoint_bad_mode_rejected(mem_env):
    h = make_handler("POST", "/api/memory",
                     {"kind": "fact", "key": "note", "value": "one",
                      "mode": "bogus"})
    h.do_POST()
    assert h._status == 400


def test_chat_save_default_title_accumulates(mem_env):
    # The chat "save to memory" path with no explicit title used to
    # overwrite (fact, "Saved note") every time.
    for i in range(3):
        h = make_handler("POST", "/api/memory/save",
                         {"content": f"chat note {i}"})
        h.do_POST()
        assert h._status == 200, response_json(h)
    h = make_handler("GET", "/api/memory")
    h.do_GET()
    rows = response_json(h)["memories"]
    assert len(rows) == 3
    assert sorted(r["key"] for r in rows) == [
        "Saved note", "Saved note (2)", "Saved note (3)"]


def test_chat_save_explicit_title_still_updates(mem_env):
    for value in ("v1", "v2"):
        h = make_handler("POST", "/api/memory/save",
                         {"title": "my note", "content": value})
        h.do_POST()
        assert h._status == 200
    h = make_handler("GET", "/api/memory")
    h.do_GET()
    rows = response_json(h)["memories"]
    assert len(rows) == 1
    assert rows[0]["value"] == "v2"


def test_memory_file_endpoints(mem_env):
    h = make_handler("POST", "/api/memory",
                     {"kind": "fact", "key": "wallace",
                      "value": "created VektorCode", "mode": "new"})
    h.do_POST()
    assert h._status == 200
    # The mirror file is written on save.
    assert app.memory_file_path().is_file()
    h = make_handler("GET", "/api/memory/file")
    h.do_GET()
    assert h._status == 200
    body = response_json(h)
    assert "## [fact] wallace" in body["content"]
    assert body["count"] == 1
    # Edit through the API (UI textarea path), then it is in the DB.
    edited = body["content"].replace(
        "created VektorCode", "created VektorCode, edited")
    h = make_handler("POST", "/api/memory/file", {"content": edited})
    h.do_POST()
    assert h._status == 200, response_json(h)
    assert response_json(h)["imported"] == 1
    assert app.store().memory_get("fact", "wallace")["value"] == (
        "created VektorCode, edited")
    # Edit on disk, then reload from file.
    path = app.memory_file_path()
    path.write_text(path.read_text(encoding="utf-8") +
                    "\n## [lesson] disk\nedited on disk\n", encoding="utf-8")
    h = make_handler("POST", "/api/memory/file/import", {})
    h.do_POST()
    assert h._status == 200
    assert response_json(h)["imported"] == 2
    assert app.store().memory_get("lesson", "disk")["value"] == "edited on disk"


def test_instructions_endpoints_and_prompt(mem_env):
    h = make_handler("GET", "/api/instructions")
    h.do_GET()
    assert h._status == 200
    body = response_json(h)
    assert body["saved"] is False  # starter text shown, nothing saved yet
    assert "Short and precise" in body["content"]
    h = make_handler("POST", "/api/instructions",
                     {"content": "# How to work with Wallace\n\n- Always test first.\n"})
    h.do_POST()
    assert h._status == 200
    prompt = app.vektor_system_prompt()
    assert "# How to work with Wallace (WORKING.md)" in prompt
    assert "Always test first." in prompt
    h = make_handler("POST", "/api/instructions", {"content": "   "})
    h.do_POST()
    assert h._status == 400


def test_mood_endpoints_and_prompt(mem_env):
    assert app.get_mood() == "focused"  # default, nothing saved
    h = make_handler("POST", "/api/mood", {"mood": "calm"})
    h.do_POST()
    assert h._status == 200
    assert response_json(h)["mood"] == "calm"
    prompt = app.vektor_system_prompt()
    assert "# Mood (Wallace's setting: calm)" in prompt
    assert "calm and steady" in prompt
    h = make_handler("GET", "/api/mood")
    h.do_GET()
    assert response_json(h)["mood"] == "calm"
    assert set(response_json(h)["moods"]) == set(app.MOODS)
    h = make_handler("POST", "/api/mood", {"mood": "angry"})
    h.do_POST()
    assert h._status == 400
    assert app.get_mood() == "calm"  # rejected value did not stick


# ---------------------------------------------------------------------------
# 4. Restart survival: fresh store, same DB + same state dir
# ---------------------------------------------------------------------------

def test_restart_survival_all_new_state(mem_env):
    store = app.store()
    for i in range(10):
        store.memory_save_unique("fact", f"k-{i}", f"v-{i}")
    store.set_setting("mood", "hyped")
    path = app.instructions_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# How to work with Wallace\n\n- Test first.\n",
                    encoding="utf-8")
    assert app.sync_memory_file() is True
    assert app.memory_file_path().is_file()
    # Restart: drop the cached store so a fresh one opens the same DB file.
    app._STORES.clear()
    store2 = app.store()
    assert len(store2.memory_list()) == 10
    assert store2.memory_get("fact", "k-3")["value"] == "v-3"
    assert app.get_mood() == "hyped"
    assert "Test first." in app.load_instructions()
    prompt = app.vektor_system_prompt()
    assert "[fact] k-0: v-0" in prompt
    assert "# Mood (Wallace's setting: hyped)" in prompt
    assert "Test first." in prompt
    # The mirror file still exists after the "restart".
    assert app.memory_file_path().is_file()
