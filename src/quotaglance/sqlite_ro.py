"""Open other applications' SQLite databases strictly read-only.

A plain ``mode=ro`` connection to a cleanly closed WAL-mode database still
creates ``-wal`` and ``-shm`` files next to it. When no ``-wal`` file exists
nobody is writing, so the file is opened as immutable instead, which leaves
the other app's directory untouched.
"""

from __future__ import annotations

import sqlite3
import urllib.parse
from pathlib import Path


def connect_readonly(path: Path, timeout: float = 0.25) -> sqlite3.Connection:
    quoted = urllib.parse.quote(str(path))
    if not path.with_name(path.name + "-wal").exists():
        return sqlite3.connect(f"file:{quoted}?mode=ro&immutable=1", uri=True, timeout=timeout)
    return sqlite3.connect(f"file:{quoted}?mode=ro", uri=True, timeout=timeout)
