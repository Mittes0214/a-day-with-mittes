"""负面事件排期。

心情默认正向，但一条平坦的正向曲线同样假。所以负面情绪不靠"随机变差"制造，
而是**先安排一件不愉快的事，再让心情跟着这件事走**——排期只决定"何时、多重"，
"是什么"由生成时的 LLM 依骨架现编（见设计文档 5.8）。

不写事件池：写死一份清单等于把她这辈子会遇到的倒霉事限定成那十几种，
几周就开始重复，而且清单和骨架组合对不上。
"""

from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import json
import random


# 强度只有两档，没有"重大"——重大事件要影响好几天，
# 而承接链只覆盖相邻一两段，扛不住跨天的情绪线。
LEVEL_MILD = "轻微"
LEVEL_MEDIUM = "中等"

LEVEL_HINTS = {
    LEVEL_MILD: "轻微（当下有点不痛快，过一会儿就好）",
    LEVEL_MEDIUM: "中等（会压着接下来两三个时段的情绪）",
}

# 独处在家的时段很难自然地发生"不顺心的事"，LLM 只能编出牵强的；
# 有人在场或人在外面时，不顺心的事是自己会长出来的。
_PREFERRED_KINDS = {"工作", "外出", "上学"}
_PREFERRED_WEIGHT = 4
_ORDINARY_WEIGHT = 1


@dataclass(frozen=True)
class NegativeEntry:
    """一条排期：某天某段会发生一件不顺心的事。"""

    day: date
    slot: str
    level: str

    def to_dict(self) -> dict[str, Any]:
        return {"date": self.day.isoformat(), "slot": self.slot, "level": self.level}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "NegativeEntry":
        return cls(
            day=date.fromisoformat(str(data["date"])),
            slot=str(data["slot"]),
            level=str(data["level"]),
        )


class NegativeScheduler:
    """按周排期，结果落盘可见。

    排期**不单独设定时任务**，挂在每天批次的开头做惰性检查（见 ``ensure_week``）：
    这样排期和生成不会错序，也不用担心定时任务漏跑导致某天没有排期数据。
    """

    def __init__(self, data_dir: Path, quota: int, medium_ratio: float) -> None:
        self._path = data_dir / "negative_schedule.json"
        self._data_dir = data_dir
        self._quota = max(0, quota)
        self._medium_ratio = min(1.0, max(0.0, medium_ratio))
        self._weeks: dict[str, list[NegativeEntry]] = {}

    def load(self) -> None:
        """读取排期文件；不存在时视作空。"""
        if not self._path.exists():
            self._weeks = {}
            return
        with self._path.open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
        weeks: dict[str, list[NegativeEntry]] = {}
        for week_start, entries in raw.items():
            if not isinstance(entries, list):
                continue
            weeks[str(week_start)] = [
                NegativeEntry.from_dict(item) for item in entries if isinstance(item, dict)
            ]
        self._weeks = weeks

    def save(self) -> None:
        """写回排期，只保留最近 8 周。"""
        self._data_dir.mkdir(parents=True, exist_ok=True)
        for key in sorted(self._weeks)[:-8]:
            del self._weeks[key]
        payload = {
            week: [entry.to_dict() for entry in entries] for week, entries in self._weeks.items()
        }
        tmp_path = self._path.with_suffix(".json.tmp")
        with tmp_path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        tmp_path.replace(self._path)

    def ensure_week(self, day: date, candidates_of: Any) -> list[NegativeEntry]:
        """确保 ``day`` 所在那一周已排期，未排则现排。

        Args:
            day: 要生成的那一天。
            candidates_of: ``(date) -> list[Segment]``，取某天的时段骨架。

        Returns:
            list[NegativeEntry]: 该周的排期结果（可能为空）。
        """
        week_start = day - timedelta(days=day.weekday())
        key = week_start.isoformat()
        if key in self._weeks:
            return self._weeks[key]

        entries = self._draw_week(week_start, candidates_of)
        self._weeks[key] = entries
        self.save()
        return entries

    def entries_of_day(self, day: date) -> list[NegativeEntry]:
        """取某天的排期。"""
        week_start = day - timedelta(days=day.weekday())
        return [entry for entry in self._weeks.get(week_start.isoformat(), []) if entry.day == day]

    def level_of(self, day: date, slot: str) -> str:
        """取某天某段的负面事件强度；没排到则返回空串。"""
        for entry in self.entries_of_day(day):
            if entry.slot == slot:
                return entry.level
        return ""

    def week_entries(self, day: date) -> tuple[date, list[NegativeEntry]]:
        """取某天所在整周的排期，连同周一日期一起返回。"""
        week_start = day - timedelta(days=day.weekday())
        return week_start, list(self._weeks.get(week_start.isoformat(), []))

    def replace_week(self, day: date, entries: list[NegativeEntry]) -> None:
        """整周替换排期，供 ``/status neg`` 的手动干预使用。"""
        week_start = day - timedelta(days=day.weekday())
        self._weeks[week_start.isoformat()] = entries
        self.save()

    def reroll_week(self, day: date, candidates_of: Any) -> list[NegativeEntry]:
        """重摇某一周的排期。"""
        week_start = day - timedelta(days=day.weekday())
        entries = self._draw_week(week_start, candidates_of)
        self._weeks[week_start.isoformat()] = entries
        self.save()
        return entries

    def _draw_week(self, week_start: date, candidates_of: Any) -> list[NegativeEntry]:
        """在一周里随机挑 quota 个时段。

        约束：避开睡眠段、同一天最多一条、不连着两天；
        优先落在有人在场或人在外面的时段。
        """
        if self._quota <= 0:
            return []

        pool: list[tuple[date, str, int]] = []
        for offset in range(7):
            current = week_start + timedelta(days=offset)
            for segment in candidates_of(current):
                if segment.kind == "睡眠":
                    continue
                preferred = segment.kind in _PREFERRED_KINDS or segment.company != "独处"
                pool.append((current, segment.slot, _PREFERRED_WEIGHT if preferred else _ORDINARY_WEIGHT))

        entries: list[NegativeEntry] = []
        used_days: set[date] = set()
        # 候选池不大（一周约 70 段），直接按权重反复抽样再筛约束即可
        for _ in range(200):
            if len(entries) >= self._quota or not pool:
                break
            day, slot, _weight = random.choices(pool, weights=[item[2] for item in pool], k=1)[0]
            if day in used_days:
                continue
            if any(abs((day - used).days) <= 1 for used in used_days):
                continue
            level = LEVEL_MEDIUM if random.random() < self._medium_ratio else LEVEL_MILD
            entries.append(NegativeEntry(day=day, slot=slot, level=level))
            used_days.add(day)

        entries.sort(key=lambda entry: (entry.day, entry.slot))
        return entries
