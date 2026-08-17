"""骨架读取、生成结果的内存副本、当前时段查询。

数据分三层（见设计文档 5.4）：

1. ``schedule_skeleton.toml`` 的手写骨架和底稿——纳入版本库，LLM 绝不回写。
2. ``data/schedule.db`` 的生成结果——每天 12:00 批量写入，**永久归档**。
3. 运行时更新——本期不做。

运行期只读内存副本：按当前时刻查表取出那一段，取不到就用底稿顶上，
任何情况下都不能因为日程没生成好而阻塞回复。
"""

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import json
import tomllib

from .schedule_db import ScheduleDB


JST = ZoneInfo("Asia/Tokyo")

# 骨架里的星期表名
DAY_KEYS = [
    "schedule_mon",
    "schedule_tue",
    "schedule_wed",
    "schedule_thu",
    "schedule_fri",
    "schedule_sat",
    "schedule_sun",
]

WEEKDAY_NAMES = ["一", "二", "三", "四", "五", "六", "日"]

SUGGEST_LENGTHS = ("简短表达", "正常回复", "长回复")

# 内存里最多保留几天（今天 + 明天，再多留一天做缓冲）。
# 这只影响热路径的内存副本，归档库里是全量永久保留的。
_MAX_CACHED_DAYS = 3


@dataclass(frozen=True)
class Segment:
    """一个时段的手写骨架。LLM 不可改动其中任何字段。"""

    start: str
    end: str
    title: str
    place: str
    outfit: str
    company: str
    kind: str

    @property
    def slot(self) -> str:
        """缓存和展示用的时段键，如 ``17:00-18:00``。"""
        return f"{self.start}-{self.end}"

    @property
    def start_minutes(self) -> int:
        return _to_minutes(self.start)

    @property
    def end_minutes(self) -> int:
        return _to_minutes(self.end)

    @property
    def duration_hours(self) -> float:
        return (self.end_minutes - self.start_minutes) / 60

    def contains(self, minutes: int) -> bool:
        return self.start_minutes <= minutes < self.end_minutes


@dataclass
class SegmentState:
    """一个时段的生成结果（LLM 产出的五个字段）。"""

    story: str
    manner: str
    mood: str
    busy: str
    suggest_length: str
    generated_at: str = ""
    # 底稿顶上的（未生成成功）标记为 False，供 /status 和事实层工具区分
    generated: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "story": self.story,
            "manner": self.manner,
            "mood": self.mood,
            "busy": self.busy,
            "suggest_length": self.suggest_length,
            "generated_at": self.generated_at,
            "generated": self.generated,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SegmentState":
        return cls(
            story=str(data.get("story") or ""),
            manner=str(data.get("manner") or ""),
            mood=str(data.get("mood") or ""),
            busy=str(data.get("busy") or ""),
            suggest_length=str(data.get("suggest_length") or ""),
            generated_at=str(data.get("generated_at") or ""),
            generated=bool(data.get("generated", True)),
        )


@dataclass
class DayCache:
    """某一天的生成结果。"""

    segments: dict[str, SegmentState] = field(default_factory=dict)
    # 批次跑到最后时的当日概要，供次日凌晨那段承接
    day_digest: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "segments": {slot: state.to_dict() for slot, state in self.segments.items()},
            "day_digest": self.day_digest,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DayCache":
        raw_segments = data.get("segments")
        segments: dict[str, SegmentState] = {}
        if isinstance(raw_segments, dict):
            for slot, value in raw_segments.items():
                if isinstance(value, dict):
                    segments[str(slot)] = SegmentState.from_dict(value)
        return cls(segments=segments, day_digest=str(data.get("day_digest") or ""))


class ScheduleStore:
    """骨架 + 生成结果的读写入口。

    生成结果的权威存储是 ``data/schedule.db``（永久归档，见 ``schedule_db``），
    这里只保留最近几天的内存副本给运行期热路径用——每轮 planner / replyer 注入
    都要取当前状态，走内存比走 SQL 稳妥。
    """

    def __init__(self, plugin_dir: Path, data_dir: Path) -> None:
        self._skeleton_path = plugin_dir / "schedule_skeleton.toml"
        self._legacy_cache_path = data_dir / "schedule_cache.json"
        self._data_dir = data_dir
        self._db = ScheduleDB(data_dir / "schedule.db")
        self._days: dict[str, list[Segment]] = {}
        self._fallback: dict[str, SegmentState] = {}
        self._cache: dict[str, DayCache] = {}

    # ── 骨架 ──
    def load_skeleton(self) -> None:
        """读取骨架和底稿，并校验时段是否首尾相接铺满一天。"""
        with self._skeleton_path.open("rb") as handle:
            raw = tomllib.load(handle)

        days: dict[str, list[Segment]] = {}
        for key in DAY_KEYS:
            rows = raw.get(key)
            if not isinstance(rows, list) or not rows:
                raise ValueError(f"骨架缺少 {key} 或为空：{self._skeleton_path}")
            segments = [
                Segment(
                    start=str(row["start"]),
                    end=str(row["end"]),
                    title=str(row["title"]),
                    place=str(row["place"]),
                    outfit=str(row["outfit"]),
                    company=str(row["company"]),
                    kind=str(row["kind"]),
                )
                for row in rows
            ]
            _validate_day(key, segments)
            days[key] = segments

        fallback_raw = raw.get("fallback")
        if not isinstance(fallback_raw, dict) or not fallback_raw:
            raise ValueError(f"骨架缺少 [fallback.*] 底稿：{self._skeleton_path}")
        fallback: dict[str, SegmentState] = {}
        for kind, value in fallback_raw.items():
            fallback[str(kind)] = SegmentState(
                story="",
                manner=str(value.get("manner") or ""),
                mood=str(value.get("mood") or ""),
                busy=str(value.get("busy") or ""),
                suggest_length=str(value.get("suggest_length") or ""),
                generated=False,
            )

        # 骨架里出现的每个 kind 都必须有底稿，否则异常时会拿到空状态
        used_kinds = {segment.kind for segments in days.values() for segment in segments}
        missing = used_kinds - set(fallback)
        if missing:
            raise ValueError(f"骨架用到的 kind 缺少对应底稿：{sorted(missing)}")

        self._days = days
        self._fallback = fallback

    def segments_of(self, day: date) -> list[Segment]:
        """取某一天的全部时段骨架。"""
        return self._days[DAY_KEYS[day.weekday()]]

    def segment_at(self, moment: datetime) -> Segment:
        """取某一时刻所在的时段骨架。"""
        minutes = moment.hour * 60 + moment.minute
        for segment in self.segments_of(moment.date()):
            if segment.contains(minutes):
                return segment
        # 骨架已在加载时校验过铺满全天，走到这里说明校验被绕过了
        raise LookupError(f"{moment:%Y-%m-%d %H:%M} 没有匹配的时段，骨架可能被改坏了")

    def previous_segment(self, day: date, segment: Segment) -> tuple[date, Segment]:
        """取给定时段的上一段，跨零点时回到前一天最后一段。"""
        segments = self.segments_of(day)
        index = segments.index(segment)
        if index > 0:
            return day, segments[index - 1]
        previous_day = day - timedelta(days=1)
        return previous_day, self.segments_of(previous_day)[-1]

    def fallback_for(self, segment: Segment) -> SegmentState:
        """按 kind 取底稿，story 由 title + place 现拼一句。"""
        base = self._fallback[segment.kind]
        return SegmentState(
            story=f"在{segment.place}，{segment.title}。",
            manner=base.manner,
            mood=base.mood,
            busy=base.busy,
            suggest_length=base.suggest_length,
            generated=False,
        )

    def awake_and_work_hours(self, day: date, segment: Segment) -> tuple[float, float]:
        """算出进入该时段时已清醒多少小时、其中工作多少小时。

        从当天最后一个睡眠段结束的位置起算；当天该段之前没有睡眠段时，
        视作从零点起一直醒着（凌晨那几段就是这种情况）。
        """
        segments = self.segments_of(day)
        index = segments.index(segment)
        start = 0
        for i in range(index - 1, -1, -1):
            if segments[i].kind == "睡眠":
                start = i + 1
                break
        awake = sum(item.duration_hours for item in segments[start:index])
        work = sum(item.duration_hours for item in segments[start:index] if item.kind == "工作")
        return round(awake, 1), round(work, 1)

    # ── 生成结果 ──
    @property
    def db(self) -> ScheduleDB:
        return self._db

    def open_db(self) -> int:
        """连库、迁移旧 JSON 缓存、把最近几天读进内存。

        Returns:
            int: 本次从旧 JSON 迁移进来的段数，没有旧文件时为 0。
        """
        self._db.connect()

        migrated = 0
        if self._legacy_cache_path.exists():
            with self._legacy_cache_path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
            migrated = self._db.import_legacy_cache(payload, self.segments_of)
            # 迁完改名而不是删掉：万一骨架换过季对不上，原文件还在手里
            self._legacy_cache_path.replace(self._legacy_cache_path.with_suffix(".json.migrated"))

        self._cache = {
            day: DayCache.from_dict(data) for day, data in self._db.load_days(_MAX_CACHED_DAYS).items()
        }
        return migrated

    def close_db(self) -> None:
        self._db.close()

    def flush(self, day: date, *, model: str = "", negative_level_of: Any = None, **day_fields: Any) -> None:
        """把某一天的内存状态写进归档库，并裁剪内存副本。

        Args:
            day: 要落库的日期。
            model: 生成用的模型任务名，写进段记录备查。
            negative_level_of: ``(slot) -> str``，取该段的负面事件强度。
            **day_fields: 批次元信息，透传给 ``ScheduleDB.upsert_day``。
        """
        cached = self._cache.get(day.isoformat())
        if cached is None:
            return

        self._db.upsert_day(day, day_digest=cached.day_digest, **day_fields)
        for seq, segment in enumerate(self.segments_of(day)):
            state = cached.segments.get(segment.slot)
            if state is None:
                continue
            self._db.upsert_segment(
                day,
                seq,
                segment,
                state,
                negative_level=negative_level_of(segment.slot) if negative_level_of else "",
                model=model,
            )

        # 内存只留最近几天；历史全在库里，需要时查库
        for key in sorted(self._cache)[:-_MAX_CACHED_DAYS]:
            del self._cache[key]

    def day_cache(self, day: date) -> DayCache | None:
        return self._cache.get(day.isoformat())

    def ensure_day_cache(self, day: date) -> DayCache:
        return self._cache.setdefault(day.isoformat(), DayCache())

    def state_at(self, moment: datetime) -> tuple[Segment, SegmentState]:
        """取某一时刻的时段骨架和状态，缓存缺失时回落到底稿。"""
        segment = self.segment_at(moment)
        cached = self._cache.get(moment.date().isoformat())
        if cached is not None:
            state = cached.segments.get(segment.slot)
            if state is not None:
                return segment, state
        return segment, self.fallback_for(segment)

    def state_of(self, day: date, segment: Segment) -> SegmentState | None:
        """取指定某天某段的生成结果，没有则返回 ``None``。"""
        cached = self._cache.get(day.isoformat())
        if cached is None:
            return None
        return cached.segments.get(segment.slot)

    def day_generated_count(self, day: date) -> int:
        """某天已生成成功的段数。"""
        cached = self._cache.get(day.isoformat())
        if cached is None:
            return 0
        return sum(1 for state in cached.segments.values() if state.generated)


def _to_minutes(value: str) -> int:
    """``HH:MM`` → 从零点起的分钟数，``24:00`` 记作 1440。"""
    hour, minute = value.split(":")
    return int(hour) * 60 + int(minute)


def _validate_day(key: str, segments: list[Segment]) -> None:
    """校验一天的时段首尾相接、铺满 00:00~24:00。"""
    cursor = 0
    for segment in segments:
        if segment.start_minutes != cursor:
            raise ValueError(f"{key} 的 {segment.slot} 与上一段不相接（应从 {_from_minutes(cursor)} 开始）")
        if segment.end_minutes <= segment.start_minutes:
            raise ValueError(f"{key} 的 {segment.slot} 结束时间不晚于开始时间")
        cursor = segment.end_minutes
    if cursor != 24 * 60:
        raise ValueError(f"{key} 没有铺满一天，最后停在 {_from_minutes(cursor)}")


def _from_minutes(value: int) -> str:
    return f"{value // 60:02d}:{value % 60:02d}"


def now_jst() -> datetime:
    """当前 JST 时间。

    插件内所有时间都必须走这里，不要用裸的 ``datetime.now()``——
    服务器时区一旦不是 JST，整份日程会整体偏移，而且错得很隐蔽。
    """
    return datetime.now(JST)


def weekday_name(day: date) -> str:
    return WEEKDAY_NAMES[day.weekday()]
