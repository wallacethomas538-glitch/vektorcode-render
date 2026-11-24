#!/usr/bin/env python3
"""Render entry point for the VektorCode dashboard."""
from http.server import ThreadingHTTPServer

from app import HOST, Handler, PORT, db, sync_memory_file


if __name__ == "__main__":
    db().close()
    # Rebuild the editable MEMORY.md mirror from the DB (best-effort:
    # Render wipes the container file on redeploy; the DB is the truth).
    sync_memory_file()
    print(f"VektorCode dashboard: http://{HOST}:{PORT}")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
