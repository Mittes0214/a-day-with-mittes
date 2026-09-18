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
- Hook ``maisaka.replyer.before_model_request``：replyer 语气与谈资注入
- Command ``/status *``：调试命令，仅 operator

作者：Mittes
版本：3.6.0
许可：GPL-v3.0-or-later
兼容：MaiBot-r-dev (SDK 2.0+)
"""

from datetime import date, datetime, time as dt_time, timedelta
from pathlib import Path
from typing import Any

import asyncio
import json
import logging
import tomllib
import uuid

from maibot_sdk import Command, HookHandler, MaiBotPlugin, Tool
from maibot_sdk.types import ToolParameterInfo, ToolParamType

from .character.wardrobe import Wardrobe
from .generation.pipeline import SegmentGenerator
from .observability.prompt_preview import PromptPreview
from .reply_style import compose as compose_reply_style
from .schedule.holidays import ScheduleGenerator
from .schedule.negative_events import LEVEL_MILD, NegativeEntry, NegativeScheduler
from .schedule.store import (
    JST,
    ScheduleStore,
    parse_moment,
    Segment,
    SegmentState,
    now_jst,
    weekday_name,
)
from .weather.fetcher import fetch_daily_forecast, fetch_weather


_logger = logging.getLogger("a_day_with_mittes")

# planner 侧状态层的锚点：主程序构造的「时间：YYYY-MM-DD HH:MM:SS」那条 User item
# （src/maisaka/chat_loop_service.py:776-780），我们插在它后面。
_PLANNER_ANCHOR_PREFIX = "时间："

# replyer 侧的兜底锚点：final user message 以「当前时间：」开头，位置固定必然存在。
_REPLYER_FALLBACK_PREFIX = "当前时间："

# reply_style 在 system prompt 里的上下两句。它们是 prompts/zh-CN/maisaka_replyer.prompt
# 模板里写死的正文，reply_style 就夹在中间独占一行。
#
# **用模板句子当锚点，不去读 global_config.personality.reply_style 再做匹配。**
# 那个值是人随时会改的配置，改完这里就静默失配；模板句子只有跟上游同步时才会动，
# 真动了也会在这条 warning 上立刻看出来。
_REPLY_STYLE_HEAD = "然后给出日常且口语化的回复，\n"
_REPLY_STYLE_TAIL = "\n你可以参考【回复信息参考】中的信息"

# 谈资工具名。planner hook 要按它从 tool_definitions 里摘工具，所以名字必须只有一处。
_TOPIC_TOOL_NAME = "get_mittes_topic"

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
        self._wardrobe: Wardrobe | None = None
        self._generator: SegmentGenerator | None = None
        self._negative: NegativeScheduler | None = None
        self._holidays: ScheduleGenerator | None = None
        self._batch_task: asyncio.Task[None] | None = None
        self._admin_task: asyncio.Task[None] | None = None
        self._batch_lock = asyncio.Lock()
        # 后台任务的强引用；不持有的话事件循环可能把它当垃圾回收掉
        self._background: set[asyncio.Task[None]] = set()
        self._plugin_config_cache: dict[str, Any] | None = None
        self._last_batch_day: date | None = None
        # (platform, 群号) → session_id。解析一次就缓存，见 _linked_sessions
        self._session_of_group: dict[tuple[str, str], str] = {}
        # session_id → (逻辑日, 时段)。planner 轮开头清、工具被调用时写、replyer 读，
        # 语义严格是「本轮 planner 取过材」。装的是已发生的事实，不是推断，
        # 所以不需要 TTL 去猜有效期。
        self._topic_pitched: dict[str, tuple[date, str]] = {}

    # ── 生命周期 ──
    async def on_load(self) -> None:
        # 统一持久化目录 data/plugins/<插件ID>；旧版落在插件源码目录 data/ 下，已迁移
        data_dir = self.ctx.paths.data_dir
        data_dir.mkdir(parents=True, exist_ok=True)

        self._store = ScheduleStore(self._plugin_dir / "schedule" / "skeleton.toml", data_dir)
        self._store.load_skeleton()
        self._store.open_db()

        self._negative = NegativeScheduler(
            data_dir,
            quota=int(await self._get_config("generation.negative_event_quota", 2)),
            medium_ratio=float(await self._get_config("generation.negative_medium_ratio", 0.3)),
        )
        self._negative.load()

        self._wardrobe = Wardrobe(self._plugin_dir / "character" / "wardrobe.toml")
        self._wardrobe.load()
        self._warn_unknown_outfits()

        self._generator = SegmentGenerator(
            self.ctx,
            self._store,
            PromptPreview(
                enabled=bool(await self._get_config("observability.prompt_preview_enabled", True)),
                max_records=int(await self._get_config("observability.prompt_preview_limit", 256)),
            ),
            model=str(await self._get_config("generation.model", "claude-sonnet-5")),
            topic_model=str(await self._get_config("generation.topic_model", "glm-5.2")),
            expression_model=str(
                await self._get_config("generation.expression_model", "claude-sonnet-5")
            ),
            base_task=str(await self._get_config("generation.base_task", "memory")),
            temperature=float(await self._get_config("generation.temperature", 0.9)),
        )
        self._holidays = ScheduleGenerator(self.ctx, data_dir)

        self._batch_task = asyncio.create_task(self._scheduler_loop())
        self._admin_task = asyncio.create_task(self._admin_job_loop())
        _logger.info("[加载] 骨架就绪，批量生成与前端管理守护已启动")

    async def on_unload(self) -> None:
        lifecycle = [task for task in (self._batch_task, self._admin_task) if task is not None]
        for task in lifecycle:
            task.cancel()
        for task in list(self._background):
            task.cancel()
        await asyncio.gather(*lifecycle, *self._background, return_exceptions=True)
        self._batch_task = None
        self._admin_task = None
        self._background.clear()
        if self._store is not None:
            self._store.close_db()

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

    async def _manner_enabled(self) -> bool:
        """表达方式（replyer 的 A 块）这一整套功能开着没有。

        关掉之后：第三轮不再调 LLM、replyer 不再注入、``/status expressions`` 和
        管理页的重跑入口都不干活。**库里已有的 manner 一律保留**——这是"暂时停用"
        不是"删除"，改回 true 就恢复，不需要重新生成。
        """
        return bool(await self._get_config("manner.enabled", True))

    async def _reply_style_enabled(self) -> bool:
        """按心情 × 体力替换 reply_style 这套功能开着没有。

        关掉之后 replyer 的 system prompt **完全不动**，主程序配置里的
        ``[personality] reply_style`` 原样生效。这跟「档位不全所以不替换」是两回事：
        那是逐段的退让，这是整套停用。``reply_style.py`` 的表原样留着，
        ``/status`` 和管理页仍会显示拼出来的结果，只是标明当前没生效。
        """
        return bool(await self._get_config("reply_style.enabled", True))

    async def _linked_sessions(self, session_id: str) -> list[str]:
        """返回和 ``session_id`` 同属一个关联组的所有会话（含它自己）。

        配置里填的是**群号**，这里用 ``ctx.chat.get_stream_by_group_id`` 解析成
        session_id——绝不自己算。主程序 CLAUDE.md 写明：业务模块不应调
        ``SessionUtils.calculate_session_id``，解析不到真实聊天流时也不要拿自算的
        hash 顶上，那种 ID 写进库里就是一条永远对不上的脏数据。

        解析结果缓存在进程内。群还没被 bot 见过时解析会失败，那一组这次就当没配——
        下次调用会重试，不做持久化，免得把"暂时查不到"固化成"没有"。
        """
        groups = await self._get_config("topic.linked_groups", []) or []
        if not isinstance(groups, list) or not session_id:
            return [session_id]

        platform = str(await self._get_config("topic.linked_platform", "qq"))
        for group in groups:
            if not isinstance(group, list) or len(group) < 2:
                continue
            resolved: list[str] = []
            for raw in group:
                key = (platform, str(raw).strip())
                if key not in self._session_of_group:
                    stream = await self.ctx.chat.get_stream_by_group_id(key[1], platform=platform)
                    resolved_id = _stream_id_of(stream)
                    if not resolved_id:
                        _logger.warning("[谈资] 关联组里的群 %s 找不到聊天流，本次跳过", key[1])
                        continue
                    self._session_of_group[key] = resolved_id
                resolved.append(self._session_of_group[key])
            if session_id in resolved:
                return resolved
        return [session_id]

    async def _share_seen(self, day: date, slot: str, session_id: str) -> bool:
        """这条谈资在**关联组内任一会话**说出口过没有。

        只用来决定"还要不要注入"。**记录不走这条**：她在关联的另一个群也说了，
        `shares` 照样给那个群记一行，否则观察数据会缺一半。
        """
        if not session_id:
            return False
        store = self._require_store()
        return any(
            store.is_shared(day, slot, linked) for linked in await self._linked_sessions(session_id)
        )

    # ── 批量生成 ──
    async def _scheduler_loop(self) -> None:
        """守护协程：到点跑批次，冷启动时补当天。

        - 冷启动时**今天和明天缺哪天补哪天**（剩余时段用底稿顶着，不阻塞回复）
        - 每天 ``generation.run_at``（默认 12:00 JST）→ 跑次日全天
        """
        await asyncio.sleep(10)  # 等主程序其余部分起来，避免抢启动资源
        try:
            store = self._require_store()
            today = self._today()
            # 系统要维持的不变量是「今天和明天都有日程」，12:00 那次批次负责往前推进；
            # 冷启动（首次部署、库被删、连着几天没跑成）应当把这个不变量整个补回来，
            # 而不是只补今天——只补今天的话，过了零点明天那几段全是底稿。
            #
            # 判据是「有没有记录」而不是「有没有生成成功」：失败的批次也会把底稿写进库，
            # 所以模型挂掉时反复重启不会每次空转一整轮。修好后用 /status batch 手动补。
            for offset, label in ((0, "今天"), (1, "明天")):
                target = today + timedelta(days=offset)
                # 走 load_day_cache 而不是 day_cache：判据是「库里有没有记录」，
                # 只看内存的话，内存里恰好没有这一天就会把整天重新生成一遍
                if store.load_day_cache(target) is None:
                    _logger.info("[批次] 库里没有%s，冷启动补跑", label)
                    await self._run_batch_guarded(target, f"冷启动补跑（{label}）")

            while True:
                await asyncio.sleep(60)
                now = now_jst()
                run_at = await self._run_at()
                if now.time() < run_at:
                    continue
                # run_at 默认 12:00，那个点逻辑日和日历日本来就重合；
                # 仍然走 _today() 是为了 run_at 万一被改到凌晨也不会错日子
                today = self._today()
                if self._last_batch_day == today:
                    continue
                self._last_batch_day = today
                target = today + timedelta(days=1)
                # 冷启动可能已经把明天补出来了（重启发生在 run_at 之后就会这样），
                # 不查一下会白跑一整轮。同样走 load_day_cache——内存里没有不等于库里没有
                if store.load_day_cache(target) is not None:
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
        """跑一天的批次：一次调用出全天，不合格的段铺底稿，最后报告。

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
            # 重生成某个已有日期时，第三轮如果临时失败，要保住上一版表达方式。
            # 新日期则退回骨架底稿，不让 replyer 收到空注入。
            previous_manners = {
                slot: state.manner for slot, state in day_cache.segments.items() if state.manner
            }
            # 凌晨那段承接的是前一天最后一段
            previous = self._previous_state(day, segments[0]) if segments else None
            levels = {segment.slot: negative.level_of(day, segment.slot) for segment in segments}

            outcome = await generator.generate_day(
                day=day,
                segments=segments,
                weather=weather,
                holiday=holiday,
                previous=previous,
                negative_levels=levels,
            )

            failures: list[tuple[Segment, str]] = []
            aborted = ""
            by_slot = {segment.slot: segment for segment in segments}
            stamp = now_jst().isoformat()
            # 功能关掉时**连底稿的 manner 都不铺**：不然新生成的日子库里照样躺着一句
            # 表达方式，只是没被注入——看起来像还在跑，排查时会误判
            manner_on = await self._manner_enabled()

            def seed_manner(slot: str, segment: Segment) -> str:
                if not manner_on:
                    return ""
                return previous_manners.get(slot) or store.fallback_for(segment).manner

            if not outcome.ok:
                # 整次调用没成功：全天铺底稿，库里留下「今天已经试过」的痕迹，
                # 重启不会再空转一轮。
                aborted = outcome.reason
                for segment in segments:
                    draft = day_cache.segments.setdefault(segment.slot, store.fallback_for(segment))
                    draft.manner = seed_manner(segment.slot, segment)
                    store.reset_shares(day, segment.slot)
            else:
                day_cache.outline = outcome.outline
                for slot, state in outcome.states.items():
                    segment = by_slot[slot]
                    state.manner = seed_manner(slot, segment)
                    state.generated_at = stamp
                    day_cache.segments[slot] = state
                    # 这一段换了新内容，旧 topic 的分享状态必须跟着作废，
                    # 否则 is_shared 会拿"上一版说过了"把新谈资一直摁住。
                    store.reset_shares(day, slot)

                # 不合格的段铺骨架底稿，原样报出来。**不补写**——定向重写已移除，
                # 想让这一段有内容就整天重来（前端「重新生成当日日程」）。
                for slot, defect in outcome.failures:
                    segment = by_slot[slot]
                    _logger.info("[批次] %s %s 不合格：%s", day, slot, defect)
                    draft = store.fallback_for(segment)
                    draft.manner = seed_manner(slot, segment)
                    draft.generated_at = stamp
                    day_cache.segments[slot] = draft
                    store.reset_shares(day, slot)
                    failures.append((segment, defect))

            # 第二轮：全天一次调用，为每段提炼一句谈资（5.10）。
            # 放在主生成之后，因为它要读全天的 story。
            round2, round2_error = {"places": 0, "topics": 0, "total": 0}, ""
            if not aborted:
                round2, round2_error = await generator.extract_round2(
                    day, segments, day_cache.segments
                )

            # 第三轮：全天一次调用，只为说话状态偏离基线的时段写 manner，
            # 其余段留空不注入。整轮失败就全天保留旧值或底稿，不回头改 story / mood。
            round3 = {"expressions": 0, "total": 0}
            round3_failures: list[tuple[Segment, str]] = []
            if not aborted:
                round3, round3_failures = await self._generate_expressions(
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
                "round2": round2,
                "round2_error": round2_error,
                "round3": round3,
                "round3_failures": round3_failures,
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
        if summary.get("round2_error"):
            lines.append(f"第二轮抽取失败：{summary['round2_error']}")
        elif not summary.get("aborted"):
            # 分开报数：两个字段是分开容错的，一个失败不牵连另一个（设计文档 5.10）
            round2 = summary.get("round2") or {}
            lines.append(f"地点：{round2.get('places', 0)}/{round2.get('total', 0)} 段")
            lines.append(f"可说的话题：{round2.get('topics', 0)} 条")
            round3 = summary.get("round3") or {}
            if not round3.get("total"):
                lines.append("表达方式：功能已关闭，未生成")
            else:
                lines.append(
                    f"表达方式：偏离 {round3.get('expressions', 0)}/{round3.get('total', 0)} 段"
                )
            for segment, reason in summary.get("round3_failures") or []:
                lines.append(f"表达方式保留旧值：{segment.slot}　{reason}")
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
        # 键在 [observability] 段下。放那儿是有点怪（它服务的是生成，不是观测），
        # 但两份 config 都这么写，代码早先却按顶层键读——取不到，一直用默认值 Tokyo，
        # 而配的正好也是 Tokyo，所以谁都没发现。以配置的位置为准。
        location = str(await self._get_config("observability.weather_location", "Tokyo"))
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
    def _require_wardrobe(self) -> Wardrobe:
        if self._wardrobe is None:
            raise RuntimeError("衣柜尚未加载")
        return self._wardrobe

    def _warn_unknown_outfits(self) -> None:
        """骨架里用到、衣柜里没有的套装名，加载时报一次。

        不抛异常：拍摄现场那身本来就不在衣柜里，而一个手滑打错的名字
        也不该把整个日程功能带下水——工具那边会照实回答，日程照常跑。
        """
        store, wardrobe = self._require_store(), self._require_wardrobe()
        unknown = {
            segment.outfit
            for segment in store.all_segments()
            if segment.outfit not in wardrobe.names
        }
        if unknown:
            _logger.warning(
                "[衣柜] 骨架里有 %d 个名字不在衣柜里，工具会按「当天临时定的」回答：%s",
                len(unknown), "、".join(sorted(unknown)),
            )

    def _today(self) -> date:
        """此刻所属的**逻辑日**（00:00~02:00 算前一天）。

        凡是「今天的日程」都要用它，不能用 ``now_jst().date()``——
        否则午夜到 02:00 之间，冷启动会去补一个还没到的日子，
        `/status day` 会显示错的一天。
        """
        return self._require_store().resolve_moment(now_jst())[0]

    def _current(self) -> tuple[datetime, date, Segment, SegmentState]:
        """取此刻的 (真实时刻, **逻辑日**, 时段, 状态)。

        逻辑日不等于 ``moment.date()``——00:00~02:00 属于前一天（见
        ``ScheduleStore.resolve_moment``）。凡是拿它去查日程、标记谈资、
        写库的地方都必须用这个逻辑日，否则每天午夜到 02:00 之间会错一整天。
        """
        moment = now_jst()
        store = self._require_store()
        day, _minutes = store.resolve_moment(moment)
        segment, state = store.state_at(moment)
        return moment, day, segment, state

    def _planner_block(
        self,
        moment: datetime,
        segment: Segment,
        state: SegmentState,
    ) -> str:
        """planner 注入文本（设计文档 3.1）。

        `所在` 直接给结论，不让 planner 自己拿当前时间去时段轴上比对——
        它是导演，不该干查表的活，查错了还没人知道。
        行程整条也给，是为了让它看得出「刚到家」还是「马上要出门」。

        行程排成一行不排表格：这是常驻注入，每轮都在，而 `所在` 已经把结论给了，
        行程只用来提供前后脉络，不值得为排版多占四行。

        **这一块打破了「一个活动名词都不能有」的纪律，是有意为之。**
        地点是最顺口的可复述内容，而实测 planner 会把这个块近乎整块转发给 replyer
        （5.14）。之所以仍然这么做：导演需要在决定怎么演之前就知道人在哪，
        而 Tool 是「该聊她在干嘛」时才调用的，那时候给已经晚了。观察项见设计文档 7。

        **谈资不在这里了。** 早先这里有一段「她手上有一件今天发生、还没跟人说过的
        小事」，由 planner 决定要不要换题。那个设计被推翻：只要那句话在，planner 就
        倾向于用它，等于每轮下一道命令；而它又拿不到内容，只能写出「我碰上件挺巧的
        小事，等我忙完再讲」这种预告（08-31 15:25 实际发生过）。现在谈资走
        ``get_mittes_topic`` 工具——代码按窗口决定露不露，planner 自己决定调不调。
        """
        store = self._require_store()
        trail = _render_trail(state)
        # 顺序是从外到内：一天的走向 → 此刻在哪 → 还剩多少体力 → 什么心情。
        # 越靠后越贴近「她现在是什么样的人」，也越贴近 planner 要做的那个决定。
        lines = ["【Mittes 此刻】"]
        if trail:
            lines.append(f"行程表：{trail}")
        lines.append(f"所在：{store.place_at(moment, segment, state)}")
        if state.physical_state:
            lines.append(f"体力：{state.physical_state}")
        lines.append(f"心情：{state.mood}")
        lines.append("可用get_mittes_schedule查询详情")
        return "\n".join(lines)

    @staticmethod
    def _topic_block_reply(topic: str) -> str:
        """通道一：接话（常驻注入，不设闸门）。

        真人在群里主动讲自己今天干了什么是极少数，绝大多数"分享"其实是**接话**——
        话题正好赶到那儿了。接话不需要许可，所以这一版常驻。

        但「换题」那个出口整个拿掉了：08-31 那五次刺耳的抛出全是换题，没有一次是接话。
        """
        return (
            f"你今天遇到的一件小事：{topic}\n"
            "它能直接回答对方、或者能给你正在说的话当例子时，才提，而且只取用得上的那一点。\n"
            "别为了说它去转话题；聊的不是这个，就当没有这回事。"
        )

    @staticmethod
    def _topic_block_pitch(topic: str) -> str:
        """通道二：开口（planner 调过 get_mittes_topic 才注入）。

        **不再写「整条回复只说这件事」。** 08-31 那五次一次都没执行——replyer 手上有
        明确的目标消息和「请自然地回复」的收尾指令，这条约束要求它放弃本职任务，
        必然落败；实际表现是"答一条、再贴一条"，中间垫一个胶水词。

        所以改成管接缝：禁掉那四个词，并把形态从"叙述"压向"带情绪的半句"——
        「早上那组黄得我难受，重修了一遍才顺眼」像憋不住说一句，
        「我今天把早上那组照片重新裁了发到自己号上」像轮到我发言了。
        """
        return (
            f"你今天遇到的一件小事：{topic}\n"
            "这会儿刚好轮得到你说自己的事。从最想吐槽或最想分享的那一点开口，"
            "一句话就够，不从头讲经过，也不说自己为什么想起它。\n"
            "不要用「话说」「对了」「先不说X」「那个先不管」把它跟上一句缝起来。"
        )

    # ── Tool ──
    @Tool(
        "get_mittes_schedule",
        visibility="visible",
        brief_description="查询 Mittes 现在在做什么。请在缺少日程信息就无法准确回答当前问题时使用",
        parameters=[
            ToolParameterInfo(
                name="time",
                param_type=ToolParamType.STRING,
                description=(
                    "要查的时刻，留空表示现在。写法："
                    "「HH:MM」查今天的某一刻；「YYYY-MM-DD HH:MM」查指定某天；"
                    "「YYYY-MM-DD」只给日期时按中午算。"
                ),
                required=False,
            ),
        ],
    )
    async def tool_get_schedule(self, time: str = "", **kwargs: Any) -> dict[str, str]:
        del kwargs
        return {"name": "get_mittes_schedule", "content": self._render_schedule_at(time)}

    def _render_schedule_at(self, raw_time: str) -> str:
        """渲染某一时刻的日程与故事（不填时刻就是此刻）。

        **未来的时刻只给安排，不给 story。** 次日的日程提前一天就生成好了，
        但那段 story 写的是「发生了什么」——事情还没发生，照着念等于让她
        预言自己的一天。安排（几点在哪、做什么）是计划，可以说。
        """
        store = self._require_store()
        now = now_jst()
        moment = now
        if raw_time.strip():
            parsed = parse_moment(raw_time)
            if parsed is None:
                return (
                    f"看不懂「{raw_time.strip()}」这个时间。"
                    "请用「HH:MM」或「YYYY-MM-DD HH:MM」，比如 19:30、2026-08-19 19:30。"
                )
            moment = parsed

        day, _minutes = store.resolve_moment(moment)
        try:
            segment = store.segment_at(moment)
        except LookupError:
            return f"{moment:%Y-%m-%d %H:%M} 不在骨架覆盖的范围里，查不到。"

        state = store.state_of(day, segment)
        head = (
            f"【Mittes 的日程】{day:%Y-%m-%d} 周{weekday_name(day)}　"
            f"{'此刻 ' if moment is now else ''}{moment:%H:%M}\n"
            f"{segment.slot}　{segment.title}\n"
            f"地点：{segment.place}　穿着：{segment.outfit}　同处：{segment.company}"
        )

        if moment > now:
            return (
                f"{head}\n\n"
                "这是**还没到的时间**，只有安排、没有经过——别把它当成已经发生的事来讲。"
            )
        if state is None or not state.story:
            return f"{head}\n\n这一段的细节没有记录，只有上面的安排。"
        return (
            f"{head}\n\n{state.story}\n\n"
            "以上是她真实经历过的，可以据此回答；不要复述原文，也不要在没人问的时候主动提起。"
        )

    @Tool(
        "get_mittes_outfit",
        brief_description=(
            "当需要描述 Mittes 穿什么时，必须调用此工具获取真实穿搭，不得推测。"
            "从头到脚都有；不填 time 就是此刻。"
        ),
        parameters=[
            ToolParameterInfo(
                name="time",
                param_type=ToolParamType.STRING,
                description=(
                    "要查的时刻，留空表示现在。写法同日程工具："
                    "「HH:MM」或「YYYY-MM-DD HH:MM」。她一天里会换好几次衣服，"
                    "问的是别的时段就要把时刻填上。"
                ),
                required=False,
            ),
        ],
    )
    async def tool_get_outfit(self, time: str = "", **kwargs: Any) -> dict[str, str]:
        del kwargs
        return {"name": "get_mittes_outfit", "content": self._render_outfit_at(time)}

    def _render_outfit_at(self, raw_time: str) -> str:
        """渲染某一时刻的穿搭。

        穿搭来自骨架的 ``outfit``（一个套装名），细节来自 ``character/wardrobe.toml``。
        名字查不到不算错误——周五拍摄现场那身是当天临时定的，衣柜里本来就没有。
        """
        store = self._require_store()
        wardrobe = self._require_wardrobe()
        now = now_jst()
        moment = now
        if raw_time.strip():
            parsed = parse_moment(raw_time)
            if parsed is None:
                return (
                    f"看不懂「{raw_time.strip()}」这个时间。"
                    "请用「HH:MM」或「YYYY-MM-DD HH:MM」，比如 19:30、2026-08-19 19:30。"
                )
            moment = parsed

        day, _minutes = store.resolve_moment(moment)
        try:
            segment = store.segment_at(moment)
        except LookupError:
            return f"{moment:%Y-%m-%d %H:%M} 不在骨架覆盖的范围里，查不到。"

        return (
            f"【Mittes 的穿搭】{day:%Y-%m-%d} 周{weekday_name(day)}　"
            f"{'此刻 ' if moment is now else ''}{moment:%H:%M}　"
            f"（{segment.slot}　{segment.title}）\n\n"
            f"{wardrobe.render(segment.outfit)}\n\n"
            "问到哪儿说哪儿，别把整份清单报一遍——没有人会那样描述自己的衣服。"
        )

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

    # ── Tool：谈资 ──
    @Tool(
        _TOPIC_TOOL_NAME,
        visibility="visible",
        brief_description=(
            "取一件她今天真实经历、可以说给人听的小事。"
            "当群里在闲聊、没有正在进行的话题需要接、而且她刚好可以说说自己的事时调用；"
            "她在忙，或者话题不是她能插的，就不要调用。"
            "（问她「现在在干嘛」用 get_mittes_schedule，那是查日程；"
            "这个是找一句她可以主动说的话头。）"
        ),
        parameters=[],
    )
    async def tool_get_topic(self, **kwargs: Any) -> dict[str, str]:
        """把本时段那条谈资交给 planner，并记下"本轮取过材"。

        **这里不重复判窗口。** 工具能被调用，说明 planner hook 已经放它进 schema 了；
        两处各判一次只会制造不一致。

        会话 id 从 ``stream_id`` / ``chat_id`` 取——主程序给插件工具的载荷里两个都有
        （``component_query.py:_build_tool_context_payload``），值就是 session_id。
        """
        session_id = str(kwargs.get("stream_id") or kwargs.get("chat_id") or "")
        moment, day, segment, state = self._current()
        if not state.topic:
            return {
                "name": _TOPIC_TOOL_NAME,
                "content": "她这会儿没有什么特别想说的事，别硬找话头。",
            }
        if session_id:
            self._topic_pitched[session_id] = (day, segment.slot)
            store = self._require_store()
            store.mark_pitched(day, segment.slot, session_id)
            # **取材即视为用掉。** 以前靠 topic_keys 在回复正文里找关键词判断"说没说
            # 出口"，那套已经取消——它抽的是 story 的书面词，而她开口是口语，
            # 实测 08-31 那条谈资 12 次注入一次都没命中，同一件事在同一个群说了三遍。
            # 现在的判据是"planner 取过材"这个确定事实：代价是取了没说也算用掉，
            # 少一次开口机会；收益是绝不会重复说，而两种错的代价是不对称的。
            store.mark_shared(
                day,
                segment.slot,
                session_id,
                moment.isoformat(),
                hit_key="工具调用",
            )
            _logger.info(
                "[谈资] 取材 %s %s 会话=%s，这条谈资就此用掉",
                day,
                segment.slot,
                session_id,
            )
        return {"name": _TOPIC_TOOL_NAME, "content": self._topic_tool_text(state.topic)}

    @staticmethod
    def _topic_tool_text(topic: str) -> str:
        """工具返回给 planner 的正文。

        末尾那句照 ``get_mittes_schedule`` 的写法。**「你不用复述」是关键**：
        planner 一旦把 topic 压缩成一句概括写进 reply_reference，story 的质感就没了，
        还会跟 replyer 手上的原文打架。
        """
        return (
            "【今天可以说的一件小事】\n"
            f"{topic}\n\n"
            "她只在场子松、或者话正好赶到这儿的时候才会提起这件事。要用就在 "
            "reply_reference 里写明这轮让她说这件事，并写上你判断合适的理由；"
            "正文她那边有，你不用复述。"
        )

    # ── 谈资闸门 ──
    async def _topic_gate(self, session_id: str) -> tuple[bool, str]:
        """这一轮谈资工具露不露，返回 (露不露, 给人看的原因)。

        五个条件全过才露：有 topic、没说出口过、取材次数没满、在窗口期内、
        对方没有正在回应她。原因字符串只给 ``/status`` 和日志用。
        """
        if not session_id:
            return False, "没有会话 id"
        if not bool(await self._get_config("topic.enabled", True)):
            return False, "谈资总开关关闭"
        if not bool(await self._get_config("topic.pitch_channel.enabled", True)):
            return False, "开口通道关闭"

        store = self._require_store()
        moment, day, segment, state = self._current()
        if not state.topic:
            return False, "这段没有 topic"
        if await self._share_seen(day, segment.slot, session_id):
            return False, "已经说出口过"

        max_pitches = int(await self._get_config("topic.pitch_channel.max_pitches", 3))
        pitched = store.pitch_count(day, segment.slot, session_id)
        if pitched >= max_pitches:
            return False, f"取材次数已满（{pitched}/{max_pitches}）"

        return await self._topic_window(moment, day, segment, session_id)

    async def _topic_window(
        self,
        moment: datetime,
        day: date,
        segment: Segment,
        session_id: str,
    ) -> tuple[bool, str]:
        """窗口判定：新鲜期 / 断点期，外加「对方正在回应她」这一条否决。

        两个窗口对应真人主动开口的两种许可（谈资设计文档 2.1）：**事情刚发生**，
        以及**她刚从一段不能说话的时间里出来**。真人不会在事情过去两小时、
        自己已经在群里说了半天话之后，突然插一句"我今天把照片重新裁了"——
        08-31 那五次刺耳的抛出全部落在时段中段，就是这个原因。

        否决那一条（C）读的是引用关系：对方用回复功能引了她的消息，说明球还在
        她这边，这一轮不许换题。实测一周里这种消息只占别人发言的 1~2%，
        所以它是个很窄的守卫，不是普遍封锁。**不包含 @ 她**——引用是对方接住了
        她那条话，@ 是对方开了个新话头，后者归 planner 本职管。
        """
        store = self._require_store()
        fresh_minutes = int(await self._get_config("topic.pitch_channel.fresh_minutes", 30))
        break_minutes = int(await self._get_config("topic.pitch_channel.breakpoint_minutes", 60))
        busy_kinds = set(await self._get_config("topic.pitch_channel.busy_kinds", []) or [])
        idle_kinds = set(await self._get_config("topic.pitch_channel.idle_kinds", []) or [])
        lookback = int(await self._get_config("topic.pitch_channel.lookback_minutes", 10))

        # 逻辑日的分钟数，不能拿 datetime 直减：跨零点那段写成 24:00-26:00
        _day, now_minutes = store.resolve_moment(moment)
        elapsed = now_minutes - segment.start_minutes

        fresh = 0 <= elapsed < fresh_minutes
        _previous_day, previous = store.previous_segment(day, segment)
        breakpoint_shape = (
            0 <= elapsed < break_minutes
            and previous.kind in busy_kinds
            and segment.kind in idle_kinds
        )
        if not fresh and not breakpoint_shape:
            return False, f"不在窗口（本段已过 {elapsed} 分钟）"

        segment_start = datetime.combine(day, dt_time(0, 0), tzinfo=JST) + timedelta(
            minutes=segment.start_minutes
        )
        # 往回至少看一小时：时段刚开始时 segment_start 就是此刻，那样一条消息都取不到，
        # C 会永远判不成立；而且被引用的那条可能是她一小时前发的，窗口太窄就认不出来
        since = min(segment_start, moment - timedelta(minutes=max(lookback, 60)))
        mine, others = await self._recent_messages(session_id, since=since, moment=moment)

        my_ids = {str(message.get("message_id")) for message in mine}
        trigger = others[-1] if others else None
        if trigger is not None and str(trigger.get("reply_to") or "") in my_ids:
            trigger_at = _message_moment(trigger)
            if trigger_at is None or moment - trigger_at <= timedelta(minutes=lookback):
                return False, "对方正在回应她（引用了她的消息）"

        if fresh:
            return True, f"新鲜期（本段第 {elapsed} 分钟）"

        # 断点期还要求她在本段里还没开过口——"回来的第一句"只有一次
        spoke = [
            message
            for message in mine
            if (_message_moment(message) or moment) >= segment_start
        ]
        if spoke:
            return False, "断点期已经用掉（本段她说过话了）"
        return True, f"断点期（上一段是{previous.kind}，已过 {elapsed} 分钟）"

    async def _recent_messages(
        self,
        session_id: str,
        *,
        since: datetime,
        moment: datetime,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """取最近的消息，拆成 (她自己的, 别人的)，各自按时间升序。

        **拉两次是为了不用去查 bot 的账号。** ``filter_mai=True`` 那一次由主程序按
        ``bot_platform_accounts`` 把她自己排除掉，两份做差就是她发的。插件自己判断
        "哪条是她发的"要么得读主程序的表、要么得猜昵称，都比多一次本地查询脏。
        窗口本来就稀有，这两次查询一天也跑不了几回。
        """
        start_time = since.timestamp()
        # 终点往后放一分钟：触发消息的入库时刻和 now_jst() 之间可能有零点几秒的差
        end_time = (moment + timedelta(minutes=1)).timestamp()
        every = await self.ctx.message.get_by_time_in_chat(
            chat_id=session_id,
            start_time=start_time,
            end_time=end_time,
            limit=80,
            limit_mode="latest",
            filter_mai=False,
        )
        others = await self.ctx.message.get_by_time_in_chat(
            chat_id=session_id,
            start_time=start_time,
            end_time=end_time,
            limit=80,
            limit_mode="latest",
            filter_mai=True,
        )
        every = [item for item in (every or []) if isinstance(item, dict)]
        others = [item for item in (others or []) if isinstance(item, dict)]
        other_ids = {str(item.get("message_id")) for item in others}
        mine = [item for item in every if str(item.get("message_id")) not in other_ids]
        key = lambda item: _message_moment(item) or datetime.min.replace(tzinfo=JST)  # noqa: E731
        return sorted(mine, key=key), sorted(others, key=key)

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
        """插入所在与心情，并决定这一轮谈资工具露不露。

        两件事互不牵连：hook 的 ``error_policy`` 是 skip，整块跳过——谈资闸门
        算错了不能把「所在/心情/行程表」一起带走，所以那段整个包在 try 里，
        且异常方向一律是"不露"。
        """
        session_id = str(kwargs.get("session_id") or "")
        # 新一轮开始，先把上一轮的取材令牌清掉。清 → 写 → 读这个顺序让令牌的
        # 语义严格等于"本轮"，不需要 TTL 去猜。
        if session_id:
            self._topic_pitched.pop(session_id, None)

        extra: dict[str, Any] = {}
        tool_definitions = kwargs.get("tool_definitions")
        if isinstance(tool_definitions, list):
            try:
                visible, reason = await self._topic_gate(session_id)
            except Exception:
                _logger.warning("[谈资] 闸门判定失败，本轮不暴露工具", exc_info=True)
                visible, reason = False, "判定异常"
            if not visible:
                # 摘而不是塞：工具始终是注册的，只是某些轮次不出现在 schema 里，
                # 这样 /status、调用链路、权限判定都不用特判。
                extra["tool_definitions"] = [
                    item
                    for item in tool_definitions
                    if _tool_name_of(item) != _TOPIC_TOOL_NAME
                ]
                _logger.debug("[谈资] 本轮不暴露工具：%s", reason)

        if not items:
            return _hook_response(items, kwargs, extra=extra or None)

        moment, _day, segment, state = self._current()
        block = self._planner_block(moment, segment, state)

        index = _find_item_index(items, lambda text: text.startswith(_PLANNER_ANCHOR_PREFIX))
        updated = list(items)
        # 找不到锚点就挂在最后：状态层晚一点出现也比不出现好
        updated.insert(index + 1 if index >= 0 else len(updated), _new_user_item(block))
        return _hook_response(updated, kwargs, extra=extra or None)

    # ── Hook：replyer 表达方式注入 ──
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
        """把 A（表达方式）和 C（谈资）放到各自合适的位置。

        A 是本轮的表达要求，仍紧贴 planner 的 reply_reference。C 只是
        可能用得上的背景经历，放在 system item 之后、聊天记录之前；
        后面的真实对话和目标消息会重新取得注意力，避免 C 和
        reply_reference 挨在一起时被误读成必须执行的任务。

        锚点从 hook 载荷算出来，不是猜位置：reference item 的正文由主程序按固定规则
        拼装（maisaka_generator_base.py:574-594）——``reply_tool_args["reply_reference"]``
        非空时正文就是它原样，否则是 ``当前思考：\\n{reply_reason}``。

        用「包含」而不是「相等」来匹配，是因为 02_owner_auth_plugin 可能已经往
        reference 首行合并过身份文案，两个插件的 hook 顺序不保证。

        另外还会按此刻的心情 × 体力档位整段替换 system prompt 里的 reply_style，
        见 ``reply_style.py``。那一步跟 A、C 都无关，所以这一轮既没有表达方式也没有
        谈资时也照做。
        """
        if not items:
            return {"success": True, "action": "continue"}

        moment, day, segment, state = self._current()
        store = self._require_store()
        updated = list(items)

        # 语气：整段换掉主程序配置里的 reply_style。两道门都得过——功能开关开着，
        # 且这一段有完整档位（底稿段没有，见 reply_style.compose）。任一不过就不碰
        # system item，主程序配置里那份原样生效。
        style = (
            compose_reply_style(state.mood_level, state.energy_level)
            if await self._reply_style_enabled()
            else ""
        )
        style_applied = False
        if style:
            system_index = _find_item_type_index(updated, "SystemMessageItem")
            rewritten = (
                _replace_reply_style(updated[system_index], style) if system_index >= 0 else None
            )
            if rewritten is None:
                _logger.warning("[replyer] system item 里没找到 reply_style 锚点，本轮沿用配置里那份")
            else:
                updated[system_index] = rewritten
                style_applied = True

        # A：表达方式。C：今天那件可说的小事，两条通道二选一——planner 本轮调过
        # get_mittes_topic 就走"开口版"，否则走"接话版"。
        manner = state.manner.strip() if await self._manner_enabled() else ""
        topic = ""
        pitched = self._topic_pitched.get(session_id)
        is_pitch = bool(pitched and pitched == (day, segment.slot))
        stop_after_shared = bool(await self._get_config("topic.stop_after_shared", True))
        already_shared = await self._share_seen(day, segment.slot, session_id)
        topic_enabled = bool(await self._get_config("topic.enabled", True))
        reply_channel = bool(await self._get_config("topic.reply_channel.enabled", True))
        if (
            state.topic
            and session_id
            and topic_enabled
            and (not stop_after_shared or not already_shared)
            and (is_pitch or reply_channel)
        ):
            topic = (
                self._topic_block_pitch(state.topic)
                if is_pitch
                else self._topic_block_reply(state.topic)
            )
        if not manner and not topic:
            if not style_applied:
                return {"success": True, "action": "continue"}
            return _hook_response(
                updated,
                kwargs,
                extra={
                    "reply_reason": reply_reason,
                    "reply_tool_args": reply_tool_args,
                    "session_id": session_id,
                },
            )

        # 接话版是低优先级背景：紧跟 system，但放在所有聊天记录之前，
        # 后面的真实对话会重新取得注意力。不塞进 system 正文，那样权重反而更高。
        if topic and not is_pitch:
            system_index = _find_item_type_index(updated, "SystemMessageItem")
            updated.insert(system_index + 1 if system_index >= 0 else 0, _new_user_item(topic))
            store.mark_injected(day, segment.slot, session_id)

        # A 仍是本轮要求：放在 reply_reference 之前。
        reference = str((reply_tool_args or {}).get("reply_reference") or "").strip()
        expected = reference or (f"当前思考：\n{reply_reason}".strip() if reply_reason else "")

        # 开口版跟 A 一起放在这儿：planner 已经在 reference 里安排了这件事，
        # 它是本轮的既定素材而不是背景材料，再压权重只会造成"指令在、材料弱"的割裂。
        if manner or (topic and is_pitch):
            index = -1
            if expected:
                index = _find_item_index(updated, lambda text: expected in text)
            if index < 0:
                index = _find_item_index(updated, lambda text: text.startswith(_REPLYER_FALLBACK_PREFIX))
            if index < 0:
                _logger.warning("[replyer] 两个锚点都没匹配上，本次跳过表达方式注入")
                # planner 已经安排了要说这件事，材料不能跟着丢，退回低位注入
                if topic and is_pitch:
                    system_index = _find_item_type_index(updated, "SystemMessageItem")
                    updated.insert(
                        system_index + 1 if system_index >= 0 else 0, _new_user_item(topic)
                    )
                    store.mark_injected(day, segment.slot, session_id)
            else:
                if topic and is_pitch:
                    updated.insert(index, _new_user_item(topic))
                    store.mark_injected(day, segment.slot, session_id)
                    index += 1
                if manner:
                    updated.insert(index, _new_user_item(manner))

        return _hook_response(
            updated,
            kwargs,
            extra={
                "reply_reason": reply_reason,
                "reply_tool_args": reply_tool_args,
                "session_id": session_id,
            },
        )

    # ── Command ──
    @Command(
        "status",
        description="查看 Mittes 当前时段状态（仅管理员）",
        pattern=r"^/status$",
        permission="operator",
    )
    async def cmd_status(self, stream_id: str = "", **kwargs: Any) -> tuple[bool, str, bool]:
        del kwargs
        moment, day, segment, state = self._current()
        store = self._require_store()
        run_at = await self._run_at()
        trail = _render_trail(state) or "（没有时段轴，退回骨架地点）"
        # 功能关掉时照样把拼出来的那串显示出来，但要标明没生效——
        # 光显示文字会让人以为 replyer 收到的就是它。
        style_off = "" if await self._reply_style_enabled() else "　← 功能已关，未生效"
        lines = [
            f"{day:%Y-%m-%d} 周{weekday_name(day)} {moment:%H:%M}（JST）　"
            f"{'（跨零点，仍算前一天）' if day != moment.date() else ''}",
            f"当前时段：{segment.slot}　{segment.title}",
            f"　骨架地点：{segment.place}　服装：{segment.outfit}",
            f"　同处：{segment.company}　性质：{segment.kind}",
            "",
            f"所在：{store.place_at(moment, segment, state)}",
            f"行程：{trail}",
            "",
            f"story：{state.story}",
            f"表达方式：{state.manner}",
            f"mood：{state.mood}",
            f"心情分档：{state.mood_level or '（旧记录未生成）'}",
            f"体力：{state.physical_state or '（旧记录未生成）'}",
            f"体力分档：{state.energy_level or '（旧记录未生成）'}",
            f"reply_style：{compose_reply_style(state.mood_level, state.energy_level) or '（档位不全，沿用配置里那份）'}{style_off}",
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
        moment, _day, segment, state = self._current()
        visible, reason = await self._topic_gate(stream_id)
        empty = "（这段没什么好说的，不注入）"
        style_note = "" if await self._reply_style_enabled() else "【功能已关，实际不替换】"
        text = (
            f"── replyer reply_style（整段替换 system prompt 里那一行）{style_note} ──\n"
            f"{compose_reply_style(state.mood_level, state.energy_level) or '（档位不全，沿用配置里那份）'}\n"
            "\n"
            "── planner 注入（插在「时间：」之后）──\n"
            f"{self._planner_block(moment, segment, state)}\n"
            "\n"
            f"── 谈资工具 {_TOPIC_TOOL_NAME}：{'露出' if visible else '不露'}（{reason}）──\n"
            + (self._topic_tool_text(state.topic) if state.topic else "（这段没有 topic）")
            + "\n\n"
            "── replyer A 表达方式（插在 reply_reference 之前）──\n"
            + (state.manner if await self._manner_enabled() else "（表达方式功能已关闭，不注入）")
            + "\n"
            "\n"
            "── replyer C 谈资 · 接话版（system 之后、聊天记录之前）──\n"
            + (self._topic_block_reply(state.topic) if state.topic else empty)
            + "\n\n"
            "── replyer C 谈资 · 开口版（调过工具的那一轮，放在 A 之前）──\n"
            + (self._topic_block_pitch(state.topic) if state.topic else empty)
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
        today, _minutes = store.resolve_moment(moment)
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
        "status_topic",
        description="查看当前时段的话题与分享状态（仅管理员）",
        pattern=r"^/status\s+topic$",
        permission="operator",
    )
    async def cmd_status_topic(self, stream_id: str = "", **kwargs: Any) -> tuple[bool, str, bool]:
        del kwargs
        store = self._require_store()
        moment, day, segment, state = self._current()
        today = day.isoformat()

        lines = [f"【谈资】{segment.slot}　{segment.title}"]
        if not state.topic:
            lines.append("这段没什么好说的，不注入。")
            await self.ctx.send.text("\n".join(lines), stream_id)
            return True, "已输出谈资状态", True

        lines.append(f"话题：{state.topic}")
        stop_after_shared = bool(await self._get_config("topic.stop_after_shared", True))
        lines.append(f"说出口后停止注入：{'是' if stop_after_shared else '否'}")
        # 关联组解析失败是静默的（群还没被 bot 见过就查不到聊天流），这里让它看得见
        linked = await self._linked_sessions(stream_id)
        lines.append(
            f"关联会话：{len(linked)} 个，组内任一说过即算说过"
            if len(linked) > 1
            else "关联会话：无（没配，或群号还没解析到聊天流）"
        )
        seen = await self._share_seen(day, segment.slot, stream_id)
        lines.append(f"本会话算不算已说过：{'算' if seen else '不算'}")
        max_pitches = int(await self._get_config("topic.pitch_channel.max_pitches", 3))
        lines.append(
            f"取材次数：{store.pitch_count(day, segment.slot, stream_id)}/{max_pitches}"
        )
        visible, reason = await self._topic_gate(stream_id)
        lines.append(f"谈资工具：{'露出' if visible else '不露'}（{reason}）")
        lines.append("")
        rows = [r for r in store.db.shares_of_day(today) if r["slot"] == segment.slot]
        if not rows:
            lines.append("还没在任何会话里注入过。")
        for row in rows:
            mark = f"已说出口 {row['shared_at'][11:16]}" if row["shared_at"] else "还没说"
            lines.append(
                f"- {row['session_id']}　注入 {row['injected']} 次　"
                f"取材 {row.get('pitched', 0)} 次　{mark}"
            )
        await self.ctx.send.text("\n".join(lines), stream_id)
        return True, "已输出谈资状态", True

    @Command(
        "status_topics",
        description="重跑某天的第二轮抽取：地点时段轴与话题（仅管理员）",
        pattern=r"^/status\s+topics(?:\s+(?P<day>\d{4}-\d{2}-\d{2}))?$",
        permission="operator",
    )
    async def cmd_status_topics(
        self, stream_id: str = "", **kwargs: Any
    ) -> tuple[bool, str, bool]:
        day = self._group(kwargs, "day")
        target = date.fromisoformat(day) if day else self._today()
        await self.ctx.send.text(f"开始重跑 {target.isoformat()} 的地点与话题……", stream_id)
        self._spawn(self._topics_text(target), stream_id)
        return True, "第二轮抽取已在后台开始", True

    async def _topics_text(self, day: date, *, topics_only: bool = False) -> str:
        """重跑某天的第二轮抽取；前端可只采用其中的 topic 结果。"""
        async with self._batch_lock:
            store = self._require_store()
            generator = self._require_generator()
            cached = store.load_day_cache(day)
            if cached is None:
                return f"{day.isoformat()} 还没有日程，先生成这一天的日程。"

            segments = store.segments_of(day)
            previous = {
                slot: (list(state.places), state.topic)
                for slot, state in cached.segments.items()
            }
            # 重新抽取必须允许模型把一段判成“没有 topic”。不先清空的话，空结果
            # 会被旧 topic 顶住，看起来像按钮没有生效。
            for state in cached.segments.values():
                state.topic = ""
                state.topic_keys = []
                if not topics_only:
                    state.places = []
            round2, error = await generator.extract_round2(day, segments, cached.segments)
            if error:
                for slot, values in previous.items():
                    if state := cached.segments.get(slot):
                        state.places, state.topic = values
                return f"第二轮抽取失败：{error}"
            if topics_only:
                for slot, values in previous.items():
                    if state := cached.segments.get(slot):
                        state.places = values[0]
            # topic 变了，绑在旧 topic 上的分享状态同样作废
            for segment in segments:
                store.reset_shares(day, segment.slot)
            store.flush(
                day,
                model=str(await self._get_config("generation.model", "replyer")),
                negative_level_of=lambda slot: self._require_negative().level_of(day, slot),
                batch_reason="topic 重跑" if topics_only else "第二轮重跑",
                batch_at=now_jst().isoformat(),
            )

        lines = [
            (
                f"【topic】{day.isoformat()}　话题 {round2['topics']} 条"
                if topics_only
                else f"【第二轮】{day.isoformat()}　"
                f"地点 {round2['places']}/{round2['total']} 段　话题 {round2['topics']} 条"
            )
        ]
        for segment in segments:
            state = cached.segments.get(segment.slot)
            if state is None:
                continue
            trail = _render_trail(state)
            if trail:
                lines.append(f"{segment.slot}　{trail}")
            if state.topic:
                lines.append(f"　　话题：{state.topic}")
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
        path = self.ctx.paths.data_dir / "schedule.db"
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
        await self.ctx.send.text(self._render_week(self._today()), stream_id)
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
        today = self._today()
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
        today = self._today()
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
        today = self._today()
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

    async def _generate_expressions(
        self,
        day: date,
        segments: list[Segment],
        states: dict[str, SegmentState],
    ) -> tuple[dict[str, int], list[tuple[Segment, str]]]:
        """全天一次调用生成表达方式。

        **大多数时段的正确结果是空串**（这一段说话状态没偏离基线，不注入），
        所以 ``expressions`` 记的是"写了几段"，不是"成功几段"——两者不再是一回事。
        模型没返回的段保留旧值；返回空串的段清成空。
        """
        candidates = [
            segment
            for segment in segments
            if (state := states.get(segment.slot)) is not None and state.generated
        ]
        stats = {"expressions": 0, "total": len(candidates)}
        failures: list[tuple[Segment, str]] = []
        if not await self._manner_enabled():
            # 功能整体关闭：不调 LLM，也不动库里已有的 manner
            return stats, failures
        generator = self._require_generator()
        manners, reason = await generator.generate_expressions(day, segments, states)
        if reason:
            _logger.error("[第三轮] %s 失败，全天保留旧值：%s", day, reason)
            return stats, [(segment, reason) for segment in candidates[:1]]
        for slot, manner in manners.items():
            state = states.get(slot)
            if state is None:
                continue
            state.manner = manner
            if manner:
                stats["expressions"] += 1
        return stats, failures

    async def _expressions_text(self, day: date) -> str:
        """只重跑某天第三轮表达方式，不改 story、mood、地点或 topic。"""
        async with self._batch_lock:
            store = self._require_store()
            if not await self._manner_enabled():
                return "表达方式功能当前是关闭的（config.toml 的 [manner] enabled = false）。"
            cached = store.load_day_cache(day)
            if cached is None:
                return f"{day.isoformat()} 还没有日程，先生成这一天的日程。"
            segments = store.segments_of(day)
            stats, failures = await self._generate_expressions(day, segments, cached.segments)
            store.flush(
                day,
                model=str(await self._get_config("generation.model", "replyer")),
                negative_level_of=lambda slot: self._require_negative().level_of(day, slot),
                batch_reason="表达方式重跑",
                batch_at=now_jst().isoformat(),
            )

        lines = [
            f"【表达方式】{day.isoformat()}　"
            f"偏离基线 {stats['expressions']}/{stats['total']} 段，其余不注入"
        ]
        lines.extend(f"全天保留旧值：{reason}" for _segment, reason in failures)
        return "\n".join(lines)

    # ── 前端管理任务 ──

    async def _admin_job_loop(self) -> None:
        """领取 viewer 写入 SQLite 的任务，让编辑与生成都在 bot 进程内生效。"""
        await asyncio.sleep(2)
        while True:
            try:
                store = self._require_store()
                job = store.db.claim_admin_job()
                if job is None:
                    await asyncio.sleep(1)
                    continue
                try:
                    result = await self._execute_admin_job(job)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    _logger.exception("[前端任务] #%s 执行失败", job.get("id"))
                    store.db.finish_admin_job(
                        int(job["id"]),
                        error=f"{type(exc).__name__}: {exc}",
                    )
                else:
                    store.db.finish_admin_job(int(job["id"]), result=result)
            except asyncio.CancelledError:
                raise
            except Exception:
                _logger.exception("[前端任务] 守护循环异常，稍后继续")
                await asyncio.sleep(2)

    async def _execute_admin_job(self, job: dict[str, Any]) -> str:
        action = str(job.get("action") or "")
        target = date.fromisoformat(str(job.get("target_date") or ""))
        store = self._require_store()

        if action == "set_expression":
            payload = json.loads(str(job.get("payload") or "{}"))
            manner = str(payload.get("manner") or "").strip()
            slot = str(job.get("slot") or "")
            if not manner:
                raise ValueError("表达方式不能为空")
            if len(manner) > 200:
                raise ValueError("表达方式不能超过 200 字")
            async with self._batch_lock:
                if store.load_day_cache(target) is None:
                    raise ValueError("这一天不存在")
                if not store.update_manner(target, slot, manner):
                    raise ValueError("时段不存在")
            _logger.info("[前端任务] %s %s 表达方式已热更新", target, slot)
            return "表达方式已保存并热加载"

        if action == "generate_day":
            if store.db.has_day(target):
                raise ValueError("这一天已经有日程，请使用重新生成")
            summary = await self.run_batch(target, reason="前端指定日期生成")
            if summary["aborted"]:
                raise RuntimeError(f"日程生成中止：{summary['aborted']}")
            return f"日程生成完成：{summary['ok']}/{summary['total']} 段"

        if action == "regenerate_day":
            if not store.db.has_day(target):
                raise ValueError("这一天还没有日程，请使用生成指定日期日程")
            store.load_day_cache(target)
            summary = await self.run_batch(target, reason="前端重新生成当日日程")
            if summary["aborted"]:
                raise RuntimeError(f"日程生成中止：{summary['aborted']}")
            return f"日程重新生成完成：{summary['ok']}/{summary['total']} 段"

        if action == "regenerate_topics":
            return await self._topics_text(target, topics_only=True)

        if action == "regenerate_expressions":
            return await self._expressions_text(target)

        raise ValueError(f"不支持的管理任务：{action}")

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


def _render_trail(state: SegmentState) -> str:
    """把地点时段轴渲染成一行。空时段轴返回空串。

    注入块、``/status``、``/status topics`` 三处共用，免得三份写法慢慢走样。
    """
    return " / ".join(f"{e['from']}-{e['to']} {e['place']}" for e in state.places)


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


def _find_item_type_index(items: list[Any], item_type: str) -> int:
    """从前往后找第一个指定类型的 Item，返回下标；找不到返回 -1。"""
    for index, item in enumerate(items):
        if isinstance(item, dict) and item.get("item_type") == item_type:
            return index
    return -1


def _replace_reply_style(item: Any, style: str) -> dict[str, Any] | None:
    """把 system item 里 reply_style 那一行换成 ``style``，返回新 item；换不了返回 None。

    只改命中锚点的那个 text part，其余 part 和 meta 原样带过去——item_id 也不换，
    这不是新增的 item，是同一条被改了正文。
    """
    if not isinstance(item, dict):
        return None
    parts = item.get("parts")
    if not isinstance(parts, list):
        return None
    for index, part in enumerate(parts):
        if not isinstance(part, dict) or part.get("type") != "text":
            continue
        text = str(part.get("text") or "")
        head = text.find(_REPLY_STYLE_HEAD)
        if head < 0:
            continue
        head += len(_REPLY_STYLE_HEAD)
        tail = text.find(_REPLY_STYLE_TAIL, head)
        if tail < 0:
            return None
        new_parts = list(parts)
        new_parts[index] = {**part, "text": text[:head] + style + text[tail:]}
        return {**item, "parts": new_parts}
    return None


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


def _tool_name_of(definition: Any) -> str:
    """从工具定义里取出名字。

    主程序把工具序列化成 OpenAI function schema（``serialize_tool_definitions``
    → ``to_openai_function_schema``），名字在 ``function.name`` 里；扁平结构也认一下，
    免得上游换了序列化方式之后这里静默失效——静默失效的后果是工具永远摘不掉。
    """
    if not isinstance(definition, dict):
        return ""
    function = definition.get("function")
    if isinstance(function, dict) and function.get("name"):
        return str(function["name"])
    return str(definition.get("name") or "")


def _message_moment(message: dict[str, Any]) -> datetime | None:
    """消息载荷里的时刻。主程序序列化成 epoch 秒的**字符串**，不是数字。"""
    try:
        return datetime.fromtimestamp(float(message.get("timestamp")), JST)
    except (TypeError, ValueError):
        return None


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
