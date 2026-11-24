#!/usr/bin/env python3
"""Render entry point for the VektorCode dashboard."""
from http.server import ThreadingHTTPServer

from app import (HOST, Handler, PORT, db, ensure_memory_file,
                 migrate_instructions_to_db)


if __name__ == "__main__":
    db().close()
    # The editable MEMORY.md mirror is created from the DB only when it
    # is missing — never regenerated at startup, or an edit Wallace made
    # on disk (not yet imported) would be silently destroyed.
    ensure_memory_file()
    # One-way migration: file-only instructions into the DB setting so
    # they survive a Render redeploy.
    migrate_instructions_to_db()
    print(f"VektorCode dashboard: http://{HOST}:{PORT}")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
