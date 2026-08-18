"""时段状态生成。

每天 12:00（JST）批量生成**次日**全部时段（见设计文档 5.3）。
批量指的是时机，不是把十段塞进一次调用——批次内部仍然一段一次调用、顺序执行，
前一段的结果喂给后一段：这样 prompt 按单段写就行，输出质量高于一次吐十段，
失败重试的粒度也是一段。

字段分工（4.3）：
- ``story``            → 事实层工具结果，被问才吐，可以有活动名词
- ``manner``           → replyer 常驻注入（A），**一个具体名词都不能有**
- ``mood`` / ``busy``  → planner 状态层，同样不含活动名词
- ``topic``            → replyer 常驻注入（C），第二轮提炼，说出口后撤掉

第二轮的话题提炼见 ``extract_topics``：全天十段一次调用，用便宜模型。
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
    weekday_name,
)


_logger = logging.getLogger("a_day_with_mittes.generator")

# WebUI 推理过程页的分类目录。只开一个——分类多了列表会很碎，
# 两种调用靠 request kind 和输出内容区分就够。
PREVIEW_STAGE = "a_day_with_mittes"
PREVIEW_SESSION = "schedule_batch"

# 人设固定文本，来自 config/bot_config.toml 的 personality 首段，
# 只做了一处改写：第二人称改第三人称。
#
# 为什么不用全文：后面那些性格调色盘讲的是"她怎么做人"，而这里要的是
# "她此刻在做什么、身体是什么状态"。喂进去模型会往人格演绎上使劲，
# story 里就会冒出心理旁白，正好撞上"不写心理总结"那条。
# 为什么必须改人称：这个 prompt 是对"写手"说话，不是对 Mittes 说话；
# 原文的"你"会和 manner 要求输出的"你"撞车。
PERSONA = """Mittes，是 17 岁的女子高中生，和店长 Sweyn 一同住在东京新宿区一家咖啡店的楼上，粉白色长发、红眼睛。
店长 Sweyn——从小照顾她长大的兄长，也是她最亲近的家人。
她课余在楼下店里换上猫耳女仆装帮忙；喜欢二次元，也做 Cos，接少量平面模特的拍摄。"""

_STORY_RULES = """## story —— 这段时间里发生了什么
- **第三人称叙述，主语是 Mittes**，100~150 字
  - 首句里必须出现「Mittes」这个名字，做主语或所有格都行。句式例如
    「Mittes 把……」「Mittes 蹲在……」「Mittes 的……还……」——
    这些只是句式，**首句写什么内容完全由这个时段自己决定**
  - 之后用「她」或承前省略都行，但**不要整段无主语**，也不要每句都念一遍名字
- **重点是故事性**：这段时间里要真的"发生了一件事"——有起因、有推进、有一点小转折，而不是一帧静止的画面
  - ✗ 端着托盘在桌椅间穿行，裙摆随脚步轻轻摇晃（这是一帧画面，没有事发生）
  - ✓ 四号桌那份蛋包饭要画小熊，手一抖把耳朵画歪了半边，正想擦掉重来，客人先笑出声，说这只反而更可爱。后来那一晚照着这只歪耳朵的又画了三份
- 事件的**规模要小**：打翻一杯水、找不到发圈、被熟客认出来、外面突然下雨、手机弹出一条消息。不要写戏剧性的大事
- 时间是一整段不是一个瞬间，可以有"先……后来……"的推进
- 必须和骨架的地点、服装、同处一致；同处里有人时，让这个人真的参与进事情里，不要只当背景
- 具体的物件和身体感受用来撑质感（手心的温度、袜口的勒痕、镜子上的水汽），但**它们是细节不是主体**，不要写成一串静态的身体特写
- 当天的天气、节日能落进去就落
- 要承接上一个时段，但**承接靠物件和动作的延续，不靠心理旁白**
  - **一样东西最多接一两段就该退场**，别让它贯穿一整天、更不能跨天。
    上一段出现过的物件，这一段可以顺手带一下，再往后就该被新的东西替掉
  - ✗ 把刚才看样片的事甩到脑后，开始专心换衣服
  - ✓ 样片还摊在楼上桌上没收，围裙带子已经在腰后绕了两圈
  - ✗ 一晚上的忙碌终于结束，整个人放松下来
  - ✓ 脚踝上被勒出的红痕泡在水里慢慢淡下去
- 不要写心理总结（"她觉得有点累"），只写看得见的东西。**第三人称比无主语更容易滑向心理旁白，这条要格外守住**"""

_MANNER_RULES = """## manner —— 这段时间她说话是什么样
**这段文字会出现在她每一条回复的上下文里，所以规则很严：**
- 第二人称"你"，两句以内，50 字以内
- 前半句写身体或情绪状态，后半句写这会怎么影响她说话
- **禁止出现任何具体事项的名词**：地点、服装、物件、动作都不行
  - ✗ 你端了一晚上盘子，脚很酸
  - ✓ 你忙了一晚上，脚有点酸
  - ✗ 你正在更衣室换衣服，心思在待会儿的班上
  - ✓ 你心思有一半已经在待会儿的事上
  - ✗ 你上了一晚上的班，这会儿刚回到家
  - ✓ 你刚从一段很满的时间里退出来，整个人还没落地
  - 抽象的强度可以写（"忙了一晚上"），可辨认的具体事不行（"端盘子"）；**"班""店里""学校""家"这类身份/场所词同样算具体事**
- 不要写篇幅要求（"少说两句"），那是另一个字段的事
- 不要解释她为什么这样"""

_MOOD_RULES = """## mood —— 一个短语，十字以内，写情绪不写事由
- **默认是正向的。** 她的底色是明亮的，多数时段应该落在"平静满足"到"雀跃"之间。只有当这个时段真的发生了不愉快的事，才写负面情绪——不要为了显得有起伏而无端低落
- 正向也有层次，不要一整天都在雀跃：踏实、懒洋洋、心里冒泡、跃跃欲试、被逗乐，都是正向
- **用词必须具体，禁止一般化的情绪词。** 要写出这份情绪的**质地**
  - ✗ 开心　　　　　　✓ 小鹿乱撞
  - ✗ 心情不错　　　　✓ 心里冒泡，想哼歌
  - ✗ 有点烦　　　　　✓ 憋着一股不服气
  - ✗ 难过　　　　　　✓ 蔫着，想找地方躲会儿
- **但具体只能落在情绪上，不能落在事由上**——这条和上一条同等重要：
  - ✗ 手心发痒想画画　（"画画"是事由）
  - ✗ 因为被夸而高兴　（"被夸"是事由）
  - ✗ 为样片被退不开心（"样片"是事由）
  - ✓ 小鹿乱撞（写出了是哪种心动，但看不出她在看什么）
- 如果上一个时段有不愉快，这一段的恢复要有过程，不要一到点就完全没事了

## busy —— 一句话，她现在方不方便搭话、会不会慢
- 40 字以内
- **和 manner 同一条禁令**：只写"腾不腾得开手、回得快不快"，不写她人在哪、在做什么
  - ✗ 独自在路上和浴室里，随时能接话
  - ✓ 独处着，随时能接话，就是回得慢一点
  - ✗ 正在店里忙，回复会慢
  - ✓ 手上一直有活，回复会慢"""

_PERSON_TABLE = """# 人称
下面几个字段的人称各不相同，别串：

| 字段 | 人称 | 说的是谁、对谁说 |
|---|---|---|
| story | 第三人称 | 讲 Mittes 的事，讲给读者听 |
| manner | 第二人称「你」 | 直接对 Mittes 说话 |
| mood / busy | 不带人称 | 对她当前状态的客观描述 |"""

# 常驻注入的三个字段（manner / mood / busy）里出现就判定不合格的
# 通用场所/身份词。骨架的 place / outfit 会另行拆成词一起查。
_BANNED_IN_MANNER = (
    "店里", "店内", "咖啡店", "学校", "教室", "课上", "家里", "回家",
    "上班", "下班", "班上", "打工", "更衣室", "浴室", "卧室", "厨房",
    "露台", "吧台", "摄影棚", "包厢", "女仆装", "制服", "睡裙", "睡衣",
    "路上", "棚里", "镜头", "便利店", "床上", "楼上", "楼下",
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


class SegmentGenerator:
    """负责调 LLM 生成时段状态，以及维护当日概要。"""

    def __init__(
        self,
        ctx: Any,
        store: ScheduleStore,
        preview: Any,
        model: str,
        digest_model: str,
        topic_model: str,
        temperature: float,
        timeout_ms: int,
    ) -> None:
        self._ctx = ctx
        self._store = store
        self._preview = preview
        self._model = model
        self._digest_model = digest_model
        self._topic_model = topic_model
        self._temperature = temperature
        self._timeout_ms = timeout_ms

    async def _call_llm(self, prompt: str, *, model: str, temperature: float) -> dict[str, Any]:
        """调一次 LLM，把异常收敛成 ``success=False`` 的返回。

        两件事必须在这里处理，否则整条批次链路会被打死：

        1. **RPC 超时**。``cap.call`` 的默认超时是 30 秒，而 replyer 那档模型
           动辄 20~40 秒，实测 36.9 秒那次直接抛了 ``RPCError``。
           SDK 的 ``ctx.llm.generate`` 不透传 ``timeout_ms``，所以这里直接调
           ``call_capability``——它是 PluginContext 的公开方法，不算伸手进主程序。
        2. **异常会一路冒泡**。``llm.generate`` 不只是返回 ``success=False``，
           传输层出问题时是直接 raise 的；不收在这里，异常会掀掉 run_batch，
           再掀掉守护协程，之后每日批次就再也不会跑了。
        """
        try:
            result = await self._ctx.call_capability(
                "llm.generate",
                timeout_ms=self._timeout_ms,
                prompt=prompt,
                model=model,
                temperature=temperature,
            )
        except Exception as exc:
            return {"success": False, "error": f"{type(exc).__name__}: {exc}", "response": ""}
        if isinstance(result, dict):
            return result
        return {"success": False, "error": f"返回值不是字典：{type(result).__name__}", "response": ""}

    # ── 单段生成 ──
    async def generate_segment(
        self,
        *,
        day: date,
        segment: Segment,
        weather: str,
        holiday: str,
        previous: tuple[Segment, SegmentState] | None,
        day_digest: str,
        negative_level: str,
    ) -> GenerationOutcome:
        """生成一个时段的五个字段。

        校验不过就重生成一次，仍不过退回底稿——不阻塞，也不静默：
        失败原因会带回 ``GenerationOutcome.reason``，进批次报告。
        """
        prompt = self._build_prompt(
            day=day,
            segment=segment,
            weather=weather,
            holiday=holiday,
            previous=previous,
            day_digest=day_digest,
            negative_level=negative_level,
        )

        last_reason = ""
        for attempt in range(2):
            result = await self._call_llm(prompt, model=self._model, temperature=self._temperature)
            self._record_preview(
                request_kind="schedule_segment",
                prompt=prompt,
                result=result,
                selection_reason=f"{day.isoformat()} {segment.slot} {segment.title}"
                + (f"（负面事件·{negative_level}）" if negative_level else "")
                + (f"　第 {attempt + 1} 次" if attempt else ""),
                output_title="时段状态",
            )

            # 调用本身就没成功（模型不可用、鉴权失败、超时）。这类错重试没有意义，
            # 而且真实原因藏在 response 里——不原样带出来，报告只会显示
            # 「输出不是合法 JSON」，把 404 说成模型不听话。
            if not result.get("success", True):
                reason = str(result.get("error") or result.get("response") or "").strip() or "模型调用失败"
                _logger.error("[生成] %s %s 调用失败：%s", day, segment.slot, reason)
                return GenerationOutcome(
                    segment=segment,
                    state=self._store.fallback_for(segment),
                    ok=False,
                    reason=reason,
                    fatal=True,
                )

            state, reason = self._parse_and_validate(
                result, segment, previous_story=previous[1].story if previous else ""
            )
            if state is not None:
                return GenerationOutcome(segment=segment, state=state, ok=True)
            last_reason = reason
            _logger.warning("[生成] %s %s 第 %d 次不合格：%s", day, segment.slot, attempt + 1, reason)

        return GenerationOutcome(
            segment=segment,
            state=self._store.fallback_for(segment),
            ok=False,
            reason=last_reason,
        )

    def _build_prompt(
        self,
        *,
        day: date,
        segment: Segment,
        weather: str,
        holiday: str,
        previous: tuple[Segment, SegmentState] | None,
        day_digest: str,
        negative_level: str,
    ) -> str:
        """拼装单段生成 prompt（设计文档 5.5）。"""
        awake, work = self._store.awake_and_work_hours(day, segment)

        blocks = [
            "你在为一个虚拟角色生成她接下来这个时段的状态。",
            f"# 角色\n{PERSONA}",
            f"# 今天\n{day.isoformat()} 星期{weekday_name(day)}"
            + (f"　{holiday}" if holiday else "")
            + (f"\n天气：{weather}" if weather else ""),
        ]

        if previous is not None:
            prev_segment, prev_state = previous
            blocks.append(
                "# 上一个时段（承接用）\n"
                f"{prev_segment.slot}　{prev_segment.title}\n"
                f"{prev_state.story}\n"
                f"当时状态：{prev_state.mood}"
            )

        today_lines = []
        if day_digest:
            today_lines.append(day_digest)
        today_lines.append(f"自上次睡眠起已清醒 {awake} 小时，其中工作 {work} 小时")
        blocks.append("# 今天此前\n" + "\n".join(today_lines))

        blocks.append(
            "# 要生成的时段（骨架不可改动）\n"
            f"时间：{segment.slot}\n"
            f"名称：{segment.title}\n"
            f"地点：{segment.place}\n"
            f"服装：{segment.outfit}\n"
            f"同处：{segment.company}\n"
            f"性质：{segment.kind}"
        )

        if negative_level:
            blocks.append(
                "# 这个时段里要发生一件不顺心的事\n"
                f"强度：{LEVEL_HINTS.get(negative_level, negative_level)}\n"
                "自己想一件事，要求：\n"
                "- 必须是在上面这个地点、这身衣服、这些人在场的情况下真的会发生的\n"
                "- 规模要小。**不要写重大事件**：生病、受伤、和人吵架、丢掉要紧的东西，都不行\n"
                "- 避开最容易想到的那几种（打翻杯子、摔一跤、手机没电）\n"
                "把它写成这段时间的主线，别让它显得是突然插进来的。"
            )

        blocks.append(_PERSON_TABLE)
        blocks.append("# 为这个时段生成四个字段\n\n" + "\n\n".join([_STORY_RULES, _MANNER_RULES, _MOOD_RULES]))
        blocks.append(
            "# 输出\n严格的 JSON 对象，字段名照上面写，不要包在代码块里：\n"
            '{"story":"…","manner":"…","mood":"…","busy":"…"}'
        )
        return "\n\n".join(blocks)

    def _parse_and_validate(
        self, result: dict[str, Any], segment: Segment, previous_story: str = ""
    ) -> tuple[SegmentState | None, str]:
        """解析并校验生成结果（设计文档 5.7）。

        硬校验（不过就重来）：manner / mood / busy 含地点/服装/场所词，
        manner 或 busy 超长。
        只告警不重来：story 字数越界——它是被问才吐的事实层，长一点没有副作用，
        为字数烧一次调用不值得。
        """
        raw = str(result.get("response") or "")
        payload = _extract_json_object(raw)
        if payload is None:
            return None, "输出不是合法 JSON"

        state = SegmentState(
            story=str(payload.get("story") or "").strip(),
            manner=str(payload.get("manner") or "").strip(),
            mood=str(payload.get("mood") or "").strip(),
            busy=str(payload.get("busy") or "").strip(),
        )

        missing = [
            name for name in ("story", "manner", "mood", "busy") if not getattr(state, name)
        ]
        if missing:
            return None, f"缺字段：{'/'.join(missing)}"

        if len(state.manner) > 60:
            return None, f"manner 超长（{len(state.manner)} 字）"
        if len(state.busy) > 50:
            return None, f"busy 超长（{len(state.busy)} 字）"

        # manner / mood / busy 三个字段全都进常驻注入（manner 进 replyer，
        # 另两个进 planner 状态层），所以禁名词这条纪律对三个都成立。
        # 只校验 manner 是不够的——实测 busy 写出过「独自在路上和浴室里」。
        for name in ("manner", "mood", "busy"):
            hit = _find_banned_word(getattr(state, name), segment)
            if hit:
                return None, f"{name} 含具体事项名词「{hit}」"

        # 首句跟上一段雷同 = 模型把某个句子当模板锁死了。
        # 实测发生过：prompt 里演示「首句要出现 Mittes」的那个例句被原样照抄，
        # 一路滚了 13 段。这类锁死光靠 prompt 措辞挡不住，得在这里拦。
        if previous_story and state.story[:15] == previous_story[:15]:
            return None, f"首句与上一段雷同（{state.story[:15]}…）"

        if not 80 <= len(state.story) <= 200:
            _logger.warning("[生成] %s story 字数 %d 越界，仅告警", segment.slot, len(state.story))

        return state, ""

    # ── 第二轮：话题提炼 ──
    async def extract_topics(
        self, day: date, segments: list[Segment], states: dict[str, SegmentState]
    ) -> tuple[int, str]:
        """为一整天的 story 提炼谈资，全天十段**一次调用**。

        事实层有个先天缺陷：几乎没人会主动问「你在干嘛」，工具不被调用，story 就用不上。
        所以给她一个主动出口——一句今天遇到的、值得说给人听的事（设计文档 5.10）。

        用的模型和 day_digest 压缩分开配：压缩只要能概括，提炼要能看出哪件事值得讲、
        并把前因后果说清楚，对模型的要求实际上更高。

        不并进主生成，因为两件事性质不同（创作 vs 抽取），而且抽取用便宜模型就够。
        一次看到全天，还能避免十个话题都是同一类。

        直接把结果写进 ``states`` 里对应的 ``topic`` / ``topic_keys``。

        Returns:
            tuple[int, str]: (产出话题的段数, 失败原因；成功时为空串)
        """
        # 睡眠段按 kind 硬拦，连判断都省掉
        candidates = [
            segment
            for segment in segments
            if segment.kind != "睡眠" and states.get(segment.slot, None) is not None
            and states[segment.slot].generated
        ]
        if not candidates:
            return 0, "没有可提炼的时段"

        prompt = self._build_topic_prompt(day, candidates, states)
        result = await self._call_llm(prompt, model=self._topic_model, temperature=0.4)
        self._record_preview(
            request_kind="schedule_topics",
            prompt=prompt,
            result=result,
            selection_reason=f"{day.isoformat()} 话题提炼（{len(candidates)} 段）",
            output_title="今日话题",
        )

        if not result.get("success", True):
            reason = str(result.get("error") or result.get("response") or "").strip() or "模型调用失败"
            _logger.error("[话题] %s 提炼失败：%s", day, reason)
            return 0, reason

        rows = _extract_json_array(str(result.get("response") or ""))
        if rows is None:
            _logger.error("[话题] %s 输出不是合法 JSON 数组", day)
            return 0, "输出不是合法 JSON 数组"

        by_slot = {segment.slot: segment for segment in candidates}
        produced = 0
        for row in rows:
            if not isinstance(row, dict):
                continue
            slot = str(row.get("slot") or "")
            if slot not in by_slot:
                continue
            topic = str(row.get("topic") or "").strip()
            if not topic:
                continue
            if len(topic) > 90:
                _logger.warning("[话题] %s %s 超长（%d 字），丢弃", day, slot, len(topic))
                continue
            raw_keys = row.get("topic_keys")
            keys = [str(k).strip() for k in raw_keys if str(k).strip()] if isinstance(raw_keys, list) else []
            if not keys:
                # 没有关键词就检测不了说没说过，那这条谈资会一直挂着——宁可不要
                _logger.warning("[话题] %s %s 没给关键词，丢弃", day, slot)
                continue
            states[slot].topic = topic
            states[slot].topic_keys = keys
            produced += 1

        _logger.info("[话题] %s 提炼出 %d/%d 条", day, produced, len(candidates))
        return produced, ""

    def _build_topic_prompt(
        self, day: date, segments: list[Segment], states: dict[str, SegmentState]
    ) -> str:
        """拼装话题提炼 prompt（设计文档 5.10）。"""
        listing = "\n\n".join(
            f"{index + 1}. {segment.slot}　{segment.title}\n{states[segment.slot].story}"
            for index, segment in enumerate(segments)
        )
        return f"""你要从一份日程里挑出「今天值得说给人听的事」。

# 角色
{PERSONA}

# 今天
{day.isoformat()} 星期{weekday_name(day)}

# 各时段
{listing}

# 要做的事
为上面每一段判断：这段时间里有没有一件事，是她之后跟人聊天时会顺口提起的？
有就写一句提示 topic，没有就把 topic 留成空字符串。

## topic 怎么写
- **第三人称**，30~60 字，**必须带前因后果**——这是一件能讲的事，不是一个标签
  - ✗ 老先生送橡皮筋扎头发（压成了名词短语，看不出为什么、后来怎样）
  - ✓ 发圈松了头发一直往下滑，常来的老先生从口袋里翻出根捆报纸的橡皮筋给她救急，她扎好后绕在手腕上没还
  - ✗ 麦克风只有一支有电（只有状况，没有她怎么应对）
  - ✓ 包厢两支麦克风只有一支有电，跟朋友挤在一块儿轮着唱，唱到最后两个人嗓子都哑了
- 要有**起因 → 发生了什么 → 结果或转折**。她转述时靠的就是这条线；
  只给一个结论，她得自己瞎编前因，编出来的多半和事实对不上
- **不要写成她的原话**（「刚借了把伞给客人」）——那样她会照着念。
  写成第三人称的转述，让她自己组织语言
- 一段只挑一件事，挑最有画面感、最像会被人接话的那件
- **同一天的几个话题不要都围着同一样东西转**。一件小物件贯穿全天是 story 的事，
  但拿出来当谈资时会变成把同一件事讲三遍——遇到这种情况，只在最有意思的那一段留话题
- **宁缺毋滥**：平淡的日常（睡觉、洗澡、发呆、刷手机、赶路）就留空。
  一天十段里有三四段值得说就够了；十段全都有，说明你的标准太松

## topic_keys 怎么写
- 2~4 个词，用来事后检测她有没有把这件事说出来
- 挑这件事**独有**的名词（伞、胶带、蛋包饭），不要挑通用词（今天、客人、店里、时候）
- topic 为空时，keys 也给空数组

# 输出
严格的 JSON 数组，不要包在代码块里。一段一个对象，slot 必须和上面完全一致。
没什么好说的那几段也要出现在数组里，只是 topic 给空字符串：
[{{"slot":"09:00-12:00","topic":"…","topic_keys":["…","…"]}}]"""

    # ── 当日概要 ──
    async def update_digest(self, day_digest: str, story: str) -> str:
        """滚动更新当日概要。

        输入永远只有三百来字（旧概要 + 一段新 story），所以可以用便宜模型。
        失败就沿用旧的，不影响主链路——它只用来防当天自我重复。
        """
        prompt = self._build_digest_prompt(day_digest, story)
        result = await self._call_llm(prompt, model=self._digest_model, temperature=0.3)
        self._record_preview(
            request_kind="schedule_digest",
            prompt=prompt,
            result=result,
            selection_reason="当日概要滚动更新",
            output_title="当日概要",
        )
        if not result.get("success", True):
            _logger.warning("[概要] 调用失败，沿用旧概要：%s", result.get("error"))
            return day_digest
        updated = str(result.get("response") or "").strip()
        if not updated:
            _logger.warning("[概要] 输出为空，沿用旧概要")
            return day_digest
        return updated

    @staticmethod
    def _build_digest_prompt(day_digest: str, story: str) -> str:
        """拼装当日概要压缩 prompt（设计文档 5.9）。"""
        return (
            "把下面这段新发生的事，并进已有的当日概要里，输出更新后的概要。\n\n"
            "要求：\n"
            "- 只写「做过什么」，两三句话，120 字以内\n"
            "- 滤掉物件、身体细节和文笔，那些留着会诱导后面的段落接着写同样的细节\n"
            "- 直接输出概要正文，不要任何解释或前缀\n\n"
            f"# 已有的当日概要\n{day_digest or '（今天刚开始，还没有）'}\n\n"
            f"# 新发生的事\n{story}"
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


def _extract_json_object(text: str) -> dict[str, Any] | None:
    """从模型输出里取出 JSON 对象，容忍前后多余文字和代码块围栏。"""
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", stripped).strip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        payload = json.loads(stripped[start : end + 1])
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


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
