"""面向生图模型的文字快照与参考用途；完整结构化资料仍保留在任务中。"""

from datetime import datetime
from typing import Any, List
from zoneinfo import ZoneInfo

import re


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


SHOT_METHODS = {"front_selfie": "前置自拍", "mirror_selfie": "对镜自拍", "self_view": "本人视角", "timer": "手机放着定时拍", "handheld": "手持拍摄",
                "helper": "身边的人帮忙拍"}
CANDIDS = {"requested": "她请对方帮忙拍", "asked": "知道在拍但照常做事", "unaware": "她没察觉", "noticed": "刚发现被拍",
           "mid_action": "动作进行到一半", "through": "隔着前景物品偷拍"}
FACE_COVERS = {"crop": "画框裁切", "object": "物品挡住", "out_of_frame": "头部不入画", "shown": "露脸", "none": "无人物"}
_NOT_HELPERS = ("一个人", "客人", "陌生人", "路人", "老师", "班主任")


def helpers_of(company: str) -> List[str]:
    """日程同伴里认识、能帮她拍照的人；客人、陌生人、老师和独处不算。"""
    names = [n.strip() for n in re.split(r"[、，,/]", company) if n.strip()]
    return [n for n in names if not any(word in n for word in _NOT_HELPERS)]


def shot_text(plan: dict[str, Any]) -> str:
    """把结构化取景方案排成生图用的「本次拍摄」：字段顺序固定，便于阅读和对照检查。"""
    def c(text: Any) -> str:  # 字段自带的句末标点去掉，由排版统一加
        return str(text).strip().rstrip("。；;，,. ")

    shot, frame, cover = plan["shot"], plan["frame"], plan["face_cover"]
    bg, light = plan["background"], plan["lighting"]
    lines = [
        f"主体：{c(plan['subject'])}。",
        f"拍法：{SHOT_METHODS[shot['method']]}。手机：{c(shot['phone'])}；高度：{c(shot['height'])}；角度：{c(shot['angle'])}；距离：{c(shot['distance'])}。",
    ]
    if plan.get("mode") in {"assist", "share"}:
        lines.append(f"拍摄者：{c(plan['share']['source'])}。拍法：{CANDIDS[plan['share']['candid']]}。")
    lines += [
        f"画框：上沿{c(frame['top'])}；下沿{c(frame['bottom'])}；左右{c(frame['sides'])}；重心{c(frame['focus'])}。",
    ]
    if cover["method"] == "shown":
        lines.append(f"脸部：完整入镜——{c(cover['detail'])}。")
    elif cover["method"] != "none":
        lines.append(f"遮脸：{FACE_COVERS[cover['method']]}——{c(cover['detail'])}。")
    if c(plan.get("pose", "")):
        lines.append(f"姿势与动作：{c(plan['pose'])}。")
    if plan.get("visible"):
        lines.append("画面可见：" + "；".join(c(item) for item in plan["visible"]) + "。")
    lines.append(f"背景：{c(bg['content'])}。背景人物：{c(bg['people'])}。")
    lines.append(f"光线：{c(light['source'])}；{c(light['direction'])}；{c(light['tone'])}。")
    if str(plan.get("prompt") or "").strip():
        lines.append(f"画面描述：{plan['prompt'].strip()}")
    return "\n".join(lines)
