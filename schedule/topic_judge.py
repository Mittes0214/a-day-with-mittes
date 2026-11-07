"""谈资用掉判定：Mittes 回复之后，让小模型判断这条回复有没有讲到那件事。

替代早先的两套判据：
- 关键词命中（从 story 书面词里抽 key，在回复里找）——她开口是口语，几乎命不中；
- 「planner 调过 get_mittes_topic 就算用掉」——那个工具随开口通道一起关了，记录永远写不进去。

只在本轮 replyer 真的注入了谈资、且这条谈资还没用掉时才判，所以每个时段每个会话
最多判「注入次数」那么多次，判定用掉之后不再注入，也就不再判。
"""

from typing import Any

import json
import re


JUDGE_SYSTEM_PROMPT = (
    "你判断一条聊天回复有没有讲到给定的那件事。\n"
    "讲到核心经历就算，哪怕只提了一个细节、换了说法、只说了半句；"
    "只是碰巧用到相同的词、但没在讲这件事，不算。\n"
    '只输出 JSON：{"used": true 或 false, "evidence": "回复里对应的那一句，没讲到就留空"}'
)


def build_judge_prompt(topic: str, reply: str) -> list[dict[str, str]]:
    """拼判定请求。只给谈资原文和回复全文，不带上下文——判的就是这两段文字的关系。"""
    return [
        {"role": "system", "content": JUDGE_SYSTEM_PROMPT},
        {"role": "user", "content": f"那件事：{topic}\n\n回复：{reply}"},
    ]


_JSON_OBJECT = re.compile(r"\{.*\}", re.S)


def parse_judge_response(text: str) -> tuple[bool, str]:
    """解析模型输出，返回 (是否用掉, 依据)。

    模型偶尔会包一层 ```json 代码块或在前后多说一句，取第一个花括号对象解析。
    解析不出来就抛 ValueError，由调用方记日志、本次不标记——不猜成用掉或没用掉。
    """
    match = _JSON_OBJECT.search(text or "")
    if not match:
        raise ValueError(f"判定输出里没有 JSON：{text!r}")
    data: Any = json.loads(match.group(0))
    if not isinstance(data, dict) or not isinstance(data.get("used"), bool):
        raise ValueError(f"判定输出缺少布尔字段 used：{text!r}")
    return data["used"], str(data.get("evidence") or "").strip()
