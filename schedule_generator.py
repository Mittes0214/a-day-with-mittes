"""节假日查询。

只负责取节假日名字，贴在生成 prompt 的「今天」那一段里。

重构时删掉了「节日专属日程 LLM 生成」那一套（``check_and_get_holiday_schedule`` /
``_generate_holiday_schedule`` 等）——它们从来没有调用方，是死代码；
新版日程本来就是每天现生成的，节日只作为生成输入之一。

作者：Mittes
版本：3.0.0
"""

import json
import os
from typing import Any

import aiohttp

HOLIDAY_URL_TEMPLATE = "https://unpkg.com/holiday-calendar@1.3.0/data/CN/{year}.json"

FIXED_HOLIDAYS = {
    "01-01": "元旦",
    "02-14": "情人节",
    "03-08": "妇女节",
    "04-01": "愚人节",
    "05-01": "劳动节",
    "05-04": "青年节",
    "06-01": "儿童节",
    "07-01": "建党节",
    "08-01": "建军节",
    "09-10": "教师节",
    "10-01": "国庆节",
    "12-25": "圣诞节",
}


class ScheduleGenerator:
    """节假日数据的下载、缓存与查询。"""

    def __init__(self, ctx):
        """初始化生成器

        Args:
            ctx: 插件上下文 (PluginContext)
        """
        self.ctx = ctx
        self._base_dir = os.path.dirname(os.path.abspath(__file__))
        self._cache_dir = os.path.join(self._base_dir, "data", "holidays")
        self._ensure_dirs()

    def _ensure_dirs(self) -> None:
        """确保缓存目录存在"""
        os.makedirs(self._cache_dir, exist_ok=True)

    def get_holiday_name(self, date_str: str, holiday_map: dict[str, Any]) -> str:
        """从缓存中获取节假日名称

        Args:
            date_str: 日期字符串，格式为 "YYYY-MM-DD"
            holiday_map: 节假日数据映射

        Returns:
            str: 节假日名称，如果不是节假日则返回空字符串
        """
        if holiday_map and date_str in holiday_map:
            info = holiday_map[date_str]
            name = info.get("name_cn", "")
            holiday_type = info.get("type", "")
            if holiday_type == "transfer_workday":
                return f"{name}（调休）"
            return name

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
                        holiday_map = {}
                        for item in data.get("dates", []):
                            holiday_map[item["date"]] = item
                        return holiday_map
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
        cache_file = os.path.join(self._cache_dir, f"{year}.json")
        if os.path.exists(cache_file):
            try:
                with open(cache_file, "r", encoding="utf-8") as f:
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
        cache_file = os.path.join(self._cache_dir, f"{year}.json")
        try:
            with open(cache_file, "w", encoding="utf-8") as f:
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
