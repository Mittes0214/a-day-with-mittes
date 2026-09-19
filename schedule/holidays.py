"""日本祝日查询。

只负责取节假日名字，贴在生成 prompt 的「今天」那一段里。

重构时删掉了「节日专属日程 LLM 生成」那一套（``check_and_get_holiday_schedule`` /
``_generate_holiday_schedule`` 等）——它们从来没有调用方，是死代码；
新版日程本来就是每天现生成的，节日只作为生成输入之一。

作者：Mittes
版本：3.0.0
"""

import json
from pathlib import Path
from typing import Any

import aiohttp

HOLIDAY_URL_TEMPLATE = "https://holidays-jp.github.io/api/v1/{year}/date.json"

FIXED_HOLIDAYS = {
    # 网络不可用时只兜底日期固定的日本法定祝日；成人の日、海の日、敬老の日、
    # 体育の日、春分/秋分及振替休日等日期会变化，不能在这里猜。
    "01-01": "元日",
    "02-11": "建国記念の日",
    "02-23": "天皇誕生日",
    "04-29": "昭和の日",
    "05-03": "憲法記念日",
    "05-04": "みどりの日",
    "05-05": "こどもの日",
    "08-11": "山の日",
    "11-03": "文化の日",
    "11-23": "勤労感謝の日",
}


class ScheduleGenerator:
    """节假日数据的下载、缓存与查询。"""

    def __init__(self, ctx: Any, data_dir: Path) -> None:
        """初始化生成器

        Args:
            ctx: 插件上下文 (PluginContext)
        """
        self.ctx = ctx
        # 国家代码进入路径，避免旧版 ``holidays/<year>.json`` 的中国节假日缓存
        # 在切换数据源后继续命中。
        self._cache_dir = data_dir / "holidays" / "JP"
        self._ensure_dirs()

    def _ensure_dirs(self) -> None:
        """确保缓存目录存在"""
        self._cache_dir.mkdir(parents=True, exist_ok=True)

    def get_holiday_name(self, date_str: str, holiday_map: dict[str, Any]) -> str:
        """从缓存中获取节假日名称

        Args:
            date_str: 日期字符串，格式为 "YYYY-MM-DD"
            holiday_map: 节假日数据映射

        Returns:
            str: 节假日名称，如果不是节假日则返回空字符串
        """
        if holiday_map and date_str in holiday_map:
            # Holidays JP API 的值就是日文祝日名（含「振替休日」）。保留对旧格式的
            # 兼容只为让方法更稳健；JP 子目录不会读到旧版中国缓存。
            info = holiday_map[date_str]
            if isinstance(info, str):
                return info
            if isinstance(info, dict):
                return str(info.get("name") or info.get("name_cn") or "")

        month_day = date_str[5:] if len(date_str) >= 5 else ""
        return FIXED_HOLIDAYS.get(month_day, "")

    async def download_holiday_data(self, year: int) -> dict[str, Any]:
        """下载指定年份的节假日数据

        Args:
            year: 年份

        Returns:
            Dict[str, Any]: 节假日数据映射
        """
        url = HOLIDAY_URL_TEMPLATE.format(year=year)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as response:
                    if response.status == 200:
                        data = await response.json()
                        if not isinstance(data, dict):
                            return {}
                        return {
                            str(day): str(name)
                            for day, name in data.items()
                            if isinstance(day, str) and isinstance(name, str)
                        }
        except Exception:
            pass
        return {}

    def load_cached_holiday(self, year: int) -> dict[str, Any]:
        """从本地缓存加载节假日数据

        Args:
            year: 年份

        Returns:
            Dict[str, Any]: 节假日数据映射
        """
        cache_file = self._cache_dir / f"{year}.json"
        if cache_file.exists():
            try:
                with cache_file.open("r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def save_cached_holiday(self, year: int, data: dict[str, Any]) -> None:
        """保存节假日数据到本地缓存

        Args:
            year: 年份
            data: 节假日数据
        """
        self._ensure_dirs()
        cache_file = self._cache_dir / f"{year}.json"
        try:
            with cache_file.open("w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    async def get_holiday_map(self, year: int) -> dict[str, Any]:
        """获取节假日数据（优先本地缓存，无则下载）

        Args:
            year: 年份

        Returns:
            Dict[str, Any]: 节假日数据映射
        """
        holiday_map = self.load_cached_holiday(year)
        if holiday_map:
            return holiday_map

        holiday_map = await self.download_holiday_data(year)
        if holiday_map:
            self.save_cached_holiday(year, holiday_map)

        return holiday_map
