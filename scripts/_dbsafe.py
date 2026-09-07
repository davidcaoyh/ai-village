"""Opening a live village.db without breaking the site that is reading it.

Every script here only reads, but `sqlite3.connect()` opens read-write, and a WAL
database needs a `-shm` sidecar to coordinate readers. SQLite creates `-shm` and
`-wal` even for a READ-ONLY connection, owned by whoever ran the process. Run as
root against a database owned by `village`, that leaves root-owned sidecars and
the web service can no longer take a normal read lock: the site stays up, every
read gets slow, and nothing in the journal explains it. Sep 6, 2026.

So: read-only, and refuse to be the process that creates those files as the wrong
user. `mode=ro` alone is not the fix - it stops writes to the data, not the
sidecars - which is why the ownership check is here and not a comment.
"""

from __future__ import annotations

import os
import pwd
import sqlite3
import sys
from pathlib import Path


def connect_readonly(path: str) -> sqlite3.Connection:
    if not Path(path).exists():
        # connect() happily creates an empty database for a path that does not
        # exist, so a wrong --db reads as a session with no events - a table of
        # zeros rather than an error. Every number here is evidence; fail instead.
        sys.exit(f"no database at {path}. Pass --db, or run from the project root.")
    _refuse_if_wrong_user(path)
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db


def _refuse_if_wrong_user(path: str) -> None:
    if os.name != "posix":
        return
    owner = os.stat(path).st_uid
    if owner == os.geteuid():
        return
    try:
        name = pwd.getpwuid(owner).pw_name
    except KeyError:
        name = str(owner)
    # Only dangerous when the sidecars are absent: opening an existing `-shm`
    # does not change its owner, which is why reading a live database as root
    # usually appears to work.
    missing = [p for p in (path + "-shm", path + "-wal") if not os.path.exists(p)]
    if os.geteuid() == 0 and missing:
        script = Path(sys.argv[0]).stem or "script"
        sys.exit(
            f"refusing to open {path} as root: it belongs to {name!r}, and SQLite "
            f"would create {', '.join(os.path.basename(p) for p in missing)} owned "
            f"by root, which stops the service reading its own database.\n\n"
            f"Re-run as that user:\n"
            f"  sudo -u {name} {sys.executable} -m scripts.{script} ...\n"
        )
    print(f"note: {path} belongs to {name!r}, not you", file=sys.stderr)
