"""独立的相机任务归档。面板以只读 SQLite 连接读取同一份记录。"""

from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import json
import sqlite3
import uuid


QUOTA_TIMEZONE = ZoneInfo("Asia/Tokyo")


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

    def reset_quota(self) -> None:
        """释放今天所有聊天的照片额度：今天的任务标上 reset，不再计入当天的露脸、不露脸自拍张数。"""
        day = datetime.now(QUOTA_TIMEZONE).date().isoformat()
        with closing(sqlite3.connect(self.db)) as con, con:
            con.execute("BEGIN IMMEDIATE")
            for task_id, raw in con.execute("SELECT id, record FROM tasks").fetchall():
                record = json.loads(raw)
                quota = record.get("quota") or {}
                if quota.get("day") == day:
                    quota["reset"] = True
                    record["quota"] = quota
                    con.execute("UPDATE tasks SET record=? WHERE id=?",
                                (json.dumps(record, ensure_ascii=False), task_id))

    def create(self, record: dict[str, Any]) -> dict[str, Any]:
        record = {**record, "id": uuid.uuid4().hex, "status": "queued",
                  "created_at": timestamp(), "updated_at": timestamp(), "events": []}
        with closing(sqlite3.connect(self.db)) as con, con:
            # 去重和入队在同一写事务中完成。照片额度按种类在取景前计算（见 kinds_sent_today），这里不扣次。
            con.execute("BEGIN IMMEDIATE")
            existing = con.execute("SELECT record FROM tasks WHERE stream_id=? AND source_msg_id=?",
                                   (record["stream_id"], record["source_msg_id"])).fetchone()
            if existing is not None:
                return json.loads(existing[0])
            requester = record.get("requester") or {}
            daily_share = bool(record.get("daily_share"))
            if record.get("preview") is not True and not daily_share and not (requester.get("platform") and requester.get("user_id")):
                raise ValueError("缺少经真实消息验证的拍照用户身份")
            # 店长、本地预览和每日分享配图都不受照片额度限制
            exempt = record.get("preview") is True or daily_share or record["requester_persona"] == "Sweyn"
            record["quota"] = {"day": datetime.now(QUOTA_TIMEZONE).date().isoformat(), "timezone": QUOTA_TIMEZONE.key, "exempt": exempt}
            con.execute(
                "INSERT INTO tasks VALUES (?,?,?,?,?,?,?)",
                (record["id"], record["stream_id"], record["source_msg_id"], record["status"],
                 record["created_at"], record["updated_at"], json.dumps(record, ensure_ascii=False)),
            )
        return record

    def find(self, stream_id: str, source_msg_id: str) -> dict[str, Any] | None:
        with closing(sqlite3.connect(self.db)) as con:
            row = con.execute("SELECT record FROM tasks WHERE stream_id=? AND source_msg_id=?",
                              (stream_id, source_msg_id)).fetchone()
        return None if row is None else json.loads(row[0])

    def create_declined(self, record: dict[str, Any]) -> dict[str, Any]:
        """预审判定这次不拍：直接落成 declined 记录，不入队、不占额度。"""
        now = timestamp()
        record = {**record, "id": uuid.uuid4().hex, "status": "declined", "created_at": now, "updated_at": now,
                  "events": [{"at": now, "stage": "declined", "text": record["decline"]["reason"]}],
                  "quota": {"day": datetime.now(QUOTA_TIMEZONE).date().isoformat(), "timezone": QUOTA_TIMEZONE.key,
                            "exempt": True}}
        with closing(sqlite3.connect(self.db)) as con, con:
            con.execute("BEGIN IMMEDIATE")
            existing = con.execute("SELECT record FROM tasks WHERE stream_id=? AND source_msg_id=?",
                                   (record["stream_id"], record["source_msg_id"])).fetchone()
            if existing is not None:
                return json.loads(existing[0])
            con.execute(
                "INSERT INTO tasks VALUES (?,?,?,?,?,?,?)",
                (record["id"], record["stream_id"], record["source_msg_id"], record["status"],
                 record["created_at"], record["updated_at"], json.dumps(record, ensure_ascii=False)),
            )
        return record

    def save(self, record: dict[str, Any]) -> None:
        record["updated_at"] = timestamp()
        with closing(sqlite3.connect(self.db)) as con, con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT status, record FROM tasks WHERE id=?", (record["id"],)).fetchone()
            if row is None:
                raise ValueError("拍照任务不存在")
            previous = json.loads(row[1])
            # 工作中的任务可能持有重置前的副本，保存时保留数据库中的重置标记。
            if (previous.get("quota") or {}).get("reset"):
                record["quota"]["reset"] = True
            con.execute("UPDATE tasks SET status=?,updated_at=?,record=? WHERE id=?",
                        (record["status"], record["updated_at"], json.dumps(record, ensure_ascii=False), record["id"]))

    def get(self, task_id: str) -> dict[str, Any]:
        with closing(sqlite3.connect(self.db)) as con:
            row = con.execute("SELECT record FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise ValueError("拍照任务不存在")
        return json.loads(row[0])

    def next_queued(self) -> dict[str, Any] | None:
        with closing(sqlite3.connect(self.db)) as con:
            row = con.execute("SELECT record FROM tasks WHERE status='queued' ORDER BY created_at LIMIT 1").fetchone()
        return json.loads(row[0]) if row else None

    def latest_sent(self, stream_id: str) -> dict[str, Any] | None:
        with closing(sqlite3.connect(self.db)) as con:
            row = con.execute("SELECT record FROM tasks WHERE stream_id=? AND status='sent' "
                              "ORDER BY updated_at DESC LIMIT 1", (stream_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def kinds_sent_today(self, stream_id: str) -> dict[str, int]:
        """这个聊天今天（东京时间）已发出的各种照片张数（casual/selfie/face），不算豁免和被重置的。"""
        day = datetime.now(QUOTA_TIMEZONE).date().isoformat()
        with closing(sqlite3.connect(self.db)) as con:
            rows = con.execute("SELECT record FROM tasks WHERE stream_id=? AND status='sent'", (stream_id,)).fetchall()
        counts: dict[str, int] = {}
        for (raw,) in rows:
            record = json.loads(raw)
            quota = record.get("quota") or {}
            kind = (record.get("plan") or {}).get("photo_kind")
            if quota.get("day") == day and not quota.get("exempt") and not quota.get("reset") and kind:
                counts[kind] = counts.get(kind, 0) + 1
        return counts

    def recent_cards(self, stream_id: str, limit: int = 4) -> list[str]:
        """这个聊天最近发出的照片用过的构图卡（从新到旧），排卡时避开刚用过的。"""
        with closing(sqlite3.connect(self.db)) as con:
            rows = con.execute("SELECT record FROM tasks WHERE stream_id=? AND status='sent' ORDER BY updated_at DESC LIMIT 20",
                               (stream_id,)).fetchall()
        cards = [str((json.loads(raw).get("plan") or {}).get("card") or "") for (raw,) in rows]
        return [card for card in cards if card][:limit]

    def recent_candids(self, limit: int = 4) -> list[str]:
        """最近几张分享照用过的抓拍方式（从新到旧），给取景分析器轮换用。"""
        with closing(sqlite3.connect(self.db)) as con:
            rows = con.execute("SELECT record FROM tasks WHERE status IN ('sent','preview','daily_share') ORDER BY updated_at DESC LIMIT 40").fetchall()
        shares = [json.loads(raw).get("share") or {} for (raw,) in rows]
        return [s["candid"] for s in shares if "candid" in s][:limit]

    def in_progress(self, stream_id: str) -> list[dict[str, Any]]:
        """这个会话里已接下、还没有结果的拍照任务，按提交先后排列。"""
        with closing(sqlite3.connect(self.db)) as con:
            rows = con.execute("SELECT record FROM tasks WHERE stream_id=? AND status IN "
                               "('queued','planning','generating','checking','repairing','ready','sending') "
                               "ORDER BY created_at", (stream_id,)).fetchall()
        return [json.loads(raw) for (raw,) in rows]

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
