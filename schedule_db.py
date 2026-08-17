"""日程归档库。

每天生成的日程写进插件自己的 SQLite（``data/schedule.db``），**永久保留**，
供事后回看和外部前端读取。

为什么不用 SDK 的 ``database.*`` 能力：那套要传 ``model_name``，对应的是主程序
ORM 里已注册的模型，用它就得往主程序加表——本插件的既定前提是主程序改动 0，
而且日程数据混进 bot 自己的库里也不合适。自带 SQLite 跟工作区里
``addon/photowall/data/library.db``（Blog 只读它）是同一个套路。

**读写分工**：运行期的热路径（每轮 planner / replyer 注入）走
``ScheduleStore`` 的内存字典，不碰数据库；数据库只在批次生成后写入、
启动时读一次。所以这里用同步的 ``sqlite3`` 是安全的——每次也就几十行。

外部前端只读时请用只读方式打开，别持有写锁：

    sqlite3.connect("file:schedule.db?mode=ro", uri=True)
"""

from datetime import date
from pathlib import Path
from typing import Any

import sqlite3


_SCHEMA = """
CREATE TABLE IF NOT EXISTS days (
    date           TEXT PRIMARY KEY,   -- YYYY-MM-DD
    weekday        INTEGER NOT NULL,   -- 0=周一 … 6=周日
    holiday        TEXT NOT NULL DEFAULT '',
    weather        TEXT NOT NULL DEFAULT '',
    day_digest     TEXT NOT NULL DEFAULT '',
    batch_reason   TEXT NOT NULL DEFAULT '',   -- 每日批次 / 冷启动补跑 / 手动触发
    batch_at       TEXT NOT NULL DEFAULT '',   -- 批次结束时刻（JST，ISO8601）
    batch_elapsed  REAL NOT NULL DEFAULT 0,    -- 批次耗时（秒）
    ok_count       INTEGER NOT NULL DEFAULT 0,
    total_count    INTEGER NOT NULL DEFAULT 0,
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

    -- LLM 生成的五个字段
    story          TEXT NOT NULL DEFAULT '',
    manner         TEXT NOT NULL DEFAULT '',
    mood           TEXT NOT NULL DEFAULT '',
    busy           TEXT NOT NULL DEFAULT '',
    suggest_length TEXT NOT NULL DEFAULT '',

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
"""


class ScheduleDB:
    """``data/schedule.db`` 的读写封装。"""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._conn: sqlite3.Connection | None = None

    def connect(self) -> None:
        """建库建表。WAL 模式让外部前端可以在 bot 写入时并发只读。"""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self._path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(_SCHEMA)
        self._conn = conn

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
            "day_digest": str(fields.get("day_digest") or ""),
            "batch_reason": str(fields.get("batch_reason") or ""),
            "batch_at": str(fields.get("batch_at") or ""),
            "batch_elapsed": float(fields.get("batch_elapsed") or 0),
            "ok_count": int(fields.get("ok_count") or 0),
            "total_count": int(fields.get("total_count") or 0),
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
            "busy": state.busy,
            "suggest_length": state.suggest_length,
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

    # ── 读 ──
    def load_days(self, limit: int) -> dict[str, dict[str, Any]]:
        """读最近若干天，供启动时填充内存热路径。

        Returns:
            dict: ``{日期: {"day_digest": str, "segments": {slot: 五字段dict}}}``
        """
        days = self._db.execute(
            "SELECT date, day_digest FROM days ORDER BY date DESC LIMIT ?", (limit,)
        ).fetchall()
        if not days:
            return {}

        result: dict[str, dict[str, Any]] = {
            row["date"]: {"day_digest": row["day_digest"], "segments": {}} for row in days
        }
        placeholders = ",".join("?" for _ in result)
        rows = self._db.execute(
            f"SELECT * FROM segments WHERE date IN ({placeholders}) ORDER BY date, seq",
            tuple(result),
        ).fetchall()
        for row in rows:
            result[row["date"]]["segments"][row["slot"]] = {
                "story": row["story"],
                "manner": row["manner"],
                "mood": row["mood"],
                "busy": row["busy"],
                "suggest_length": row["suggest_length"],
                "generated": bool(row["generated"]),
                "generated_at": row["generated_at"],
            }
        return result

    def count_segments(self) -> int:
        """归档里一共有多少段，供 /status 展示。"""
        return int(self._db.execute("SELECT COUNT(*) FROM segments").fetchone()[0])

    def date_range(self) -> tuple[str, str]:
        """归档覆盖的首末日期；空库返回两个空串。"""
        row = self._db.execute("SELECT MIN(date), MAX(date) FROM days").fetchone()
        return (row[0] or "", row[1] or "")

    # ── 迁移 ──
    def import_legacy_cache(self, payload: dict[str, Any], segments_of: Any) -> int:
        """把旧的 schedule_cache.json 导入归档。

        旧格式没有骨架快照，只能按日期回查当时的骨架——换过季的话对不上，
        所以只用于一次性迁移，对不上的段直接跳过。

        Returns:
            int: 成功导入的段数。
        """
        imported = 0
        for day_str, day_data in payload.items():
            try:
                day = date.fromisoformat(day_str)
            except ValueError:
                continue
            segments = {segment.slot: (index, segment) for index, segment in enumerate(segments_of(day))}
            self.upsert_day(day, day_digest=day_data.get("day_digest", ""), batch_reason="迁移自 JSON 缓存")
            for slot, raw in (day_data.get("segments") or {}).items():
                found = segments.get(slot)
                if found is None:
                    continue
                seq, segment = found
                self.upsert_segment(day, seq, segment, _LegacyState(raw))
                imported += 1
        return imported


class _LegacyState:
    """把旧 JSON 里的一段包成 upsert_segment 认得的形状。"""

    def __init__(self, raw: dict[str, Any]) -> None:
        self.story = str(raw.get("story") or "")
        self.manner = str(raw.get("manner") or "")
        self.mood = str(raw.get("mood") or "")
        self.busy = str(raw.get("busy") or "")
        self.suggest_length = str(raw.get("suggest_length") or "")
        self.generated = bool(raw.get("generated", True))
        self.generated_at = str(raw.get("generated_at") or "")
