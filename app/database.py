"""SQLite 访问层：连接、建表与基础读写。"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Optional

from .config import settings

# 进程内写锁，配合 FastAPI 线程池保证“先查后插/状态变更”的原子性
_write_lock = threading.RLock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    sample_code   TEXT NOT NULL UNIQUE,
    collected_at  TEXT NOT NULL,          -- 规范化为 UTC ISO-8601
    collector     TEXT NOT NULL,
    seal_number   TEXT NOT NULL,
    approved      INTEGER NOT NULL DEFAULT 0,
    approved_at   TEXT,
    approver      TEXT,
    approve_note  TEXT,
    snapshot_json TEXT,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS custody_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    event_no     TEXT NOT NULL UNIQUE,    -- 全局唯一事件号
    sample_code  TEXT NOT NULL,
    event_type   TEXT NOT NULL,           -- transfer/receipt/seal_check/storage
    occurred_at  TEXT NOT NULL,           -- 发生时间（重算依据）
    recorded_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    payload_json TEXT NOT NULL,           -- 全部业务字段
    FOREIGN KEY (sample_code) REFERENCES samples(sample_code)
);

CREATE INDEX IF NOT EXISTS idx_events_sample_time
    ON custody_events(sample_code, occurred_at, id);
"""


def _connect() -> sqlite3.Connection:
    Path(settings.db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(settings.db_path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


@contextmanager
def get_conn():
    conn = _connect()
    try:
        yield conn
    finally:
        conn.close()


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(_SCHEMA)
        conn.commit()


@contextmanager
def write_tx():
    """串行化的写事务。"""
    with _write_lock:
        conn = _connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


# ---------------- samples ----------------

def insert_sample(conn: sqlite3.Connection, sample_code: str, collected_at: str,
                  collector: str, seal_number: str) -> None:
    conn.execute(
        "INSERT INTO samples(sample_code, collected_at, collector, seal_number)"
        " VALUES (?,?,?,?)",
        (sample_code, collected_at, collector, seal_number),
    )


def get_sample(sample_code: str) -> Optional[sqlite3.Row]:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM samples WHERE sample_code=?", (sample_code,)).fetchone()
        return row


def list_samples() -> list[sqlite3.Row]:
    with get_conn() as conn:
        return list(conn.execute("SELECT * FROM samples ORDER BY sample_code"))


def approve_sample(conn: sqlite3.Connection, sample_code: str, approver: str,
                   approved_at: str, note: Optional[str], snapshot: dict[str, Any]) -> None:
    conn.execute(
        "UPDATE samples SET approved=1, approved_at=?, approver=?, approve_note=?, snapshot_json=?"
        " WHERE sample_code=?",
        (approved_at, approver, note, json.dumps(snapshot, ensure_ascii=False, sort_keys=True), sample_code),
    )


# ---------------- events ----------------

def insert_event(conn: sqlite3.Connection, event_no: str, sample_code: str,
                 event_type: str, occurred_at: str, payload: dict[str, Any]) -> None:
    conn.execute(
        "INSERT INTO custody_events(event_no, sample_code, event_type, occurred_at, payload_json)"
        " VALUES (?,?,?,?,?)",
        (event_no, sample_code, event_type, occurred_at,
         json.dumps(payload, ensure_ascii=False, sort_keys=True)),
    )


def get_event(event_no: str) -> Optional[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM custody_events WHERE event_no=?", (event_no,)).fetchone()


def list_events(sample_code: str) -> list[sqlite3.Row]:
    """按发生时间升序、同时间按录入先后(id)返回，证据链重放顺序。"""
    with get_conn() as conn:
        return list(conn.execute(
            "SELECT * FROM custody_events WHERE sample_code=? ORDER BY occurred_at, id",
            (sample_code,),
        ))


def count_events(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM custody_events").fetchone()[0]


def count_samples(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0]


def bulk_insert_demo(conn: sqlite3.Connection, samples: Iterable[tuple], events: Iterable[tuple]) -> None:
    conn.executemany(
        "INSERT INTO samples(sample_code, collected_at, collector, seal_number)"
        " VALUES (?,?,?,?)",
        list(samples),
    )
    conn.executemany(
        "INSERT INTO custody_events(event_no, sample_code, event_type, occurred_at, payload_json)"
        " VALUES (?,?,?,?,?)",
        list(events),
    )
