"""时段状态生成。

每天 12:00（JST）生成**次日**全天，**一次调用出十来段**。

早先是一段一次调用、顺序执行。那样每段的写手都看不到后面会发生什么，只能把
250 字写成一个自洽的小故事，于是十段各自闭合、跨段因果为零——读起来像十篇流水账，
而不是一天。改成一次调用之后，写手看得见全天骨架，可以让一件事跨几段发展再收。

失败重试的粒度仍然是**一段**：不合格的段走 ``rewrite_segment`` 定向重写，
把全天正文和脉络给模型，只让它重写那一段。整天重来太贵，也没必要。

字段分工（4.3）：
- ``story``            → 事实层工具结果，被问才吐，可以有活动名词
- ``manner``           → replyer 常驻注入（A），**一个具体名词都不能有**
- ``mood``            → planner 注入，同样不含活动名词
- ``topic``            → replyer 常驻注入（C），第二轮提炼，说出口后撤掉
- ``outline``（脉络）  → 模型动笔前给自己写的规划，落进 ``days.outline``，不进任何注入

第二轮见 ``extract_round2``：从 story 里抽地点时段轴和谈资，全天十段一次调用，用便宜模型。
"""

from dataclasses import dataclass
from datetime import date
from typing import Any

import json
import logging
import re

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

# 人设固定文本，只保留写 story 用得上的几个支点，外加一句性格概述。
#
# 为什么不用人设档案全文：那些章节讲的是"她怎么做人"，而这里要的是
# "她此刻在做什么"。喂进去模型会往人格演绎上使劲，story 里就会冒出心理旁白，
# 把"发生了一件事"稀释成一段内心戏。
#
# 但**一句话的性格概述要留**（末行那句调色盘）。实测拿掉它，模型会退回通用妹系模板：
# 「哼了一声」「顺走一块马卡龙也毫不心虚」「带着点撒娇的劲儿」，一天里冒出六处。
# 概述是索引，不是演绎材料，一行的代价换掉整套模板很划算。
#
# 反过来，**别把档案的行为清单整段搬进来**。试过一版 24 行的（做事方式 / 和身边人 /
# 情绪起落），确实压住了模板，但把正文字数从 279 挤到 207，十段里四段跌破 200 字下限。
# prompt 的行数和正文的篇幅是在抢同一份预算。
# 为什么是第三人称：这个 prompt 是对"写手"说话，不是对 Mittes 说话；
# 第二人称的"你"会和 manner 要求输出的"你"撞车。
#
# **不写"猫耳女仆装"。** 人设档案 6.4 写明猫耳、猫尾、铃铛都是**道具**，
# 猫耳只是偶尔在店里戴。写进这里的话，story 会天天给她戴上——
# 这跟旧骨架把 outfit 写成"猫耳女仆装"是同一个毛病，只是换了个地方。
# 穿什么由骨架的 outfit 逐段给，不在这里定。
PERSONA = """Mittes，16 岁的女子高中生，和店长 Sweyn 一同住在东京新宿区一家咖啡店的楼上。
银白色长发，发梢过渡成浅樱粉，红眼睛，身量娇小。
店长 Sweyn，是她最亲近的哥哥。
她课余在楼下店里帮忙；喜欢二次元，也做 Cos，接少量平面模特的拍摄。
Mittes的性格就像调色盘：天然あざとさ是Mittes的底色，得体从容是她的主色调，对店长Sweyn近乎执念的兄控情结是隐藏辅色。由这些性格衍生组合而成的，才是活生生的Mittes。"""

# 脉络：动笔前先规划，和正文同在一次调用里产出。
#
# 一次调用不等于一次思考——模型仍然是逐字往下写的，直接从第一段落笔时它并不知道
# 傍晚会发生什么，和逐段生成的困境是同一个。先把全局决策落成文字，正文才有依据。
#
# 它**不跨调用传递**（定向重写除外），所以不构成「示范」：它是模型给自己写的备忘，
# 写完自己用掉。真要拆成两次调用喂给十段，那就等于给每段打了样，另说。
_OUTLINE_RULES = """## 第一步：脉络
三到五行，写清楚今天有哪 1~2 件事是跨段的：在哪一段起头、哪几段推进、哪一段收，
或者干脆不收、留到明天。除此之外的时段各自独立就好——一天里大部分时候是没有情节的，
不要给每段都安排事情。

这一节只写事情和它的走向，**不写措辞、不写细节、不写情绪词**。它是你给自己的规划，
不是正文的草稿。"""

_STORY_RULES = """## story —— 这段时间里发生了什么
- **第三人称叙述，主语是 Mittes**，每段 200~300 字，首句里要出现「Mittes」这个名字
- 当成一段小说来写，这段时间里真的发生了一件事——是日常里的小事，不是戏剧性的大事
- 必须和骨架的地点、服装、同处一致；同处里有人时，让这个人真的参与进事情里
- **只有脉络里登记的线索可以跨段。** 没登记的物件、话头，一两段之内就该退场
- **大多数段不要收尾。** 时间到了就断在半路，让下一段接着往下写。一整天里只有最后一段是真的结束
- **叙述者不要跳出角色去解释动作的用意。** 动作写出来就够了，后面别再补一句它意味着什么
- **每段结尾不要用总结性的感悟或升华。** 时间到了就停，不替这一段做小结
- **情绪写成行为，不写成形容词"""

_MANNER_RULES = """## manner —— 这段时间她说话是什么样
**这段文字会出现在她每一条回复的上下文里，所以规则很严：**
- 第二人称"你"，两句以内，50 字以内
- 写她现在的状态，以及这会怎么影响她说话
- **禁止出现任何具体事项的名词**：地点、服装、物件、动作都不行
  - ✗ 你端了一晚上盘子，脚很酸
  - ✓ 你忙了一晚上，脚有点酸
  - ✗ 你正在更衣室换衣服，心思在待会儿的班上
  - ✓ 你心思有一半已经在待会儿的事上
  - ✗ 你上了一晚上的班，这会儿刚回到家
  - ✓ 你刚从一段很满的时间里退出来，整个人还没落地
  - 抽象的强度可以写（"忙了一晚上"），可辨认的具体事不行（"端盘子"）；**"班""店里""学校""家"这类身份/场所词同样算具体事**"""

_MOOD_RULES = """## mood —— 一个短语，十字以内，写情绪不写事由
- **默认是正向的。** 她的底色是明亮的，多数时段应该落在"平静满足"到"雀跃"之间。只有当这个时段真的发生了不愉快的事，才写负面情绪——不要为了显得有起伏而无端低落
- 正向也有层次，和上一个时段的强度拉开一点
- **用词必须具体，写出这份情绪的质地，禁止一般化的情绪词**
  - ✗ 开心 / 心情不错 / 有点烦 / 难过
- **但具体只能落在情绪上，不能落在事由上**——这条和上一条同等重要：
  - ✗ 手心发痒想画画　（"画画"是事由）
  - ✗ 因为被夸而高兴　（"被夸"是事由）
  - ✓ 小鹿乱撞（写出了是哪种心动，但看不出她在看什么）
- 如果上一个时段有不愉快，这一段的恢复要有过程，不要一到点就完全没事了"""

_PERSON_TABLE = """# 人称
下面几个字段的人称各不相同，别串：

| 字段 | 人称 | 说的是谁、对谁说 |
|---|---|---|
| story | 第三人称 | 讲 Mittes 的事，讲给读者听 |
| manner | 第二人称「你」 | 直接对 Mittes 说话 |
| mood | 不带人称 | 对她当前情绪的客观描述 |"""

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

        text = str(getattr(result, "response", "") or "")
        if not text.strip():
            # 空响应最常见的成因不是模型罢工，而是**推理把 max_tokens 吃光了**：
            # 推理模型的 reasoning 和正文共用这一个预算，想得太久就一个字正文都吐不出来，
            # 而 status 仍是 completed，主程序不当失败。实测 glm-5.2 跑全天话题提炼时
            # 4000 token 全烧在 reasoning 上。这两种情况要分开报，否则排查会走偏。
            used = int(getattr(result, "completion_tokens", 0) or 0)
            if used >= _MAX_TOKENS:
                reason = f"输出被 max_tokens 截断：{used}/{_MAX_TOKENS} token 全部用于推理，未产出正文"
            else:
                reason = f"模型返回空响应（completion_tokens={used}）"
            return {"success": False, "error": reason, "response": "", "model": model}
        return {"success": True, "response": text, "model": getattr(result, "model_name", model)}

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
        """一次调用生成全天的脉络和每一段的三个字段。

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

            payload = _extract_sections(str(result.get("response") or ""), ("story", "manner", "mood"))
            if payload is None:
                last_reason = "输出里找不到 ### 分节"
            else:
                state = SegmentState(
                    story=str(payload.get("story") or "").strip(),
                    manner=str(payload.get("manner") or "").strip(),
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
        """拼装全天生成 prompt。"""
        blocks = [
            "你在为一个虚拟角色生成她一整天的状态。",
            f"# 角色\n{PERSONA}",
            f"# 今天\n{day.isoformat()} 星期{weekday_name(day)}"
            + (f"　{holiday}" if holiday else "")
            + (f"\n天气：{weather}" if weather else ""),
        ]

        if previous is not None:
            prev_segment, prev_state = previous
            blocks.append(
                "# 昨天最后一段（承接用）\n"
                f"{prev_segment.slot}　{prev_segment.title}\n"
                f"{prev_state.story}\n"
                f"当时状态：{prev_state.mood}"
            )

        blocks.append(
            "# 今天的骨架（不可改动，一段都不能增删或合并）\n" + self._skeleton_block(day, segments)
        )

        marked = [
            f"- {segment.slot}　{LEVEL_HINTS.get(negative_levels[segment.slot], negative_levels[segment.slot])}"
            for segment in segments
            if negative_levels.get(segment.slot)
        ]
        if marked:
            blocks.append(
                "# 这几段里要发生不顺心的事\n"
                + "\n".join(marked)
                + "\n自己想是什么事。规模要小，**不要写重大事件**："
                "生病、受伤、和人吵架、丢掉要紧的东西，都不行——"
                "这套设定扛不住跨天的情绪线。"
            )

        blocks.append(_PERSON_TABLE)
        blocks.append("# 要做的事\n\n" + _OUTLINE_RULES)
        blocks.append(
            "## 第二步：为每一段生成三个字段\n\n"
            + "\n\n".join([_STORY_RULES, _MANNER_RULES, _MOOD_RULES])
        )
        blocks.append(_output_block(segments))
        return "\n\n".join(blocks)

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
        """拼装定向重写 prompt：全天骨架 + 脉络 + 前后段原文。"""
        index = segments.index(segment) if segment in segments else -1
        blocks = [
            f"下面是一份已经写好的日程，其中 {segment.slot} 这一段要重写。别的段不用动，也不要输出。",
            f"# 角色\n{PERSONA}",
            f"# 今天\n{day.isoformat()} 星期{weekday_name(day)}"
            + (f"　{holiday}" if holiday else "")
            + (f"\n天气：{weather}" if weather else ""),
            "# 今天的骨架（不可改动）\n" + self._skeleton_block(day, segments),
        ]
        if outline:
            blocks.append("# 今天的脉络\n" + outline)

        for label, offset in (("上一段", -1), ("下一段", 1)):
            neighbour = segments[index + offset] if 0 <= index + offset < len(segments) else None
            state = states.get(neighbour.slot) if neighbour else None
            if neighbour is not None and state is not None and state.story:
                blocks.append(f"# {label}（{neighbour.slot}　{neighbour.title}）\n{state.story}")

        current = states.get(segment.slot)
        if current is not None and current.story:
            blocks.append("# 这一段现在的写法（要替换掉）\n" + current.story)
        if defect:
            blocks.append(f"# 为什么要重写\n{defect}")
        if negative_level:
            blocks.append(
                "# 这一段里要发生一件不顺心的事\n"
                f"强度：{LEVEL_HINTS.get(negative_level, negative_level)}\n"
                "自己想一件事。规模要小，**不要写重大事件**。"
            )

        blocks.append(_PERSON_TABLE)
        blocks.append(
            "# 重写这一段\n"
            f"时间：{segment.slot}　名称：{segment.title}\n"
            f"地点：{segment.place}｜服装：{segment.outfit}｜同处：{segment.company}｜性质：{segment.kind}\n\n"
            + "\n\n".join([_STORY_RULES, _MANNER_RULES, _MOOD_RULES])
        )
        blocks.append(
            "# 输出\n"
            "只输出这一段的三个小节，每节用一行「### 字段名」开头。\n\n"
            "### story\n（正文）\n\n### manner\n（正文）\n\n### mood\n（正文）"
        )
        return "\n\n".join(blocks)

    def _skeleton_block(self, day: date, segments: list[Segment]) -> str:
        """全天骨架清单。清醒/工作小时数逐段给，写手才知道这会儿该有多累。"""
        lines = []
        for index, segment in enumerate(segments, 1):
            awake, work = self._store.awake_and_work_hours(day, segment)
            line = (
                f"{index}. {segment.slot}　{segment.title}\n"
                f"　　地点：{segment.place}｜服装：{segment.outfit}｜"
                f"同处：{segment.company}｜性质：{segment.kind}"
            )
            if segment.kind != "睡眠":
                line += f"\n　　到这一段为止：自上次睡眠起已清醒 {awake} 小时，其中工作 {work} 小时"
            lines.append(line)
        return "\n".join(lines)

    def _validate_state(self, state: SegmentState, segment: Segment, previous_story: str) -> str:
        """校验一段的三个字段，返回不合格原因；空串表示通过（设计文档 5.7）。

        硬校验（不过就重写）：缺字段，manner 超长，manner / mood 含地点/服装/场所词，
        首句与上一段雷同。
        只告警不重写：story 字数越界——它是被问才吐的事实层，长一点没有副作用，
        为字数烧一次调用不值得。
        """
        missing = [name for name in ("story", "manner", "mood") if not getattr(state, name)]
        if missing:
            return f"缺字段：{'/'.join(missing)}"

        if len(state.manner) > 60:
            return f"manner 超长（{len(state.manner)} 字）"

        # manner 进 replyer、mood 进 planner，两个都是常驻注入，
        # 所以禁名词这条纪律对两个都成立。
        for name in ("manner", "mood"):
            hit = _find_banned_word(getattr(state, name), segment)
            if hit:
                return f"{name} 含具体事项名词「{hit}」"

        # 首句跟上一段雷同 = 模型把某个句子当模板锁死了。
        # 实测发生过：prompt 里演示「首句要出现 Mittes」的那个例句被原样照抄，
        # 一路滚了 13 段。这类锁死光靠 prompt 措辞挡不住，得在这里拦。
        if previous_story and state.story[:15] == previous_story[:15]:
            return f"首句与上一段雷同（{state.story[:15]}…）"

        if not 150 <= len(state.story) <= 380:
            _logger.warning("[生成] %s story 字数 %d 越界，仅告警", segment.slot, len(state.story))

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
        """拼装第二轮 prompt：地点时段轴 + 话题提炼（设计文档 5.10）。"""
        listing = "\n\n".join(
            f"{index + 1}. {segment.slot}　{segment.title}\n{states[segment.slot].story}"
            for index, segment in enumerate(segments)
        )
        return f"""你要从一份日程里抽两样东西：她每一段待过哪些地方，以及这一段有没有值得说给人听的事。

# 角色
{PERSONA}

# 今天
{day.isoformat()} 星期{weekday_name(day)}

# 各时段
{listing}

# 要做的事
为上面每一段排出 places，并判断有没有一件事是她之后跟人聊天时会顺口提起的——
有就写一句 topic，没有就把 topic 留成空字符串。

## places 怎么写
- 从 story 里看出她这段时间待过哪些地方，按先后排成一条时段轴，地点用 story 里的说法
- 每段从几点到几点由你定
- 第一条从这个时段的开始时刻起，最后一条到结束时刻止，相邻两条首尾相接
- 从头到尾没挪过地方就只写一条，占满整段

## topic 怎么写
- **是从 story 里挑一件事，不是把 story 概括一遍**。一段 story 里可能有好几件事在发生，
  挑出其中最像会被人接话的那一件，照它原本的样子写；
  其余的不用带上，也不要为了都装进去而压缩
- **人称跟 story 一致**：第三人称，主语是 Mittes
- 50~80 字，要有**起因 → 发生了什么 → 结果或转折**
  - ✗ Sweyn 给了根皮筋（压成名词短语，看不出为什么、后来怎样）
  - ✓ Mittes 临营业前发现围裙带子开了线，越扯越松，翻遍抽屉只找出一枚别针，她把断口折进去别住，蝴蝶结往那边挪半寸盖着
- **同一天的几个话题不要都围着同一样东西转**。一件小物件贯穿全天是 story 的事，
  拿出来当谈资会变成把同一件事讲三遍——只在最有意思的那一段留话题

## topic_keys 怎么写
- 2~4 个词，用来事后检测她有没有把这件事说出来
- 挑这件事**独有**的名词，不要用通用词（今天、客人、店里、时候）——
  通用词会让检测在她根本没提这件事的时候误判
- topic 为空时，keys 也给空数组

# 输出
严格的 JSON 数组，不要包在代码块里。一段一个对象，slot 必须和上面完全一致。
没什么好说的那几段也要出现在数组里，只是 topic 给空字符串：
[{{"slot":"21:30-23:00",
  "places":[{{"from":"21:30","to":"22:00","place":"KTV 洗手间"}},
            {{"from":"22:00","to":"22:45","place":"街区夜路"}},
            {{"from":"22:45","to":"23:00","place":"家门口"}}],
  "topic":"…","topic_keys":["…","…"]}}]"""

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


def _output_block(segments: list[Segment]) -> str:
    """输出格式说明。只示范前两段，剩下的让它照推。"""
    sample = "\n\n".join(
        f"## {segment.slot}\n### story\n（正文）\n### manner\n（正文）\n### mood\n（正文）"
        for segment in segments[:2]
    )
    return (
        "# 输出\n"
        "先一节脉络，再按骨架顺序写完每一段，一段都不能少，时间必须和骨架完全一致。\n"
        "正文里想用引号、破折号、换行都可以，不用管转义。\n\n"
        "### 脉络\n（正文）\n\n" + sample + "\n\n（……以此类推，直到最后一段）"
    )


def _parse_day(text: str, segments: list[Segment]) -> tuple[str, dict[str, SegmentState]]:
    """从全天输出里拆出脉络和每段的三个字段。

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
        payload = _extract_sections(match.group(2), ("story", "manner", "mood"))
        if payload is None:
            continue
        states[slot] = SegmentState(
            story=str(payload.get("story") or "").strip(),
            manner=str(payload.get("manner") or "").strip(),
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
