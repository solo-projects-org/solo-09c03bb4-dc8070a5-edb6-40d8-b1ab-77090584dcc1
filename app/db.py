"""SQLite 持久化层。

设计要点：
- 所有写入都在一个串行化的写事务中完成（应用层写锁 + BEGIN IMMEDIATE），
  报告变更、资格重算、版本与回执在同一事务里一起生效；
- versions 表记录每次生效，receipts 与 versions 一一对应，
  进程失败/重启后仍可按版本号或“最近生效版”查询；
- 批次按生效顺序自增 version；同号批次原样重放返回历史回执，
  同号异文在事务内拒收（不产生新版本）。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS versions (
    version        INTEGER PRIMARY KEY,
    batch_no       TEXT NOT NULL UNIQUE,
    effective_at   TEXT NOT NULL,
    payload_json   TEXT NOT NULL,
    baselines_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS receipts (
    version      INTEGER PRIMARY KEY REFERENCES versions(version),
    batch_no     TEXT NOT NULL UNIQUE,
    receipt_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS report_events (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    version        INTEGER NOT NULL REFERENCES versions(version),
    report_no      TEXT NOT NULL,
    paper_a        TEXT NOT NULL,
    paper_b        TEXT NOT NULL,
    action         TEXT NOT NULL CHECK (action IN ('register', 'withdraw')),
    batch_no       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reports (
    report_no   TEXT PRIMARY KEY,
    paper_a     TEXT NOT NULL,
    paper_b     TEXT NOT NULL,
    status      TEXT NOT NULL CHECK (status IN ('active', 'withdrawn')),
    reg_batch   TEXT NOT NULL,
    reg_version INTEGER NOT NULL,
    wd_batch    TEXT,
    wd_version  INTEGER
);

CREATE TABLE IF NOT EXISTS baseline_events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    version   INTEGER NOT NULL REFERENCES versions(version),
    batch_no  TEXT NOT NULL,
    baselines_json TEXT NOT NULL
);
"""


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class Database:
    def __init__(self, path: str):
        self.path = path
        self._write_lock = threading.RLock()
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA synchronous=FULL")
        return conn

    def _init_schema(self) -> None:
        # executescript 会先隐式提交，无法包在手动事务里；
        # 建表语句全部 IF NOT EXISTS，重复执行幂等。
        with self._write_lock:
            conn = self._connect()
            try:
                conn.executescript(_SCHEMA)
            finally:
                conn.close()

    @contextmanager
    def write_transaction(self):
        """串行化写事务：同一进程内批次按到达顺序逐个生效。"""
        with self._write_lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                yield conn
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            finally:
                conn.close()

    @contextmanager
    def read_connection(self):
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    # ---------- 查询 ----------

    def latest_version(self, conn: sqlite3.Connection) -> int:
        row = conn.execute("SELECT COALESCE(MAX(version), 0) AS v FROM versions").fetchone()
        return row["v"]

    def baselines_at(self, conn: sqlite3.Connection, version: int) -> list[str]:
        row = conn.execute(
            "SELECT baselines_json FROM versions WHERE version = ?", (version,)
        ).fetchone()
        return json.loads(row["baselines_json"]) if row else []

    def active_edges_at(self, conn: sqlite3.Connection, version: int) -> list[tuple[str, str, str]]:
        rows = conn.execute(
            """
            SELECT report_no, paper_a, paper_b FROM reports
            WHERE status = 'active'
              AND reg_version <= ?
              AND (wd_version IS NULL OR wd_version > ?)
            ORDER BY report_no
            """,
            (version, version),
        ).fetchall()
        return [(r["report_no"], r["paper_a"], r["paper_b"]) for r in rows]

    def all_papers_at(self, conn: sqlite3.Connection, version: int) -> set[str]:
        rows = conn.execute(
            """
            SELECT paper_a, paper_b FROM reports
            WHERE reg_version <= ?
            """,
            (version,),
        ).fetchall()
        papers: set[str] = set()
        for r in rows:
            papers.add(r["paper_a"])
            papers.add(r["paper_b"])
        papers.update(self.baselines_at(conn, version))
        return papers

    def get_version_row(self, conn: sqlite3.Connection, version: int) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM versions WHERE version = ?", (version,)
        ).fetchone()

    def get_receipt(self, conn: sqlite3.Connection, batch_no: str) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM receipts WHERE batch_no = ?", (batch_no,)
        ).fetchone()

    def get_report(self, conn: sqlite3.Connection, report_no: str) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM reports WHERE report_no = ?", (report_no,)
        ).fetchone()

    def list_reports(self, conn: sqlite3.Connection) -> list[sqlite3.Row]:
        return conn.execute(
            "SELECT * FROM reports ORDER BY reg_version, report_no"
        ).fetchall()

    def list_versions(self, conn: sqlite3.Connection) -> list[sqlite3.Row]:
        return conn.execute("SELECT * FROM versions ORDER BY version").fetchall()

    # ---------- 写入 ----------

    def insert_version(
        self,
        conn: sqlite3.Connection,
        version: int,
        batch_no: str,
        payload: dict[str, Any],
        baselines: list[str],
    ) -> None:
        conn.execute(
            "INSERT INTO versions(version, batch_no, effective_at, payload_json, baselines_json)"
            " VALUES (?, ?, ?, ?, ?)",
            (version, batch_no, utc_now_iso(),
             json.dumps(payload, ensure_ascii=False, sort_keys=True),
             json.dumps(sorted(baselines), ensure_ascii=False)),
        )

    def insert_receipt(self, conn: sqlite3.Connection, version: int, batch_no: str, receipt: dict) -> None:
        conn.execute(
            "INSERT INTO receipts(version, batch_no, receipt_json) VALUES (?, ?, ?)",
            (version, batch_no, json.dumps(receipt, ensure_ascii=False, sort_keys=True)),
        )

    def insert_baseline_event(self, conn: sqlite3.Connection, version: int, batch_no: str, baselines: list[str]) -> None:
        conn.execute(
            "INSERT INTO baseline_events(version, batch_no, baselines_json) VALUES (?, ?, ?)",
            (version, batch_no, json.dumps(sorted(baselines), ensure_ascii=False)),
        )

    def insert_report_event(
        self, conn: sqlite3.Connection, version: int, batch_no: str,
        report_no: str, paper_a: str, paper_b: str, action: str,
    ) -> None:
        conn.execute(
            "INSERT INTO report_events(version, batch_no, report_no, paper_a, paper_b, action)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (version, batch_no, report_no, paper_a, paper_b, action),
        )

    def register_report(
        self, conn: sqlite3.Connection, report_no: str, paper_a: str, paper_b: str,
        batch_no: str, version: int,
    ) -> None:
        conn.execute(
            "INSERT INTO reports(report_no, paper_a, paper_b, status, reg_batch, reg_version)"
            " VALUES (?, ?, ?, 'active', ?, ?)",
            (report_no, paper_a, paper_b, batch_no, version),
        )

    def withdraw_report(self, conn: sqlite3.Connection, report_no: str, batch_no: str, version: int) -> None:
        conn.execute(
            "UPDATE reports SET status = 'withdrawn', wd_batch = ?, wd_version = ?"
            " WHERE report_no = ?",
            (batch_no, version, report_no),
        )
