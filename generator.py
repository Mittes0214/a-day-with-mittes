"""时段状态生成。

每天 12:00（JST）生成**次日**全天，**一次调用出十来段**。

早先是一段一次调用、顺序执行。那样每段的写手都看不到后面会发生什么，只能把
250 字写成一个自洽的小故事，于是十段各自闭合、跨段因果为零——读起来像十篇流水账，
而不是一天。改成一次调用之后，写手看得见全天骨架，可以让一件事跨几段发展再收。

失败重试的粒度仍然是**一段**：不合格的段走 ``rewrite_segment`` 定向重写，
把全天正文和脉络给模型，只让它重写那一段。整天重来太贵，也没必要。

字段分工（4.3）：
- ``story``            → 事实层工具结果，被问才吐，可以有活动名词
- ``mood``             → planner 注入，第一轮随 story 生成
- ``topic``            → replyer 常驻注入（C），第二轮提炼，说出口后撤掉
- ``manner``           → replyer 常驻注入（A），第三轮逐时段从 story + mood 生成
- ``outline``（脉络）  → 模型动笔前给自己写的规划，落进 ``days.outline``，不进任何注入

第二轮见 ``extract_round2``：从 story 里抽地点时段轴和谈资，全天十段一次调用，用便宜模型。
第三轮见 ``generate_expression``：每个时段单独生成表达方式，失败只影响本段 manner。
"""

from dataclasses import dataclass
from datetime import date
from typing import Any

import json
import logging
import re
import time

from . import prompts
from .negative_events import LEVEL_HINTS
from .schedule_store import (
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

# 给模型看的文字全在 ``prompts/`` 下，一次请求一个文件：``day.prompt``（全天生成）、
# ``rewrite.prompt``（定向重写）、``round2.prompt``（第二轮抽取）、
# ``expression.prompt``（第三轮逐时段表达方式）。
# 为什么这么配、哪些东西**不能**写进去，见 ``prompts/README.md``——那些理由不能放在
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
class GenerationOutcome:
    """一段的生成结果。失败时 ``state`` 是底稿。"""

    segment: Segment
    state: SegmentState
    ok: bool
    reason: str = ""
    # 调用层面就失败了（模型不可用、鉴权错、超时），不是模型写得不合格。
    # 这种错重试一万次也一样，整批应当立刻中止。
    fatal: bool = False


@dataclass
class DayOutcome:
    """一次全天调用的结果。

    ``states`` 只放**通过校验**的段；没通过的进 ``failures``，由调用方逐段
    走 ``rewrite_segment``。一段不合格不牵连别的段——这是把重试粒度留在段上的关键。
    """

    outline: str
    states: dict[str, SegmentState]
    failures: list[tuple[str, str]]
    ok: bool
    reason: str = ""
    fatal: bool = False


@dataclass
class ExpressionOutcome:
    """第三轮单个时段的表达方式结果。"""

    manner: str
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
        base_task: str,
        temperature: float,
    ) -> None:
        self._ctx = ctx
        self._store = store
        self._preview = preview
        self._model = model
        self._topic_model = topic_model
        self._base_task = base_task
        self._temperature = temperature

    async def _call_llm(self, prompt: str, *, model: str, temperature: float) -> dict[str, Any]:
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
        if not text.strip():
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
        return {"success": True, "response": text, "model": result.model_name or model, **usage}

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
        """一次调用生成全天的脉络和每一段的 story / mood。

        **校验按段做。** 某一段不合格只把它记进 ``failures``，其余段照常收下——
        调用方拿 ``failures`` 逐段走 ``rewrite_segment``，不用整天重来。

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
        result = await self._call_llm(prompt, model=self._model, temperature=self._temperature)
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

        outline, parsed = _parse_day(str(result.get("response") or ""), segments)
        if not parsed:
            _logger.error("[生成] %s 输出里找不到任何时段分节", day)
            return DayOutcome(
                outline=outline, states={}, failures=[], ok=False, reason="输出里找不到任何时段分节"
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

        _logger.info(
            "[生成] %s 全天 %d 段，合格 %d，待重写 %d", day, len(segments), len(states), len(failures)
        )
        return DayOutcome(outline=outline, states=states, failures=failures, ok=True)

    # ── 定向重写 ──
    async def rewrite_segment(
        self,
        *,
        day: date,
        segment: Segment,
        segments: list[Segment],
        outline: str,
        states: dict[str, SegmentState],
        weather: str,
        holiday: str,
        negative_level: str,
        defect: str = "",
    ) -> GenerationOutcome:
        """只重写一段，前后文照给。

        全天调用里某段不合格时用它补，``/status regen`` 和 ``/status next`` 也走这条。
        比早先的「整段从头生成」信息多：模型看得到脉络和前后段，改出来的东西接得上。

        Args:
            defect: 校验报出来的不合格原因。原样带给模型——它比任何泛泛的
                「写好一点」都具体。为空表示不是因为不合格，是人工要求重写。
        """
        prompt = self._build_rewrite_prompt(
            day=day,
            segment=segment,
            segments=segments,
            outline=outline,
            states=states,
            weather=weather,
            holiday=holiday,
            negative_level=negative_level,
            defect=defect,
        )

        index = segments.index(segment) if segment in segments else -1
        previous_story = ""
        if index > 0:
            previous_state = states.get(segments[index - 1].slot)
            previous_story = previous_state.story if previous_state else ""

        last_reason = ""
        for attempt in range(2):
            result = await self._call_llm(prompt, model=self._model, temperature=self._temperature)
            self._record_preview(
                request_kind="schedule_rewrite",
                prompt=prompt,
                result=result,
                selection_reason=f"{day.isoformat()} {segment.slot} {segment.title}"
                + (f"（{defect}）" if defect else "")
                + (f"　第 {attempt + 1} 次" if attempt else ""),
                output_title="定向重写",
            )
            if not result.get("success", True):
                reason = str(result.get("error") or result.get("response") or "").strip() or "模型调用失败"
                _logger.error("[重写] %s %s 调用失败：%s", day, segment.slot, reason)
                return GenerationOutcome(
                    segment=segment,
                    state=self._store.fallback_for(segment),
                    ok=False,
                    reason=reason,
                    fatal=True,
                )

            payload = _extract_sections(str(result.get("response") or ""), ("story", "mood"))
            if payload is None:
                last_reason = "输出里找不到 ### 分节"
            else:
                state = SegmentState(
                    story=str(payload.get("story") or "").strip(),
                    manner="",
                    mood=str(payload.get("mood") or "").strip(),
                )
                last_reason = self._validate_state(state, segment, previous_story)
                if not last_reason:
                    return GenerationOutcome(segment=segment, state=state, ok=True)
            _logger.warning(
                "[重写] %s %s 第 %d 次不合格：%s", day, segment.slot, attempt + 1, last_reason
            )

        return GenerationOutcome(
            segment=segment,
            state=self._store.fallback_for(segment),
            ok=False,
            reason=last_reason,
        )

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
        """拼装全天生成 prompt（``prompts/day.prompt``）。"""
        previous_block = ""
        if previous is not None:
            prev_segment, prev_state = previous
            previous_block = prompts.render(
                "day",
                "previous",
                slot=prev_segment.slot,
                title=prev_segment.title,
                story=prev_state.story,
                mood=prev_state.mood,
            )

        marked = "\n".join(
            prompts.render(
                "day",
                "negative_item",
                slot=segment.slot,
                hint=LEVEL_HINTS.get(negative_levels[segment.slot], negative_levels[segment.slot]),
            )
            for segment in segments
            if negative_levels.get(segment.slot)
        )

        return prompts.render(
            "day",
            date=day.isoformat(),
            weekday=weekday_name(day),
            holiday=f"　{holiday}" if holiday else "",
            weather=prompts.render("day", "weather", weather=weather) if weather else "",
            previous=previous_block,
            skeleton=self._skeleton_block("day", day, segments),
            negative=prompts.render("day", "negative", marked=marked) if marked else "",
            # 只示范前两段，剩下的让它照推
            output_sample="\n\n".join(
                prompts.render("day", "output_sample_item", slot=segment.slot)
                for segment in segments[:2]
            ),
        )

    def _build_rewrite_prompt(
        self,
        *,
        day: date,
        segment: Segment,
        segments: list[Segment],
        outline: str,
        states: dict[str, SegmentState],
        weather: str,
        holiday: str,
        negative_level: str,
        defect: str,
    ) -> str:
        """拼装定向重写 prompt（``prompts/rewrite.prompt``）：全天骨架 + 脉络 + 前后段原文。"""
        index = segments.index(segment) if segment in segments else -1

        def neighbour(label: str, offset: int) -> str:
            other = segments[index + offset] if 0 <= index + offset < len(segments) else None
            if other is None:
                return ""
            state = states.get(other.slot)
            if state is None or not state.story:
                return ""
            return prompts.render(
                "rewrite", "neighbour", label=label, slot=other.slot, title=other.title, story=state.story
            )

        current = states.get(segment.slot)
        return prompts.render(
            "rewrite",
            date=day.isoformat(),
            weekday=weekday_name(day),
            holiday=f"　{holiday}" if holiday else "",
            weather=prompts.render("rewrite", "weather", weather=weather) if weather else "",
            skeleton=self._skeleton_block("rewrite", day, segments),
            outline=prompts.render("rewrite", "outline", outline=outline) if outline else "",
            previous_neighbour=neighbour("上一段", -1),
            next_neighbour=neighbour("下一段", 1),
            current=(
                prompts.render("rewrite", "current", story=current.story)
                if current is not None and current.story
                else ""
            ),
            defect=prompts.render("rewrite", "defect", defect=defect) if defect else "",
            negative=(
                prompts.render(
                    "rewrite", "negative", hint=LEVEL_HINTS.get(negative_level, negative_level)
                )
                if negative_level
                else ""
            ),
            slot=segment.slot,
            title=segment.title,
            place=segment.place,
            outfit=segment.outfit,
            company=segment.company,
            kind=segment.kind,
        )

    def _skeleton_block(self, name: str, day: date, segments: list[Segment]) -> str:
        """全天骨架清单。清醒/工作小时数逐段给，写手才知道这会儿该有多累。

        Args:
            name: 用哪个文件里的片段——``day`` 和 ``rewrite`` 各存一份，见
                ``prompts/README.md`` 里「共享文字是抄的」那一节。
        """
        lines = []
        for index, segment in enumerate(segments, 1):
            awake, work = self._store.awake_and_work_hours(day, segment)
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
                    awake=(
                        ""
                        if segment.kind == "睡眠"
                        else prompts.render(name, "skeleton_awake", awake=awake, work=work)
                    ),
                )
            )
        return "\n".join(lines)

    def _validate_state(self, state: SegmentState, segment: Segment, previous_story: str) -> str:
        """校验第一轮的一段 story / mood，返回不合格原因；空串表示通过。

        硬校验（不过就重写）：缺字段，mood 含地点/服装/场所词，首句与上一段雷同。
        只告警不重写：story 字数越界——它是被问才吐的事实层，长一点没有副作用，
        为字数烧一次调用不值得。
        """
        missing = [name for name in ("story", "mood") if not getattr(state, name)]
        if missing:
            return f"缺字段：{'/'.join(missing)}"

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

    # ── 第三轮：逐时段表达方式 ──
    async def generate_expression(
        self,
        day: date,
        segment: Segment,
        state: SegmentState,
    ) -> ExpressionOutcome:
        """根据一个时段已经生成的 story + mood，单独生成 replyer 表达方式。

        每个时段是一次独立调用。校验失败只重试本时段，不回头改 story / mood；
        调用层失败立即返回 fatal，让调用方停止继续烧同类请求并保留旧值或底稿。
        """
        prompt = prompts.render(
            "expression",
            slot=segment.slot,
            story=state.story,
            mood=state.mood,
        )
        last_reason = ""
        for attempt in range(2):
            result = await self._call_llm(
                prompt,
                model=self._model,
                temperature=self._temperature,
            )
            self._record_preview(
                request_kind="schedule_expression",
                prompt=prompt,
                result=result,
                selection_reason=(
                    f"{day.isoformat()} {segment.slot} 表达方式"
                    + (f"　第 {attempt + 1} 次" if attempt else "")
                ),
                output_title="时段表达方式",
            )
            if not result.get("success", True):
                reason = str(result.get("error") or result.get("response") or "").strip()
                return ExpressionOutcome(
                    manner="",
                    ok=False,
                    reason=reason or "模型调用失败",
                    fatal=True,
                )

            manner = _clean_expression(str(result.get("response") or ""))
            last_reason = self._validate_expression(manner, segment)
            if not last_reason:
                return ExpressionOutcome(manner=manner, ok=True)
            _logger.warning(
                "[第三轮] %s %s 第 %d 次不合格：%s",
                day,
                segment.slot,
                attempt + 1,
                last_reason,
            )

        return ExpressionOutcome(manner="", ok=False, reason=last_reason)

    @staticmethod
    def _validate_expression(manner: str, segment: Segment) -> str:
        if not manner:
            return "表达方式为空"
        if len(manner) > 60:
            return f"表达方式超长（{len(manner)} 字）"
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
        - ``topic`` / ``topic_keys``：一句值得说给人听的小事。
          事实层有个先天缺陷——几乎没人会主动问「你在干嘛」，工具不被调用 story 就用不上；
          谈资给她一个主动出口。

        不并进主生成，因为两件事性质不同（创作 vs 抽取），抽取用便宜模型就够。
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
        result = await self._call_llm(prompt, model=self._topic_model, temperature=0.4)
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

        rows = _extract_json_array(str(result.get("response") or ""))
        if rows is None:
            _logger.error("[第二轮] %s 输出不是合法 JSON 数组", day)
            return dict(empty, total=len(candidates)), "输出不是合法 JSON 数组"

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
            raw_keys = row.get("topic_keys")
            keys = [str(k).strip() for k in raw_keys if str(k).strip()] if isinstance(raw_keys, list) else []
            if not keys:
                # 没有关键词就检测不了说没说过，那这条谈资会一直挂着——宁可不要
                _logger.warning("[第二轮] %s %s 没给关键词，丢弃 topic", day, slot)
                continue
            states[slot].topic = topic
            states[slot].topic_keys = keys
            produced += 1

        _logger.info(
            "[第二轮] %s 地点 %d/%d 段，话题 %d 条", day, placed, len(candidates), produced
        )
        return {"topics": produced, "places": placed, "total": len(candidates)}, ""

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
        """拼装第二轮 prompt（``prompts/round2.prompt``）：地点时段轴 + 话题提炼（设计文档 5.10）。"""
        return prompts.render(
            "round2",
            date=day.isoformat(),
            weekday=weekday_name(day),
            listing="\n\n".join(
                prompts.render(
                    "round2",
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


def _parse_day(text: str, segments: list[Segment]) -> tuple[str, dict[str, SegmentState]]:
    """从全天输出里拆出脉络和每段的 story / mood。

    只认骨架里存在的 slot：模型偶尔会自己多写一段或把时间写错，那种段直接丢掉，
    对应的骨架段会落进 ``failures`` 走重写，比悄悄收下一段对不上号的内容安全。
    """
    outline = ""
    match = re.search(r"^###\s*脉络\s*$(.*?)(?=^##\s|\Z)", text, re.M | re.S)
    if match:
        outline = match.group(1).strip()

    valid = {segment.slot for segment in segments}
    states: dict[str, SegmentState] = {}
    for match in re.finditer(
        r"^##\s*(\d{1,2}:\d{2}-\d{1,2}:\d{2})\s*$(.*?)(?=^##\s+\d{1,2}:|\Z)", text, re.M | re.S
    ):
        slot = match.group(1)
        if slot not in valid or slot in states:
            continue
        payload = _extract_sections(match.group(2), ("story", "mood"))
        if payload is None:
            continue
        states[slot] = SegmentState(
            story=str(payload.get("story") or "").strip(),
            manner="",
            mood=str(payload.get("mood") or "").strip(),
        )
    return outline, states


def _extract_sections(text: str, names: tuple[str, ...]) -> dict[str, str] | None:
    """从「### 字段名」分节的输出里取各节正文。

    **为什么不用 JSON。** story 是 200~300 字的自由散文，而我们要求它当成小说写——
    小说必然有对话，对话必然有引号。把这种文本塞进 JSON 字符串，任何一个未转义的
    半角引号都会毁掉整个对象。实测 134 次时段生成里失败 12 次（8%），其中 10 次
    就是正文里的半角引号。那不是模型写坏了，是我们让它用错了格式。

    分节文本零转义，引号、换行、反斜杠都不再是问题。
    第二轮的 places / topic 仍然用 JSON——那边是数组套对象、字段短，JSON 是对的格式。

    解析尽量宽容：``### story``、``###story``、``### story：`` 都认，
    行首以外的 ``###`` 不当分节。缺任何一节返回 ``None``。
    """
    pattern = re.compile(r"^[ \t]*#{2,4}[ \t]*(" + "|".join(names) + r")[ \t]*[:：]?[ \t]*$", re.M | re.I)
    marks = list(pattern.finditer(text))
    if not marks:
        return None

    sections: dict[str, str] = {}
    for index, mark in enumerate(marks):
        end = marks[index + 1].start() if index + 1 < len(marks) else len(text)
        # 同一节出现两次时以第一次为准，后面的多半是模型自我重复
        sections.setdefault(mark.group(1).lower(), text[mark.end():end].strip())
    if any(name not in sections for name in names):
        return None
    return sections


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
