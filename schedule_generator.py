"""节假日日程生成器 — 新 SDK 版本

负责获取节假日数据并生成节日专属日程。

作者：Mittes
版本：2.0.0
"""

import asyncio
import json
import os
from datetime import datetime
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
    """节假日日程生成器"""

    def __init__(self, ctx):
        """初始化生成器

        Args:
            ctx: 插件上下文 (PluginContext)
        """
        self.ctx = ctx
        self._base_dir = os.path.dirname(os.path.abspath(__file__))
        self._cache_dir = os.path.join(self._base_dir, "data", "holidays")
        self._schedule_cache_dir = os.path.join(self._base_dir, "data", "schedules")
        self._generation_locks: dict[str, bool] = {}
        self._ensure_dirs()

    def _ensure_dirs(self) -> None:
        """确保缓存目录存在"""
        os.makedirs(self._cache_dir, exist_ok=True)
        os.makedirs(self._schedule_cache_dir, exist_ok=True)

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

    def get_cached_holiday_schedule(self, date_str: str, holiday_name: str) -> dict[str, str] | None:
        """获取缓存的节日日程

        Args:
            date_str: 日期字符串
            holiday_name: 节日名称

        Returns:
            Optional[Dict[str, str]]: 日程数据，如果不存在则返回 None
        """
        filename = f"{date_str}_{holiday_name}.json"
        path = os.path.join(self._schedule_cache_dir, filename)

        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return None

    async def check_and_get_holiday_schedule(
        self, date_str: str, holiday_name: str
    ) -> dict[str, str] | None:
        """检查并获取节日日程，如果不存在则触发生成

        Args:
            date_str: 日期字符串
            holiday_name: 节日名称

        Returns:
            Optional[Dict[str, str]]: 日程数据
        """
        schedule = self.get_cached_holiday_schedule(date_str, holiday_name)
        if schedule:
            return schedule

        asyncio.create_task(self._generate_holiday_schedule(date_str, holiday_name))
        return None

    async def _generate_holiday_schedule(self, date_str: str, holiday_name: str) -> None:
        """调用 LLM 生成节日日程（后台任务）

        Args:
            date_str: 日期字符串
            holiday_name: 节日名称
        """
        if date_str in self._generation_locks:
            return
        self._generation_locks[date_str] = True

        try:
            prompt = self._build_generation_prompt(date_str, holiday_name)

            result = await self.ctx.llm.generate(prompt, model_type="replyer")

            if result:
                schedule_data = self._parse_schedule_result(result)
                if schedule_data:
                    self._save_holiday_schedule(date_str, holiday_name, schedule_data)

        except Exception:
            pass
        finally:
            if date_str in self._generation_locks:
                del self._generation_locks[date_str]

    def _build_generation_prompt(self, date_str: str, holiday_name: str) -> str:
        """构建日程生成提示词

        Args:
            date_str: 日期字符串
            holiday_name: 节日名称

        Returns:
            str: 提示词
        """
        return f"""请为【{holiday_name}】（{date_str}）生成一份特别的24小时日程。

要求：
1. 结合节日特点（例如元旦去神社、看日出；情人节做巧克力；圣诞节装饰等）
2. 保持日常人设（女仆工作、高中生生活），但加入节日元素
3. 仅仅生成1天的日程
4. 输出格式必须为 JSON 格式

格式示例：
{{
  "time_range_00_01": "...",
  "time_range_01_02": "...",
  ...
  "time_range_23_00": "..."
}}

请直接输出 JSON 内容。"""

    def _parse_schedule_result(self, result: str) -> dict[str, str] | None:
        """解析 LLM 返回的日程结果

        Args:
            result: LLM 返回的字符串

        Returns:
            Optional[Dict[str, str]]: 解析后的日程数据
        """
        content = result
        if "```json" in content:
            content = content.split("```json")[1].split("```")[0].strip()
        elif "```" in content:
            content = content.split("```")[1].split("```")[0].strip()

        try:
            schedule_data = json.loads(content)
            if isinstance(schedule_data, dict) and len(schedule_data) >= 20:
                return {k: str(v) for k, v in schedule_data.items()}
        except json.JSONDecodeError:
            pass

        return None

    def _save_holiday_schedule(
        self, date_str: str, holiday_name: str, schedule_data: dict[str, str]
    ) -> None:
        """保存节日日程到缓存

        Args:
            date_str: 日期字符串
            holiday_name: 节日名称
            schedule_data: 日程数据
        """
        self._ensure_dirs()
        filename = f"{date_str}_{holiday_name}.json"
        path = os.path.join(self._schedule_cache_dir, filename)
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(schedule_data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
