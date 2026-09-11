"""时段状态生成。

每天 12:00（JST）生成**次日**全天，**一次调用出十来段**。

早先是一段一次调用、顺序执行。那样每段的写手都看不到后面会发生什么，只能把
250 字写成一个自洽的小故事，于是十段各自闭合、跨段因果为零——读起来像十篇流水账，
而不是一天。改成一次调用之后，写手看得见全天骨架，可以让一件事跨几段发展再收。

不合格的段**不重试**：铺骨架底稿，原样报进批次结果。早先有一条「定向重写」的补写
路径（单独把那一段连同全天脉络重新喂给模型），已移除——它是这里唯一读 ``outline``
的地方，为一年不出几次的校验失败养一整条 prompt + 调用链不划算。

字段分工（4.3）：
- ``story``            → 事实层工具结果，被问才吐，可以有活动名词
- ``mood`` / ``physical_state`` / 两项分档 → planner 注入与前端展示，
  第一轮随 story 生成
- ``topic``            → replyer 常驻注入（C），第二轮提炼，说过之后按
  ``topic.stop_after_shared`` 决定还注不注入
- ``manner``           → replyer 常驻注入（A），第三轮逐时段从 story + mood 生成
- ``outline``（脉络）  → 模型动笔前给自己写的规划，落进 ``days.outline``，
  不进任何注入，也不再有别的消费方——只供前端回看

第二轮见 ``extract_round2``：从 story 里抽地点时段轴和谈资，全天十段一次调用。
第三轮见 ``generate_expressions``：全天一次调用，只为偏离基线的时段写 manner。
"""

from dataclasses import dataclass
from datetime import date
from typing import Any

import json
import logging
import re
import time

from . import prompt_loader as prompts
from ..schedule.negative_events import LEVEL_HINTS
from ..schedule.store import (
    ScheduleStore,
    Segment,
    SegmentState,
    to_minutes,
    weekday_name,
)


_logger = logging.getLogger("a_day_with_mittes.generator")

# WebUI 推理过程页的分类目录。只开一个——分类多了列表会很碎，
# 两种调用靠 request kind 和输出内容区分就够。
PREVIEW_STAGE = "a_day_with_mittes"
PREVIEW_SESSION = "schedule_batch"

# 单次调用的输出上限。给得很宽，因为**推理和正文共用这个预算**：
# glm-5.2 跑全天话题提炼时把 4000 token 全烧在 reasoning 上，一个字正文都没吐出来，
# 而 status 仍是 completed，主程序不当失败。正文本身最多一两千 token，
# 上限给高不额外花钱（按实际用量计费），但能挡住这类静默截断。
_MAX_TOKENS = 32000

# 第一轮不再用 Markdown 分节承载结果，而是让模型把整天作为一次
# 原生工具调用交付。工具没有副作用，它的参数就是生成结果。
_DAY_SUBMIT_TOOL = "submit_day_story"
_TOPIC_SUBMIT_TOOL = "submit_day_topics"
_MOOD_LEVELS = ("正面", "中性", "负面")
_ENERGY_LEVELS = ("良好", "一般", "不佳")

# 给模型看的文字全在 ``prompts/`` 下，一次请求一个文件：
# ``01_生成全天故事与情绪.prompt``、``02_提取地点与话题.prompt``、
# ``03_生成表达方式.prompt``。
# 为什么这么配、哪些东西**不能**写进去，见根目录 ``DESIGN.md``——那些理由不能放在
# ``.prompt`` 里，文件内容是原样发给模型的。

# 常驻注入的两个字段（manner / mood）里出现就判定不合格的
# 通用场所/身份词。骨架的 place / outfit 会另行拆成词一起查。
# 这是**校验用**的词表，不是 prompt——「规则只能排除不能示范」那条约束的是
# 喂给模型的文字，落盘校验列得越全越好。
_BANNED_IN_MANNER = (
    # 场所
    "店里", "店内", "咖啡店", "学校", "教室", "课上", "家里", "回家",
    "更衣室", "浴室", "卧室", "厨房", "露台", "吧台", "摄影棚", "包厢",
    "营业区", "工作间", "便利店", "床上", "楼上", "楼下", "一楼", "二楼", "三楼",
    "电车", "神保町", "原宿", "路上", "棚里",
    # 身份 / 事由
    "上班", "下班", "班上", "打工", "镜头",
    # 衣物——骨架的 outfit 现在填的是套装名（一整串），拆不出可检测的词，
    # 所以衣物词全靠这张表兜住
    "女仆装", "制服", "校服", "睡衣", "睡裙", "连衣裙", "百褶",
    "短袖", "长袖", "短裤", "围裙", "发带", "丝带", "披肩",
    "拖鞋", "乐福鞋", "帆布鞋", "运动鞋", "玛丽珍",
    "书包", "托特包", "草帽", "棒球帽",
)


@dataclass
class DayOutcome:
    """一次全天调用的结果。

    ``states`` 只放**通过校验**的段；没通过的进 ``failures``，调用方按段铺骨架底稿
    并原样报出来。一段不合格不牵连别的段，但也不再自动补写——定向重写已移除。
    """

    outline: str
    states: dict[str, SegmentState]
    failures: list[tuple[str, str]]
    ok: bool
    reason: str = ""
    fatal: bool = False


class SegmentGenerator:
    """负责调 LLM 生成全天状态，以及第二轮抽取。"""

    def __init__(
        self,
        ctx: Any,
        store: ScheduleStore,
        preview: Any,
        model: str,
        topic_model: str,
        expression_model: str,
        base_task: str,
        temperature: float,
    ) -> None:
        self._ctx = ctx
        self._store = store
        self._preview = preview
        self._model = model
        self._topic_model = topic_model
        self._expression_model = expression_model
        self._base_task = base_task
        self._temperature = temperature

    async def _call_llm(
        self,
        prompt: str,
        *,
        model: str,
        temperature: float,
        tool_options: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """调一次 LLM，把异常收敛成 ``success=False`` 的返回。

        **不走 ``llm.generate`` 那条 capability**，直接用主程序的 ``LLMServiceClient``。
        原因是 capability 只收**任务名**（``core.py`` 里 ``resolve_task_name(args["model"])``），
        而任务名只决定候选池：``replyer`` 的 ``selection_strategy`` 是 ``random``，
        四个模型随机挑，sonnet 5 只有 1/4 的概率——整套 prompt 的判据是照着 sonnet 5
        的水平定的，随机挑模型会让每天的质量方差压过 prompt 本身的影响。
        ``LLMServiceClient`` 这一层收 ``model_name``，能钉死具体模型。
        写法参照同部署的 ``03_RssImageFeederPlugin/_bridge.py``。

        ``self._base_task`` 只当**基座**：模型、温度、max_tokens 全部由这里覆盖，
        真正借用的是它的 ``hard_timeout`` 和统计管线。选 ``memory``（240 秒）是因为
        ``replyer`` 只有 60 秒、``utils`` 只有 20 秒，而这里一次要吐全天十来段。

        > **余量很薄。** 改成一次出全天之后，实测单次 147~195 秒（sonnet 5，十段），
        > 240 秒只剩两成余量。骨架变长、模型变慢都可能顶穿。真顶穿了表现为整天 fatal、
        > 全天退底稿，这时候该换一个 hard_timeout 更宽的基座任务，而不是改这里。

        异常必须收在这里：主程序这条链路同样是直接 raise 的，不收住会掀掉 run_batch，
        再掀掉守护协程，之后每日批次就再也不会跑了。
        """
        # 延迟导入：插件加载早于主程序部分模块，放模块顶层有循环导入风险。
        # 不做 try/except 兜底——导入失败属于主程序 API 变更，必须立刻暴露。
        from src.common.data_models.llm_service_data_models import LLMGenerationOptions
        from src.services.llm_service import LLMServiceClient

        started = time.monotonic()
        try:
            client = LLMServiceClient(
                task_name=self._base_task,
                request_type="plugin.a_day_with_mittes.schedule",
            )
            result = await client.generate_response(
                prompt,
                options=LLMGenerationOptions(
                    model_name=model,
                    temperature=temperature,
                    max_tokens=_MAX_TOKENS,
                    tool_options=tool_options,
                ),
            )
        except Exception as exc:
            return {"success": False, "error": f"{type(exc).__name__}: {exc}", "response": ""}

        # 用量和耗时透传给推理记录。WebUI 那页的 token 列和耗时列读的就是这几个键
        # （``metadata.prompt_tokens`` / ``completion_tokens`` / ``total_tokens`` /
        # ``duration_ms``），不带上去页面就一路显示 0 Token、没有耗时。
        usage = {
            "prompt_tokens": result.prompt_tokens,
            "completion_tokens": result.completion_tokens,
            "total_tokens": result.total_tokens,
            "duration_ms": (time.monotonic() - started) * 1000,
        }

        text = result.response
        tool_calls = [
            {
                "id": call.call_id,
                "name": call.func_name,
                "args": call.args or {},
                "extra_content": call.extra_content or {},
            }
            for call in (result.tool_calls or [])
        ]
        if not text.strip() and not tool_calls:
            # 空响应最常见的成因不是模型罢工，而是**推理把 max_tokens 吃光了**：
            # 推理模型的 reasoning 和正文共用这一个预算，想得太久就一个字正文都吐不出来，
            # 而 status 仍是 completed，主程序不当失败。实测 glm-5.2 跑全天话题提炼时
            # 4000 token 全烧在 reasoning 上。这两种情况要分开报，否则排查会走偏。
            used = result.completion_tokens
            if used >= _MAX_TOKENS:
                reason = f"输出被 max_tokens 截断：{used}/{_MAX_TOKENS} token 全部用于推理，未产出正文"
            else:
                reason = f"模型返回空响应（completion_tokens={used}）"
            return {"success": False, "error": reason, "response": "", "model": model, **usage}
        return {
            "success": True,
            "response": text,
            "tool_calls": tool_calls,
            "tool_definitions": tool_options or [],
            "model": result.model_name or model,
            **usage,
        }

    # ── 全天生成 ──
    async def generate_day(
        self,
        *,
        day: date,
        segments: list[Segment],
        weather: str,
        holiday: str,
        previous: tuple[Segment, SegmentState] | None,
        negative_levels: dict[str, str],
    ) -> DayOutcome:
        """一次原生工具调用生成全天脉络和每一段的完整身心状态。

        **校验按段做。** 某一段不合格只把它记进 ``failures``，其余段照常收下——
        不合格的段由调用方铺骨架底稿并报出来，不整天重来，也不单独补写。

        只有调用本身失败（模型不可用、鉴权错、超时）才 ``fatal``，那种错重试没有意义。
        """
        prompt = self._build_day_prompt(
            day=day,
            segments=segments,
            weather=weather,
            holiday=holiday,
            previous=previous,
            negative_levels=negative_levels,
        )
        result = await self._call_llm(
            prompt,
            model=self._model,
            temperature=self._temperature,
            tool_options=[self._day_submit_tool(segments)],
        )
        self._record_preview(
            request_kind="schedule_day",
            prompt=prompt,
            result=result,
            selection_reason=f"{day.isoformat()} 全天（{len(segments)} 段）",
            output_title="全天状态",
        )

        if not result.get("success", True):
            reason = str(result.get("error") or result.get("response") or "").strip() or "模型调用失败"
            _logger.error("[生成] %s 全天调用失败：%s", day, reason)
            return DayOutcome(outline="", states={}, failures=[], ok=False, reason=reason, fatal=True)

        outline, parsed, parse_error = _parse_day_call(result.get("tool_calls"), segments)
        if not parsed:
            reason = parse_error or "工具调用里找不到任何时段"
            _logger.error("[生成] %s %s", day, reason)
            return DayOutcome(
                outline=outline, states={}, failures=[], ok=False, reason=reason
            )

        states: dict[str, SegmentState] = {}
        failures: list[tuple[str, str]] = []
        # 「首句雷同」要和**上一段实际采用的** story 比，所以只在通过时才推进
        previous_story = previous[1].story if previous else ""
        for segment in segments:
            state = parsed.get(segment.slot)
            if state is None:
                failures.append((segment.slot, "这一段没出现在输出里"))
                continue
            reason = self._validate_state(state, segment, previous_story)
            if reason:
                failures.append((segment.slot, reason))
                continue
            states[segment.slot] = state
            previous_story = state.story

        if parse_error:
            _logger.warning("[生成] %s 工具参数结构有偏差：%s", day, parse_error)

        _logger.info(
            "[生成] %s 全天 %d 段，合格 %d，不合格 %d", day, len(segments), len(states), len(failures)
        )
        return DayOutcome(outline=outline, states=states, failures=failures, ok=True)

    # ── prompt 拼装 ──
    def _build_day_prompt(
        self,
        *,
        day: date,
        segments: list[Segment],
        weather: str,
        holiday: str,
        previous: tuple[Segment, SegmentState] | None,
        negative_levels: dict[str, str],
    ) -> str:
        """拼装全天生成 prompt（``prompts/01_生成全天故事与情绪.prompt``）。"""
        previous_block = ""
        if previous is not None:
            prev_segment, prev_state = previous
            previous_block = prompts.render(
                "01_生成全天故事与情绪",
                "previous",
                slot=prev_segment.slot,
                title=prev_segment.title,
                story=prev_state.story,
                mood=prev_state.mood,
                physical_state=prev_state.physical_state,
            )

        marked = "\n".join(
            prompts.render(
                "01_生成全天故事与情绪",
                "negative_item",
                slot=segment.slot,
                hint=LEVEL_HINTS.get(negative_levels[segment.slot], negative_levels[segment.slot]),
            )
            for segment in segments
            if negative_levels.get(segment.slot)
        )

        return prompts.render(
            "01_生成全天故事与情绪",
            date=day.isoformat(),
            weekday=weekday_name(day),
            holiday=f"　{holiday}" if holiday else "",
            weather=prompts.render("01_生成全天故事与情绪", "weather", weather=weather)
            if weather
            else "",
            previous=previous_block,
            skeleton=self._skeleton_block("01_生成全天故事与情绪", day, segments),
            negative=(
                prompts.render("01_生成全天故事与情绪", "negative", marked=marked)
                if marked
                else ""
            ),
        )

    @staticmethod
    def _day_submit_tool(segments: list[Segment]) -> dict[str, Any]:
        """构造当天专用的提交工具；slot 枚举随骨架动态收紧。"""
        item_schema = {
            "type": "object",
            "properties": {
                "slot": {
                    "type": "string",
                    "enum": [segment.slot for segment in segments],
                    "pattern": r"^\d{2}:\d{2}-\d{2}:\d{2}$",
                    "description": (
                        "骨架中的时段。只填 HH:MM-HH:MM 时间，"
                        "绝对不要在后面附加时段标题或其他文字。"
                    ),
                },
                "story": {
                    "type": "string",
                    "description": "第三人称的具体经历，以 Mittes 为中心。",
                },
                "mood": {
                    "type": "string",
                    "description": "story 自然留下的情绪，只写情绪，不写身体状况。",
                },
                "physical_state": {
                    "type": "string",
                    "description": (
                        "当时剩余的可支配体力：疲劳、困意、乏力或精力。"
                        "按全天累积消耗判断，不写心情，不把舒服、清爽等表面感受当体力。"
                    ),
                },
                "mood_level": {
                    "type": "string",
                    "enum": list(_MOOD_LEVELS),
                    "description": "mood 中最主要情绪的分档。",
                },
                "energy_level": {
                    "type": "string",
                    "enum": list(_ENERGY_LEVELS),
                    "description": (
                        "physical_state 的独立体力分档：良好=无明显疲劳，"
                        "一般=有累意但能正常继续，不佳=明显困倦乏力、想停下休息。"
                        "不受 mood_level 影响。"
                    ),
                },
            },
            "required": [
                "slot",
                "story",
                "mood",
                "physical_state",
                "mood_level",
                "energy_level",
            ],
            "additionalProperties": False,
        }
        parameters = {
            "type": "object",
            "properties": {
                "outline": {
                    "type": "string",
                    "description": "为了保持前后一致而写的全天脉络；没有明显线索就直接说没有。",
                },
                "segments": {
                    "type": "array",
                    "description": "按骨架顺序列出全部时段，不得增删、重复或合并。",
                    "items": item_schema,
                    "minItems": len(segments),
                    "maxItems": len(segments),
                },
            },
            "required": ["outline", "segments"],
            "additionalProperties": False,
        }
        return {
            "type": "function",
            "function": {
                "name": _DAY_SUBMIT_TOOL,
                "description": (
                    "交付写完的 Mittes 全天故事。最终必须且只能调用一次，"
                    "工具参数就是完整成品。"
                ),
                "parameters": parameters,
            },
        }

    def _skeleton_block(self, name: str, day: date, segments: list[Segment]) -> str:
        """全天骨架清单。

        **不再逐段给清醒/工作小时数。** 那个数原本是给「累」一个客观锚点，实测反而
        成了唯一的锚点：写手拿到刻度就照着填，一天读下来是一条单调递增的疲劳曲线。
        累不累应当从写手自己写的情节里长出来；而「这是醒来后第几段、中间干过几段活」
        本来就在这份按时间排的清单里，那个数是冗余的。
        ``ScheduleStore.awake_and_work_hours`` 保留不动，只是第一轮不再用它。

        Args:
            name: 用哪个文件里的片段。
        """
        del day
        lines = []
        for index, segment in enumerate(segments, 1):
            lines.append(
                prompts.render(
                    name,
                    "skeleton_item",
                    index=index,
                    slot=segment.slot,
                    title=segment.title,
                    place=segment.place,
                    outfit=segment.outfit,
                    company=segment.company,
                    kind=segment.kind,
                )
            )
        return "\n".join(lines)

    def _validate_state(self, state: SegmentState, segment: Segment, previous_story: str) -> str:
        """校验第一轮的一段完整结果，返回不合格原因。

        硬校验：缺字段，两项分档越界，mood 含地点/服装/场所词，
        首句与上一段雷同。
        只告警不重写：story 字数越界——它是被问才吐的事实层，长一点没有副作用，
        为字数烧一次调用不值得。
        """
        missing = [
            name
            for name in ("story", "mood", "physical_state", "mood_level", "energy_level")
            if not getattr(state, name)
        ]
        if missing:
            return f"缺字段：{'/'.join(missing)}"

        if state.mood_level not in _MOOD_LEVELS:
            return f"心情分档不合法：{state.mood_level}"
        if state.energy_level not in _ENERGY_LEVELS:
            return f"体力分档不合法：{state.energy_level}"

        hit = _find_banned_word(state.mood, segment)
        if hit:
            return f"mood 含具体事项名词「{hit}」"

        # 首句跟上一段雷同 = 模型把某个句子当模板锁死了。
        # 实测发生过：prompt 里演示「首句要出现 Mittes」的那个例句被原样照抄，
        # 一路滚了 13 段。这类锁死光靠 prompt 措辞挡不住，得在这里拦。
        if previous_story and state.story[:15] == previous_story[:15]:
            return f"首句与上一段雷同（{state.story[:15]}…）"

        if not 150 <= len(state.story) <= 380:
            _logger.warning("[生成] %s story 字数 %d 越界，仅告警", segment.slot, len(state.story))

        return ""

    # ── 第三轮：全天表达方式 ──
    async def generate_expressions(
        self,
        day: date,
        segments: list[Segment],
        states: dict[str, SegmentState],
    ) -> tuple[dict[str, str], str]:
        """挑出这一天说话状态偏离基线的时段，为它们各写一句表达方式。

        **从「每段一次调用」改成「全天一次调用」。** 两个理由：

        - “偏不偏离”是相对**当天基线**的判断，逐段调用给不了这个视野；
        - 大多数段的正确答案是“不写”，逐段调用等于花十一次请求买两三条。

        失败面跟着变了：一次挂掉就是当天一条 manner 都没有。这在旧设计下是灾难，
        现在不是——“大部分时段没有 manner”本来就是新常态，replyer 侧空串直接跳过注入。

        Returns:
            tuple[dict, str]: ({slot: manner}, 失败原因)。manner 为空串表示这段不注入；
            返回的字典**只包含模型给出且校验通过的段**，其余段由调用方保留旧值。
        """
        candidates = [
            segment
            for segment in segments
            if (state := states.get(segment.slot)) is not None and state.generated
        ]
        if not candidates:
            return {}, "没有可生成的时段"

        prompt = prompts.render(
            "03_生成表达方式",
            date=day.isoformat(),
            weekday=weekday_name(day),
            listing="\n\n".join(
                prompts.render(
                    "03_生成表达方式",
                    "listing_item",
                    index=index + 1,
                    slot=segment.slot,
                    title=segment.title,
                    kind=segment.kind,
                    mood=states[segment.slot].mood,
                    story=states[segment.slot].story,
                )
                for index, segment in enumerate(candidates)
            ),
        )
        result = await self._call_llm(
            prompt, model=self._expression_model, temperature=self._temperature
        )
        self._record_preview(
            request_kind="schedule_expression",
            prompt=prompt,
            result=result,
            selection_reason=f"{day.isoformat()} 第三轮表达方式（{len(candidates)} 段）",
            output_title="表达方式",
        )
        if not result.get("success", True):
            reason = str(result.get("error") or result.get("response") or "").strip()
            return {}, reason or "模型调用失败"

        rows = _extract_json_array(str(result.get("response") or ""))
        if rows is None:
            _logger.error("[第三轮] %s 输出不是合法 JSON 数组", day)
            return {}, "输出不是合法 JSON 数组"

        by_slot = {segment.slot: segment for segment in candidates}
        manners: dict[str, str] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            slot = str(row.get("slot") or "")
            if slot not in by_slot:
                continue
            manner = _clean_expression(str(row.get("manner") or ""))
            if not manner:
                # 判为“没有明显偏离”。这是**期望中的多数**，不是失败
                manners[slot] = ""
                continue
            reason = self._validate_expression(manner, by_slot[slot])
            if reason:
                # 单段不合格只丢这一段：它退回旧值，不牵连当天其余段
                _logger.warning("[第三轮] %s %s 不合格，丢弃：%s", day, slot, reason)
                continue
            manners[slot] = manner

        written = sum(1 for value in manners.values() if value)
        _logger.info(
            "[第三轮] %s 偏离 %d 段 / 共 %d 段", day, written, len(candidates)
        )
        return manners, ""

    @staticmethod
    def _validate_expression(manner: str, segment: Segment) -> str:
        """校验一句非空的表达方式。空串由调用方当作“这段不写”，不进这里。"""
        if len(manner) > 40:
            return f"表达方式超长（{len(manner)} 字）"
        physical = _find_physical_word(manner)
        if physical:
            return f"表达方式写了兑现不了的「{physical}」"
        hit = _find_banned_word(manner, segment)
        if hit:
            return f"表达方式含具体事项名词「{hit}」"
        return ""

    # ── 第二轮：地点时段轴 + 话题提炼 ──
    async def extract_round2(
        self, day: date, segments: list[Segment], states: dict[str, SegmentState]
    ) -> tuple[dict[str, int], str]:
        """从一整天的 story 里抽两样东西，全天十段**一次调用**（设计文档 5.10）。

        - ``places``：她这一段待过哪些地方，排成首尾相接的时段轴。
          骨架的 ``place`` 是一段一个值，跨场所的时段只能把几个地点挤进一个字符串、
          没有时间边界，所以「她现在在哪」在那 90 分钟里答不出来。
        - ``topic``：一句值得说给人听的小事。
          事实层有个先天缺陷——几乎没人会主动问「你在干嘛」，工具不被调用 story 就用不上；
          谈资给她一个主动出口。

        不并进主生成，因为两件事性质不同（创作 vs 抽取）。抽取本可以用便宜模型，
        但 topic 是常驻注入到 replyer 的，写糙一点她开口就糙一点——``topic_model``
        现在填的是和主生成同一档。
        ``places`` 尤其不能进主生成：模型如果知道写完 story 还得交一份地点时段轴，
        可能会为了填满它把人挪来挪去，写出本来不该发生的移动。

        两个字段**分开容错**：一段的时段轴不合格不影响它的 topic，反之亦然。
        直接把结果写进 ``states``。

        Returns:
            tuple[dict, str]: ({"places": 段数, "topics": 条数, "total": 候选段数}, 失败原因)
        """
        # 睡眠段按 kind 硬拦，连判断都省掉
        candidates = [
            segment
            for segment in segments
            if segment.kind != "睡眠" and states.get(segment.slot, None) is not None
            and states[segment.slot].generated
        ]
        empty = {"places": 0, "topics": 0, "total": 0}
        if not candidates:
            return empty, "没有可提炼的时段"

        prompt = self._build_round2_prompt(day, candidates, states)
        result = await self._call_llm(
            prompt,
            model=self._topic_model,
            temperature=0.4,
            tool_options=[self._round2_submit_tool(candidates)],
        )
        self._record_preview(
            request_kind="schedule_topics",
            prompt=prompt,
            result=result,
            selection_reason=f"{day.isoformat()} 第二轮抽取（{len(candidates)} 段）",
            output_title="地点与话题",
        )

        if not result.get("success", True):
            reason = str(result.get("error") or result.get("response") or "").strip() or "模型调用失败"
            _logger.error("[第二轮] %s 失败：%s", day, reason)
            return dict(empty, total=len(candidates)), reason

        rows, parse_error = _parse_round2_call(result.get("tool_calls"), candidates)
        if parse_error:
            _logger.error("[第二轮] %s 工具参数不合格：%s", day, parse_error)
            return dict(empty, total=len(candidates)), parse_error

        by_slot = {segment.slot: segment for segment in candidates}
        produced = 0
        placed = 0
        for row in rows:
            if not isinstance(row, dict):
                continue
            slot = str(row.get("slot") or "")
            if slot not in by_slot:
                continue

            # places 与 topic 分字段容错：一个不合格不牵连另一个（设计文档 5.10）。
            # 两者的损失量级不一样——topic 没了只是少一次开口机会，
            # places 没了是常驻注入每一轮都缺。
            places = self._parse_places(day, by_slot[slot], row.get("places"))
            if places:
                states[slot].places = places
                placed += 1

            topic = str(row.get("topic") or "").strip()
            if not topic:
                continue
            if len(topic) > 100:
                _logger.warning("[第二轮] %s %s topic 超长（%d 字），丢弃", day, slot, len(topic))
                continue
            states[slot].topic = topic
            # keys 已退役：新写的 topic 一律配空数组，免得旧 topic 的关键词
            # 留在新 topic 旁边，看起来像还在用
            states[slot].topic_keys = []
            produced += 1

        _logger.info(
            "[第二轮] %s 地点 %d/%d 段，话题 %d 条", day, placed, len(candidates), produced
        )
        return {"topics": produced, "places": placed, "total": len(candidates)}, ""

    @staticmethod
    def _round2_submit_tool(segments: list[Segment]) -> dict[str, Any]:
        """构造第二轮的单一交付工具，slot 枚举随当天候选段收紧。"""
        place_schema = {
            "type": "object",
            "properties": {
                "from": {
                    "type": "string",
                    "description": "这个地点的起始时刻，格式 HH:MM。",
                },
                "to": {
                    "type": "string",
                    "description": "这个地点的结束时刻，格式 HH:MM。",
                },
                "place": {
                    "type": "string",
                    "description": "从 story 中得出的具体地点说法。",
                },
            },
            "required": ["from", "to", "place"],
            "additionalProperties": False,
        }
        item_schema = {
            "type": "object",
            "properties": {
                "slot": {
                    "type": "string",
                    "enum": [segment.slot for segment in segments],
                    "pattern": r"^\d{2}:\d{2}-\d{2}:\d{2}$",
                    "description": (
                        "对应 story 的原时段。只填 HH:MM-HH:MM 时间，"
                        "绝对不要在后面附加时段标题或其他文字。"
                    ),
                },
                "places": {
                    "type": "array",
                    "description": (
                        "按时间先后排列的地点轴；首尾覆盖整个 slot，"
                        "相邻记录时间首尾相接。"
                    ),
                    "items": place_schema,
                    "minItems": 1,
                },
                "topic": {
                    "type": "string",
                    "description": (
                        "从 story 中挑出的一件值得顺口分享的事；第三人称，"
                        "50~80 字，有起因、发生经过和结果或转折。没有就填空字符串。"
                    ),
                },
            },
            "required": ["slot", "places", "topic"],
            "additionalProperties": False,
        }
        parameters = {
            "type": "object",
            "properties": {
                "segments": {
                    "type": "array",
                    "description": (
                        "按原顺序列出所有候选时段；全天非空 topic 必须为 3~6 条。"
                    ),
                    "items": item_schema,
                    "minItems": len(segments),
                    "maxItems": len(segments),
                },
            },
            "required": ["segments"],
            "additionalProperties": False,
        }
        return {
            "type": "function",
            "function": {
                "name": _TOPIC_SUBMIT_TOOL,
                "description": (
                    "交付从 Mittes 全天 story 中抽取的地点时段轴和谈资。"
                    "最终必须且只能调用一次，工具参数就是完整成品。"
                ),
                "parameters": parameters,
            },
        }

    @staticmethod
    def _parse_places(day: date, segment: Segment, raw: Any) -> list[dict[str, str]]:
        """校验并规整一段的地点时段轴（设计文档 5.10）。

        只校验时间，**地点不做任何检查**——地点的说法从 story 里来，
        不要求落在骨架的 ``place`` 范围内（那一栏是概括，story 走过的地方比它细）。

        时间必须首尾相接、覆盖整个时段：注入时要按当前时刻查表，
        中间留空档就意味着那几分钟查不到人在哪。不合格整段丢弃，
        退回骨架 ``place``——半截的时段轴比没有更糟。
        """
        if not isinstance(raw, list) or not raw:
            return []
        entries: list[dict[str, str]] = []
        for item in raw:
            if not isinstance(item, dict):
                return []
            start, end = str(item.get("from") or "").strip(), str(item.get("to") or "").strip()
            place = str(item.get("place") or "").strip()
            if not start or not end or not place:
                return []
            entries.append({"from": start, "to": end, "place": place})

        try:
            bounds = [(to_minutes(e["from"]), to_minutes(e["to"])) for e in entries]
        except (ValueError, IndexError):
            _logger.warning("[第二轮] %s %s 地点时间格式不对，丢弃", day, segment.slot)
            return []

        seg_start, seg_end = to_minutes(segment.start), to_minutes(segment.end)
        problems = []
        if bounds[0][0] != seg_start:
            problems.append(f"首条从 {entries[0]['from']} 起，应为 {segment.start}")
        if bounds[-1][1] != seg_end:
            problems.append(f"末条到 {entries[-1]['to']} 止，应为 {segment.end}")
        for index, (start, end) in enumerate(bounds):
            if start >= end:
                problems.append(f"第 {index + 1} 条时间倒挂")
            if index and bounds[index - 1][1] != start:
                problems.append(f"第 {index} 与第 {index + 1} 条之间不相接")
        if problems:
            _logger.warning("[第二轮] %s %s 地点时段轴不合格：%s", day, segment.slot, "；".join(problems))
            return []
        return entries

    def _build_round2_prompt(
        self, day: date, segments: list[Segment], states: dict[str, SegmentState]
    ) -> str:
        """拼装第二轮 prompt（``prompts/02_提取地点与话题.prompt``）：地点时段轴 + 话题提炼。"""
        return prompts.render(
            "02_提取地点与话题",
            date=day.isoformat(),
            weekday=weekday_name(day),
            listing="\n\n".join(
                prompts.render(
                    "02_提取地点与话题",
                    "listing_item",
                    index=index + 1,
                    slot=segment.slot,
                    title=segment.title,
                    story=states[segment.slot].story,
                )
                for index, segment in enumerate(segments)
            ),
        )

    def _record_preview(
        self,
        *,
        request_kind: str,
        prompt: str,
        result: dict[str, Any],
        selection_reason: str,
        output_title: str,
    ) -> None:
        """写 WebUI 推理记录；失败不影响生成本身。"""
        try:
            self._preview.record(
                stage=PREVIEW_STAGE,
                session=PREVIEW_SESSION,
                request_kind=request_kind,
                prompt=prompt,
                result=result,
                selection_reason=selection_reason,
                output_title=output_title,
            )
        except Exception as exc:
            _logger.warning("[推理记录] 写入失败：%s", type(exc).__name__)


def _parse_day_call(
    raw_calls: Any,
    segments: list[Segment],
) -> tuple[str, dict[str, SegmentState], str]:
    """从 ``submit_day_story`` 的原生工具参数中取出整天。

    JSON Schema 能收紧单项形状，但不能保证 slot 不重不漏；这里再做一次
    与当天骨架的集合和顺序对账。结构有偏差时仍尽量收下对得上的段，
    缺失段交给上层铺底稿。
    """
    if not isinstance(raw_calls, list):
        return "", {}, f"模型没有调用 {_DAY_SUBMIT_TOOL}"
    calls = [call for call in raw_calls if isinstance(call, dict) and call.get("name") == _DAY_SUBMIT_TOOL]
    if len(calls) != 1:
        return "", {}, f"{_DAY_SUBMIT_TOOL} 调用次数应为 1，实际为 {len(calls)}"

    args = calls[0].get("args")
    if not isinstance(args, dict):
        return "", {}, "工具参数不是对象"
    outline = str(args.get("outline") or "").strip()
    rows = args.get("segments")
    if not isinstance(rows, list):
        return outline, {}, "segments 不是数组"

    expected = [segment.slot for segment in segments]
    valid = set(expected)
    actual: list[str] = []
    states: dict[str, SegmentState] = {}
    duplicates: set[str] = set()
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        slot = _canonical_slot(raw.get("slot"), segments)
        actual.append(slot)
        if slot not in valid:
            continue
        if slot in states:
            duplicates.add(slot)
            del states[slot]
            continue
        if slot in duplicates:
            continue
        states[slot] = SegmentState(
            story=str(raw.get("story") or "").strip(),
            manner="",
            mood=str(raw.get("mood") or "").strip(),
            physical_state=str(raw.get("physical_state") or "").strip(),
            mood_level=str(raw.get("mood_level") or "").strip(),
            energy_level=str(raw.get("energy_level") or "").strip(),
        )

    problems: list[str] = []
    if actual != expected:
        problems.append("segments 的 slot 顺序或集合与骨架不一致")
    if duplicates:
        problems.append(f"重复 slot：{'/'.join(sorted(duplicates))}")
    if not outline:
        problems.append("缺 outline")
    return outline, states, "；".join(problems)


def _parse_round2_call(
    raw_calls: Any,
    segments: list[Segment],
) -> tuple[list[dict[str, Any]], str]:
    """取出 ``submit_day_topics`` 参数，并与候选时段严格对账。"""
    if not isinstance(raw_calls, list):
        return [], f"模型没有调用 {_TOPIC_SUBMIT_TOOL}"
    calls = [
        call
        for call in raw_calls
        if isinstance(call, dict) and call.get("name") == _TOPIC_SUBMIT_TOOL
    ]
    if len(calls) != 1:
        return [], f"{_TOPIC_SUBMIT_TOOL} 调用次数应为 1，实际为 {len(calls)}"

    args = calls[0].get("args")
    if not isinstance(args, dict):
        return [], "工具参数不是对象"
    rows = args.get("segments")
    if not isinstance(rows, list):
        return [], "segments 不是数组"
    if not all(isinstance(row, dict) for row in rows):
        return [], "segments 里有不是对象的项"

    expected = [segment.slot for segment in segments]
    actual = [_canonical_slot(row.get("slot"), segments) for row in rows]
    if actual != expected:
        return [], "segments 的 slot 顺序或集合与候选时段不一致"

    # 后续消费者只认标准 slot。兼容分支如果收到「时间 + 正确标题」，
    # 在这里收回纯时间，不把模型的展示性后缀泄露到后面。
    normalized_rows = [dict(row, slot=slot) for row, slot in zip(rows, actual, strict=True)]

    topics = sum(1 for row in normalized_rows if str(row.get("topic") or "").strip())
    if not 3 <= topics <= 6:
        return [], f"非空 topic 应为 3~6 条，实际为 {topics} 条"
    return normalized_rows, ""


def _canonical_slot(raw: Any, segments: list[Segment]) -> str:
    """把 slot 收敛为骨架里的纯时间键。

    工具 Schema 已经给了 enum 和 pattern，但某些兼容代理不会真正强制它们。
    实测模型偶尔会照抄骨架行，返回「02:00-09:00　睡眠」。只有当
    后缀与该 slot 的骨架标题完全相同时才容错；任意后缀仍按错误处理。
    """
    value = str(raw or "").strip()
    for segment in segments:
        if value == segment.slot:
            return segment.slot
        if value.startswith(segment.slot):
            suffix = value[len(segment.slot):].strip()
            if suffix == segment.title:
                return segment.slot
    return value


def _extract_json_array(text: str) -> list[Any] | None:
    """从模型输出里取出 JSON 数组，容忍前后多余文字和代码块围栏。"""
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", stripped).strip()
    start = stripped.find("[")
    end = stripped.rfind("]")
    if start < 0 or end <= start:
        return None
    try:
        payload = json.loads(stripped[start : end + 1])
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, list) else None


def _clean_expression(text: str) -> str:
    """把第三轮偶发的围栏、引号和换行收成可直接注入的一句话。"""
    value = text.strip()
    if value.startswith("```"):
        value = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", value).strip()
    value = re.sub(r"\s+", " ", value).strip()
    if len(value) >= 2 and value[0] in "\"“" and value[-1] in "\"”":
        value = value[1:-1].strip()
    return value


# 打字模拟按消息长度算，跟注入的文字无关，所以这些词写了也兑现不了——
# 模型唯一能“照做”的方式是表演性地打一串省略号或者说一句“我刚在忙”。
# 当初取消 busy 字段就是同一个理由，这里把它挡在生成侧。
_PHYSICAL_WORDS = (
    "语速", "慢半拍", "停顿", "打字", "回得慢", "回复慢", "回复得慢", "秒回", "隔很久",
)


def _find_physical_word(text: str) -> str:
    """找出表达方式里兑现不了的物理描述。"""
    return next((word for word in _PHYSICAL_WORDS if word in text), "")


def _find_banned_word(text: str, segment: Segment) -> str:
    """在常驻注入的字段里找出不该出现的具体事项名词。

    除通用场所/身份词外，还要查这一段骨架自己的地点和服装——
    生成时把它们写进来是最常见的破法。
    """
    for word in _BANNED_IN_MANNER:
        if word in text:
            return word
    for source in (segment.place, segment.outfit):
        for token in re.split(r"[\s/、，,·—－\-]+", source):
            token = token.strip()
            # 两个字以上才查，避免"家""店"这种单字误伤正常表达
            if len(token) >= 2 and token in text:
                return token
    return ""
