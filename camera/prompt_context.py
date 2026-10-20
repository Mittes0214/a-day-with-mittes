"""面向生图模型的文字快照与参考用途；完整结构化资料仍保留在任务中。"""

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo


def snapshot_text(snapshot: dict[str, Any]) -> str:
    lines = []
    if snapshot.get("captured_at"):
        moment = datetime.fromisoformat(snapshot["captured_at"]).astimezone(ZoneInfo("Asia/Tokyo"))
        lines.append(f"拍摄时间：{moment:%Y年%m月%d日 %H:%M:%S}，日本时间。")
    for key, label in [("slot", "当前时段"), ("title", "日程主题"), ("place", "拍摄时所在地"), ("outfit", "当前穿搭")]:
        if snapshot.get(key):
            lines.append(f"{label}：{snapshot[key]}。")
    for key, label in [("hair", "当前发型"), ("top", "上装"), ("bottom", "下装"),
                       ("legs", "袜子"), ("feet", "鞋子"), ("accessories", "配饰")]:
        value = snapshot.get("outfit_details", {}).get(key)
        if value:
            lines.append(f"{label}：{value.rstrip('。')}。")
    if snapshot.get("outfit_optional"):
        lines.append("可选衣着或配饰：" + snapshot["outfit_optional"])
    for key, label in [("hair_base", "头发外貌"), ("nails", "指甲")]:
        value = snapshot.get("appearance", {}).get(key)
        if value:
            lines.append(f"{label}：{value.rstrip('。')}。")
    if snapshot.get("story"):
        lines.extend(["", "本时段的日程故事（涵盖整个时段，不代表拍摄时刻已经全部发生）：", snapshot["story"]])
    if snapshot.get("topic"):
        lines.extend(["", "当前话题：" + snapshot["topic"]])
    return "\n".join(lines)


def reference_roles_text(references: list[dict[str, Any]]) -> str:
    lines = []
    for item in references:
        name = f"图{item['index']}"
        if item["kind"] == "outfit":
            name += f"（当前穿搭：{item['outfit']}）"
        lines.append(f"{name}：{item['role'].rstrip('。')}。")
    return "\n".join(lines)
