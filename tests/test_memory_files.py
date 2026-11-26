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


# ---------------------------------------------------------------------------
# 5. Follow-up 1 (Big Chat CHANGES REQUESTED on PR #17, 2026-11-23)
# ---------------------------------------------------------------------------

def test_unique_save_is_concurrency_safe(mem_env):
    """Finding 1: 40 simultaneous same-key saves must create 40 rows."""
    import threading
    store = app.store()
    errors = []

    def save(i):
        try:
            store.memory_save_unique("fact", "note", f"value {i}")
        except Exception as exc:  # noqa: BLE001 - collected, asserted below
            errors.append(exc)

    threads = [threading.Thread(target=save, args=(i,)) for i in range(40)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    rows = store.memory_list()
    assert len(rows) == 40
    assert len({(r["kind"], r["key"]) for r in rows}) == 40
    assert {r["value"] for r in rows} == {f"value {i}" for i in range(40)}


def test_import_replaces_removed_memories(mem_env):
    """Finding 2: a memory removed from the file must leave the DB."""
    store = app.store()
    for key in ("a", "b", "c"):
        store.memory_save("fact", key, f"value {key}")
    app.sync_memory_file()
    path = app.memory_file_path()
    path.write_text(
        "# VektorCode memories\n\n"
        "## [fact] a\nvalue A2\n\n"
        "## [fact] c\nvalue c\n\n"
        "## [fact] d\nvalue d\n", encoding="utf-8")
    imported, skipped = app.import_memory_file()
    assert (imported, skipped) == (3, 0)
    rows = {(r["kind"], r["key"]): r["value"]
            for r in store.memory_list()}
    assert rows == {("fact", "a"): "value A2",
                    ("fact", "c"): "value c",
                    ("fact", "d"): "value d"}
    # The mirror regenerates from the DB: B stays gone.
    assert "## [fact] b" not in path.read_text(encoding="utf-8")


def test_import_with_bad_row_aborts_without_touching_db(mem_env):
    """Finding 2: a bad row aborts the whole import; old data survives."""
    store = app.store()
    store.memory_save("fact", "keep", "stays")
    path = app.memory_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("## [fact] keep\nstays\n\n## [bogus] bad\nnope\n",
                    encoding="utf-8")
    with pytest.raises(ValueError):
        app.import_memory_file()
    assert store.memory_get("fact", "keep")["value"] == "stays"
    assert len(store.memory_list()) == 1


def test_import_empty_file_changes_nothing(mem_env):
    """Finding 2: an empty file imports nothing and never wipes the DB."""
    store = app.store()
    store.memory_save("fact", "keep", "stays")
    path = app.memory_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")
    assert app.import_memory_file() == (0, 0)
    assert len(store.memory_list()) == 1


def test_memory_file_endpoint_rejects_content_without_memories(mem_env):
    """Finding 2: the UI textarea cannot wipe the DB with no memories."""
    h = make_handler("POST", "/api/memory/file",
                     {"content": "just some text, no headings\n"})
    h.do_POST()
    assert h._status == 400
    assert not app.memory_file_path().exists()
    assert len(app.store().memory_list()) == 0


def test_prompt_loads_each_memory_once(mem_env):
    """Finding 3: a DB memory appears exactly once in the prompt."""
    store = app.store()
    store.memory_save("fact", "wallace", "created VektorCode")
    app.sync_memory_file()
    user_path = app.VEKTOR_STATE / "memories" / "USER.md"
    user_path.parent.mkdir(parents=True, exist_ok=True)
    user_path.write_text("Wallace likes short answers.\n", encoding="utf-8")
    prompt = app.vektor_system_prompt()
    assert prompt.count("created VektorCode") == 1
    assert "# Long-term memory" in prompt
    assert "## MEMORY.md" not in prompt
    assert "Wallace likes short answers." in prompt  # USER.md still loads


def test_instructions_survive_without_the_file(mem_env):
    """Finding 4: instructions live in the DB setting, not just the file."""
    h = make_handler("POST", "/api/instructions",
                     {"content": "# How to work with Wallace\n\n"
                                "- Always test first.\n"})
    h.do_POST()
    assert h._status == 200
    assert response_json(h)["durable"] is True
    h = make_handler("GET", "/api/instructions")
    h.do_GET()
    body = response_json(h)
    assert body["saved"] is True and body["durable"] is True
    # The file is gone (Render redeploy wipes it): instructions survive.
    app.instructions_file_path().unlink()
    assert app.load_instructions().strip().endswith("Always test first.")
    assert app.instructions_are_durable() is True
    # Restart: fresh store, same DB + state dir.
    app._STORES.clear()
    assert "Always test first." in app.load_instructions()
    assert app.instructions_are_durable() is True


def test_instructions_file_fallback_and_migration(mem_env):
    """Finding 4: legacy file-only instructions load, then migrate to DB."""
    path = app.instructions_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# How to work with Wallace\n\n- File only.\n",
                    encoding="utf-8")
    assert "File only." in app.load_instructions()
    assert app.instructions_are_durable() is False
    app.migrate_instructions_to_db()
    assert app.instructions_are_durable() is True
    assert "File only." in app.load_instructions()


def test_get_memory_file_does_not_overwrite_disk_edits(mem_env):
    """Finding 5: GET /api/memory/file returns disk content as-is."""
    store = app.store()
    store.memory_save("fact", "wallace", "created VektorCode")
    app.sync_memory_file()
    path = app.memory_file_path()
    path.write_text(path.read_text(encoding="utf-8") +
                    "\n## [lesson] disk\nedited on disk, not yet imported\n",
                    encoding="utf-8")
    h = make_handler("GET", "/api/memory/file")
    h.do_GET()
    assert h._status == 200
    body = response_json(h)
    assert "edited on disk, not yet imported" in body["content"]
    assert body["count"] == 2
    # The DB is untouched by the read.
    assert store.memory_get("lesson", "disk") is None
    assert len(store.memory_list()) == 1


def test_ensure_memory_file_never_overwrites_existing_file(mem_env):
    """Finding 5 (startup): an existing file is left alone."""
    path = app.memory_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("## [fact] mine\nmy edit\n", encoding="utf-8")
    app.ensure_memory_file()
    assert path.read_text(encoding="utf-8") == "## [fact] mine\nmy edit\n"
    # Missing file: created from the DB.
    path.unlink()
    app.store().memory_save("fact", "wallace", "created VektorCode")
    app.ensure_memory_file()
    assert "## [fact] wallace" in path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Fake PostgREST: the Supabase legs, with real conflict semantics
# ---------------------------------------------------------------------------

class FakePostgrest:
    """In-memory PostgREST stand-in for the Supabase memory table."""

    def __init__(self):
        self.rows = {}  # (kind, key) -> row dict

    def request(self, method, table, query=None, payload=None, prefer=None,
                timeout=10):
        from urllib.parse import unquote
        assert table == "vektor_memory"
        query = query or {}
        if method == "GET":
            out = [dict(r) for r in self.rows.values()]
            for name in ("kind", "key"):
                filt = query.get(name)
                if filt and filt.startswith("eq."):
                    want = unquote(filt[3:])
                    out = [r for r in out if r[name] == want]
            if query.get("order", "").startswith("updated_at.desc"):
                out.sort(key=lambda r: r["updated_at"], reverse=True)
            if "limit" in query:
                out = out[:int(query["limit"])]
            return out
        if method == "POST":
            if prefer and "ignore-duplicates" in prefer:
                inserted = []
                for row in payload:
                    if (row["kind"], row["key"]) not in self.rows:
                        self.rows[(row["kind"], row["key"])] = dict(row)
                        inserted.append(dict(row))
                return (inserted
                        if "return=representation" in prefer else None)
            for row in payload:
                self.rows[(row["kind"], row["key"])] = dict(row)
            return None
        if method == "DELETE":
            doomed = []
            for (k, kk) in list(self.rows):
                ok = True
                for name, val in (("kind", k), ("key", kk)):
                    filt = query.get(name)
                    if filt and filt.startswith("eq."):
                        if val != unquote(filt[3:]):
                            ok = False
                if ok:
                    doomed.append((k, kk))
            for key in doomed:
                del self.rows[key]
            return None
        raise AssertionError(f"unexpected method {method}")


def _supabase_store(monkeypatch):
    from vektor_cloud.storage import SupabaseStore
    fake = FakePostgrest()
    store = SupabaseStore("https://example.supabase.co", "test-key")
    monkeypatch.setattr(store, "_request", fake.request)
    return store, fake


def test_supabase_unique_save_suffixes_and_noops(monkeypatch):
    """Finding 1, Supabase leg: ignore-duplicates insert, same-value noop."""
    store, fake = _supabase_store(monkeypatch)
    r1 = store.memory_save_unique("fact", "note", "one")
    r2 = store.memory_save_unique("fact", "note", "two")
    assert r1["key"] == "note" and r2["key"] == "note (2)"
    r3 = store.memory_save_unique("fact", "note", "one")
    assert r3["key"] == "note"
    assert len(fake.rows) == 2


def test_supabase_unique_save_is_concurrency_safe(monkeypatch):
    """Finding 1, Supabase leg: racing inserts lose cleanly, retry wins."""
    import threading
    import time
    store, fake = _supabase_store(monkeypatch)
    # Slow the GETs so every thread observes "absent" before any POST.
    orig_request = fake.request

    def slow_request(method, table, query=None, payload=None, prefer=None,
                     timeout=10):
        if method == "GET":
            time.sleep(0.01)
        return orig_request(method, table, query, payload, prefer, timeout)

    monkeypatch.setattr(store, "_request", slow_request)
    errors = []

    def save(i):
        try:
            store.memory_save_unique("fact", "race", f"value {i}")
        except Exception as exc:  # noqa: BLE001 - collected below
            errors.append(exc)

    threads = [threading.Thread(target=save, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert len(fake.rows) == 10
    assert len({k for (_, k) in fake.rows}) == 10
    assert {r["value"] for r in fake.rows.values()} == {
        f"value {i}" for i in range(10)}


def test_supabase_replace_all_deletes_removed_rows(monkeypatch):
    """Finding 2, Supabase leg: replace deletes rows missing from the set."""
    store, fake = _supabase_store(monkeypatch)
    store.memory_save("fact", "a", "1")
    store.memory_save("fact", "b", "2")
    store.memory_save("fact", "c", "3")
    n = store.memory_replace_all([("fact", "a", "1b"), ("fact", "d", "4")])
    assert n == 2
    assert {(r["kind"], r["key"]) for r in store.memory_list()} == {
        ("fact", "a"), ("fact", "d")}
    assert store.memory_get("fact", "a")["value"] == "1b"


def test_supabase_replace_all_validates_first(monkeypatch):
    """Finding 2, Supabase leg: a bad row aborts before any write."""
    store, fake = _supabase_store(monkeypatch)
    store.memory_save("fact", "keep", "stays")
    with pytest.raises(ValueError):
        store.memory_replace_all([("fact", "keep", "stays"),
                                  ("bogus", "bad", "nope")])
    assert store.memory_get("fact", "keep")["value"] == "stays"
    assert len(fake.rows) == 1

# ---------------------------------------------------------------------------
# Follow-up 2 (Big Chat CHANGES REQUESTED on FU1): strict import parsing
# and truthful instructions durability
# ---------------------------------------------------------------------------

def _seed_two_memories(mem_env):
    """Two memories in the DB + a matching MEMORY.md mirror on disk."""
    store = app.store()
    store.memory_save("fact", "keep-a", "first stays")
    store.memory_save("fact", "keep-b", "second stays")
    app.sync_memory_file()
    return store


def test_import_rejects_empty_value_and_keeps_db(mem_env):
    """FU2 finding 1: an entry with an empty value aborts the import."""
    store = _seed_two_memories(mem_env)
    path = app.memory_file_path()
    path.write_text(
        "## [fact] keep-a\nfirst stays changed\n"
        "## [fact] hollow\n"
        "## [fact] keep-b\nsecond stays\n", encoding="utf-8")
    with pytest.raises(ValueError, match="empty value"):
        app.import_memory_file()
    # Nothing was written: not even the valid entry's changed value.
    assert store.memory_get("fact", "keep-a")["value"] == "first stays"
    assert store.memory_get("fact", "keep-b")["value"] == "second stays"
    assert store.memory_get("fact", "hollow") is None
    assert len(store.memory_list()) == 2


def test_import_rejects_malformed_heading_and_keeps_db(mem_env):
    """FU2 finding 1: an unclosed heading aborts the import."""
    store = _seed_two_memories(mem_env)
    path = app.memory_file_path()
    path.write_text(
        "## [fact] keep-a\nfirst stays\n"
        "## [fact broken\n"
        "## [fact] keep-b\nsecond stays\n", encoding="utf-8")
    with pytest.raises(ValueError, match="malformed"):
        app.import_memory_file()
    assert len(store.memory_list()) == 2
    assert store.memory_get("fact", "keep-a")["value"] == "first stays"
    # Parse-level: empty kind / empty key are malformed too.
    with pytest.raises(ValueError, match="malformed"):
        app.parse_memory_file("## [] key\nvalue\n")
    with pytest.raises(ValueError, match="malformed"):
        app.parse_memory_file("## [fact] \nvalue\n")
    with pytest.raises(ValueError, match="malformed"):
        app.parse_memory_file("## [fact\nvalue\n")


def test_import_rejects_duplicate_key_and_keeps_db(mem_env):
    """FU2 finding 1: a repeated (kind, key) aborts the import."""
    store = _seed_two_memories(mem_env)
    path = app.memory_file_path()
    path.write_text(
        "## [fact] keep-a\nfirst stays\n"
        "## [fact] keep-a\nsecond copy\n", encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate"):
        app.import_memory_file()
    assert len(store.memory_list()) == 2
    assert store.memory_get("fact", "keep-a")["value"] == "first stays"


def test_post_memory_file_rejects_bad_document_before_writing(mem_env):
    """FU2 finding 1: the textarea endpoint rejects a bad document
    before the file on disk is touched."""
    store = _seed_two_memories(mem_env)
    path = app.memory_file_path()
    before = path.read_text(encoding="utf-8")
    h = make_handler("POST", "/api/memory/file", {"content":
        "## [fact] keep-a\nfirst stays\n"
        "## [fact] hollow\n"
        "## [fact] keep-b\nsecond stays\n"})
    h.do_POST()
    assert h._status == 400, response_json(h)
    assert "empty value" in response_json(h)["error"]
    assert path.read_text(encoding="utf-8") == before
    assert len(store.memory_list()) == 2
    # And the import endpoint reports the same rejection.
    path.write_text(
        "## [fact] keep-a\nfirst stays\n"
        "## [fact] keep-a\nsecond copy\n", encoding="utf-8")
    h = make_handler("POST", "/api/memory/file/import")
    h.do_POST()
    assert h._status == 400, response_json(h)
    assert "duplicate" in response_json(h)["error"]
    assert len(store.memory_list()) == 2


def test_get_memory_file_reports_parse_error_not_500(mem_env):
    """FU2 finding 1: a malformed file still reads as-is (200)."""
    _seed_two_memories(mem_env)
    path = app.memory_file_path()
    path.write_text("## [fact] keep-a\nfirst stays\n"
                    "## [fact broken\n", encoding="utf-8")
    h = make_handler("GET", "/api/memory/file")
    h.do_GET()
    assert h._status == 200
    body = response_json(h)
    assert "first stays" in body["content"]
    assert body["count"] == 0
    assert body["parse_error"] and "malformed" in body["parse_error"]
    # The DB is untouched by the read.
    assert len(app.store().memory_list()) == 2


def _supabase_down_env(mem_env, monkeypatch, render=True):
    """mem_env plus a configured-but-unreachable Supabase.

    render=True models the Render deployment, where container SQLite
    is ephemeral; render=False models local/Termux. Render is
    modelled by patching app.on_render (the durability decision's
    seam) rather than the RENDER env var, which would also close
    the auth gate (require_auth refuses password-less requests on
    Render) — the auth gate is not what this test exercises.
    """
    monkeypatch.setattr(app, "on_render", lambda: render)
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_KEY", "test-key")
    app._STORES.clear()
    store = app.store()
    assert store._remote is not None

    def boom(*args, **kwargs):
        raise ConnectionError("supabase down")

    monkeypatch.setattr(store._remote, "_request", boom)
    return store


def test_instructions_report_sqlite_when_supabase_down(mem_env, monkeypatch):
    """FU2 finding 2 (Render): a Supabase outage must NOT report
    durable — the write landed in ephemeral SQLite."""
    _supabase_down_env(mem_env, monkeypatch, render=True)
    saved, durable, backend = app.save_instructions(
        "# How to work with Wallace\n\n- Always test first.\n")
    assert saved is True
    assert durable is False
    assert backend == "sqlite"
    assert app.instructions_are_durable() is False
    # The API says the same, and the content is still readable
    # from the SQLite fallback (it is saved, just not durable).
    h = make_handler("POST", "/api/instructions",
                     {"content": "# How to work with Wallace\n\n"
                                "- Always test first.\n"})
    h.do_POST()
    assert h._status == 200
    body = response_json(h)
    assert body["durable"] is False
    assert body["backend"] == "sqlite"
    h = make_handler("GET", "/api/instructions")
    h.do_GET()
    body = response_json(h)
    assert body["durable"] is False
    # The setting exists — in SQLite, which the backend field says
    # plainly. Durable is False because on Render that SQLite is
    # ephemeral; the content is still readable from the fallback.
    assert body["backend"] == "sqlite"
    assert "Always test first." in body["content"]


def test_instructions_sqlite_is_durable_off_render(mem_env, monkeypatch):
    """FU2 finding 2 (local): off Render the SQLite file persists,
    so a SQLite-backed save is honestly durable there."""
    _supabase_down_env(mem_env, monkeypatch, render=False)
    saved, durable, backend = app.save_instructions(
        "# How to work with Wallace\n\n- Local only.\n")
    assert (saved, durable, backend) == (True, True, "sqlite")
    assert app.instructions_are_durable() is True


class FakeSettingsPostgrest(FakePostgrest):
    """FakePostgrest plus the vektor_settings table."""

    def __init__(self):
        super().__init__()
        self.settings = {}

    def request(self, method, table, query=None, payload=None, prefer=None,
                timeout=10):
        if table == "vektor_settings":
            if method == "GET":
                filt = (query or {}).get("key", "")
                if filt.startswith("eq."):
                    from urllib.parse import unquote
                    key = unquote(filt[3:])
                    return ([{"value": self.settings[key]}]
                            if key in self.settings else [])
                return [{"key": k, "value": v}
                        for k, v in self.settings.items()]
            if method == "POST":
                for row in payload or []:
                    self.settings[row["key"]] = row["value"]
                return None
            raise AssertionError(f"unexpected method {method}")
        return super().request(method, table, query, payload, prefer,
                               timeout)


def test_instructions_report_supabase_when_up(mem_env, monkeypatch):
    """FU2 finding 2: a Supabase write reports durable + supabase."""
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_KEY", "test-key")
    app._STORES.clear()
    store = app.store()
    fake = FakeSettingsPostgrest()
    monkeypatch.setattr(store._remote, "_request", fake.request)
    saved, durable, backend = app.save_instructions(
        "# How to work with Wallace\n\n- Always test first.\n")
    assert (saved, durable, backend) == (True, True, "supabase")
    assert fake.settings.get("instructions", "").endswith(
        "Always test first.\n")
    assert app.instructions_are_durable() is True
    h = make_handler("GET", "/api/instructions")
    h.do_GET()
    body = response_json(h)
    assert body["durable"] is True
    assert body["backend"] == "supabase"
    assert "Always test first." in body["content"]


# ---------------------------------------------------------------------------
# FU3 (Big Chat review of FU2, Nov 23): mistyped headings + single-read GET
# ---------------------------------------------------------------------------

def test_import_rejects_mistyped_heading_and_keeps_db(mem_env):
    """FU3 finding 1: a level-two heading without the opening bracket
    is malformed — it must not be silently swallowed as preamble, or
    a replace import could delete the memory it names."""
    store = app.store()
    store.memory_save("fact", "forgotten", "this must not disappear")
    store.memory_save("fact", "keep", "this remains")
    app.sync_memory_file()
    path = app.memory_file_path()
    # Big Chat's example verbatim: the mistyped heading comes first,
    # before a valid entry, under the file's own level-one header.
    path.write_text(
        "# VektorCode memories\n\n"
        "## fact] forgotten\n"
        "This memory should not disappear.\n\n"
        "## [fact] keep\n"
        "This memory remains.\n", encoding="utf-8")
    with pytest.raises(ValueError, match="malformed"):
        app.import_memory_file()
    # The DB is untouched: the mistyped memory survives.
    assert store.memory_get("fact", "forgotten")["value"] == (
        "this must not disappear")
    assert store.memory_get("fact", "keep")["value"] == "this remains"
    assert len(store.memory_list()) == 2
    # The import endpoint reports the same rejection, DB still intact.
    h = make_handler("POST", "/api/memory/file/import")
    h.do_POST()
    assert h._status == 400, response_json(h)
    assert "malformed" in response_json(h)["error"]
    assert len(store.memory_list()) == 2
    # Parse-level: a mistyped heading inside a value is malformed
    # too, not value text; a bare '## ' heading is malformed too.
    with pytest.raises(ValueError, match="malformed"):
        app.parse_memory_file(
            "## [fact] keep\nfirst line\n## note to self\nsecond line\n")
    with pytest.raises(ValueError, match="malformed"):
        app.parse_memory_file("## [fact] keep\nvalue\n## \n")


def test_subheading_inside_value_stays_value_text(mem_env):
    """FU3 finding 1 guard: a deeper heading inside a value is plain
    text — only level-two headings are heading positions."""
    store = app.store()
    store.memory_save("fact", "notes", "line one\n### detail\nline three")
    app.sync_memory_file()
    imported, skipped = app.import_memory_file()
    assert imported == 1 and skipped == 0
    assert store.memory_get("fact", "notes")["value"] == (
        "line one\n### detail\nline three")


def test_get_instructions_reads_setting_once(mem_env, monkeypatch):
    """FU3 finding 2: GET /api/instructions derives content, saved,
    backend and durability from a single store read — no split reads
    that could disagree if the Supabase fallback flips between them."""
    store = app.store()
    app.save_instructions("# How to work with Wallace\n\n"
                          "- Always test first.\n")
    calls = []
    orig = store.call_with_backend

    def counting(name, *args, **kwargs):
        calls.append(name)
        return orig(name, *args, **kwargs)

    monkeypatch.setattr(store, "call_with_backend", counting)

    def _boom():
        raise AssertionError(
            "split read: GET must not call the old helpers")

    monkeypatch.setattr(app, "load_instructions", _boom)
    monkeypatch.setattr(app, "instructions_status", _boom)
    h = make_handler("GET", "/api/instructions")
    h.do_GET()
    assert h._status == 200, response_json(h)
    body = response_json(h)
    assert "Always test first." in body["content"]
    assert body["saved"] is True
    assert body["backend"] == "sqlite"
    # Off Render the local SQLite file persists, so it is durable.
    assert body["durable"] is True
    assert calls == ["get_setting"]


# ---------------------------------------------------------------------------
# Memory delete + long-value fixes (2026-11-25, Wallace's report)
#
# Wallace: memories "won't disappear" (delete says deleted, row stays) and
# he "can't save no more" (long pastes refused). Root causes under test:
#  1. SupabaseStore._request re-encodes pre-quoted filter values
#     (urlencode without "%" in safe), so keys with spaces/parentheses
#     ("note (2)") never match a row and DELETE removes nothing while
#     memory_delete still reports success.
#  2. validate_memory capped values at 12000 chars, refusing long pastes.
# ---------------------------------------------------------------------------

from vektor_cloud import storage as _storage
from vektor_cloud.storage import SupabaseStore, validate_memory


class _FakeResp:
    def __init__(self, body=b""):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_supabase_delete_url_single_encoded(monkeypatch):
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["method"] = req.get_method()
        return _FakeResp(b"[]")

    monkeypatch.setattr(_storage, "urlopen", fake_urlopen)
    store = SupabaseStore("https://example.supabase.co", "test-key")
    store.memory_delete("fact", "note (2)")
    assert seen["method"] == "DELETE"
    # The filter value must be encoded exactly once: %20, never %2520.
    assert "key=eq.note%20%282%29" in seen["url"]
    assert "%2520" not in seen["url"]


def test_supabase_delete_reports_actual_removal(monkeypatch):
    def fake_urlopen(req, timeout=None):
        return _FakeResp(b"[]")  # no rows came back: nothing was deleted

    monkeypatch.setattr(_storage, "urlopen", fake_urlopen)
    store = SupabaseStore("https://example.supabase.co", "test-key")
    assert store.memory_delete("fact", "missing") is False


def test_validate_memory_accepts_long_value():
    kind, key, value = validate_memory("fact", "big paste", "x" * 50000)
    assert len(value) == 50000


def test_sqlite_delete_suffixed_key(mem_env):
    store = DashboardStore(mem_env / "m.db")
    store.memory_save_unique("fact", "note", "first")
    store.memory_save_unique("fact", "note", "second")
    keys = sorted(m["key"] for m in store.memory_list())
    assert keys == ["note", "note (2)"]
    assert store.memory_delete("fact", "note (2)") is True
    keys = [m["key"] for m in store.memory_list()]
    assert keys == ["note"]
