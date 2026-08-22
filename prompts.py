"""``prompts/`` 目录的读取与渲染。

把**三阶段生成**的 prompt 挪出 Python，改配方不用碰代码。**一次请求一个文件**：
`day.prompt` 是全天主生成，`round2.prompt` 是抽取，`expression.prompt` 是逐时段表达方式。

注入和工具返回那些短句不走这里，留在 `plugin.py`：它们嵌在代码流程里，不是独立请求。

## 文件格式

主体写在文件开头。可选块和重复项写成片段，附在文件末尾，用单独一行 `@@ 片段名` 分隔：

    正文……
    {optional_block}

    @@ optional_block
    只有满足条件时才拼进去的内容

占位符是 `{名字}`，**不走 ``str.format``**——`round2.prompt` 里有成段的 JSON 示例，
裸大括号会把 format 打崩。替换是**单趟扫描**：填进去的正文不会被后面的键二次替换，
否则 story 里万一出现 `{skeleton}` 这样的字面量就会被当占位符吃掉。
没被传值的占位符原样留在输出里，方便一眼看出漏了什么，而不是抛异常或者悄悄变成空。

渲染后会做两件收尾：去掉行尾空白、把三个以上连续换行压成两个。首尾只剥换行，
不剥空格——骨架清单靠全角空格缩进，而 U+3000 在 Python 里是算作空白的。可选块留空时
上下会多出空行，靠这一步收干净，所以模板里可以放心地一行一个占位符。

## 共享文字是抄的，不是引的

`day.prompt` 和 `rewrite.prompt` 里的角色设定、人称表、三段字段规则是**各存一份**。
故意的：一个文件就是一次完整请求，读的时候不用跳转。代价是改规则要改两处，
改完记得对一遍——真分叉了，两条链路的产出会以很难察觉的方式互相偏移。
"""

from pathlib import Path
from typing import Any

import re


_DIR = Path(__file__).resolve().parent / "prompts"
_FRAGMENT_RE = re.compile(r"^@@[ \t]+(\S+)[ \t]*$", re.M)
_BLANKS_RE = re.compile(r"\n{3,}")
_TRAILING_RE = re.compile(r"[ \t]+$", re.M)
# 只认 {标识符}，所以 round2 里的 {"slot": …} 那类 JSON 不会被误当成占位符
_PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")

_cache: dict[str, dict[str, str]] = {}


def _sections(name: str) -> dict[str, str]:
    """读一个 prompt 文件并按片段拆开，主体的键是空串。

    缓存在进程内。改了 `.prompt` 要重载插件才生效——主程序的文件监听覆盖整个
    插件目录，保存即重载，所以实际用起来不用管这件事。
    """
    if name in _cache:
        return _cache[name]

    text = (_DIR / f"{name}.prompt").read_text(encoding="utf-8")
    marks = list(_FRAGMENT_RE.finditer(text))
    # 只剥换行，不用 strip()：全角空格 U+3000 在 Python 里算空白，
    # 而骨架清单那几行就是拿它做缩进的，strip 会把缩进吃掉。
    sections = {"": (text[: marks[0].start()] if marks else text).strip("\n")}
    for index, mark in enumerate(marks):
        end = marks[index + 1].start() if index + 1 < len(marks) else len(text)
        sections[mark.group(1)] = text[mark.end() : end].strip("\n")

    _cache[name] = sections
    return sections


def render(name: str, fragment: str = "", /, **values: Any) -> str:
    """渲染一段 prompt。

    Args:
        name: 文件名，不带 ``.prompt``。
        fragment: 片段名；留空取主体。
        **values: 占位符的值。``None`` 按空字符串处理。

    Returns:
        str: 渲染并收尾后的文本。
    """
    sections = _sections(name)
    if fragment not in sections:
        raise KeyError(f"{name}.prompt 里没有片段「{fragment}」")

    def substitute(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in values:
            return match.group(0)
        value = values[key]
        return "" if value is None else str(value)

    text = _PLACEHOLDER_RE.sub(substitute, sections[fragment])
    return _BLANKS_RE.sub("\n\n", _TRAILING_RE.sub("", text)).strip("\n")
