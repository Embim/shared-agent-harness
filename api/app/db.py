import json
import sqlite3
import threading
from pathlib import Path

from .config import settings

_conn: sqlite3.Connection | None = None
# Один писатель. SQLite физически не умеет параллельную запись; вместо
# ловли "database is locked" держим дисциплину одного соединения под локом.
_lock = threading.RLock()


def connect() -> sqlite3.Connection:
    global _conn
    if _conn is not None:
        return _conn
    path = Path(settings.db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")      # читатели не блокируются писателем
    c.execute("PRAGMA synchronous=NORMAL")
    c.execute("PRAGMA busy_timeout=5000")
    c.execute("PRAGMA foreign_keys=ON")
    _conn = c
    return c


def init_db() -> None:
    ddl = (Path(__file__).parent / "schema.sql").read_text(encoding="utf-8")
    with _lock:
        connect().executescript(ddl)


def q(sql: str, *params) -> list[sqlite3.Row]:
    with _lock:
        return connect().execute(sql, params).fetchall()


def q1(sql: str, *params) -> sqlite3.Row | None:
    rows = q(sql, *params)
    return rows[0] if rows else None


def ex(sql: str, *params) -> int:
    with _lock:
        cur = connect().execute(sql, params)
        return cur.lastrowid or 0


def row_to_dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    if "payload" in d and isinstance(d["payload"], str):
        try:
            d["payload"] = json.loads(d["payload"])
        except json.JSONDecodeError:
            d["payload"] = {"raw": d["payload"]}
    return d
