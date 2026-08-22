"""日程归档库。

每天生成的日程写进插件自己的 SQLite（``data/schedule.db``），**永久保留**，
供事后回看和外部前端读取。

为什么不用 SDK 的 ``database.*`` 能力：那套要传 ``model_name``，对应的是主程序
ORM 里已注册的模型，用它就得往主程序加表——本插件的既定前提是主程序改动 0，
而且日程数据混进 bot 自己的库里也不合适。自带 SQLite 跟工作区里
``addon/photowall/data/library.db``（Blog 只读它）是同一个套路。

**读写分工**：运行期的热路径（每轮 planner / replyer 注入）走
``ScheduleStore`` 的内存字典，不碰数据库；批次、手动编辑和管理任务才写库。
前端通过 ``admin_jobs`` 递交操作，运行中的插件领取后同时更新内存和归档。
"""

from datetime import date
from pathlib import Path
from typing import Any

import json
import sqlite3


_SCHEMA = """
CREATE TABLE IF NOT EXISTS days (
    date           TEXT PRIMARY KEY,   -- YYYY-MM-DD
    weekday        INTEGER NOT NULL,   -- 0=周一 … 6=周日
    holiday        TEXT NOT NULL DEFAULT '',
    weather        TEXT NOT NULL DEFAULT '',
    day_digest     TEXT NOT NULL DEFAULT '',   -- 历史遗留：一段一调用时代的当日概要，只读不写
    outline        TEXT NOT NULL DEFAULT '',   -- 脉络：模型动笔前给自己写的全天规划
    batch_reason   TEXT NOT NULL DEFAULT '',   -- 每日批次 / 冷启动补跑 / 手动触发
    batch_at       TEXT NOT NULL DEFAULT '',   -- 批次结束时刻（JST，ISO8601）
    batch_elapsed  REAL NOT NULL DEFAULT 0,    -- 批次耗时（秒）
    aborted        TEXT NOT NULL DEFAULT ''    -- 非空表示整批中止，内容是原因
);

CREATE TABLE IF NOT EXISTS segments (
    date           TEXT NOT NULL,
    slot           TEXT NOT NULL,      -- HH:MM-HH:MM
    seq            INTEGER NOT NULL,   -- 当天第几段，从 0 起

    -- 骨架快照。骨架本身会随换季整份替换，所以这里存当时的值，
    -- 不要指望回看时还能从 schedule_skeleton.toml 反查出来。
    title          TEXT NOT NULL,
    place          TEXT NOT NULL,
    outfit         TEXT NOT NULL,
    company        TEXT NOT NULL,
    kind           TEXT NOT NULL,

    -- 第一轮主生成（story / mood）+ 第三轮逐时段生成（manner）
    story          TEXT NOT NULL DEFAULT '',
    manner         TEXT NOT NULL DEFAULT '',
    mood           TEXT NOT NULL DEFAULT '',
    busy           TEXT NOT NULL DEFAULT '',

    -- 第二轮（从 story 里抽取）
    places         TEXT NOT NULL DEFAULT '[]',   -- 地点时段轴，JSON：[{from,to,place}, …]
    topic          TEXT NOT NULL DEFAULT '',     -- 空表示这段没什么好说的
    topic_keys     TEXT NOT NULL DEFAULT '[]',   -- JSON 数组

    generated      INTEGER NOT NULL DEFAULT 0,  -- 0 表示这一段用的是底稿
    negative_level TEXT NOT NULL DEFAULT '',    -- 轻微 / 中等 / 空
    model          TEXT NOT NULL DEFAULT '',
    generated_at   TEXT NOT NULL DEFAULT '',

    PRIMARY KEY (date, slot)
);

CREATE INDEX IF NOT EXISTS idx_segments_date ON segments(date);
CREATE INDEX IF NOT EXISTS idx_segments_kind ON segments(kind);
CREATE INDEX IF NOT EXISTS idx_segments_negative ON segments(negative_level)
    WHERE negative_level <> '';

-- 谈资的分享状态，按会话独立计数：跟不同的人聊，同一件事说一遍是正常的。
-- 必须落库而不是放内存，否则重启后她会把今天已经说过的事再说一遍。
CREATE TABLE IF NOT EXISTS shares (
    date       TEXT NOT NULL,
    slot       TEXT NOT NULL,
    session_id TEXT NOT NULL,
    injected   INTEGER NOT NULL DEFAULT 0,   -- 这条谈资在该会话被注入过多少次
    shared_at  TEXT NOT NULL DEFAULT '',     -- 检测到她说出口的时刻；空 = 还没说
    reply_text TEXT NOT NULL DEFAULT '',     -- 判定为"说出口"的那条回复原文
    hit_key    TEXT NOT NULL DEFAULT '',     -- 触发判定的那个关键词
    PRIMARY KEY (date, slot, session_id)
);

CREATE INDEX IF NOT EXISTS idx_shares_date ON shares(date);

-- viewer 与运行中插件之间的管理任务队列。viewer 只投递和查状态，真正改日程、
-- 调 LLM 的工作都由插件进程完成，保证内存热路径与 SQLite 始终一致。
CREATE TABLE IF NOT EXISTS admin_jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    action      TEXT NOT NULL,
    target_date TEXT NOT NULL DEFAULT '',
    slot        TEXT NOT NULL DEFAULT '',
    payload     TEXT NOT NULL DEFAULT '{}',
    status      TEXT NOT NULL DEFAULT 'pending',
    result      TEXT NOT NULL DEFAULT '',
    error       TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    started_at  TEXT NOT NULL DEFAULT '',
    finished_at TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_admin_jobs_status ON admin_jobs(status, id);
"""


# 各表**新增过**的列：列名 → 类型与默认值。
# 只需要列出可能在旧库里缺席的列；建库时 _SCHEMA 已经带上它们了，
# 这份表是给"库比代码旧"的情况用的。
# 各表**废弃掉**的列。留着不删是有代价的：ok_count / total_count 可以从
# segments 数出来，存一份就必然会跟实际漂——换骨架裁掉旧段之后，
# 段数变了而这两个数还是旧的，前端就显示出「19/11」这种不可能的比值。
# 可派生的东西不存，是消除这类漂移的唯一办法。
_OBSOLETE_COLUMNS: dict[str, tuple[str, ...]] = {
    "days": ("ok_count", "total_count"),
}

_EXPECTED_COLUMNS: dict[str, dict[str, str]] = {
    "segments": {
        "topic": "TEXT NOT NULL DEFAULT ''",
        "topic_keys": "TEXT NOT NULL DEFAULT '[]'",
        "places": "TEXT NOT NULL DEFAULT '[]'",
    },
    "days": {
        "outline": "TEXT NOT NULL DEFAULT ''",
    },
    "shares": {
        "reply_text": "TEXT NOT NULL DEFAULT ''",
        "hit_key": "TEXT NOT NULL DEFAULT ''",
    },
}


class ScheduleDB:
    """``data/schedule.db`` 的读写封装。"""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._conn: sqlite3.Connection | None = None

    def connect(self) -> None:
        """建库建表并补齐缺失的列。WAL 模式让外部前端可以在 bot 写入时并发只读。"""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self._path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(_SCHEMA)
        self._conn = conn
        self._migrate()
        # 插件热重载或进程退出时，正在执行的任务来不及回写完成状态。
        # 新实例接手后重新排队，避免前端永远卡在“执行中”。
        conn.execute("UPDATE admin_jobs SET status='pending', started_at='' WHERE status='running'")

    def _migrate(self) -> None:
        """给已存在的表补上新增的列。

        ``CREATE TABLE IF NOT EXISTS`` 对已存在的表是**完全不动**的——加了新列也不会生效，
        于是读的时候 ``row["新列"]`` 直接抛 IndexError，插件 on_load 失败、
        整个日程功能静默消失。这个坑在 v3.0 → v3.1 加 topic 列时实际撞到过。

        归档库是永久保留的，不能靠"删库重建"过版本，所以这里做最小迁移：
        比对声明与实际列名，缺什么补什么。SQLite 的 ADD COLUMN 是常数时间的。
        """
        for table, columns in _EXPECTED_COLUMNS.items():
            existing = {row[1] for row in self._db.execute(f"PRAGMA table_info({table})")}
            for name, ddl in columns.items():
                if name not in existing:
                    self._db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")

        # 反向：删掉已经废弃的列。留着的话它们会继续被 SELECT * 读到，
        # 而里面装的是过期的值——那比没有更糟。
        for table, obsolete in _OBSOLETE_COLUMNS.items():
            existing = {row[1] for row in self._db.execute(f"PRAGMA table_info({table})")}
            for name in obsolete:
                if name in existing:
                    self._db.execute(f"ALTER TABLE {table} DROP COLUMN {name}")

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    @property
    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("数据库尚未连接")
        return self._conn

    # ── 写 ──
    def upsert_day(self, day: date, **fields: Any) -> None:
        """写入或更新某一天的批次元信息。"""
        row = {
            "date": day.isoformat(),
            "weekday": day.weekday(),
            "holiday": str(fields.get("holiday") or ""),
            "weather": str(fields.get("weather") or ""),
            "outline": str(fields.get("outline") or ""),
            "batch_reason": str(fields.get("batch_reason") or ""),
            "batch_at": str(fields.get("batch_at") or ""),
            "batch_elapsed": float(fields.get("batch_elapsed") or 0),
            "aborted": str(fields.get("aborted") or ""),
        }
        columns = ",".join(row)
        placeholders = ",".join(f":{key}" for key in row)
        updates = ",".join(f"{key}=excluded.{key}" for key in row if key != "date")
        self._db.execute(
            f"INSERT INTO days ({columns}) VALUES ({placeholders}) "
            f"ON CONFLICT(date) DO UPDATE SET {updates}",
            row,
        )

    def upsert_segment(
        self,
        day: date,
        seq: int,
        segment: Any,
        state: Any,
        *,
        negative_level: str = "",
        model: str = "",
    ) -> None:
        """写入或更新一个时段（骨架快照 + 生成结果）。"""
        row = {
            "date": day.isoformat(),
            "slot": segment.slot,
            "seq": seq,
            "title": segment.title,
            "place": segment.place,
            "outfit": segment.outfit,
            "company": segment.company,
            "kind": segment.kind,
            "story": state.story,
            "manner": state.manner,
            "mood": state.mood,
            # busy 在 v3.2 取消，列保留只为不丢历史值；新行恒为空串
            "busy": state.busy,
            "places": json.dumps(state.places, ensure_ascii=False),
            "topic": state.topic,
            "topic_keys": json.dumps(state.topic_keys, ensure_ascii=False),
            "generated": 1 if state.generated else 0,
            "negative_level": negative_level,
            "model": model,
            "generated_at": state.generated_at,
        }
        columns = ",".join(row)
        placeholders = ",".join(f":{key}" for key in row)
        updates = ",".join(f"{key}=excluded.{key}" for key in row if key not in ("date", "slot"))
        self._db.execute(
            f"INSERT INTO segments ({columns}) VALUES ({placeholders}) "
            f"ON CONFLICT(date, slot) DO UPDATE SET {updates}",
            row,
        )

    def update_manner(self, day: date, slot: str, manner: str) -> bool:
        """只更新一个时段的表达方式；用于前端编辑，不改其他生成字段。"""
        changed = self._db.execute(
            "UPDATE segments SET manner = ? WHERE date = ? AND slot = ?",
            (manner, day.isoformat(), slot),
        ).rowcount
        return bool(changed)

    def has_day(self, day: date) -> bool:
        row = self._db.execute("SELECT 1 FROM days WHERE date = ?", (day.isoformat(),)).fetchone()
        return row is not None

    def load_day(self, day: date) -> dict[str, Any] | None:
        """读取一天供管理任务临时装入内存；历史日期也可用。"""
        row = self._db.execute(
            "SELECT date, outline, day_digest FROM days WHERE date = ?",
            (day.isoformat(),),
        ).fetchone()
        if row is None:
            return None
        result: dict[str, Any] = {
            "outline": row["outline"] or row["day_digest"],
            "segments": {},
        }
        rows = self._db.execute(
            "SELECT * FROM segments WHERE date = ? ORDER BY seq",
            (day.isoformat(),),
        ).fetchall()
        for item in rows:
            result["segments"][item["slot"]] = self._segment_state_dict(item)
        return result

    # ── 读 ──
    def load_days(self, limit: int) -> dict[str, dict[str, Any]]:
        """读最近若干天，供启动时填充内存热路径。

        Returns:
            dict: ``{日期: {"outline": str, "segments": {slot: 五字段dict}}}``

        ``outline`` 为空时回落到 ``day_digest``：那是一段一调用时代的当日概要，
        换成一次出全天之后不再写入，但老记录里还留着，viewer 翻旧日期得有东西可显示。
        """
        days = self._db.execute(
            "SELECT date, outline, day_digest FROM days ORDER BY date DESC LIMIT ?", (limit,)
        ).fetchall()
        if not days:
            return {}

        result: dict[str, dict[str, Any]] = {
            row["date"]: {"outline": row["outline"] or row["day_digest"], "segments": {}}
            for row in days
        }
        placeholders = ",".join("?" for _ in result)
        rows = self._db.execute(
            f"SELECT * FROM segments WHERE date IN ({placeholders}) ORDER BY date, seq",
            tuple(result),
        ).fetchall()
        for row in rows:
            result[row["date"]]["segments"][row["slot"]] = self._segment_state_dict(row)
        return result

    @staticmethod
    def _segment_state_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "story": row["story"],
            "manner": row["manner"],
            "mood": row["mood"],
            "busy": row["busy"],
            "places": json.loads(row["places"] or "[]"),
            "topic": row["topic"],
            "topic_keys": json.loads(row["topic_keys"] or "[]"),
            "generated": bool(row["generated"]),
            "generated_at": row["generated_at"],
        }

    # ── viewer 管理任务 ──
    def claim_admin_job(self) -> dict[str, Any] | None:
        """原子领取最早一条待处理任务。"""
        self._db.execute("BEGIN IMMEDIATE")
        try:
            row = self._db.execute(
                "SELECT * FROM admin_jobs WHERE status = 'pending' ORDER BY id LIMIT 1"
            ).fetchone()
            if row is None:
                self._db.execute("COMMIT")
                return None
            changed = self._db.execute(
                "UPDATE admin_jobs SET status='running', started_at=CURRENT_TIMESTAMP "
                "WHERE id=? AND status='pending'",
                (row["id"],),
            ).rowcount
            self._db.execute("COMMIT")
            return dict(row) if changed else None
        except Exception:
            self._db.execute("ROLLBACK")
            raise

    def finish_admin_job(self, job_id: int, *, result: str = "", error: str = "") -> None:
        status = "failed" if error else "completed"
        self._db.execute(
            "UPDATE admin_jobs SET status=?, result=?, error=?, finished_at=CURRENT_TIMESTAMP "
            "WHERE id=?",
            (status, result, error, job_id),
        )

    def load_shares(self, dates: list[str]) -> dict[tuple[str, str, str], tuple[int, str]]:
        """读若干天的谈资分享状态，供启动时填充内存。

        Returns:
            dict: ``{(日期, 时段, 会话): (注入次数, 说出口时刻)}``
        """
        if not dates:
            return {}
        placeholders = ",".join("?" for _ in dates)
        rows = self._db.execute(
            f"SELECT * FROM shares WHERE date IN ({placeholders})", tuple(dates)
        ).fetchall()
        return {
            (row["date"], row["slot"], row["session_id"]): (row["injected"], row["shared_at"])
            for row in rows
        }

    def upsert_share(
        self,
        day: date,
        slot: str,
        session_id: str,
        injected: int,
        shared_at: str,
        reply_text: str = "",
        hit_key: str = "",
    ) -> None:
        """写入或更新一条谈资分享状态。

        每次注入都会调一次。SQLite 本地写入是微秒级的，放在 replyer hook 里不成负担，
        换来的是任何时刻查库都能看到准确的注入次数。

        ``reply_text`` / ``hit_key`` 只在"检测到说出口"那一次带值，注入时是空串——
        所以更新时给了才覆盖，否则之后再有注入会把回复原文冲掉。
        这两列纯粹给人查：光看「已说出口」判断不了检测准不准，得看她到底说了什么、
        又是哪个关键词触发的（5.11 那条已知风险靠它们排查）。
        """
        self._db.execute(
            "INSERT INTO shares (date, slot, session_id, injected, shared_at, reply_text, hit_key) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(date, slot, session_id) DO UPDATE SET "
            "injected=excluded.injected, shared_at=excluded.shared_at, "
            "reply_text=CASE WHEN excluded.reply_text <> '' "
            "THEN excluded.reply_text ELSE shares.reply_text END, "
            "hit_key=CASE WHEN excluded.hit_key <> '' "
            "THEN excluded.hit_key ELSE shares.hit_key END",
            (day.isoformat(), slot, session_id, injected, shared_at, reply_text, hit_key),
        )

    def prune_day(self, day: date, keep_slots: set[str]) -> int:
        """删掉某一天里**不属于当前骨架**的时段及其分享状态。

        骨架是「这一天有哪些时段」的唯一真相。改了骨架（换季、重排）之后，
        旧 slot 会永远留在库里：段数统计、viewer、``/status day`` 全都跟着错，
        而且不会报错——只会多出几段查不到出处的日程。

        只在 ``flush`` 里调，也就是只裁剪「正在被重写的那一天」。
        不在启动时全库对账：换季替换骨架之后，那样会把整个上一季的归档抹掉。
        """
        if not keep_slots:
            return 0
        holes = ",".join("?" for _ in keep_slots)
        args = (day.isoformat(), *keep_slots)
        removed = self._db.execute(
            f"DELETE FROM segments WHERE date = ? AND slot NOT IN ({holes})", args
        ).rowcount
        self._db.execute(
            f"DELETE FROM shares WHERE date = ? AND slot NOT IN ({holes})", args
        )
        return removed

    def delete_shares(self, day: date, slot: str) -> None:
        """清掉某一段的全部分享状态。

        谈资的分享状态是**绑在那条 topic 上**的，可这张表按 (date, slot, session_id)
        建键，换了 topic 键还是同一个。所以一段被重新生成之后必须清，否则
        ``is_shared`` 会拿旧 topic 的"说过了"把新 topic 一直摁住——静默的，
        既不报错也不进日志，只有前端那行"已说出口"还挂着才看得出来。
        """
        self._db.execute(
            "DELETE FROM shares WHERE date = ? AND slot = ?", (day.isoformat(), slot)
        )

    def shares_of_day(self, day: str) -> list[dict[str, Any]]:
        """取某一天的全部分享记录，供 viewer 和 /status topic 展示。"""
        rows = self._db.execute(
            "SELECT * FROM shares WHERE date = ? ORDER BY slot, session_id", (day,)
        ).fetchall()
        return [dict(row) for row in rows]

    def count_segments(self) -> int:
        """归档里一共有多少段，供 /status 展示。"""
        return int(self._db.execute("SELECT COUNT(*) FROM segments").fetchone()[0])

    def date_range(self) -> tuple[str, str]:
        """归档覆盖的首末日期；空库返回两个空串。"""
        row = self._db.execute("SELECT MIN(date), MAX(date) FROM days").fetchone()
        return (row[0] or "", row[1] or "")
