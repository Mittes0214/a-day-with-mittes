"""Mittes 的一天插件 (A Day With Mittes)

给 Mittes 一份会自己长出来的日程，并让它以正确的方式影响她说话。

核心是**事实层与状态层分离**（设计文档第 2 节）：

| | 内容 | 出现时机 | 是否含活动名词 |
|---|---|---|---|
| 事实层 | 她具体在做什么（故事化文本） | 只有被问才出现（Tool） | 有，这是它的价值 |
| 状态层 | 她的身体/情绪，以及这如何影响说话 | 每轮都在（常驻注入） | **绝对没有** |

「正在洗碗」是一句可陈述的事实，无论从哪条路进上下文，模型都有复述冲动；
而「你有点累、话短」不是可陈述内容，模型没法复述它，只能照做。
状态层的不可复述性完全建立在「没有活动名词」这条纪律上，这条一破，整套设计的地基就没了。

组件：
- Tool ``get_current_schedule``：事实层，返回当前时段的故事化文本
- Tool ``get_weather``：实时天气查询
- Hook ``maisaka.planner.before_request``：planner 状态层注入
- Hook ``maisaka.replyer.before_model_request``：replyer 语气注入
- Command ``/status *``：调试命令，仅 operator

作者：Mittes
版本：3.1.1
许可：GPL-v3.0-or-later
兼容：MaiBot-r-dev (SDK 2.0+)
"""

from datetime import date, datetime, time as dt_time, timedelta
from pathlib import Path
from typing import Any

import asyncio
import logging
import tomllib
import uuid

from maibot_sdk import Command, HookHandler, MaiBotPlugin, Tool
from maibot_sdk.types import ToolParameterInfo, ToolParamType

from .generator import SegmentGenerator
from .negative_events import LEVEL_MILD, NegativeEntry, NegativeScheduler
from .preview import PromptPreview
from .schedule_generator import ScheduleGenerator
from .schedule_store import (
    ScheduleStore,
    Segment,
    SegmentState,
    now_jst,
    weekday_name,
)
from .weather_fetcher import fetch_daily_forecast, fetch_weather


_logger = logging.getLogger("a_day_with_mittes")

# planner 侧状态层的锚点：主程序构造的「时间：YYYY-MM-DD HH:MM:SS」那条 User item
# （src/maisaka/chat_loop_service.py:776-780），我们插在它后面。
_PLANNER_ANCHOR_PREFIX = "时间："

# replyer 侧的兜底锚点：final user message 以「当前时间：」开头，位置固定必然存在。
_REPLYER_FALLBACK_PREFIX = "当前时间："

_ROLE_BY_ITEM_TYPE = {
    "SystemMessageItem": "system",
    "UserMessageItem": "user",
    "AssistantMessageItem": "assistant",
}


class ADayWithMittesPlugin(MaiBotPlugin):
    def __init__(self) -> None:
        super().__init__()
        self._plugin_dir = Path(__file__).parent
        self._store: ScheduleStore | None = None
        self._generator: SegmentGenerator | None = None
        self._negative: NegativeScheduler | None = None
        self._holidays: ScheduleGenerator | None = None
        self._batch_task: asyncio.Task[None] | None = None
        self._batch_lock = asyncio.Lock()
        # 后台任务的强引用；不持有的话事件循环可能把它当垃圾回收掉
        self._background: set[asyncio.Task[None]] = set()
        self._plugin_config_cache: dict[str, Any] | None = None
        self._last_batch_day: date | None = None

    # ── 生命周期 ──
    async def on_load(self) -> None:
        data_dir = self._plugin_dir / "data"
        data_dir.mkdir(parents=True, exist_ok=True)

        self._store = ScheduleStore(self._plugin_dir, data_dir)
        self._store.load_skeleton()
        self._store.open_db()

        self._negative = NegativeScheduler(
            data_dir,
            quota=int(await self._get_config("generation.negative_event_quota", 2)),
            medium_ratio=float(await self._get_config("generation.negative_medium_ratio", 0.3)),
        )
        self._negative.load()

        self._generator = SegmentGenerator(
            self.ctx,
            self._store,
            PromptPreview(
                enabled=bool(await self._get_config("observability.prompt_preview_enabled", True)),
                max_records=int(await self._get_config("observability.prompt_preview_limit", 256)),
            ),
            model=str(await self._get_config("generation.model", "claude-sonnet-5")),
            digest_model=str(await self._get_config("generation.digest_model", "glm-5.2")),
            topic_model=str(await self._get_config("generation.topic_model", "glm-5.2")),
            base_task=str(await self._get_config("generation.base_task", "memory")),
            temperature=float(await self._get_config("generation.temperature", 0.9)),
        )
        self._holidays = ScheduleGenerator(self.ctx)

        self._batch_task = asyncio.create_task(self._scheduler_loop())
        _logger.info("[加载] 骨架就绪，批量生成守护已启动")

    async def on_unload(self) -> None:
        if self._store is not None:
            self._store.close_db()
        if self._batch_task is not None:
            self._batch_task.cancel()
            self._batch_task = None
        for task in list(self._background):
            task.cancel()
        self._background.clear()

    async def on_config_update(self, scope: str, config_data: dict[str, object], version: str) -> None:
        del scope, config_data, version
        self._plugin_config_cache = None

    def get_components(self) -> list[dict[str, Any]]:
        """按 config.toml 的 [components] 开关过滤已禁用的 Tool。

        覆盖默认实现，让禁用的工具完全不出现在 LLM 的 tool_definitions 中。
        本方法在 Runner 加载时同步调用，走不了异步的 self.ctx.config，
        所以直接读插件目录下的 config.toml；开关改动需要重启 MaiBot 才生效。
        """
        components = super().get_components()
        toggles = self._read_component_toggles()
        return [
            component
            for component in components
            if not (
                component.get("type", "").upper() == "TOOL"
                and not toggles.get(f"enable_{component.get('name', '')}", True)
            )
        ]

    def _read_component_toggles(self) -> dict[str, bool]:
        """从插件目录的 config.toml 读取 [components] 段。"""
        path = self._plugin_dir / "config.toml"
        if not path.exists():
            return {}
        with path.open("rb") as handle:
            data = tomllib.load(handle)
        return {key: bool(value) for key, value in (data.get("components") or {}).items()}

    # ── 配置读取 ──
    async def _ensure_plugin_config(self) -> dict[str, Any]:
        if self._plugin_config_cache is None:
            raw = await self.ctx.config.get_all()
            self._plugin_config_cache = _unwrap_config(raw)
        return self._plugin_config_cache

    async def _get_config(self, key: str, default: Any = None) -> Any:
        config = await self._ensure_plugin_config()
        current: Any = config
        for part in key.split("."):
            if not isinstance(current, dict) or part not in current:
                return default
            current = current[part]
        return current

    # ── 批量生成 ──
    async def _scheduler_loop(self) -> None:
        """守护协程：到点跑批次，冷启动时补当天。

        - 冷启动时**今天和明天缺哪天补哪天**（剩余时段用底稿顶着，不阻塞回复）
        - 每天 ``generation.run_at``（默认 12:00 JST）→ 跑次日全天
        """
        await asyncio.sleep(10)  # 等主程序其余部分起来，避免抢启动资源
        try:
            store = self._require_store()
            today = now_jst().date()
            # 系统要维持的不变量是「今天和明天都有日程」，12:00 那次批次负责往前推进；
            # 冷启动（首次部署、库被删、连着几天没跑成）应当把这个不变量整个补回来，
            # 而不是只补今天——只补今天的话，过了零点明天那几段全是底稿。
            #
            # 判据是「有没有记录」而不是「有没有生成成功」：失败的批次也会把底稿写进库，
            # 所以模型挂掉时反复重启不会每次空转一整轮。修好后用 /status batch 手动补。
            for offset, label in ((0, "今天"), (1, "明天")):
                target = today + timedelta(days=offset)
                if store.day_cache(target) is None:
                    _logger.info("[批次] 库里没有%s，冷启动补跑", label)
                    await self._run_batch_guarded(target, f"冷启动补跑（{label}）")

            while True:
                await asyncio.sleep(60)
                now = now_jst()
                run_at = await self._run_at()
                if now.time() < run_at:
                    continue
                if self._last_batch_day == now.date():
                    continue
                self._last_batch_day = now.date()
                target = now.date() + timedelta(days=1)
                # 冷启动可能已经把明天补出来了（重启发生在 run_at 之后就会这样），
                # 不查一下会白跑一整轮
                if store.day_cache(target) is not None:
                    _logger.info("[批次] %s 已有记录，跳过每日批次", target)
                    continue
                await self._run_batch_guarded(target, "每日批次")
        except asyncio.CancelledError:
            raise
        except Exception:
            _logger.exception("[批次] 守护协程异常退出")

    async def _run_batch_guarded(self, day: date, reason: str) -> None:
        """跑一次批次，并保证异常不会掀掉守护协程。

        守护协程一旦退出，之后每天的批次就都不会再跑，直到下次重启——
        这比某一天生成失败严重得多，所以这里必须把异常吃掉。
        """
        try:
            await self.run_batch(day, reason=reason)
        except asyncio.CancelledError:
            raise
        except Exception:
            _logger.exception("[批次] %s 执行失败，守护继续", day)

    async def _run_at(self) -> dt_time:
        raw = str(await self._get_config("generation.run_at", "12:00"))
        hour, minute = raw.split(":")
        return dt_time(int(hour), int(minute))

    async def run_batch(self, day: date, *, reason: str) -> dict[str, Any]:
        """跑一天的批次：顺序生成每一段，滚动更新当日概要，最后报告。

        Args:
            day: 要生成的日期。
            reason: 触发原因，写进日志和报告。

        Returns:
            dict[str, Any]: 批次结果概要，供 ``/status`` 复用。
        """
        async with self._batch_lock:
            store = self._require_store()
            generator = self._require_generator()
            negative = self._require_negative()
            started = now_jst()

            # 排期挂在批次开头做惰性检查：这样排期和生成不会错序，
            # 也不用担心单独的定时任务漏跑导致某天没有排期数据。
            negative.ensure_week(day, store.segments_of)

            weather = await self._forecast_for(day)
            holiday = await self._holiday_name(day)

            segments = store.segments_of(day)
            day_cache = store.ensure_day_cache(day)
            # 凌晨那段承接的是前一天最后一段，概要也从前一天末尾接上
            previous_cache = store.day_cache(day - timedelta(days=1))
            digest = previous_cache.day_digest if previous_cache is not None else ""

            failures: list[tuple[Segment, str]] = []
            aborted = ""
            # 连续几段都在调用层失败 → 模型是真的挂了，别再往下打十几次。
            # 只失败一段则继续：偶发的超时不该赔上一整天。
            consecutive_fatal = 0
            for segment in segments:
                previous = self._previous_state(day, segment)
                level = negative.level_of(day, segment.slot)
                outcome = await generator.generate_segment(
                    day=day,
                    segment=segment,
                    weather=weather,
                    holiday=holiday,
                    previous=previous,
                    day_digest=digest,
                    negative_level=level,
                )
                outcome.state.generated_at = now_jst().isoformat()
                day_cache.segments[segment.slot] = outcome.state

                if outcome.fatal:
                    consecutive_fatal += 1
                    failures.append((segment, outcome.reason))
                    if consecutive_fatal < 2:
                        _logger.warning(
                            "[批次] %s %s 调用失败（%s），继续下一段",
                            day, segment.slot, outcome.reason,
                        )
                        continue
                    # 连着两段都打不通，模型是真的挂了。剩余时段直接铺底稿，
                    # 让库里留下「今天已经试过」的痕迹，重启不会再空转一轮。
                    aborted = outcome.reason
                    for rest in segments[segments.index(segment) :]:
                        day_cache.segments.setdefault(rest.slot, store.fallback_for(rest))
                    break
                consecutive_fatal = 0

                if not outcome.ok:
                    failures.append((segment, outcome.reason))
                    continue
                if segment.kind == "睡眠":
                    # 概要写的是「醒着的这一天里做过什么」，跨过睡眠段就该归零
                    digest = ""
                else:
                    digest = await generator.update_digest(digest, outcome.state.story)

            day_cache.day_digest = digest

            # 第二轮：全天一次调用，为每段提炼一句谈资（5.10）。
            # 放在主生成之后，因为它要读全天的 story。
            topics, topic_error = 0, ""
            if not aborted:
                topics, topic_error = await generator.extract_topics(
                    day, segments, day_cache.segments
                )

            elapsed = (now_jst() - started).total_seconds()
            generated = store.day_generated_count(day)
            store.flush(
                day,
                model=str(await self._get_config("generation.model", "replyer")),
                negative_level_of=lambda slot: negative.level_of(day, slot),
                holiday=holiday,
                weather=weather,
                batch_reason=reason,
                batch_at=now_jst().isoformat(),
                batch_elapsed=elapsed,
                ok_count=generated,
                total_count=len(segments),
                aborted=aborted,
            )

            summary = {
                "day": day,
                "total": len(segments),
                "ok": generated,
                "failures": failures,
                "aborted": aborted,
                "elapsed": elapsed,
                "negative": negative.entries_of_day(day),
                "topics": topics,
                "topic_error": topic_error,
                "reason": reason,
            }
            _logger.info(
                "[批次] %s %s：%d/%d 段完成，耗时 %.0f 秒%s",
                day,
                reason,
                generated,
                len(segments),
                elapsed,
                f"，因「{aborted}」中止" if aborted else "",
            )
            await self._report(summary)
            return summary

    def _previous_state(self, day: date, segment: Segment) -> tuple[Segment, SegmentState] | None:
        """取上一段的骨架和生成结果，用于承接；上一段没生成过则返回 None。"""
        store = self._require_store()
        previous_day, previous_segment = store.previous_segment(day, segment)
        state = store.state_of(previous_day, previous_segment)
        if state is None or not state.generated:
            return None
        return previous_segment, state

    async def _report(self, summary: dict[str, Any]) -> None:
        """把批次结果发到报告群。成功也发——静默成功等于没有监控。"""
        group_id = str(await self._get_config("observability.report_group_id", "")).strip()
        if not group_id:
            return

        day: date = summary["day"]
        lines = [f"【日程生成】{day.isoformat()} 周{weekday_name(day)}"]
        failures: list[tuple[Segment, str]] = summary["failures"]
        if summary.get("aborted"):
            lines.append(f"{summary['ok']}/{summary['total']} 段完成，其余已中止并退回底稿。")
            lines.append(f"中止原因：{summary['aborted']}")
            lines.append("模型调用失败，重试无意义；修好后用 /status batch 手动补跑。")
        elif failures:
            lines.append(f"{summary['ok']}/{summary['total']} 段完成，{len(failures)} 段已退回底稿：")
            lines.extend(f"- {segment.slot}　{reason}" for segment, reason in failures)
        else:
            lines.append(f"{summary['ok']}/{summary['total']} 段完成，耗时 {summary['elapsed']:.0f} 秒")
        if summary.get("topic_error"):
            lines.append(f"话题提炼失败：{summary['topic_error']}")
        elif not summary.get("aborted"):
            lines.append(f"可说的话题：{summary.get('topics', 0)} 条")
        for entry in summary["negative"]:
            lines.append(f"负面事件：{entry.slot}（{entry.level}）")

        platform = str(await self._get_config("observability.report_platform", "qq"))
        stream = await self.ctx.chat.get_stream_by_group_id(group_id, platform=platform)
        stream_id = _stream_id_of(stream)
        if not stream_id:
            _logger.warning("[报告] 找不到群 %s 的聊天流，跳过本次报告", group_id)
            return
        await self.ctx.send.text("\n".join(lines), stream_id)

    async def _forecast_for(self, day: date) -> str:
        """取目标日期的天气预报。提前一天生成拿不到实时天气，只能用预报。"""
        location = str(await self._get_config("weather_location", "Tokyo"))
        return await fetch_daily_forecast(location, day.isoformat())

    async def _holiday_name(self, day: date) -> str:
        """取当天的日本节假日名，没有则返回空串。"""
        if self._holidays is None:
            return ""
        try:
            holiday_map = await self._holidays.get_holiday_map(day.year)
            name = self._holidays.get_holiday_name(day.isoformat(), holiday_map)
        except Exception as exc:
            _logger.warning("[节假日] 查询失败：%s", type(exc).__name__)
            return ""
        return f"【{name}】" if name else ""

    # ── 事实层与状态层的取值 ──
    def _current(self) -> tuple[datetime, Segment, SegmentState]:
        moment = now_jst()
        segment, state = self._require_store().state_at(moment)
        return moment, segment, state

    def _planner_block(self, state: SegmentState) -> str:
        """planner 状态层文本。

        这里一个活动名词都不能有：planner 知道「她有点忙但能搭话」，
        不知道她在换衣服，想抄也没得抄。
        """
        return f"【Mittes 当前状态】\n忙碌度：{state.busy}\n心情：{state.mood}"

    @staticmethod
    def _topic_block(topic: str) -> str:
        """replyer 侧的谈资文案（C）。

        措辞要明确留出不说的余地——它是常驻注入的，如果写成硬性要求，
        她会在任何语境下硬插一句。
        """
        return f"有件小事可以说：{topic}\n聊得上就提一句，聊不上就别硬插。"

    # ── Tool ──
    @Tool(
        "get_current_schedule",
        brief_description="当需要描述 Mittes 当前在做什么时，必须调用此工具获取真实日程，不得推测。",
        parameters=[],
    )
    async def tool_get_schedule(self, **kwargs: Any) -> dict[str, str]:
        del kwargs
        moment, segment, state = self._current()
        content = (
            "【Mittes 当前日程】\n"
            f"{moment:%Y-%m-%d} 周{weekday_name(moment.date())} {moment:%H:%M}\n"
            "\n"
            f"{segment.slot}　{segment.title}\n"
            f"{state.story}\n"
            "\n"
            "以上是她真实的当下，可以据此回答；不要复述原文，也不要在没人问的时候主动提起。"
        )
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
    async def tool_get_weather(self, location: str = "", **kwargs: Any) -> dict[str, str]:
        del kwargs
        location = location.strip()
        if not location:
            return {"name": "get_weather", "content": "请提供要查询的城市或地点名称"}
        return {"name": "get_weather", "content": await fetch_weather(location)}

    # ── Hook：planner 状态层 ──
    @HookHandler(
        "maisaka.planner.before_request",
        name="schedule_state_planner",
        mode="blocking",
        order="normal",
        timeout_ms=3000,
        error_policy="skip",
    )
    async def handle_planner_before_request(
        self,
        items: list[Any] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """在「时间：」那条 User item 后面插入状态层。"""
        if not items:
            return _hook_response(items, kwargs)

        _moment, _segment, state = self._current()
        block = self._planner_block(state)

        index = _find_item_index(items, lambda text: text.startswith(_PLANNER_ANCHOR_PREFIX))
        updated = list(items)
        # 找不到锚点就挂在最后：状态层晚一点出现也比不出现好
        updated.insert(index + 1 if index >= 0 else len(updated), _new_user_item(block))
        return _hook_response(updated, kwargs)

    # ── Hook：replyer 语气注入 ──
    @HookHandler(
        "maisaka.replyer.before_model_request",
        name="schedule_manner_replyer",
        mode="blocking",
        order="normal",
        timeout_ms=3000,
        error_policy="skip",
    )
    async def handle_replyer_before_model_request(
        self,
        items: list[Any] | None = None,
        reply_reason: str = "",
        reply_tool_args: dict[str, Any] | None = None,
        session_id: str = "",
        **kwargs: Any,
    ) -> dict[str, Any]:
        """在 planner 的 reply_reference item 之前插入 A（说话方式）和 C（谈资）。

        锚点从 hook 载荷算出来，不是猜位置：reference item 的正文由主程序按固定规则
        拼装（maisaka_generator_base.py:574-594）——``reply_tool_args["reply_reference"]``
        非空时正文就是它原样，否则是 ``当前思考：\\n{reply_reason}``。

        用「包含」而不是「相等」来匹配，是因为 02_owner_auth_plugin 可能已经往
        reference 首行合并过身份文案，两个插件的 hook 顺序不保证。
        """
        if not items:
            return {"success": True, "action": "continue"}

        moment, segment, state = self._current()
        store = self._require_store()

        # A：说话方式。C：今天那件可说的小事，说出口之后就不再注入（5.11）。
        blocks = [state.manner] if state.manner else []
        share_pending = False
        if state.topic and session_id and not store.is_shared(moment.date(), segment.slot, session_id):
            blocks.append(self._topic_block(state.topic))
            share_pending = True
        if not blocks:
            return {"success": True, "action": "continue"}

        reference = str((reply_tool_args or {}).get("reply_reference") or "").strip()
        expected = reference or (f"当前思考：\n{reply_reason}".strip() if reply_reason else "")

        index = -1
        if expected:
            index = _find_item_index(items, lambda text: expected in text)
        if index < 0:
            index = _find_item_index(items, lambda text: text.startswith(_REPLYER_FALLBACK_PREFIX))
        if index < 0:
            _logger.warning("[replyer] 两个锚点都没匹配上，本次不注入")
            return {"success": True, "action": "continue"}

        updated = list(items)
        for offset, text in enumerate(blocks):
            updated.insert(index + offset, _new_user_item(text))

        if share_pending:
            store.mark_injected(moment.date(), segment.slot, session_id)

        return _hook_response(
            updated,
            kwargs,
            extra={
                "reply_reason": reply_reason,
                "reply_tool_args": reply_tool_args,
                "session_id": session_id,
            },
        )

    # ── Hook：谈资的消费检测 ──
    @HookHandler(
        "maisaka.replyer.after_response",
        name="schedule_topic_consume",
        mode="observe",
        order="normal",
        timeout_ms=3000,
        error_policy="skip",
    )
    async def handle_replyer_after_response(
        self,
        response: str = "",
        session_id: str = "",
        **kwargs: Any,
    ) -> dict[str, Any]:
        """检测她有没有把今天那件小事说出去；说了就把谈资标记掉，之后不再注入。

        用 ``observe`` 模式：这个 hook 本身能改写回复正文、甚至要求重新生成，
        而我们只想读——observe 从机制上保证碰不坏回复。

        判据是**命中任意一个关键词就算说过**。阈值偏松是有意的，两种错的代价不对称：
        误判「说过了」只是少一次开口机会；误判「没说」是她把同一件事说第二遍，
        那才是最要避免的。
        """
        del kwargs
        if not response or not session_id:
            return {"success": True, "action": "continue"}

        moment, segment, state = self._current()
        if not state.topic or not state.topic_keys:
            return {"success": True, "action": "continue"}

        store = self._require_store()
        if store.is_shared(moment.date(), segment.slot, session_id):
            return {"success": True, "action": "continue"}

        hit = next((key for key in state.topic_keys if key and key in response), "")
        if not hit:
            return {"success": True, "action": "continue"}

        store.mark_shared(moment.date(), segment.slot, session_id, moment.isoformat())
        _logger.info(
            "[谈资] %s %s 在会话 %s 说出口了（命中「%s」），不再注入",
            moment.date(),
            segment.slot,
            session_id,
            hit,
        )
        return {"success": True, "action": "continue"}

    # ── Command ──
    @Command(
        "status",
        description="查看 Mittes 当前时段状态（仅管理员）",
        pattern=r"^/status$",
        permission="operator",
    )
    async def cmd_status(self, stream_id: str = "", **kwargs: Any) -> tuple[bool, str, bool]:
        del kwargs
        moment, segment, state = self._current()
        run_at = await self._run_at()
        lines = [
            f"{moment:%Y-%m-%d} 周{weekday_name(moment.date())} {moment:%H:%M}（JST）",
            f"当前时段：{segment.slot}　{segment.title}",
            f"　地点：{segment.place}　服装：{segment.outfit}",
            f"　同处：{segment.company}　性质：{segment.kind}",
            "",
            f"story：{state.story}",
            f"manner：{state.manner}",
            f"mood：{state.mood}",
            f"busy：{state.busy}",
            f"topic：{state.topic or '（这段没什么好说的）'}",
            "",
            "来源：生成结果" if state.generated else "来源：底稿（该段未生成成功）",
            f"下次批次：每天 {run_at:%H:%M} 生成次日全天",
        ]
        await self.ctx.send.text("\n".join(lines), stream_id)
        return True, "已输出当前状态", True

    @Command(
        "status_prompt",
        description="查看本时段实际注入 planner / replyer 的原文（仅管理员）",
        pattern=r"^/status\s+prompt$",
        permission="operator",
    )
    async def cmd_status_prompt(self, stream_id: str = "", **kwargs: Any) -> tuple[bool, str, bool]:
        del kwargs
        _moment, _segment, state = self._current()
        text = (
            "── planner 状态层（插在「时间：」之后）──\n"
            f"{self._planner_block(state)}\n"
            "\n"
            "── replyer A 说话方式（插在 reply_reference 之前）──\n"
            f"{state.manner}\n"
            "\n"
            "── replyer C 谈资（A 之后；说出口后就不再注入）──\n"
            + (self._topic_block(state.topic) if state.topic else "（这段没什么好说的，不注入）")
        )
        await self.ctx.send.text(text, stream_id)
        return True, "已输出注入原文", True

    @Command(
        "status_day",
        description="查看今天各时段的生成状态（仅管理员）",
        pattern=r"^/status\s+day$",
        permission="operator",
    )
    async def cmd_status_day(self, stream_id: str = "", **kwargs: Any) -> tuple[bool, str, bool]:
        del kwargs
        store = self._require_store()
        negative = self._require_negative()
        moment = now_jst()
        today = moment.date()
        current = store.segment_at(moment)

        lines = [f"【今日日程】{today.isoformat()} 周{weekday_name(today)}"]
        for segment in store.segments_of(today):
            state = store.state_of(today, segment)
            if state is None:
                mark = "未生成"
            elif state.generated:
                mark = "已生成"
            else:
                mark = "底稿"
            row = f"{'▶' if segment is current else '　'}{segment.slot}　{segment.title}　[{mark}]"
            level = negative.level_of(today, segment.slot)
            if level:
                row += f"　※负面事件·{level}"
            lines.append(row)
        await self.ctx.send.text("\n".join(lines), stream_id)
        return True, "已输出今日日程", True

    @Command(
        "status_regen",
        description="强制重生成当前时段（仅管理员）",
        pattern=r"^/status\s+regen$",
        permission="operator",
    )
    async def cmd_status_regen(self, stream_id: str = "", **kwargs: Any) -> tuple[bool, str, bool]:
        del kwargs
        moment, segment, old_state = self._current()
        await self.ctx.send.text(f"正在重生成 {segment.slot}　{segment.title}……", stream_id)
        self._spawn(self._regen_text(moment.date(), segment, old_state), stream_id)
        return True, "已开始重生成当前时段", True

    async def _regen_text(self, day: date, segment: Segment, old_state: SegmentState) -> str:
        new_state = await self._regenerate(day, segment)
        return (
            f"【重生成】{segment.slot}　{segment.title}\n"
            "\n"
            f"[旧] {old_state.mood}｜{old_state.manner}\n"
            f"[新] {new_state.mood}｜{new_state.manner}\n"
            "\n"
            f"{new_state.story}"
        )

    @Command(
        "status_next",
        description="提前生成下一个时段但不切换（仅管理员）",
        pattern=r"^/status\s+next$",
        permission="operator",
    )
    async def cmd_status_next(self, stream_id: str = "", **kwargs: Any) -> tuple[bool, str, bool]:
        del kwargs
        store = self._require_store()
        moment = now_jst()
        today = moment.date()
        segments = store.segments_of(today)
        index = segments.index(store.segment_at(moment))
        if index + 1 < len(segments):
            target_day, target = today, segments[index + 1]
        else:
            target_day = today + timedelta(days=1)
            target = store.segments_of(target_day)[0]

        await self.ctx.send.text(f"正在生成 {target.slot}　{target.title}……", stream_id)
        self._spawn(self._next_text(target_day, target), stream_id)
        return True, "已开始生成下一时段", True

    async def _next_text(self, day: date, segment: Segment) -> str:
        state = await self._regenerate(day, segment)
        return (
            f"【下一时段】{day.isoformat()} {segment.slot}　{segment.title}\n"
            "\n"
            f"{state.story}\n"
            "\n"
            f"manner：{state.manner}\nmood：{state.mood}\nbusy：{state.busy}"
        )

    @Command(
        "status_topic",
        description="查看当前时段的话题与分享状态（仅管理员）",
        pattern=r"^/status\s+topic$",
        permission="operator",
    )
    async def cmd_status_topic(self, stream_id: str = "", **kwargs: Any) -> tuple[bool, str, bool]:
        del kwargs
        store = self._require_store()
        moment, segment, state = self._current()
        today = moment.date().isoformat()

        lines = [f"【谈资】{segment.slot}　{segment.title}"]
        if not state.topic:
            lines.append("这段没什么好说的，不注入。")
            await self.ctx.send.text("\n".join(lines), stream_id)
            return True, "已输出谈资状态", True

        lines.append(f"话题：{state.topic}")
        lines.append(f"关键词：{'、'.join(state.topic_keys)}")
        lines.append("")
        rows = [r for r in store.db.shares_of_day(today) if r["slot"] == segment.slot]
        if not rows:
            lines.append("还没在任何会话里注入过。")
        for row in rows:
            mark = f"已说出口 {row['shared_at'][11:16]}" if row["shared_at"] else "还没说"
            lines.append(f"- {row['session_id']}　注入 {row['injected']} 次　{mark}")
        await self.ctx.send.text("\n".join(lines), stream_id)
        return True, "已输出谈资状态", True

    @Command(
        "status_topics",
        description="重跑某天的话题提炼（仅管理员）",
        pattern=r"^/status\s+topics(?:\s+(?P<day>\d{4}-\d{2}-\d{2}))?$",
        permission="operator",
    )
    async def cmd_status_topics(
        self, stream_id: str = "", **kwargs: Any
    ) -> tuple[bool, str, bool]:
        day = self._group(kwargs, "day")
        target = date.fromisoformat(day) if day else now_jst().date()
        await self.ctx.send.text(f"开始重跑 {target.isoformat()} 的话题提炼……", stream_id)
        self._spawn(self._topics_text(target), stream_id)
        return True, "话题提炼已在后台开始", True

    async def _topics_text(self, day: date) -> str:
        """重跑某天的话题提炼并渲染结果。"""
        store = self._require_store()
        generator = self._require_generator()
        cached = store.day_cache(day)
        if cached is None:
            return f"{day.isoformat()} 还没有日程，先跑 /status batch {day.isoformat()}。"

        segments = store.segments_of(day)
        produced, error = await generator.extract_topics(day, segments, cached.segments)
        store.flush(
            day,
            model=str(await self._get_config("generation.model", "replyer")),
            negative_level_of=lambda slot: self._require_negative().level_of(day, slot),
            batch_reason="话题重提炼",
            batch_at=now_jst().isoformat(),
        )
        if error:
            return f"话题提炼失败：{error}"

        lines = [f"【话题】{day.isoformat()}　{produced} 条"]
        for segment in segments:
            state = cached.segments.get(segment.slot)
            if state is not None and state.topic:
                lines.append(f"{segment.slot}　{state.topic}")
        return "\n".join(lines)

    @Command(
        "status_db",
        description="查看日程归档库的规模（仅管理员）",
        pattern=r"^/status\s+db$",
        permission="operator",
    )
    async def cmd_status_db(self, stream_id: str = "", **kwargs: Any) -> tuple[bool, str, bool]:
        del kwargs
        store = self._require_store()
        first, last = store.db.date_range()
        path = self._plugin_dir / "data" / "schedule.db"
        size = path.stat().st_size / 1024 if path.exists() else 0
        lines = [
            "【日程归档库】",
            f"路径：{path}",
            f"覆盖：{first or '—'} ~ {last or '—'}",
            f"段数：{store.db.count_segments()}　文件：{size:.0f} KB",
            "",
            "外部只读："
            'sqlite3.connect("file:schedule.db?mode=ro", uri=True)',
        ]
        await self.ctx.send.text("\n".join(lines), stream_id)
        return True, "已输出归档库信息", True

    @Command(
        "status_neg",
        description="查看本周负面事件排期（仅管理员）",
        pattern=r"^/status\s+neg$",
        permission="operator",
    )
    async def cmd_status_neg(self, stream_id: str = "", **kwargs: Any) -> tuple[bool, str, bool]:
        del kwargs
        await self.ctx.send.text(self._render_week(now_jst().date()), stream_id)
        return True, "已输出负面事件排期", True

    @Command(
        "status_neg_reroll",
        description="重摇本周负面事件排期（仅管理员）",
        pattern=r"^/status\s+neg\s+reroll$",
        permission="operator",
    )
    async def cmd_status_neg_reroll(self, stream_id: str = "", **kwargs: Any) -> tuple[bool, str, bool]:
        del kwargs
        store = self._require_store()
        today = now_jst().date()
        self._require_negative().reroll_week(today, store.segments_of)
        await self.ctx.send.text("已重摇。\n" + self._render_week(today), stream_id)
        return True, "已重摇负面事件排期", True

    @Command(
        "status_neg_clear",
        description="清空本周负面事件排期（仅管理员）",
        pattern=r"^/status\s+neg\s+clear$",
        permission="operator",
    )
    async def cmd_status_neg_clear(self, stream_id: str = "", **kwargs: Any) -> tuple[bool, str, bool]:
        del kwargs
        today = now_jst().date()
        self._require_negative().replace_week(today, [])
        await self.ctx.send.text("本周负面事件排期已清空。", stream_id)
        return True, "已清空负面事件排期", True

    @Command(
        "status_neg_add",
        description="手动指定一条负面事件（仅管理员）",
        pattern=r"^/status\s+neg\s+add\s+(?P<day>\d{4}-\d{2}-\d{2})\s+(?P<slot>\d{2}:\d{2}-\d{2}:\d{2})(?:\s+(?P<level>轻微|中等))?$",
        permission="operator",
    )
    async def cmd_status_neg_add(
        self,
        stream_id: str = "",
        **kwargs: Any,
    ) -> tuple[bool, str, bool]:
        day = self._group(kwargs, "day")
        slot = self._group(kwargs, "slot")
        level = self._group(kwargs, "level")
        store = self._require_store()
        negative = self._require_negative()
        target_day = date.fromisoformat(day)
        if not any(segment.slot == slot for segment in store.segments_of(target_day)):
            await self.ctx.send.text(f"{target_day} 没有 {slot} 这个时段。", stream_id)
            return False, "时段不存在", True

        _week_start, entries = negative.week_entries(target_day)
        entries = [entry for entry in entries if not (entry.day == target_day and entry.slot == slot)]
        entries.append(NegativeEntry(day=target_day, slot=slot, level=level or LEVEL_MILD))
        entries.sort(key=lambda entry: (entry.day, entry.slot))
        negative.replace_week(target_day, entries)

        await self.ctx.send.text("已添加。\n" + self._render_week(target_day), stream_id)
        return True, "已添加负面事件", True

    @Command(
        "status_batch",
        description="立即跑一次批次，默认次日（仅管理员）",
        pattern=r"^/status\s+batch(?:\s+(?P<day>\d{4}-\d{2}-\d{2}|today))?$",
        permission="operator",
    )
    async def cmd_status_batch(
        self,
        stream_id: str = "",
        **kwargs: Any,
    ) -> tuple[bool, str, bool]:
        day = self._group(kwargs, "day")
        today = now_jst().date()
        if day == "today":
            target = today
        elif day:
            target = date.fromisoformat(day)
        else:
            target = today + timedelta(days=1)

        store = self._require_store()
        await self.ctx.send.text(
            f"开始生成 {target.isoformat()} 的日程，共 {len(store.segments_of(target))} 段，"
            "要跑几分钟，完成后报到日志群。",
            stream_id,
        )
        self._spawn(self._batch_text(target), stream_id)
        return True, "批次已在后台开始", True

    async def _batch_text(self, day: date) -> str:
        """跑批次并返回一句给发起者的回执；详细结果由 run_batch 自己报到日志群。"""
        summary = await self.run_batch(day, reason="手动触发")
        if summary["aborted"]:
            return f"{day.isoformat()} 批次中止：{summary['aborted']}"
        return f"{day.isoformat()} 批次完成：{summary['ok']}/{summary['total']} 段。"

    # ── 内部工具 ──

    @staticmethod
    def _group(kwargs: dict[str, Any], name: str) -> str:
        """取正则命名捕获组。

        **运行时不会把命名组拆成同名形参**，而是整包塞进一个 ``matched_groups``
        字典里（``component_query.py`` 组装 invoke_args，``runner_main`` 再
        ``**invoke.args`` 展开）。所以写成 ``async def cmd(self, day: str = "")``
        永远只拿得到默认值——这是静默失效：命令照常执行，只是参数当没给。
        内置的 plugin_management 就是按 ``matched_groups`` 读的，照它来。
        """
        groups = kwargs.get("matched_groups")
        if not isinstance(groups, dict):
            return ""
        return str(groups.get(name) or "").strip()
    def _spawn(self, coro: Any, stream_id: str) -> None:
        """把耗时的活儿丢到后台，命令本身立刻返回。

        ``plugin.invoke_command`` 的 RPC 超时是 60 秒，而一次时段生成就要二三十秒、
        一整批要好几分钟。同步做完再返回必然超时——虽然协程还会跑完，但调用方
        看到的是一条 E_TIMEOUT 报错，看起来像失败了。
        """

        async def runner() -> None:
            try:
                text = await coro
            except Exception as exc:
                _logger.exception("[命令] 后台任务失败")
                text = f"执行失败：{type(exc).__name__}: {exc}"
            if text:
                await self.ctx.send.text(text, stream_id)

        task = asyncio.create_task(runner())
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    def _render_week(self, day: date) -> str:
        """渲染某一周的负面事件排期。"""
        store = self._require_store()
        week_start, entries = self._require_negative().week_entries(day)
        week_end = week_start + timedelta(days=6)
        lines = [f"【负面事件排期】{week_start.isoformat()} ~ {week_end.isoformat()}"]
        if not entries:
            lines.append("（本周没有排期）")
            return "\n".join(lines)
        for entry in entries:
            title = next(
                (segment.title for segment in store.segments_of(entry.day) if segment.slot == entry.slot),
                "",
            )
            lines.append(f"周{weekday_name(entry.day)} {entry.slot}　{title}　（{entry.level}）")
        return "\n".join(lines)

    async def _regenerate(self, day: date, segment: Segment) -> SegmentState:
        """重生成单段并写回缓存。"""
        store = self._require_store()
        generator = self._require_generator()
        negative = self._require_negative()

        outcome = await generator.generate_segment(
            day=day,
            segment=segment,
            weather=await self._forecast_for(day),
            holiday=await self._holiday_name(day),
            previous=self._previous_state(day, segment),
            day_digest=(store.day_cache(day).day_digest if store.day_cache(day) else ""),
            negative_level=negative.level_of(day, segment.slot),
        )
        outcome.state.generated_at = now_jst().isoformat()
        store.ensure_day_cache(day).segments[segment.slot] = outcome.state
        store.flush(
            day,
            model=str(await self._get_config("generation.model", "replyer")),
            negative_level_of=lambda slot: negative.level_of(day, slot),
            batch_reason="单段重生成",
            batch_at=now_jst().isoformat(),
        )
        return outcome.state

    def _require_store(self) -> ScheduleStore:
        if self._store is None:
            raise RuntimeError("插件尚未加载完成：ScheduleStore 未就绪")
        return self._store

    def _require_generator(self) -> SegmentGenerator:
        if self._generator is None:
            raise RuntimeError("插件尚未加载完成：SegmentGenerator 未就绪")
        return self._generator

    def _require_negative(self) -> NegativeScheduler:
        if self._negative is None:
            raise RuntimeError("插件尚未加载完成：NegativeScheduler 未就绪")
        return self._negative


def _unwrap_config(raw: Any) -> dict[str, Any]:
    """剥掉 config.get_all 可能包的 result / value 外壳。"""
    if not isinstance(raw, dict):
        return {}
    for key in ("result", "value"):
        inner = raw.get(key)
        if isinstance(inner, dict):
            return _unwrap_config(inner) if set(inner) & {"result", "value"} else inner
    return raw


def _item_text(item: Any) -> str:
    """取 Item 的纯文本内容；非消息 Item 返回空串。"""
    if not isinstance(item, dict) or item.get("item_type") not in _ROLE_BY_ITEM_TYPE:
        return ""
    parts = item.get("parts")
    if not isinstance(parts, list):
        return ""
    return "".join(
        str(part.get("text") or "")
        for part in parts
        if isinstance(part, dict) and part.get("type") == "text"
    )


def _find_item_index(items: list[Any], predicate: Any) -> int:
    """从后往前找第一条正文满足条件的 Item，返回下标；找不到返回 -1。

    从后往前是因为两个锚点（「时间：」和「当前时间：」）都在尾部，
    而历史消息里可能出现同样开头的旧内容。
    """
    for index in range(len(items) - 1, -1, -1):
        text = _item_text(items[index])
        if text and predicate(text):
            return index
    return -1


def _new_user_item(text: str) -> dict[str, Any]:
    """合成一条新的 User Item。item_id 必须全局唯一，否则主程序校验会拒收。"""
    return {
        "item_type": "UserMessageItem",
        "meta": {
            "item_id": f"a-day-with-mittes-{uuid.uuid4().hex}",
            "logical_turn_id": None,
            "timestamp": now_jst().isoformat(),
        },
        "parts": [{"type": "text", "text": text}],
    }


def _hook_response(
    items: list[Any] | None,
    kwargs: dict[str, Any],
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构造 hook 返回值。

    主程序对 modified_kwargs 是**整体替换**而非合并，所以未声明的入参
    （item_schema_version、session_id 等）必须经 **kwargs 原样回传，否则会被丢掉。
    """
    modified_kwargs = dict(kwargs)
    if extra:
        modified_kwargs.update(extra)
    modified_kwargs["items"] = items
    return {"success": True, "action": "continue", "modified_kwargs": modified_kwargs}


def _stream_id_of(stream: Any) -> str:
    """从 chat 能力返回的聊天流对象里取出 stream_id。"""
    if isinstance(stream, dict):
        return str(stream.get("stream_id") or stream.get("session_id") or "")
    return str(getattr(stream, "stream_id", "") or getattr(stream, "session_id", "") or "")


def create_plugin() -> ADayWithMittesPlugin:
    return ADayWithMittesPlugin()
