"""基于 SQLite 的结果账本。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path


class Ledger:
    """记录已完成批次的结果，保证重复提交返回原结果。"""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS runs ("
                "  run_id TEXT PRIMARY KEY,"
                "  result_json TEXT NOT NULL"
                ")"
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._db_path)

    def get(self, run_id: str) -> dict | None:
        """返回已完成的批次结果；不存在时返回 None。"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT result_json FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        if row is None:
            return None
        return json.loads(row[0])

    def put(self, run_id: str, result: dict) -> None:
        """持久化批次结果；同一 run_id 不覆盖已有结果。"""
        payload = json.dumps(result, ensure_ascii=False, sort_keys=True)
        with self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO runs (run_id, result_json) VALUES (?, ?)",
                (run_id, payload),
            )
