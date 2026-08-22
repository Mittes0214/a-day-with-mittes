"""衣柜：把 ``wardrobe.toml`` 读进来，按套装名查从头到脚的细节。

**两种粒度，两个去处**（设计文档 4.2）：

- 套装**名字**写在骨架的 ``outfit`` 里，进时段生成的 prompt，是 story 拿到的
  全部服装信息；
- 从头到脚的**细节**只有这里有，只给 ``get_mittes_outfit`` 工具用，
  **绝不进 story 的 prompt**——清单喂进去会把 story 往服装说明书上带。

骨架里可能出现衣柜里没有的名字（周五拍摄现场那身是品牌方或角色的衣服，
每次都不同）。这种情况不报错，工具照实回答「每次不同」。
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import logging
import tomllib


_logger = logging.getLogger("a_day_with_mittes.wardrobe")

# 从头到脚的渲染顺序与中文标签。TOML 里字段名用 ASCII，展示时才翻译，
# 跟骨架的 start/end/title 一个规矩。
_PART_LABELS: tuple[tuple[str, str], ...] = (
    ("hair", "头发"),
    ("top", "上身"),
    ("bottom", "下身"),
    ("legs", "腿"),
    ("feet", "脚"),
    ("accessories", "配饰"),
)

_ALWAYS_LABELS: tuple[tuple[str, str], ...] = (
    ("hair_base", "头发底子"),
    ("nails", "美甲"),
    ("props", "道具"),
)


@dataclass
class Outfit:
    """一套穿搭。``parts`` 按 ``_PART_LABELS`` 的顺序保存，缺的字段直接不在里面。"""

    name: str
    parts: dict[str, str] = field(default_factory=dict)
    # 不是每天都会加的东西（比如店里的猫耳发箍），回答时要标明它是可选的
    optional: str = ""


class Wardrobe:
    """``wardrobe.toml`` 的只读封装。"""

    def __init__(self, wardrobe_path: Path) -> None:
        self._path = wardrobe_path
        self._outfits: dict[str, Outfit] = {}
        self._always: dict[str, str] = {}

    def load(self) -> None:
        """读衣柜。文件缺失或格式不对直接抛——这是随插件走的资产，不该缺。"""
        with self._path.open("rb") as handle:
            raw: dict[str, Any] = tomllib.load(handle)

        wardrobe_raw = raw.get("wardrobe")
        if not isinstance(wardrobe_raw, dict) or not wardrobe_raw:
            raise ValueError(f"衣柜缺少 [wardrobe.*]：{self._path}")

        self._always = {
            key: str(value).strip()
            for key, value in (raw.get("always") or {}).items()
            if str(value).strip()
        }
        self._outfits = {}
        for name, value in wardrobe_raw.items():
            parts = {
                key: str(value.get(key) or "").strip()
                for key, _label in _PART_LABELS
                if str(value.get(key) or "").strip()
            }
            self._outfits[str(name)] = Outfit(
                name=str(name),
                parts=parts,
                optional=str(value.get("optional") or "").strip(),
            )
        _logger.info("[衣柜] 载入 %d 套穿搭", len(self._outfits))

    @property
    def names(self) -> set[str]:
        return set(self._outfits)

    def get(self, name: str) -> Outfit | None:
        return self._outfits.get(name.strip())

    def render(self, name: str) -> str:
        """渲染一套穿搭，供工具结果使用。

        查不到的名字不当错误处理——骨架里本来就允许出现衣柜外的名字。
        """
        outfit = self.get(name)
        if outfit is None:
            return (
                f"这一身：{name}\n"
                "　这是当天临时定的衣服（品牌方或角色的），每次都不一样，衣柜里没有固定记录。"
            )

        lines = [f"这一身：{outfit.name}"]
        lines += [f"　{label}：{outfit.parts[key]}" for key, label in _PART_LABELS if key in outfit.parts]
        if outfit.optional:
            lines.append(f"　有时会加：{outfit.optional}")
        if self._always:
            lines.append("")
            lines.append("不分场合一直有的：")
            lines += [
                f"　{label}：{self._always[key]}"
                for key, label in _ALWAYS_LABELS
                if key in self._always
            ]
        return "\n".join(lines)
