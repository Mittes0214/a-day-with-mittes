"""Mittes 的一天插件 (A Day With Mittes)

为 Mittes 提供日常环境上下文工具，全部通过 Tool 按需暴露给 LLM，不注入到任何 prompt：

- `get_current_schedule`：按星期 × 小时配置的角色活动状态查询，支持节假日识别
- `get_weather`：基于 Open-Meteo 的实时天气与近 3 天预报查询

作者：Mittes
版本：2.1.0
许可：GPL-v3.0-or-later
兼容：MaiBot-r-dev (SDK 2.0+)
"""

from maibot_sdk import MaiBotPlugin, Tool
from maibot_sdk.types import ToolParameterInfo, ToolParamType
from pathlib import Path
from typing import Any

import datetime
import tomllib

from .schedule_generator import ScheduleGenerator
from .weather_fetcher import fetch_weather, fetch_weather_brief


_TIME_RANGES = [
    ("00:00-01:00", "time_range_00_01", 0, 1 * 60),
    ("01:00-02:00", "time_range_01_02", 1 * 60, 2 * 60),
    ("02:00-03:00", "time_range_02_03", 2 * 60, 3 * 60),
    ("03:00-04:00", "time_range_03_04", 3 * 60, 4 * 60),
    ("04:00-05:00", "time_range_04_05", 4 * 60, 5 * 60),
    ("05:00-06:00", "time_range_05_06", 5 * 60, 6 * 60),
    ("06:00-07:00", "time_range_06_07", 6 * 60, 7 * 60),
    ("07:00-08:00", "time_range_07_08", 7 * 60, 8 * 60),
    ("08:00-09:00", "time_range_08_09", 8 * 60, 9 * 60),
    ("09:00-10:00", "time_range_09_10", 9 * 60, 10 * 60),
    ("10:00-11:00", "time_range_10_11", 10 * 60, 11 * 60),
    ("11:00-12:00", "time_range_11_12", 11 * 60, 12 * 60),
    ("12:00-13:00", "time_range_12_13", 12 * 60, 13 * 60),
    ("13:00-14:00", "time_range_13_14", 13 * 60, 14 * 60),
    ("14:00-15:00", "time_range_14_15", 14 * 60, 15 * 60),
    ("15:00-16:00", "time_range_15_16", 15 * 60, 16 * 60),
    ("16:00-17:00", "time_range_16_17", 16 * 60, 17 * 60),
    ("17:00-18:00", "time_range_17_18", 17 * 60, 18 * 60),
    ("18:00-19:00", "time_range_18_19", 18 * 60, 19 * 60),
    ("19:00-20:00", "time_range_19_20", 19 * 60, 20 * 60),
    ("20:00-21:00", "time_range_20_21", 20 * 60, 21 * 60),
    ("21:00-22:00", "time_range_21_22", 21 * 60, 22 * 60),
    ("22:00-23:00", "time_range_22_23", 22 * 60, 23 * 60),
    ("23:00-00:00", "time_range_23_00", 23 * 60, 24 * 60),
]

DAY_PREFIX = {
    0: "schedule_mon",
    1: "schedule_tue",
    2: "schedule_wed",
    3: "schedule_thu",
    4: "schedule_fri",
    5: "schedule_sat",
    6: "schedule_sun",
}

WEEKDAY_NAMES = ["一", "二", "三", "四", "五", "六", "日"]


class ADayWithMittesPlugin(MaiBotPlugin):
    def __init__(self) -> None:
        super().__init__()
        self._schedule_generator: ScheduleGenerator | None = None
        self._plugin_config_cache: dict[str, Any] | None = None

    async def on_load(self) -> None:
        self._schedule_generator = ScheduleGenerator(self.ctx)

    async def on_unload(self) -> None:
        pass

    def get_components(self) -> list[dict[str, Any]]:
        """按 config.toml 的 [components] 开关过滤已禁用的 Tool。

        覆盖默认实现，让禁用的工具完全不出现在 LLM 的 tool_definitions 中。
        本方法在 Runner 加载时同步调用，无法走异步 self.ctx.config，
        所以直接读插件目录下的 config.toml；开关改动需要重启 MaiBot 才会生效。
        """
        components = super().get_components()
        toggles = self._read_component_toggles()
        return [
            c
            for c in components
            if not (
                c.get("type", "").upper() == "TOOL"
                and not toggles.get(f"enable_{c.get('name', '')}", True)
            )
        ]

    @staticmethod
    def _read_component_toggles() -> dict[str, bool]:
        """从插件目录的 config.toml 读取 [components] 段的开关字典。"""
        try:
            with (Path(__file__).parent / "config.toml").open("rb") as f:
                data = tomllib.load(f)
            return {k: bool(v) for k, v in (data.get("components") or {}).items()}
        except Exception:
            return {}

    # ── 配置读取 ──
    def _extract_config_dict(self, raw: Any) -> dict[str, Any]:
        if not isinstance(raw, dict):
            return {}
        if "result" in raw:
            inner = raw["result"]
            if isinstance(inner, dict) and "value" in inner:
                val = inner["value"]
                return val if isinstance(val, dict) else {}
            if isinstance(inner, dict):
                return inner
        if "value" in raw:
            val = raw["value"]
            return val if isinstance(val, dict) else {}
        for key in ("schedule_mon", "schedule_tue", "plugin"):
            if key in raw:
                return raw
        return raw if raw else {}

    async def _ensure_plugin_config(self) -> dict[str, Any]:
        if self._plugin_config_cache is None:
            import logging
            _log = logging.getLogger("a_day_with_mittes.config")
            raw = await self.ctx.config.get_all()
            self._plugin_config_cache = self._extract_config_dict(raw)
            _log.info(f"[Config] keys={list(self._plugin_config_cache.keys()) if isinstance(self._plugin_config_cache, dict) else 'N/A'}")
        return self._plugin_config_cache or {}

    def _resolve_nested_key(self, config: dict, key: str, default: Any = None) -> Any:
        keys = key.split(".")
        current = config
        for k in keys:
            if isinstance(current, dict) and k in current:
                current = current[k]
            else:
                return default
        return current

    async def _get_config(self, key: str, default: Any = None) -> Any:
        try:
            config = await self._ensure_plugin_config()
            result = self._resolve_nested_key(config, key, "___SENTINEL___")
            if result == "___SENTINEL___":
                return default
            return result
        except Exception:
            return default

    # ── 日程查询 ──
    async def _get_current_schedule(self) -> tuple[str, str]:
        now = datetime.datetime.now()
        current_time_minutes = now.hour * 60 + now.minute
        prefix = DAY_PREFIX.get(now.weekday(), "schedule")

        for time_range, key_name, start_minutes, end_minutes in _TIME_RANGES:
            status = await self._get_config(f"{prefix}.{key_name}", "")
            if start_minutes > end_minutes:
                hit = current_time_minutes >= start_minutes or current_time_minutes < end_minutes
            else:
                hit = start_minutes <= current_time_minutes < end_minutes
            if hit and status:
                return time_range, str(status)

        return "", ""

    async def _get_next_hour_status(self) -> str:
        now = datetime.datetime.now()
        prefix = DAY_PREFIX.get(now.weekday(), "schedule")
        return await self._get_status_by_hour_with_prefix(prefix, now.hour + 1)

    async def _get_status_by_hour_with_prefix(self, prefix: str, hour: int) -> str:
        if hour < 0:
            hour = 24 + hour
        elif hour >= 24:
            hour = hour - 24
        minutes = hour * 60 + 30
        for _, key_name, start_minutes, end_minutes in _TIME_RANGES:
            if start_minutes > end_minutes:
                hit = minutes >= start_minutes or minutes < end_minutes
            else:
                hit = start_minutes <= minutes < end_minutes
            if hit:
                val = await self._get_config(f"{prefix}.{key_name}", "")
                return str(val) if val else ""
        return ""

    @Tool(
        "get_current_schedule",
        brief_description="当需要描述 Mittes 当前在做什么时，必须调用此工具获取真实日程，不得推测。",
        parameters=[],
    )
    async def tool_get_schedule(self, **kwargs):
        _ = kwargs
        time_range, status = await self._get_current_schedule()
        if not time_range or not status:
            return {"name": "get_current_schedule", "content": "当前没有日程信息"}

        now = datetime.datetime.now()
        weekday = WEEKDAY_NAMES[now.weekday()]

        if self._schedule_generator:
            try:
                holiday_map = await self._schedule_generator.get_holiday_map(now.year)
                holiday_name = self._schedule_generator.get_holiday_name(now.strftime("%Y-%m-%d"), holiday_map)
                if holiday_name:
                    weekday = f"{weekday}【{holiday_name}】"
            except Exception:
                pass

        next_status = await self._get_next_hour_status()
        current_hour = now.hour
        current_hour_end = (now.hour + 1) % 24
        weather_brief = ""
        weather_location = await self._get_config("weather_location", "Tokyo")
        try:
            weather_brief = await fetch_weather_brief(weather_location)
        except Exception:
            weather_brief = ""
        if weather_brief:
            header = f"今天是星期{weekday}，现在的天气{weather_brief}，"
        else:
            header = f"今天是星期{weekday}，"

        current_info = f"当前时间 {current_hour:02d}:00-{current_hour_end:02d}:00: {status}"
        next_info = ""
        if next_status:
            next_hour = (now.hour + 1) % 24
            next_hour_end = (now.hour + 2) % 24
            next_info = f"\n下一时段 {next_hour:02d}:00-{next_hour_end:02d}:00: {next_status}"

        content = f"{header}\n{current_info}{next_info}\n不要主动提及自己的日程，除非有人问及"
        return {"name": "get_current_schedule", "content": content}

    @Tool(
        "get_weather",
        brief_description="查询指定城市的实时天气和近 3 天预报；用户询问天气，或回复需要参考当前天气时调用。",
        parameters=[
            ToolParameterInfo(
                name="location",
                param_type=ToolParamType.STRING,
                description="城市的英文/罗马字名称（如：东京→Tokyo、上海→Shanghai），不要直接传中文，否则可能匹配到同名小地名。",
                required=True,
            ),
        ],
    )
    async def tool_get_weather(self, location: str = "", **kwargs):
        _ = kwargs
        location = (location or "").strip()
        if not location:
            return {"name": "get_weather", "content": "请提供要查询的城市或地点名称"}
        content = await fetch_weather(location)
        return {"name": "get_weather", "content": content}

    async def on_config_update(self, scope: str, config_data: dict[str, object], version: str) -> None:
        _ = (scope, config_data, version)
        self._plugin_config_cache = None


def create_plugin() -> ADayWithMittesPlugin:
    return ADayWithMittesPlugin()
