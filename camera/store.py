"""独立的相机任务归档。面板以只读 SQLite 连接读取同一份记录。"""

from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import json
import sqlite3
import uuid


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


class CameraStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.db = root / "camera.db"

    def initialize(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.db)) as con, con:
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("""CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY, stream_id TEXT NOT NULL, source_msg_id TEXT NOT NULL,
                status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                record TEXT NOT NULL, UNIQUE(stream_id, source_msg_id))""")
            con.execute("CREATE INDEX IF NOT EXISTS task_status ON tasks(status, created_at)")

    def create(self, record: dict[str, Any]) -> dict[str, Any]:
        record = {**record, "id": uuid.uuid4().hex, "status": "queued",
                  "created_at": timestamp(), "updated_at": timestamp(), "events": []}
        with closing(sqlite3.connect(self.db)) as con, con:
            con.execute(
                "INSERT OR IGNORE INTO tasks VALUES (?,?,?,?,?,?,?)",
                (record["id"], record["stream_id"], record["source_msg_id"], record["status"],
                 record["created_at"], record["updated_at"], json.dumps(record, ensure_ascii=False)),
            )
            row = con.execute("SELECT record FROM tasks WHERE stream_id=? AND source_msg_id=?",
                              (record["stream_id"], record["source_msg_id"])).fetchone()
        return json.loads(row[0])

    def save(self, record: dict[str, Any]) -> None:
        record["updated_at"] = timestamp()
        with closing(sqlite3.connect(self.db)) as con, con:
            con.execute("UPDATE tasks SET status=?,updated_at=?,record=? WHERE id=?",
                        (record["status"], record["updated_at"], json.dumps(record, ensure_ascii=False), record["id"]))

    def next_queued(self) -> dict[str, Any] | None:
        with closing(sqlite3.connect(self.db)) as con:
            row = con.execute("SELECT record FROM tasks WHERE status='queued' ORDER BY created_at LIMIT 1").fetchone()
        return json.loads(row[0]) if row else None

    def latest_sent(self, stream_id: str) -> dict[str, Any] | None:
        with closing(sqlite3.connect(self.db)) as con:
            row = con.execute("SELECT record FROM tasks WHERE stream_id=? AND status='sent' "
                              "ORDER BY updated_at DESC LIMIT 1", (stream_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def recover(self) -> None:
        """不重试结果未知的生成和发送；尚未开始的排队任务照常恢复。"""
        with closing(sqlite3.connect(self.db)) as con:
            rows = con.execute("SELECT record FROM tasks WHERE status IN "
                               "('planning','generating','checking','repairing','ready','sending')").fetchall()
        for (raw,) in rows:
            record = json.loads(raw)
            status = "delivery_unknown" if record["status"] == "sending" else "interrupted"
            record["events"].append({"at": timestamp(), "stage": status, "text": "插件重载或进程中断，未自动重试"})
            record["status"] = status
            self.save(record)
