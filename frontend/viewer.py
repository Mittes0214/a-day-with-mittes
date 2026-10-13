"""日程归档与管理页面。

把 ``data/schedule.db`` 里的日程按天翻出来看，也能通过任务队列编辑表达方式、
生成日程和重跑 topic。任务由正在运行的插件领取，viewer 自己不调用 LLM。

    uv run python plugins/04_a_day_with_mittes/frontend/viewer.py          # 只有本机能开
    uv run python plugins/04_a_day_with_mittes/frontend/viewer.py --lan    # 局域网内可开

只用标准库，不引任何依赖；每个请求短连接 SQLite，WAL 模式下不会长期占写锁。

**``--lan`` 没有任何鉴权**：同一局域网里知道地址的人都能看到全部日程内容。
这是自用小工具的取舍，别往公网端口映射。
"""

from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import argparse
import json
import socket
import sqlite3
import sys
import threading
import tomllib
import urllib.request


PLUGIN_DIR = Path(__file__).resolve().parent.parent
# 表达方式那张表跟运行时同源：插件用它拼 reply_style，这里原样发到页面上做预览。
# 前端自己抄一份的话，改完插件页面还显示旧文本——看着对、实际不对，比不显示更糟。
# reply_style.py 只用标准库，import 它不会把 maibot_sdk 拖进这个独立脚本。
sys.path.insert(0, str(PLUGIN_DIR))
import reply_style  # noqa: E402
# 日程库随插件数据一起迁到统一持久化目录 data/plugins/<插件ID>/。
# PLUGIN_DIR 固定是 <项目根>/plugins/<本插件目录>，parents[1] 即项目根。
PROJECT_ROOT = PLUGIN_DIR.parents[1]
DB_PATH = PROJECT_ROOT / "data" / "plugins" / "khiqwq.a-day-with-mittes" / "schedule.db"
WARDROBE_PATH = PLUGIN_DIR / "character" / "wardrobe.toml"
CONFIG_PATH = PLUGIN_DIR / "config.toml"
WEEKDAY_NAMES = ["一", "二", "三", "四", "五", "六", "日"]

# 祝日表跟插件共用同一份缓存：插件生成日程时下载，viewer 画日历时读。
# 路径必须跟 schedule/holidays.py 的 ``data_dir / "holidays" / "JP"`` 对齐，
# 错开的话两边各下一份，改了数据源还会有一边继续吃旧缓存。
HOLIDAY_DIR = DB_PATH.parent / "holidays" / "JP"
HOLIDAY_URL_TEMPLATE = "https://holidays-jp.github.io/api/v1/{year}/date.json"
_HOLIDAY_LOCK = threading.Lock()
_HOLIDAY_MEM: dict[int, dict[str, Any]] = {}


def _connect(db_path: Path, *, readonly: bool = True) -> sqlite3.Connection:
    """每个 HTTP 请求使用自己的短连接，避免 ThreadingHTTPServer 共享连接。"""
    if readonly:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    else:
        conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _list_days(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT d.date, d.weekday, d.aborted,
               -- 段数从 segments 现数，不存冗余列：存一份就必然会跟实际漂
               (SELECT COUNT(*) FROM segments s WHERE s.date = d.date) AS total_count,
               (SELECT COUNT(*) FROM segments s
                 WHERE s.date = d.date AND s.generated = 1) AS ok_count,
               (SELECT COUNT(*) FROM segments s
                 WHERE s.date = d.date AND s.negative_level <> '') AS negative_count
          FROM days d
         ORDER BY d.date DESC
        """
    ).fetchall()
    return [dict(row) for row in rows]


def _load_day(conn: sqlite3.Connection, day: str) -> dict[str, Any] | None:
    meta = conn.execute(
        """
        SELECT d.*,
               (SELECT COUNT(*) FROM segments s WHERE s.date = d.date) AS total_count,
               (SELECT COUNT(*) FROM segments s
                 WHERE s.date = d.date AND s.generated = 1) AS ok_count
          FROM days d WHERE d.date = ?
        """,
        (day,),
    ).fetchone()
    if meta is None:
        return None
    segments = conn.execute(
        "SELECT * FROM segments WHERE date = ? ORDER BY seq", (day,)
    ).fetchall()
    shares = conn.execute(
        "SELECT * FROM shares WHERE date = ? ORDER BY slot, session_id", (day,)
    ).fetchall()
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in shares:
        grouped.setdefault(row["slot"], []).append(dict(row))
    return {
        "meta": dict(meta),
        "segments": [dict(row) for row in segments],
        "gate": _load_gate_config(CONFIG_PATH),
        "manner_enabled": _manner_enabled(CONFIG_PATH),
        "shares": grouped,
    }


def _normalize_holidays(data: Any) -> dict[str, str]:
    """把缓存文件里的祝日表统一成 ``{"YYYY-MM-DD": "名字"}``。

    Holidays JP 的原始格式就是这样；旧版中国节假日缓存的值是个 dict
    （``{"name": ..., "type": ...}``），这里一并认下来，免得读到历史文件就整月空白。
    """
    if not isinstance(data, dict):
        return {}
    out: dict[str, str] = {}
    for day, info in data.items():
        if not isinstance(day, str) or len(day) != 10:
            continue
        if isinstance(info, str):
            name = info
        elif isinstance(info, dict):
            name = str(info.get("name") or info.get("name_cn") or "")
        else:
            continue
        if name:
            out[day] = name
    return out


def _download_holidays(year: int) -> dict[str, str]:
    """现下一年的祝日表。失败一律返回空表——日历照常画，只是没有祝日标记。"""
    request = urllib.request.Request(
        HOLIDAY_URL_TEMPLATE.format(year=year),
        headers={"User-Agent": "a-day-with-mittes-viewer"},
    )
    try:
        with urllib.request.urlopen(request, timeout=6) as response:
            if response.status != 200:
                return {}
            return _normalize_holidays(json.loads(response.read().decode("utf-8")))
    except Exception:
        return {}


def _load_holidays(year: int) -> dict[str, Any]:
    """取某一年的日本祝日表，优先读插件缓存，没有就下载并写回同一个文件。

    viewer 自己也下是有意的：插件只在生成那一年的日程时才会去下，而日历可以被翻到
    任意一年。写回的路径和格式跟插件完全一致，所以下完两边都受益。

    ``source`` 会发到页面上：``none`` 时页面要写明"这一年的祝日表没取到"，
    否则一整年没有任何标记，看着跟"这一年真的没有祝日"分不出来。
    """
    with _HOLIDAY_LOCK:
        hit = _HOLIDAY_MEM.get(year)
    if hit is not None:
        return hit

    cache_file = HOLIDAY_DIR / f"{year}.json"
    days: dict[str, str] = {}
    source = "cache"
    if cache_file.exists():
        try:
            days = _normalize_holidays(json.loads(cache_file.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            days = {}
    if not days:
        days = _download_holidays(year)
        source = "network" if days else "none"
        if days:
            try:
                HOLIDAY_DIR.mkdir(parents=True, exist_ok=True)
                cache_file.write_text(
                    json.dumps(days, ensure_ascii=False, indent=2), encoding="utf-8"
                )
            except OSError:
                pass

    result = {"year": year, "source": source, "days": days}
    # 没取到的年份不进内存缓存：否则断网时翻过一次，网络恢复了也要重启 viewer 才能拿到。
    if source != "none":
        with _HOLIDAY_LOCK:
            _HOLIDAY_MEM[year] = result
    return result


def _manner_enabled(path: Path) -> bool:
    """表达方式功能开着没有（config.toml 的 ``[manner] enabled``）。

    关掉时这个页面不显示表达方式：既不显示正文，也不给编辑框和重跑按钮——
    功能停用期间留着一个改了不生效的输入框，比不显示更让人困惑。
    库里的历史值原样保留，打开就回来。
    """
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return True
    return bool((data.get("manner") or {}).get("enabled", True))


def _reply_style_enabled(path: Path) -> bool:
    """按心情 × 体力替换 reply_style 这套功能开着没有（``[reply_style] enabled``）。

    关掉时页面**照样把九格显示出来**——这是设计文案的地方，停用期间还要能改。
    但每处都必须标明当前没生效，否则页面展示的是一套 replyer 根本收不到的文字，
    「看着对、实际不对」比不显示更糟。
    """
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return True
    return bool((data.get("reply_style") or {}).get("enabled", True))


def _load_gate_config(path: Path) -> dict[str, Any]:
    """读 config.toml 里的窗口参数，给前端那条时间条用。

    **必须跟运行时同源。** 前端要是自己写死一份 30 分钟 / 六种 kind，
    改了配置之后条上画的还是旧规则——看着对、实际不对，这种偏差比没有这条更糟。
    读不到就退回内置默认值，和插件里 `_get_config` 的默认值保持一致。
    """
    defaults: dict[str, Any] = {
        "fresh_minutes": 30,
        "breakpoint_minutes": 60,
        "busy_kinds": ["睡眠", "工作", "上学"],
        "idle_kinds": ["自由", "外出", "通勤"],
    }
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return defaults
    pitch = ((data.get("topic") or {}).get("pitch_channel")) or {}
    return {key: pitch.get(key, value) for key, value in defaults.items()}


def _outfit_images(wardrobe_path: Path, part: str = "outer") -> dict[str, Path]:
    """外搭复用模型映射，内搭单列；只读取衣柜目录内的 PNG。"""
    if part not in ("outer", "inner"):
        raise ValueError("服饰图片类型不合法")
    manifest = wardrobe_path.with_name("innerwear_images.toml" if part == "inner" else "outfit_images.toml")
    if not manifest.exists():
        return {}
    with manifest.open("rb") as handle:
        images = tomllib.load(handle)["images"]
    root = wardrobe_path.parent.resolve()
    paths: dict[str, Path] = {}
    for name, relative in images.items():
        image = (root / relative).resolve()
        if not image.is_relative_to(root) or image.suffix.lower() != ".png":
            raise ValueError(f"服饰图片路径不合法：{name}")
        if not image.is_file():
            raise FileNotFoundError(f"缺少服饰设定图：{name}")
        paths[name] = image
    return paths


def _load_wardrobe(path: Path) -> dict[str, Any]:
    """读衣柜。**每次请求都重读**——它是手写资产，改完刷新页面就该看到，
    不该为了省一次文件读取而要求重启 viewer。图片仅返回可用套装名，原图按需加载。
    """
    try:
        with path.open("rb") as handle:
            raw = tomllib.load(handle)
        images = _outfit_images(path)
        inner_images = _outfit_images(path, "inner")
        names = set(raw.get("wardrobe") or {})
        if set(images) != names or set(inner_images) != names:
            raise ValueError("外搭、内搭图片映射必须覆盖衣柜中的全部搭配")
    except (OSError, ValueError) as exc:
        return {"error": f"读不了衣柜资料：{exc}"}
    return {
        "always": raw.get("always") or {},
        "wardrobe": raw.get("wardrobe") or {},
        "images": list(images),
        "inner_images": list(inner_images),
        "image_versions": {
            part: {name: str(image.stat().st_mtime_ns) for name, image in paths.items()}
            for part, paths in (("outer", images), ("inner", inner_images))
        },
    }


_ALLOWED_ACTIONS = {
    "set_expression",
    "generate_day",
    "regenerate_day",
    "regenerate_topics",
    "regenerate_expressions",
}


def _enqueue_job(conn: sqlite3.Connection, payload: dict[str, Any]) -> int:
    action = str(payload.get("action") or "")
    if action not in _ALLOWED_ACTIONS:
        raise ValueError("不支持的操作")
    target = str(payload.get("date") or "")
    date.fromisoformat(target)
    slot = str(payload.get("slot") or "")
    body: dict[str, Any] = {}
    if action == "set_expression":
        manner = str(payload.get("manner") or "").strip()
        if not slot:
            raise ValueError("缺少时段")
        if not manner:
            raise ValueError("表达方式不能为空")
        if len(manner) > 200:
            raise ValueError("表达方式不能超过 200 字")
        exists = conn.execute(
            "SELECT 1 FROM segments WHERE date=? AND slot=?",
            (target, slot),
        ).fetchone()
        if exists is None:
            raise ValueError("这一天或时段不存在")
        body["manner"] = manner
    cursor = conn.execute(
        "INSERT INTO admin_jobs (action, target_date, slot, payload) VALUES (?, ?, ?, ?)",
        (action, target, slot, json.dumps(body, ensure_ascii=False)),
    )
    return int(cursor.lastrowid)


def _load_job(conn: sqlite3.Connection, job_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM admin_jobs WHERE id=?", (job_id,)).fetchone()
    return dict(row) if row is not None else None


class Handler(BaseHTTPRequestHandler):
    db_path: Path
    wardrobe_path: Path

    def do_GET(self) -> None:  # noqa: N802 —— BaseHTTPRequestHandler 的固定签名
        path = urlparse(self.path).path
        if path == "/":
            self._send(200, "text/html; charset=utf-8", _page_bytes())
            return
        if path == "/api/wardrobe":
            self._send_json(_load_wardrobe(self.wardrobe_path))
            return
        if path == "/api/outfit-image":
            query = parse_qs(urlparse(self.path).query)
            name = (query.get("name") or [""])[0]
            part = (query.get("part") or ["outer"])[0]
            if part not in ("outer", "inner"):
                self._send_json({"error": "服饰图片类型不合法"}, status=400)
                return
            try:
                image = _outfit_images(self.wardrobe_path, part).get(name)
                if image is None:
                    self._send_json({"error": "这套穿搭没有设定图"}, status=404)
                    return
                self._send(200, "image/png", image.read_bytes(), cache_control="private, max-age=300")
            except (OSError, ValueError) as exc:
                self._send_json({"error": f"读不了服饰设定图：{exc}"}, status=500)
            return
        if path == "/api/holidays":
            query = parse_qs(urlparse(self.path).query)
            try:
                year = int((query.get("year") or [""])[0])
            except ValueError:
                self._send_json({"error": "年份不是数字"}, status=400)
                return
            if not 1970 <= year <= 2100:
                self._send_json({"error": "年份超出范围"}, status=400)
                return
            self._send_json(_load_holidays(year))
            return
        if path == "/api/days":
            with _connect(self.db_path) as conn:
                self._send_json(_list_days(conn))
            return
        if path.startswith("/api/day/"):
            day = path.rsplit("/", 1)[-1]
            with _connect(self.db_path) as conn:
                data = _load_day(conn, day)
            if data is None:
                self._send_json({"error": "没有这一天的记录"}, status=404)
                return
            self._send_json(data)
            return
        if path.startswith("/api/job/"):
            try:
                job_id = int(path.rsplit("/", 1)[-1])
            except ValueError:
                self._send_json({"error": "任务编号不正确"}, status=400)
                return
            with _connect(self.db_path) as conn:
                job = _load_job(conn, job_id)
            if job is None:
                self._send_json({"error": "任务不存在"}, status=404)
                return
            self._send_json(job)
            return
        self._send(404, "text/plain; charset=utf-8", "Not Found".encode("utf-8"))

    def do_POST(self) -> None:  # noqa: N802 —— BaseHTTPRequestHandler 的固定签名
        path = urlparse(self.path).path
        if path != "/api/jobs":
            self._send_json({"error": "Not Found"}, status=404)
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > 8192:
                raise ValueError("请求内容为空或过长")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("请求格式不正确")
            with _connect(self.db_path, readonly=False) as conn:
                job_id = _enqueue_job(conn, payload)
        except (ValueError, json.JSONDecodeError, sqlite3.Error) as exc:
            self._send_json({"error": str(exc)}, status=400)
            return
        self._send_json({"id": job_id, "status": "pending"}, status=202)

    def _send_json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(status, "application/json; charset=utf-8", body)

    def _send(self, status: int, content_type: str, body: bytes, *, cache_control: str = "no-store") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", cache_control)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: Any) -> None:
        """默认实现会把每个请求打到 stderr，太吵，静音。"""
        del fmt, args


PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Mittes 的一天</title>
<style>
:root {
  /* 告诉浏览器这页两套主题都支持。不写的话浏览器一律按浅色渲染它自己那部分 UI，
     深色下滚动条就是亮白的一条，日期选择器同理。 */
  color-scheme: light dark;
  --bg: #faf9f7; --panel: #fff; --line: #e6e2dc; --text: #2c2a28;
  --muted: #8a8378; --accent: #c96a5a; --badge: #f0ece6;
  /* 时间条上的三类 kind：睡眠 / 忙（工作·上学）/ 闲（自由·外出·通勤）。
     浅色主题下要压得够暗——条子上是白字，太浅会读不清 */
  --kind-sleep: #7d7568; --kind-busy: #a85b4d; --kind-idle: #4e7c6a;
  --band-ink: #fff;
  /* 日历：按日本的习惯，土曜蓝、日曜和祝日红。蓝红分处冷暖两端，
     红绿色觉也能分开——单靠深浅的话周六周日就成了一个颜色。 */
  --sat: #3f6ea8; --sun: #bf5546; --hol-bg: #f6e9e5;
  --bar: rgba(201, 106, 90, .32); --bar-on: rgba(201, 106, 90, .58);
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #1c1b19; --panel: #252320; --line: #37342f; --text: #e8e4dd;
    --muted: #9a9288; --accent: #e08b78; --badge: #322e29;
    --kind-sleep: #4d4840; --kind-busy: #7d4f47; --kind-idle: #456b5f;
    --band-ink: #eae6df;
    --sat: #8bacd8; --sun: #e08b78; --hol-bg: #3a2b26;
    --bar: rgba(224, 139, 120, .28); --bar-on: rgba(224, 139, 120, .52);
  }
}
* { box-sizing: border-box; }
/* 默认滚动条在深色页面上是白亮的一条，比页面里任何内容都抢眼。
   两套属性都写：Chrome 认标准的 scrollbar-color，老一点的 WebKit 认 ::-webkit-*。 */
* { scrollbar-width: thin; scrollbar-color: var(--bar) transparent; }
::-webkit-scrollbar { width: 9px; height: 9px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb {
  background: var(--bar); border-radius: 10px;
  border: 2px solid transparent; background-clip: padding-box;
}
::-webkit-scrollbar-thumb:hover { background: var(--bar-on); background-clip: padding-box; }
body {
  margin: 0; background: var(--bg); color: var(--text);
  font: 15px/1.7 -apple-system, "Noto Sans CJK SC", "Microsoft YaHei", sans-serif;
  display: flex; min-height: 100vh;
}
aside {
  width: 238px; flex: none; border-right: 1px solid var(--line);
  padding: 16px 0; overflow-y: auto; height: 100vh; position: sticky; top: 0;
}
aside h1 { font-size: 15px; margin: 0 16px 12px; letter-spacing: .05em; }
aside a {
  display: block; padding: 7px 16px; color: var(--text);
  text-decoration: none; font-variant-numeric: tabular-nums; font-size: 14px;
}
aside a:hover { background: var(--badge); }
aside a.on { background: var(--badge); border-left: 3px solid var(--accent); padding-left: 13px; }
aside .sub { color: var(--muted); font-size: 12px; margin-left: 6px; }
aside #more-days {
  display: block; width: calc(100% - 32px); margin: 6px 16px 0; padding: 5px 0;
  font: inherit; font-size: 12.5px; color: var(--muted); text-align: left;
  background: none; border: 0; border-top: 1px solid var(--line); cursor: pointer;
}
aside #more-days:hover { color: var(--text); }
/* ── 右侧日历 ──
   main 封了 900px，宽屏下右边一直空着一大块。这里放一张月历：
   周末、祝日、連休、哪天有归档，一眼看全，点一下就跳过去。
   margin-left:auto 让它贴右边——不加的话它紧挨着 main，右边照样空一条。 */
#cal {
  width: 300px; flex: none; margin-left: auto; padding: 16px 14px 28px;
  border-left: 1px solid var(--line);
  height: 100vh; position: sticky; top: 0; overflow-y: auto;
}
#cal .cal-head { display: flex; align-items: center; gap: 4px; margin-bottom: 10px; }
#cal .cal-head h2 {
  flex: 1; margin: 0; font-size: 14px; letter-spacing: .04em;
  font-variant-numeric: tabular-nums;
}
#cal .cal-head button {
  font: inherit; font-size: 12.5px; line-height: 1; padding: 5px 8px;
  color: var(--muted); background: none; border: 1px solid var(--line);
  border-radius: 6px; cursor: pointer;
}
#cal .cal-head button:hover { color: var(--text); border-color: var(--muted); }
/* minmax(0, 1fr) 不能省：`1fr` 等价于 `minmax(auto, 1fr)`，auto 那一头是内容的
   最小尺寸，格子撑不下去就整体往外溢——实测列宽被顶到 47.5px，七列 343px 塞进
   270px 的盒子里，星期日那一列直接被挤出视口，页面上只剩六列。 */
#cal .grid { display: grid; grid-template-columns: repeat(7, minmax(0, 1fr)); gap: 2px; }
#cal .wd { text-align: center; font-size: 11px; color: var(--muted); padding-bottom: 3px; }
#cal .wd.sat { color: var(--sat); }
#cal .wd.sun { color: var(--sun); }
#cal .cell {
  position: relative; display: flex; align-items: center; justify-content: center;
  height: 34px; border-radius: 6px; text-decoration: none;
  font-size: 13px; font-variant-numeric: tabular-nums; color: var(--text);
}
#cal .cell.sat { color: var(--sat); }
#cal .cell.sun, #cal .cell.hol { color: var(--sun); }
#cal .cell.hol { background: var(--hol-bg); }
/* 连休整段都铺底色，中间的格子不留圆角，一段连休就连成一条带子。
   跨周换行时断在行尾，正好读成"接着下一行"。 */
#cal .cell.rest { background: var(--hol-bg); border-radius: 0; }
#cal .cell.run-start { border-top-left-radius: 6px; border-bottom-left-radius: 6px; }
#cal .cell.run-end { border-top-right-radius: 6px; border-bottom-right-radius: 6px; }
#cal .cell.out { opacity: .32; }
/* 有归档的那天：底下一个点。加粗不够——周末本来就是彩色的，粗细混在一起看不出来 */
#cal a.cell { font-weight: 600; }
#cal a.cell::after {
  content: ""; position: absolute; bottom: 3px; left: 50%; transform: translateX(-50%);
  width: 4px; height: 4px; border-radius: 50%; background: currentColor; opacity: .55;
}
#cal a.cell:hover { background: var(--badge); }
#cal .cell.today { box-shadow: inset 0 0 0 1.5px var(--muted); }
#cal .cell.on {
  background: var(--accent); color: var(--band-ink); box-shadow: none; border-radius: 6px;
}
#cal .cell.on::after { background: var(--band-ink); opacity: .8; }
#cal .cal-sec { margin-top: 14px; border-top: 1px solid var(--line); padding-top: 10px; }
#cal .cal-sec b {
  display: block; margin-bottom: 5px; font-size: 11px; font-weight: 600;
  letter-spacing: .08em; color: var(--muted);
}
#cal .hol-item { display: flex; gap: 7px; font-size: 12.5px; line-height: 1.75; }
#cal .hol-item i {
  flex: none; min-width: 58px; font-style: normal; color: var(--sun);
  font-variant-numeric: tabular-nums;
}
/* 「顺延」「夹日」「黄金周」这类小牌子，祝日行和连休行共用一套 */
#cal .tag {
  flex: none; font-size: 10.5px; color: var(--muted); white-space: nowrap;
  border: 1px solid var(--line); border-radius: 4px; padding: 0 4px;
  align-self: center; margin-left: 6px;
}
#cal .hol-item .tag { margin-left: 0; }
/* 祝日 / 连休下面那行说明。左边缩进对齐日期后面的名字，读起来是一整块 */
#cal .hol-why {
  margin: 1px 0 9px 65px; font-size: 11.5px; line-height: 1.65; color: var(--muted);
}
#cal .hol-why.flush { margin-left: 0; }
#cal .run { font-size: 12.5px; line-height: 1.8; font-variant-numeric: tabular-nums; }
#cal .run em { font-style: normal; color: var(--accent); font-weight: 600; }
#cal .cal-note { margin-top: 12px; font-size: 11.5px; line-height: 1.7; color: var(--muted); }
#cal .legend {
  display: flex; flex-wrap: wrap; gap: 3px 12px; margin-top: 10px;
  font-size: 11.5px; color: var(--muted);
}
#cal .legend i { font-style: normal; }
#cal .legend .sw {
  display: inline-block; width: 10px; height: 10px; border-radius: 3px;
  background: var(--hol-bg); vertical-align: -1px; margin-right: 4px;
}
#cal .legend .dot {
  display: inline-block; width: 4px; height: 4px; border-radius: 50%;
  background: var(--muted); vertical-align: 3px; margin-right: 4px;
}
/* aside 238 + main 900 = 1138，再窄就该把日历让出去给正文 */
@media (max-width: 1240px) { #cal { display: none; } }
main { flex: 1; min-width: 0; padding: 28px 32px 64px; max-width: 900px; }
.dayhead { border-bottom: 1px solid var(--line); padding-bottom: 14px; margin-bottom: 24px; }
.dayhead h2 { margin: 0 0 6px; font-size: 22px; }
.dayhead .meta { color: var(--muted); font-size: 13px; }
.digest {
  background: var(--panel); border: 1px solid var(--line); border-radius: 8px;
  padding: 10px 14px; margin-top: 12px; font-size: 14px; color: var(--muted);
}
/* ── 全天时间条 ──
   一天十来段拉得很长，翻到底才知道大概。这条是"这一天长什么样"的缩略图：
   颜色是 kind，斜纹是谈资的新鲜期窗口，▼ 是断点，竖线是此刻。点一下跳到那一段。 */
.band-wrap { position: relative; margin-top: 14px; padding-top: 15px; }
.band {
  position: relative; display: flex; height: 34px;
  border: 1px solid var(--line); border-radius: 6px; overflow: hidden;
}
.band-cell {
  position: relative; min-width: 0; padding: 0; border: 0; cursor: pointer;
  border-right: 1px solid rgba(255,255,255,.28);
  font: inherit; font-size: 12px; color: var(--band-ink);
  display: flex; align-items: center; justify-content: center;
  overflow: hidden; white-space: nowrap;
}
.band-cell:last-child { border-right: 0; }
.band-cell.sleep { background: var(--kind-sleep); }
.band-cell.busy  { background: var(--kind-busy); }
.band-cell.idle  { background: var(--kind-idle); }
.band-cell:hover { filter: brightness(1.08); }
.band-cell:focus-visible { outline: 2px solid var(--text); outline-offset: -2px; }
.band-cell .label {
  position: relative; z-index: 1; padding: 0 4px; display: block;
  max-width: 100%; overflow: hidden; text-overflow: ellipsis;
}
/* 新鲜期：从这一段开头起的前 N 分钟，只画在有 topic 的段上 */
.band-cell .fresh {
  position: absolute; left: 0; top: 0; bottom: 0;
  background: repeating-linear-gradient(135deg,
    rgba(255,255,255,.85) 0 2px, transparent 2px 5px);
}
/* 断点的 ▼ 要露在条子上方，所以这一层不能放进 .band——那里 overflow:hidden 会裁掉它 */
.band-marks {
  position: absolute; left: 0; right: 0; top: 15px; height: 36px; pointer-events: none;
}
.band-marks .brk {
  position: absolute; top: 0; bottom: 0; width: 0;
  border-left: 2px solid var(--accent);
}
.band-marks .brk::after {
  content: "▼"; position: absolute; top: -15px; left: -5px;
  font-size: 10px; color: var(--accent); line-height: 1;
}
.band-marks .now {
  position: absolute; top: 0; bottom: 0; width: 0;
  border-left: 2px dashed var(--text);
}
.band-axis { display: flex; margin-top: 3px; font-size: 11px; color: var(--muted);
  font-variant-numeric: tabular-nums; }
.band-axis span { min-width: 0; overflow: hidden; white-space: nowrap; padding-left: 2px; }
.band-legend { display: flex; flex-wrap: wrap; gap: 4px 14px; margin-top: 8px;
  font-size: 12px; color: var(--muted); }
.band-legend i { display: inline-block; width: 10px; height: 10px; border-radius: 2px;
  margin-right: 5px; vertical-align: -1px; }
.band-legend .hatch { background: repeating-linear-gradient(135deg,
  var(--muted) 0 2px, transparent 2px 5px); border: 1px solid var(--line); }

.seg {
  background: var(--panel); border: 1px solid var(--line); border-radius: 10px;
  padding: 16px 18px; margin-bottom: 14px;
}
.seg.now { border-color: var(--accent); }
.seg h3 { margin: 0 0 4px; font-size: 16px; }
.seg h3 .slot { color: var(--accent); font-variant-numeric: tabular-nums; margin-right: 10px; }
.skeleton { color: var(--muted); font-size: 12.5px; margin-bottom: 10px; }
.story { margin: 0 0 12px; }
.fields { display: grid; grid-template-columns: 62px 1fr; gap: 4px 10px; font-size: 13.5px; }
.fields dt { color: var(--muted); }
.fields dd { margin: 0; }
.state-level {
  display: inline-block; border-radius: 999px; padding: 0 7px; margin-left: 8px;
  font-size: 11.5px; line-height: 19px; vertical-align: 1px;
  background: var(--badge); color: var(--muted);
}
.state-level.positive, .state-level.good { background: #4e7c6a; color: #fff; }
.state-level.negative, .state-level.poor { background: var(--accent); color: #fff; }
.badge {
  display: inline-block; background: var(--badge); border-radius: 4px;
  padding: 1px 7px; font-size: 12px; margin-left: 8px; color: var(--muted);
}
.badge.neg { background: var(--accent); color: #fff; }
.topic {
  margin-top: 12px; padding: 10px 12px; border-radius: 8px;
  background: var(--badge); font-size: 13.5px;
}
.topic.none { color: var(--muted); font-size: 12.5px; padding: 6px 12px; }
.topic .tag {
  display: inline-block; background: var(--accent); color: #fff; border-radius: 4px;
  padding: 0 6px; font-size: 11.5px; margin-right: 8px; vertical-align: 1px;
}
.trail { display: flex; flex-wrap: wrap; gap: 6px; margin: 8px 0 2px; }
.trail .leg {
  font-size: 12.5px; color: var(--muted); background: var(--badge);
  padding: 2px 8px; border-radius: 999px; white-space: nowrap;
}
.trail .leg.on { color: var(--panel); background: var(--accent); font-weight: 600; }
.topic .keys { color: var(--muted); font-size: 12.5px; margin-top: 6px; }
.topic .share { color: var(--muted); font-size: 12.5px; margin-top: 3px; }
.topic .share b { color: var(--accent); font-weight: 600; }
.topic .said {
  margin: 4px 0 2px; padding: 6px 9px; border-radius: 6px;
  background: var(--badge); border-left: 3px solid var(--accent);
  color: var(--text); font-size: 13px; line-height: 1.6;
  white-space: pre-wrap; word-break: break-word;
}
.topic .said.none { color: var(--muted); border-left-color: var(--line); font-style: italic; }
.empty { color: var(--muted); padding: 40px 0; }
.create-day { padding: 0 16px 14px; border-bottom: 1px solid var(--line); margin-bottom: 8px; }
.create-day label { display: block; color: var(--muted); font-size: 12px; margin-bottom: 5px; }
.create-day input { width: 100%; margin-bottom: 7px; }
input, textarea, button { font: inherit; }
input, textarea {
  color: var(--text); background: var(--panel); border: 1px solid var(--line);
  border-radius: 6px; padding: 7px 9px;
}
button.action {
  border: 1px solid var(--line); color: var(--text); background: var(--badge);
  border-radius: 6px; padding: 6px 10px; cursor: pointer;
}
button.action:hover { border-color: var(--accent); }
button.action.primary { color: #fff; background: var(--accent); border-color: var(--accent); }
button.action:disabled { opacity: .55; cursor: wait; }
.create-day button { width: 100%; }
.job-status { color: var(--muted); font-size: 12px; margin-top: 6px; min-height: 1.4em; }
.job-status.error { color: var(--accent); }
.day-actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }
.day-actions .action {
  color: var(--accent); background: transparent; border-color: var(--accent);
}
.day-actions .action:hover { color: #fff; background: var(--accent); }
.expression { margin-top: 12px; border-top: 1px solid var(--line); padding-top: 11px; }
.expression label { display: block; color: var(--muted); font-size: 12.5px; margin-bottom: 5px; }
.expression textarea {
  display: block; width: 100%; min-height: 62px; resize: vertical; line-height: 1.55;
}
.expression-foot { display: flex; align-items: center; gap: 9px; margin-top: 7px; }
.expression-foot .job-status { margin: 0; flex: 1; }

/* 骨架行里可点的穿搭名 */
.skeleton .outfit {
  font: inherit; color: var(--accent); background: none; border: 0; padding: 0;
  cursor: pointer; border-bottom: 1px dashed currentColor;
}
.skeleton .outfit:hover { background: var(--badge); }

/* ── 表达方式九宫格 ──
   心情三档 × 体力三档拼出九种 reply_style。格子是取景器，点哪格就把哪格拼给人看；
   运行时该发哪一格由当前时段的档位决定，跟这里点了什么无关。 */
.style-panel {
  border: 1px solid var(--line); border-radius: 8px; background: var(--panel);
  padding: 14px 16px 16px; margin-bottom: 24px;
}
.style-panel h3 { margin: 0 0 4px; font-size: 15px; }
.style-panel .hint { color: var(--muted); font-size: 12.5px; margin: 0 0 12px; }
.style-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; }
.style-cell {
  font: inherit; text-align: left; cursor: pointer; position: relative;
  border: 1px solid var(--line); border-radius: 6px; background: var(--bg);
  padding: 9px 10px 10px; display: flex; flex-direction: column;
  gap: 4px; align-items: flex-start;
}
.style-cell:hover { border-color: var(--muted); }
.style-cell.on { border-color: var(--accent); box-shadow: inset 0 0 0 1px var(--accent); }
.style-cell .state-level { margin-left: 0; }
.style-cell .whence {
  position: absolute; top: 7px; right: 9px;
  font-size: 11px; color: var(--accent); letter-spacing: .05em;
}
.style-out { margin-top: 13px; border-top: 1px solid var(--line); padding-top: 12px; }
.style-out p { margin: 0; font-size: 14.5px; }
.style-out .part-mood { color: var(--accent); }
.style-out .part-energy { color: var(--kind-idle); }
.style-out .part-fixed { color: var(--muted); }
.style-out .legend { margin-top: 9px; font-size: 12px; color: var(--muted); }

/* ── 【表达】页的树 ──
   九宫格看的是「此刻落在哪一格」，这棵树看的是「同一句口吻下三档体力各怎么写」。
   同一份数据两种排法，改文案时对着树看，跑起来对着九宫格看。

   连接线用 li 的两个伪元素画：::before 是竖干（最后一个子节点只画到横枝为止，
   免得多出一截悬空的线），::after 是横枝。横枝的 top 跟节点框第一行的中线对齐——
   li 上边距 12 + 边框 1 + 内边距 11 + 半行高 ≈ 36px，所以每个节点框的第一行
   必须是「档位标签 + 正文」同一行，多一行标题就会错位。 */
.tree { max-width: 820px; }
.tree ul { list-style: none; margin: 0; padding: 0 0 0 30px; }
.tree li { position: relative; padding: 12px 0 0 26px; }
.tree li::before {
  content: ""; position: absolute; left: 0; top: 0; bottom: 0;
  border-left: 1px solid var(--line);
}
.tree li:last-child::before { bottom: auto; height: 36px; }
.tree li::after {
  content: ""; position: absolute; left: 0; top: 36px; width: 26px;
  border-top: 1px solid var(--line);
}
.tree .node {
  display: flex; gap: 12px; align-items: flex-start;
  border: 1px solid var(--line); border-radius: 8px; background: var(--panel);
  padding: 11px 14px;
}
.tree .node .state-level { margin-left: 0; flex: none; }
.tree .node p { margin: 0; flex: 1; min-width: 0; }
/* 心情节点是这一支的头，给它一条竖色带；体力叶子保持素净，视线才有层次 */
.tree .node.mood { border-left: 3px solid var(--accent); }
.tree .node.leaf { background: var(--bg); }
.tree-root {
  border: 1px solid var(--line); border-radius: 8px; background: var(--panel);
  padding: 12px 14px;
}
.tree-root h3 { margin: 0 0 6px; font-size: 14px; }
.tree-root p { margin: 0; color: var(--muted); font-size: 13.5px; }
.tree-root .fixed-text { color: var(--text); margin-top: 6px; }
.tree-note {
  border: 1px solid var(--line); border-radius: 8px; padding: 12px 16px;
  color: var(--muted); font-size: 13.5px; max-width: 820px; margin-top: 22px;
}
.tree-note p { margin: 0 0 6px; }
.tree-note p:last-child { margin-bottom: 0; }
.style-off {
  display: inline-block; margin-left: 10px; padding: 1px 9px; border-radius: 999px;
  background: var(--accent); color: #fff; font-size: 12px; vertical-align: 3px;
}
.tree-note code {
  font-family: inherit; color: var(--text); background: var(--badge);
  border-radius: 4px; padding: 1px 6px;
}

/* Hallmark · 衣柜局部：沿用页面色彩，每套外搭 / 内搭成对，完整展示图片。 */
.outfit-section { margin-bottom: 24px; }
.outfit-section-head { display: flex; flex-wrap: wrap; align-items: baseline; gap: 10px; margin-bottom: 12px; }
.outfit-section-head h3 { font-size: 15px; margin: 0; }
.outfit-section-head span { font-size: 12px; color: var(--muted); }
.outfit-strip { display: flex; gap: 14px; overflow-x: auto; padding-bottom: 8px; scroll-snap-type: x proximity; }
.outfit-strip .outfit-card { flex: 0 0 min(95%, 680px); scroll-snap-align: start; }
.outfit-card { container-type: inline-size; min-width: 0; background: var(--panel); border: 1px solid var(--line); border-radius: 10px; overflow: hidden; }
.outfit-caption { padding: 14px 18px; border-bottom: 1px solid var(--line); }
.outfit-caption h3 { margin: 0; font-size: 15px; line-height: 1.6; overflow-wrap: anywhere; }
.outfit-current { color: var(--accent); font-size: 11px; margin-bottom: 4px; }
.outfit-panels { display: grid; grid-template-columns: minmax(0, 1fr); }
.outfit-panel { min-width: 0; }
.outfit-panel + .outfit-panel { border-top: 1px solid var(--line); }
.outfit-panel-head { display: flex; justify-content: space-between; align-items: baseline; gap: 12px; padding: 10px 16px; }
.outfit-panel-head h4 { margin: 0; font-size: 13px; }
.outfit-panel-head span { font-size: 11px; color: var(--muted); }
@container (min-width: 36rem) {
  .outfit-panels { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .outfit-panel + .outfit-panel { border-top: 0; border-left: 1px solid var(--line); }
}
.outfit-picture { display: block; position: relative; width: 100%; padding: 0; border: 0; cursor: zoom-in; background: #fffdf8; color: var(--text); }
.outfit-picture img { display: block; width: 100%; height: auto; aspect-ratio: 3 / 2; object-fit: contain; background: #fffdf8; }
.outfit-picture[data-status="loading"]::after { content: "正在加载…"; position: absolute; inset: 0; display: grid; place-items: center; color: var(--muted); pointer-events: none; }
.outfit-picture .zoom-hint {
  position: absolute; right: 10px; bottom: 10px; border-radius: 5px; padding: 3px 8px;
  background: var(--panel); color: var(--text); font-size: 11px; opacity: 0;
}
@media (hover: hover) { .outfit-picture:hover .zoom-hint { opacity: 1; } }
.outfit-picture:focus-visible .zoom-hint { opacity: 1; }
.outfit-picture:focus-visible { outline: 2px solid var(--text); outline-offset: -3px; }
.outfit-picture:active { opacity: .85; }
.outfit-details { border-top: 1px solid var(--line); font-size: 12.5px; }
.outfit-details summary { color: var(--muted); cursor: pointer; padding: 12px 16px; min-height: 44px; }
.outfit-details summary:hover { color: var(--text); }
.outfit-details summary:focus-visible { outline: 2px solid var(--text); outline-offset: -3px; }
.outfit-details .fields { margin: 0; padding: 2px 16px 16px; overflow-wrap: anywhere; }
.outfit-details dd { white-space: pre-line; }
.outfit-missing { display: grid; place-items: center; aspect-ratio: 3 / 2; margin: 0; padding: 16px; color: var(--muted); background: var(--badge); font-size: 13px; }
.outfit-image-error { display: grid; place-items: center; aspect-ratio: 3 / 2; margin: 0; padding: 16px; color: var(--muted); background: var(--badge); }

/* 穿搭细节弹层 */
#modal {
  display: none; position: fixed; inset: 0; z-index: 20;
  background: rgba(0,0,0,.38); padding: 24px; overflow-y: auto;
}
#modal.on { display: flex; align-items: flex-start; justify-content: center; }
#modal .sheet {
  background: var(--panel); border: 1px solid var(--line); border-radius: 10px;
  padding: 20px 22px; max-width: 1280px; width: 100%; margin: auto;
  box-shadow: 0 10px 40px rgba(0,0,0,.25); position: relative;
}
#modal .sheet h3 { margin: 0 32px 12px 0; font-size: 17px; }
#modal .outfit-full { display: block; width: 100%; height: auto; max-height: 72vh; object-fit: contain; background: #fffdf8; border-radius: 6px; }
#modal .original { display: block; width: fit-content; margin: 8px 0 14px auto; font-size: 12px; color: var(--muted); text-underline-offset: 3px; }
#modal .outfit-details { margin-top: 12px; }
#modal .close {
  position: absolute; top: 4px; right: 6px; width: 44px; height: 44px; font-size: 22px; line-height: 1;
  background: none; border: 0; color: var(--muted); cursor: pointer;
}
#modal .close:hover { color: var(--text); }
#modal .none { color: var(--muted); font-style: italic; }
.extra-link { margin-top: 10px; border-top: 1px solid var(--line); padding-top: 12px !important; }
@media (max-width: 640px) {
  body { flex-direction: column; }
  aside { width: auto; height: auto; position: static; border-right: 0; border-bottom: 1px solid var(--line); }
  main { padding: 20px 16px 48px; }
  .outfit-picture .zoom-hint { opacity: 1; }
  #modal { padding: 12px; }
  #modal .sheet { padding: 16px; }
  #cal {
    display: block; width: auto; height: auto; position: static; margin: 0;
    border-left: 0; border-top: 1px solid var(--line);
  }
}
</style>
</head>
<body>
<aside>
  <h1>Mittes 的一天</h1>
  <div class="create-day">
    <label for="generate-date">指定日期</label>
    <input id="generate-date" type="date">
    <button id="generate-day" class="action primary">生成指定日期日程</button>
    <div id="generate-status" class="job-status"></div>
  </div>
  <nav id="days"></nav>
</aside>
<main id="main"><p class="empty">加载中…</p></main>
<aside id="cal"></aside>
<div id="modal"></div>
<script>
const WD = ['一','二','三','四','五','六','日'];
const esc = s => (s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

// 表达方式那张表由 viewer 从 reply_style.py 原样发过来（见 _page_bytes），
// 前端不留副本。心情决定口吻，体力决定这个口吻还剩多少输出；体力那层是先按心情
// 分组的，所以取值要 ENERGY_BLOCK[心情][体力]，不是 ENERGY_BLOCK[体力]。
const STYLE_TABLE = __STYLE_TABLE__;
const MOOD_BLOCK = STYLE_TABLE.voice;
const ENERGY_BLOCK = STYLE_TABLE.stamina;
const STYLE_FIXED = STYLE_TABLE.fixed;
// 功能关掉时九格照常显示（这里是改文案的地方），但每处都要挂上这块牌子。
const STYLE_OFF = STYLE_TABLE.enabled === false
  ? '<span class="style-off">功能已关闭，当前不替换 reply_style</span>' : '';
const MOODS = STYLE_TABLE.moods;
const ENERGIES = STYLE_TABLE.energies;
let stylePick = null;

let knownDays = [];

async function loadDays() {
  const days = await api('/api/days');
  knownDays = days.map(d => d.date);
  const link = d => {
    const bad = d.aborted ? '中止' : (d.ok_count < d.total_count ? `${d.ok_count}/${d.total_count}` : '');
    const neg = d.negative_count ? ' ※' : '';
    return `<a href="#${d.date}" data-d="${d.date}">${d.date.slice(5)} 周${WD[d.weekday]}
      <span class="sub">${bad}${neg}</span></a>`;
  };
  // 归档只会越来越长，全列出来底下的「表达」入口会被顶到要滚动才够得着。
  // 近七天是日常真正会点的范围，更早的收进折叠区（days 已按日期倒序）。
  const older = days.slice(7);
  const foldout = older.length
    ? `<button type="button" id="more-days" aria-expanded="false">更早的 ${older.length} 天</button>
       <div id="older-days" hidden>${older.map(link).join('')}</div>`
    : '';
  document.getElementById('days').innerHTML = days.slice(0, 7).map(link).join('') + foldout
    // 衣柜整页已移到 mittes-panel 的 /wardrobe 单独维护，这里只留当日穿搭
    + `<a href="#expression" data-d="expression" class="extra-link">表达　<span class="sub">心情 × 体力</span></a>`;
}

function toggleOlder(open) {
  const box = document.getElementById('older-days');
  const button = document.getElementById('more-days');
  if (!box || !button) return;
  box.hidden = !open;
  button.setAttribute('aria-expanded', String(open));
  button.textContent = open ? '收起更早的日期' : `更早的 ${box.children.length} 天`;
}

// 直接用旧日期的 hash 进来时，选中的那条在折叠区里，左边会看不出停在哪天。
function revealActiveDay() {
  const box = document.getElementById('older-days');
  if (box && box.hidden && box.querySelector('a.on')) toggleOlder(true);
}

// ── 右侧月历 ──
// 周末、祝日、連休、哪天有归档，全画在一张月历上。祝日表走 /api/holidays，
// 跟插件共用同一份缓存。
//
// 日期算术**一律走 Date.UTC + getUTC***：new Date(y, m, d) 是按打开页面那台机器的
// 本地时区建的，浏览器在 UTC+X 时整张日历会错一格；UTC 里做加减则跟时区无关。
// 十六个法定祝日各是什么。名字用 holidays-jp 发回来的原文当 key（就是日文原名），
// 值是一句中文说明——这页面是给不熟悉日本节假日的人看的，只列个日文名等于没说。
const HOLIDAY_NOTE = {
  '元日': '元旦。日本过阳历新年，这天是一年之始，也是正月的头一天。',
  '成人の日': '成人日。1 月的第二个周一，各地给刚成年的人办仪式。',
  '建国記念の日': '建国纪念日。取传说中神武天皇即位的那一天。',
  '天皇誕生日': '天皇生日。跟着在位的天皇走，换代就换日子。',
  '春分の日': '春分。昼夜等长，传统上这几天去扫墓祭祖。',
  '昭和の日': '昭和天皇的生日，留下来用作回顾昭和那段年代。',
  '憲法記念日': '宪法纪念日。1947 年现行宪法正式施行的日子。',
  'みどりの日': '绿之日。亲近草木、感谢自然的日子。',
  'こどもの日': '儿童节。挂鲤鱼旗，祝孩子平安长大。',
  '海の日': '海之日。7 月的第三个周一，感谢海的恩惠——岛国才有的节日。',
  '山の日': '山之日。2016 年才开始放，十六个祝日里最年轻的一个。',
  '敬老の日': '敬老日。9 月的第三个周一，敬重长者、祝贺长寿。',
  '秋分の日': '秋分。跟春分一样，是扫墓追思的日子。',
  'スポーツの日': '体育日。10 月的第二个周一，本来纪念 1964 年东京奥运会开幕。',
  '文化の日': '文化日。1946 年宪法公布的日子，主题是热爱自由与和平。',
  '勤労感謝の日': '勤劳感谢日。感谢一年的收成，也互相感谢彼此的劳动。',
};

// 振替休日的名字是「原祝日 振替休日」这种拼法，国民の休日则自成一条，
// 两者都不在上面那张表里，单独处理。
function holidayNote(name) {
  if (/振替休日/.test(name)) {
    const base = name.replace(/振替休日/, '').trim();
    return base
      ? `${base}赶上周日，补休顺延到之后头一个平日，就落在今天。`
      : '祝日赶上周日，补休顺延到之后头一个平日，就落在今天。';
  }
  if (name === '国民の休日') return '前后两天都是祝日，夹在中间的这天也跟着放。';
  return HOLIDAY_NOTE[name] || '';
}

const HOL = {};
let calYM = null;        // 当前显示的月份 "YYYY-MM"
let selectedDay = null;  // 正在看的那天，日历上要高亮

const pad2 = n => String(n).padStart(2, '0');
const isoOf = d => `${d.getUTCFullYear()}-${pad2(d.getUTCMonth() + 1)}-${pad2(d.getUTCDate())}`;
const holOf = s => ((HOL[s.slice(0, 4)] || {}).days || {})[s] || '';
// 非工作日 = 周六 / 周日 / 祝日。日本没有调休补班，不存在"周末被征用去上班"这种情况，
// 所以这个判断不需要再查一张补班表。
const isRest = d => d.getUTCDay() === 0 || d.getUTCDay() === 6 || !!holOf(isoOf(d));

async function ensureHolidays(years) {
  await Promise.all([...new Set(years)].map(async y => {
    if (HOL[y]) return;
    try { HOL[y] = await api('/api/holidays?year=' + y); }
    catch { HOL[y] = {year: y, source: 'none', days: {}}; }
  }));
}

// 连着的非工作日。三天起才算连休——两天就是普通周末，列出来全是噪音。
function restRuns(y, m) {
  const runs = [];
  let cur = null;
  const stop = new Date(Date.UTC(y, m + 1, 7));
  // 往前后各多扫一周：连休经常跨月，只扫本月会把跨月的那段从中间切断
  for (let d = new Date(Date.UTC(y, m, -6)); d <= stop; d.setUTCDate(d.getUTCDate() + 1)) {
    if (isRest(d)) {
      if (cur) { cur.to = isoOf(d); cur.n++; cur.days.push(isoOf(d)); }
      else cur = {from: isoOf(d), to: isoOf(d), n: 1, days: [isoOf(d)]};
    } else {
      if (cur && cur.n >= 3) runs.push(cur);
      cur = null;
    }
  }
  if (cur && cur.n >= 3) runs.push(cur);
  return runs;   // 不在这里按月过滤：格子上色要连邻月那几天一起标，列表才另外筛
}

const mdOf = s => `${+s.slice(5, 7)}/${+s.slice(8)}`;

async function renderCal() {
  const box = document.getElementById('cal');
  if (!box) return;
  // 「今天」取逻辑日（00:00~02:00 算前一天），跟左边的日期列表一个口径，
  // 否则凌晨那两个小时里日历高亮的日子和列表选中的日子会差一天。
  const today = jstNow().date;
  if (!calYM) calYM = (selectedDay || today).slice(0, 7);
  const y = +calYM.slice(0, 4);
  const m = +calYM.slice(5, 7) - 1;

  const lead = (new Date(Date.UTC(y, m, 1)).getUTCDay() + 6) % 7;  // 周一起排
  const span = new Date(Date.UTC(y, m + 1, 0)).getUTCDate();
  const cells = [];
  for (let i = 0; i < Math.ceil((lead + span) / 7) * 7; i++) {
    cells.push(new Date(Date.UTC(y, m, 1 - lead + i)));
  }
  // 连休里每一天在整段中的位置，画底色时用：整段连成一条，只有两头是圆角。
  // 在 draw() 里重算（祝日表到货前后不一样），这里先占个位。
  let runMark = new Map();

  const cell = d => {
    const s = isoOf(d);
    const w = d.getUTCDay();
    const name = holOf(s);
    const cls = ['cell'];
    if (d.getUTCMonth() !== m) cls.push('out');
    if (w === 6) cls.push('sat');
    if (w === 0) cls.push('sun');
    if (name) cls.push('hol');
    // 连休的日子一律铺底色。只给祝日铺的话，凑成连休的那两天周末看着还是普通周末
    const mark = runMark.get(s);
    if (mark) cls.push('rest', 'run-' + mark);
    if (s === today) cls.push('today');
    if (s === selectedDay) cls.push('on');
    const tip = name ? ` title="${esc(name)}"` : '';
    return knownDays.includes(s)
      ? `<a class="${cls.join(' ')}" href="#${s}"${tip}>${d.getUTCDate()}</a>`
      : `<span class="${cls.join(' ')}"${tip}>${d.getUTCDate()}</span>`;
  };

  const draw = () => {
  const spans = restRuns(y, m);
  runMark = new Map();
  spans.forEach(r => r.days.forEach((s, i) => {
    runMark.set(s, i === 0 ? 'start' : i === r.days.length - 1 ? 'end' : 'mid');
  }));

  const holidays = Object.keys((HOL[y] || {}).days || {})
    .filter(s => s.slice(0, 7) === calYM).sort()
    .map(s => {
      const name = holOf(s);
      // 振替休日＝祝日撞上周日往后顺延；国民の休日＝两个祝日夹着的那天也放。
      // 这两种是日本这边"多出来的假"，跟国内那种周末补班的调休不是一回事。
      const tag = /振替休日/.test(name) ? '顺延'
        : /国民の休日/.test(name) ? '夹日' : '';
      const wd = WD[(new Date(`${s}T00:00:00Z`).getUTCDay() + 6) % 7];
      const why = holidayNote(name);
      return `<div class="hol-item"><i>${mdOf(s)} 周${wd}</i><span>${esc(name)}</span>`
        + (tag ? `<span class="tag">${tag}</span>` : '') + `</div>`
        + (why ? `<div class="hol-why">${esc(why)}</div>` : '');
    }).join('');

  const head = `${y}-${pad2(m + 1)}`;
  const runs = spans
    .filter(r => r.to >= `${head}-01` && r.from <= `${head}-31`)
    .map(r => {
    // 逐天写出理由：周末就写周几，祝日就写祝日名。连休为什么连得起来，一行就看明白了
    const why = r.days.map(s => {
      const name = holOf(s);
      if (name) return name;
      return new Date(`${s}T00:00:00Z`).getUTCDay() === 6 ? '周六' : '周日';
    }).join(' · ');
    // 两个有俗名的长假。日历上只标不解释的话，听别人说起"黄金周"会对不上号
    const gw = r.days.some(s => ['04-29', '05-03', '05-04', '05-05'].includes(s.slice(5)));
    const sw = !gw && r.n >= 5 && r.days.some(s => s.slice(5, 7) === '09');
    const tag = gw ? '黄金周' : sw ? '白银周' : '';
    return `<div class="run">${mdOf(r.from)} – ${mdOf(r.to)}　<em>连休 ${r.n} 天</em>`
      + (tag ? `<span class="tag">${tag}</span>` : '') + `</div>`
      + `<div class="hol-why flush">${esc(why)}</div>`;
  }).join('');

  const missing = ((HOL[y] || {}).source === 'none')
    ? `<div class="cal-note">${y} 年的祝日表没取到（缓存里没有，也没连上 holidays-jp），
         这一个月只标了周末。</div>`
    : '';

  box.innerHTML = `
    <div class="cal-head">
      <h2>${y} 年 ${m + 1} 月</h2>
      <button type="button" data-cal="prev" title="上个月">‹</button>
      <button type="button" data-cal="today">今天</button>
      <button type="button" data-cal="next" title="下个月">›</button>
    </div>
    <div class="grid">
      ${WD.map((w, i) => `<div class="wd${i === 5 ? ' sat' : i === 6 ? ' sun' : ''}">${w}</div>`).join('')}
      ${cells.map(cell).join('')}
    </div>
    ${missing}
    ${holidays ? `<div class="cal-sec"><b>本月祝日</b>${holidays}</div>` : ''}
    ${runs ? `<div class="cal-sec"><b>连休</b>${runs}</div>` : ''}
    <div class="cal-note">日本没有调休补班，只有多出来的假：祝日撞上周日，补休顺延到之后头一个平日；
      两个祝日中间夹的那天也放。这两种都已经算在上面了。</div>
    <div class="legend">
      <i style="color:var(--sat)">周六</i>
      <i style="color:var(--sun)">周日 · 祝日</i>
      <i><span class="sw"></span>底色是祝日和连休</i>
      <i><span class="dot"></span>有归档，可点</i>
      <i>细框是今天</i>
    </div>`;
  };

  // 缺哪年的祝日表就去取，但**先按手头有的画一遍**：取一年要走网络，最慢 6 秒，
  // 这段时间里整条侧栏空着的话看着就像坏了。拿到之后再画一遍补上祝日。
  const years = [y, cells[0].getUTCFullYear(), cells[cells.length - 1].getUTCFullYear()];
  draw();
  if (years.some(each => !HOL[each])) {
    await ensureHolidays(years);
    draw();
  }
}

async function boot() {
  document.getElementById('generate-date').value = jstNow().date;
  await loadDays();
  window.addEventListener('hashchange', route);
  await route();
}

async function route() {
  if (location.hash === '#expression') {
    document.querySelectorAll('#days a').forEach(a => a.classList.toggle('on', a.dataset.d === 'expression'));
    renderExpression();
    selectedDay = null;
    renderCal();
    window.scrollTo(0, 0);
    return;
  }
  const today = jstNow().date;
  const day = location.hash.slice(1)
    || (knownDays.includes(today) ? today : knownDays[0]);
  if (!day) {
    document.getElementById('main').innerHTML =
      '<p class="empty">归档库里还没有数据，可以从左侧指定日期生成。</p>';
    renderCal();   // 一条归档都没有时日历照样画，至少能看祝日和周末
    return;
  }
  document.querySelectorAll('#days a').forEach(a => a.classList.toggle('on', a.dataset.d === day));
  revealActiveDay();
  // 选中哪天，日历就翻到哪个月；手动翻月的结果保留到下次选日期为止
  selectedDay = day;
  calYM = day.slice(0, 7);
  renderCal();
  const data = await api('/api/day/' + day);
  if (data.error) { document.getElementById('main').innerHTML = `<p class="empty">${esc(data.error)}</p>`; return; }
  render(day, data, await wardrobe());
  focusNow();
}

// 打开页面就停在「现在」这一段上。她的日程一天十段、拉得很长，
// 22:30 那段要滚到最底下才看得见。
function focusNow() {
  const now = document.querySelector('.seg.now');
  if (now) {
    // instant 而不是 smooth：页面刚渲染完就滚，平滑动画反而像页面在抖
    now.scrollIntoView({ block: 'center', behavior: 'instant' });
  } else {
    window.scrollTo(0, 0);
  }
}

function render(day, data, w) {
  const m = data.meta;
  const d = new Date(day + 'T00:00:00');
  const jst = jstNow();
  const isToday = jst.date === day;
  const nowSlot = isToday ? jst.minutes : -1;

  const meta = [
    m.weather && esc(m.weather),
    m.holiday && esc(m.holiday),
    `${m.ok_count}/${m.total_count} 段生成`,
    m.batch_reason && esc(m.batch_reason),
    m.batch_elapsed ? `耗时 ${Math.round(m.batch_elapsed)}s` : '',
    isToday ? `现在 ${jst.clock} JST${jst.wrapped ? '（跨零点，仍算这一天）' : ''}` : '',
  ].filter(Boolean).join('　·　');

  // 此刻那一段的档位。不是今天、或者这一段还是底稿（没有档位）时为 null，
  // 九宫格就没有「现在」标记，默认停在中性 · 一般。
  stylePick = null;
  let current = null;
  for (const s of data.segments) {
    const [a, b] = s.slot.split('-').map(t => +t.slice(0,2) * 60 + +t.slice(3));
    if (nowSlot >= a && nowSlot < b && s.mood_level && s.energy_level) {
      current = {mood: s.mood_level, energy: s.energy_level};
      break;
    }
  }

  const segs = data.segments.map(s => {
    const [a, b] = s.slot.split('-').map(t => +t.slice(0,2) * 60 + +t.slice(3));
    const now = nowSlot >= a && nowSlot < b;
    return `<article class="seg${now ? ' now' : ''}" data-anchor="${esc(s.slot)}">
      <h3><span class="slot">${s.slot}</span>${esc(s.title)}
        ${s.generated ? '' : '<span class="badge">底稿</span>'}
        ${s.negative_level ? `<span class="badge neg">不顺心 · ${esc(s.negative_level)}</span>` : ''}
      </h3>
      <div class="skeleton">${esc(s.place)}　/　<button class="outfit" data-o="${esc(s.outfit)}">${esc(s.outfit)}</button>　/　${esc(s.company)}　/　${esc(s.kind)}</div>
      ${trail(s, now ? nowSlot : -1)}
      <p class="story">${esc(s.story)}</p>
      <dl class="fields">
        <dt>心情</dt><dd>${esc(s.mood)}${stateLevel(s.mood_level)}</dd>
        <dt>体力</dt><dd>${esc(s.physical_state || '—')}${stateLevel(s.energy_level)}</dd>
      </dl>
      ${data.manner_enabled === false ? '' : `<div class="expression">
        <label>表达方式</label>
        <textarea maxlength="200" data-expression>${esc(s.manner)}</textarea>
        <div class="expression-foot">
          <button class="action save-expression" data-day="${day}" data-slot="${esc(s.slot)}">保存</button>
          <span class="job-status"></span>
        </div>
      </div>`}
      ${topicBlock(s, (data.shares || {})[s.slot] || [])}
    </article>`;
  }).join('');

  document.getElementById('main').innerHTML = `
    <div class="dayhead">
      <h2>${day} 周${WD[m.weekday]}${m.aborted ? ' —— 批次中止' : ''}</h2>
      <div class="meta">${meta}</div>
      ${m.aborted ? `<div class="digest">中止原因：${esc(m.aborted)}</div>` : ''}
      ${m.outline ? `<div class="digest">脉络：${esc(m.outline)}</div>`
        : m.day_digest ? `<div class="digest">当日概要：${esc(m.day_digest)}</div>` : ''}
      ${timeline(data, nowSlot)}
      <div class="day-actions">
        <button class="action" id="regen-day" data-day="${day}">重新生成当日日程</button>
        <button class="action" id="regen-topics" data-day="${day}">重新生成 topic</button>
        ${data.manner_enabled === false ? '' : `<button class="action" id="regen-expressions" data-day="${day}">重新生成表达方式</button>`}
        <span id="day-job-status" class="job-status"></span>
      </div>
    </div>${dayOutfits(data.segments, nowSlot, w)}${styleGrid(current)}${segs}`;
}

// 九宫格本体。current 只决定「现在」标记和默认选中格；点别的格子只换预览。
function styleGrid(current) {
  const pick = stylePick || current || {mood: '中性', energy: '一般'};
  stylePick = pick;
  const cells = [];
  for (const mood of MOODS) {
    for (const energy of ENERGIES) {
      const on = pick.mood === mood && pick.energy === energy;
      const now = current && current.mood === mood && current.energy === energy;
      cells.push(`<button type="button" class="style-cell${on ? ' on' : ''}"
        data-mood="${mood}" data-energy="${energy}">${now ? '<span class="whence">现在</span>' : ''}
        ${stateLevel(mood)}${stateLevel(energy)}</button>`);
    }
  }
  const hint = current
    ? '「现在」是此刻那一段的档位。点格子看别的组合。'
    : '这一天没有正在进行的时段，默认停在中性 · 一般。点格子看别的组合。';
  return `<section class="style-panel">
    <h3>表达方式组合${STYLE_OFF}</h3>
    <p class="hint">${hint}</p>
    <div class="style-grid">${cells.join('')}</div>
    <div class="style-out" id="style-out">${styleText(pick)}</div>
  </section>`;
}

// 三段拼起来就是要发给 replyer 的那串，顺序跟 reply_style.compose 一致：
// 口吻 → 体力 → 固定。分色是为了一眼看出哪半句归哪个板块管，也方便发现两块说重了——
// 比如心情已经给过温度，体力那块就不该再给一次。
function styleText(pick) {
  return `<p><span class="part-mood">${esc(MOOD_BLOCK[pick.mood])}</span>`
    + `<span class="part-energy">${esc(ENERGY_BLOCK[pick.mood][pick.energy])}</span>`
    + `<span class="part-fixed">${esc(STYLE_FIXED)}</span></p>`
    + `<p class="legend"><span class="part-mood">心情板块</span>　`
    + `<span class="part-energy">体力板块</span>　`
    + `<span class="part-fixed">固定部分</span></p>`;
}

// 【表达】页：一棵真正带连接线的树。根是拼接规则，往下三个心情节点，每个心情
// 节点再挂它自己那三档体力，全文列出，方便横着比也竖着比。
//
// 每个节点框的第一行都得是「档位标签 + 正文」同一行——横枝的位置是按这个算死的，
// 节点里多加一行标题就会跟线错开。固定部分只在根节点出现一次：九片叶子上印九遍
// 同一句，真正在变的那半句反而看不出来了。
function renderExpression() {
  const nodes = MOODS.map(mood => {
    const leaves = ENERGIES.map(energy => `<li>
      <div class="node leaf">${stateLevel(energy)}<p>${esc(ENERGY_BLOCK[mood][energy])}</p></div>
    </li>`).join('');
    return `<li>
      <div class="node mood">${stateLevel(mood)}<p>${esc(MOOD_BLOCK[mood])}</p></div>
      <ul>${leaves}</ul>
    </li>`;
  }).join('');

  document.getElementById('main').innerHTML = `
    <div class="dayhead">
      <h2>表达方式${STYLE_OFF}</h2>
      <div class="meta">心情决定口吻，体力决定这个口吻还剩多少输出　·　${MOODS.length} × ${ENERGIES.length} = ${MOODS.length * ENERGIES.length} 格</div>
    </div>
    <div class="tree">
      <div class="tree-root">
        <h3>reply_style ＝ 口吻 ＋ 体力 ＋ 固定</h3>
        <p>整段替换主程序配置里的那份。每条路径末尾都接同一句固定部分：</p>
        <p class="fixed-text">${esc(STYLE_FIXED)}</p>
      </div>
      <ul>${nodes}</ul>
    </div>
    <div class="tree-note">
      <p>红色竖带的是心情节点，它下面挂的三条是这一档心情<b>专属</b>的体力写法——三档心情各有各的一组，改一组不会牵动另一组。</p>
      <p>正面和中性眼下三档字面全同，但已经是两张独立的表——改一边不会牵动另一边。</p>
      <p>底稿段（没有档位）整套不替换，沿用 bot_config.toml 里配置的那份。</p>
      ${STYLE_TABLE.enabled === false ? '<p><b>这套功能当前是关闭的</b>（插件 config.toml 的 <code>[reply_style] enabled</code>）。下面的文案照常可以改，但 replyer 现在收到的是 bot_config.toml 里的 reply_style 原文。</p>' : ''}
    </div>`;
}

function stateLevel(value) {
  const name = String(value || '').trim();
  if (!name) return '';
  const cls = {
    '正面': 'positive', '中性': 'neutral', '负面': 'negative',
    '良好': 'good', '一般': 'neutral', '不佳': 'poor',
  }[name] || 'neutral';
  return `<span class="state-level ${cls}">${esc(name)}</span>`;
}

async function api(path, options = {}) {
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok || data.error) throw new Error(data.error || `请求失败：${response.status}`);
  return data;
}

async function enqueue(payload, statusNode, button) {
  statusNode.classList.remove('error');
  statusNode.textContent = '已排队，等待 bot 处理…';
  button.disabled = true;
  try {
    const created = await api('/api/jobs', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload),
    });
    while (true) {
      await new Promise(resolve => setTimeout(resolve, 1000));
      const job = await api('/api/job/' + created.id);
      if (job.status === 'pending') {
        statusNode.textContent = '已排队，等待 bot 处理…';
      } else if (job.status === 'running') {
        statusNode.textContent = 'bot 正在处理…';
      } else if (job.status === 'completed') {
        statusNode.textContent = job.result || '已完成';
        return job;
      } else if (job.status === 'failed') {
        throw new Error(job.error || '任务执行失败');
      }
    }
  } catch (error) {
    statusNode.classList.add('error');
    statusNode.textContent = error.message;
    throw error;
  } finally {
    button.disabled = false;
  }
}

async function refreshDay(day) {
  await loadDays();
  location.hash = day;
  await route();
}

// 地点时段轴。当前那一段高亮——只在"现在"落在这个时段里时才标。
function slotMinutes(slot) {
  // 「HH:MM-HH:MM」→ [起, 止]，分钟数。跨零点那段写成 24:00-26:00，
  // 直接算出来就是 1440~1560，不用特判。
  const [a, b] = slot.split('-');
  return [+a.slice(0,2) * 60 + +a.slice(3), +b.slice(0,2) * 60 + +b.slice(3)];
}

// 全天时间条。整天是一个逻辑日 = 首段起点起算的 24 小时，所以宽度按分钟数分。
function timeline(data, nowMin) {
  const segs = data.segments || [];
  if (!segs.length) return '';
  const gate = data.gate || {};
  const freshMinutes = +gate.fresh_minutes || 30;
  const busy = gate.busy_kinds || [];
  const idle = gate.idle_kinds || [];
  const dayStart = slotMinutes(segs[0].slot)[0];
  const total = 1440;

  const cells = segs.map((s, i) => {
    const [a, b] = slotMinutes(s.slot);
    const span = Math.max(1, b - a);
    // 颜色只分三档：睡眠、忙（工作·上学）、闲（自由·外出·通勤）
    const cls = s.kind === '睡眠' ? 'sleep' : (busy.includes(s.kind) ? 'busy' : 'idle');
    const hasTopic = !!(s.topic || '').trim();
    // 新鲜期只画在有 topic 的段上——没 topic 的段窗口本来就作废
    const fresh = hasTopic
      ? `<span class="fresh" style="width:${Math.min(100, freshMinutes / span * 100)}%"></span>`
      : '';
    const tip = `${s.slot}　${s.title}（${s.kind}）${hasTopic ? '　有可说的事' : ''}`;
    // 格子里写 kind 不写标题：标题动辄十几个字，60 分钟的格子只有四十来像素，
    // 挤进去只能截成半截。kind 两个字一定放得下，完整标题留给 tooltip。
    return `<button class="band-cell ${cls}" style="flex:${span} 1 0" data-jump="${esc(s.slot)}"
      title="${esc(tip)}">${fresh}<span class="label">${esc(s.kind)}</span></button>`;
  }).join('');

  // 断点：上一段"不能玩手机"、这一段"能"。首段的上一段属于前一天，
  // 这个页面只有当天数据，所以 i=0 不画——实际首段是睡眠，本来也不构成断点。
  const marks = segs.map((s, i) => {
    if (i === 0) return '';
    if (!(busy.includes(segs[i - 1].kind) && idle.includes(s.kind))) return '';
    const left = (slotMinutes(s.slot)[0] - dayStart) / total * 100;
    return `<i class="brk" style="left:${left}%" title="断点：上一段是${esc(segs[i - 1].kind)}"></i>`;
  }).join('') + (nowMin >= 0
    ? `<i class="now" style="left:${(nowMin - dayStart) / total * 100}%" title="现在"></i>` : '');

  const axis = segs.map(s => {
    const [a, b] = slotMinutes(s.slot);
    const span = Math.max(1, b - a);
    // 窄格子放不下标签，留空免得挤成一团
    const text = span / total * 100 >= 4 ? String(a / 60 | 0).padStart(2, '0') + ':' + String(a % 60).padStart(2, '0') : '';
    return `<span style="flex:${span} 1 0">${text}</span>`;
  }).join('');

  return `<div class="band-wrap">
    <div class="band">${cells}</div>
    <div class="band-marks">${marks}</div>
    <div class="band-axis">${axis}</div>
    <div class="band-legend">
      <span><i style="background:var(--kind-sleep)"></i>睡眠</span>
      <span><i style="background:var(--kind-busy)"></i>工作 · 上学</span>
      <span><i style="background:var(--kind-idle)"></i>自由 · 外出 · 通勤</span>
      <span><i class="hatch"></i>新鲜期（有可说的事，前 ${freshMinutes} 分钟）</span>
      <span style="color:var(--accent)">▼ 断点</span>
      ${nowMin >= 0 ? '<span>┆ 现在</span>' : ''}
    </div>
  </div>`;
}

function trail(s, nowMin) {
  let ps = [];
  try { ps = JSON.parse(s.places || '[]'); } catch (e) { ps = []; }
  if (!ps.length) return '';
  const cells = ps.map(p => {
    const a = +p.from.slice(0,2) * 60 + +p.from.slice(3);
    const b = +p.to.slice(0,2) * 60 + +p.to.slice(3);
    const on = nowMin >= a && nowMin < b;
    return `<span class="leg${on ? ' on' : ''}">${esc(p.from)}-${esc(p.to)}　${esc(p.place)}</span>`;
  }).join('');
  return `<div class="trail">${cells}</div>`;
}

function topicBlock(s, shares) {
  if (!s.topic) return '<div class="topic none">这段没什么好说的</div>';
  // 「用掉」现在等于 planner 调过 get_mittes_topic；旧记录里的 hit_key 是关键词，
  // 新记录里是"工具调用"，两种都照原样显示
  const rows = shares.map(r => {
    if (!r.shared_at) {
      return `<div class="share">注入 ${r.injected} 次${r.pitched ? `　取材 ${r.pitched} 次` : ''}，还没用掉</div>`;
    }
    const hit = r.hit_key ? `　${esc(r.hit_key)}` : '';
    const said = r.reply_text ? `<div class="said">${esc(r.reply_text)}</div>` : '';
    return `<div class="share"><b>已用掉 ${esc(r.shared_at.slice(11, 16))}</b>`
      + `　注入 ${r.injected} 次${r.pitched ? `　取材 ${r.pitched} 次` : ''}${hit}${said}</div>`;
  }).join('');
  return `<div class="topic">
    <span class="tag">可说</span>${esc(s.topic)}
    ${rows || '<div class="share">还没在任何会话里注入过</div>'}
  </div>`;
}

// 衣柜。一次取回缓存住——同一页里会被点很多次。
let _wardrobe = null;
async function wardrobe() {
  if (!_wardrobe) _wardrobe = await (await fetch('/api/wardrobe')).json();
  return _wardrobe;
}

const PART_LABELS = [['hair','头发'],['top','上身'],['bottom','下身'],
                     ['legs','腿'],['feet','脚'],['accessories','配饰'],
                     ['underwear','内衣与打底'],['optional','有时会加']];
const ALWAYS_LABELS = [['hair_base','头发底子'],['nails','美甲'],['props','道具']];

function outfitRows(o, part = 'outer') {
  return PART_LABELS.filter(([k]) => o[k] && (part === 'inner' ? k === 'underwear' : k !== 'underwear'))
    .map(([k, label]) => `<dt>${label}</dt><dd>${esc(o[k])}</dd>`).join('');
}

// URL 在前端构造，让面板的 /embed/schedule/ 代理与独立 viewer 使用同一路径。
function outfitImageUrl(name, w, part = 'outer') {
  const names = part === 'inner' ? w.inner_images : w.images;
  if (!(names || []).includes(name)) return '';
  const version = w.image_versions?.[part]?.[name] || '';
  return '/api/outfit-image?name=' + encodeURIComponent(name) + '&part=' + part + '&v=' + encodeURIComponent(version);
}

function outfitDescription(o, part = 'outer') {
  return o ? `<details class="outfit-details"><summary>${part === 'inner' ? '内搭' : '外搭'}文字说明</summary>
      <dl class="fields">${outfitRows(o, part)}</dl></details>` : '';
}

function alwaysDescription(w, className = '') {
  return `<details class="outfit-details ${className}"><summary>固定细节 · 发型、美甲与随身物品</summary>
    <dl class="fields">${ALWAYS_LABELS.filter(([k]) => (w.always || {})[k])
      .map(([k, label]) => `<dt>${label}</dt><dd>${esc(w.always[k])}</dd>`).join('')}</dl></details>`;
}

function outfitPanel(name, w, part) {
  const url = outfitImageUrl(name, w, part);
  const o = (w.wardrobe || {})[name];
  const label = part === 'inner' ? '内搭' : '外搭';
  const picture = url ? `<button type="button" class="outfit-picture" data-status="loading" data-o="${esc(name)}" data-part="${part}" aria-label="放大查看${esc(name)}${label}">
      <img src="${esc(url)}" alt="${esc(name)}${label}设定图" width="1536" height="1024" loading="lazy" decoding="async">
      <span class="zoom-hint" aria-hidden="true">查看大图 ↗</span></button>`
    : `<p class="outfit-missing">${o ? label + '设定图尚未添加' : '当日临时搭配 · 暂无设定图'}</p>`;
  return `<section class="outfit-panel" aria-label="${esc(name)}${label}"><div class="outfit-panel-head"><h4>${label}</h4><span>独立设定图</span></div>${picture}${outfitDescription(o, part)}</section>`;
}

function outfitCard(name, w, current = false) {
  return `<article class="outfit-card"><div class="outfit-caption">
      ${current ? '<div class="outfit-current">此刻穿着</div>' : ''}<h3>${esc(name)}</h3>
    </div><div class="outfit-panels">${outfitPanel(name, w, 'outer')}${outfitPanel(name, w, 'inner')}</div></article>`;
}

function dayOutfits(segments, nowSlot, w) {
  const names = [...new Set(segments.map(s => s.outfit).filter(Boolean))];
  if (!names.length) return '';
  if (w.error) return `<section class="outfit-section"><h3>当日穿搭</h3><p class="empty">${esc(w.error)}</p></section>`;
  const current = segments.find(s => {
    const [a, b] = slotMinutes(s.slot);
    return nowSlot >= a && nowSlot < b;
  });
  // 当前搭配排在最前，归档日则保持当天首次穿着的顺序。
  if (current && names.includes(current.outfit)) {
    names.splice(names.indexOf(current.outfit), 1);
    names.unshift(current.outfit);
  }
  return `<section class="outfit-section" aria-label="当日穿搭">
    <div class="outfit-section-head"><h3>当日穿搭</h3><span>${names.length} 套</span></div>
    <div class="outfit-strip" tabindex="0" aria-label="当日服饰图片，可横向滚动">${names.map(name => outfitCard(name, w, name === current?.outfit)).join('')}</div>
    </section>`;
}

let outfitTrigger = null;
function closeOutfit() {
  document.getElementById('modal').classList.remove('on');
  document.body.style.overflow = '';
  document.querySelectorAll('body > aside, body > main').forEach(node => node.inert = false);
  if (outfitTrigger?.isConnected) outfitTrigger.focus({preventScroll: true});
}

// 点段落里的穿搭名或画廊图片 → 先看完整设定图，文字默认收起。
async function showOutfit(name, part = 'outer') {
  outfitTrigger = document.activeElement;
  const w = await wardrobe();
  const o = (w.wardrobe || {})[name];
  const url = outfitImageUrl(name, w, part);
  const label = part === 'inner' ? '内搭' : '外搭';
  const body = w.error ? `<p class="none">${esc(w.error)}</p>`
    : url ? `<img class="outfit-full" src="${esc(url)}" alt="${esc(name)}${label}设定图" width="1536" height="1024">
        <a class="original" href="${esc(url)}" target="_blank" rel="noopener">查看原图 ↗</a>`
    : `<p class="none">${o ? '这套穿搭尚未添加设定图。' : '这一身不在衣柜里，是当天临时定的搭配，暂无设定图。'}</p>`;
  const always = o ? alwaysDescription(w) : '';
  document.getElementById('modal').innerHTML =
    `<div class="sheet" role="dialog" aria-modal="true" aria-labelledby="outfit-title">
      <button type="button" class="close" aria-label="关闭服饰详情">×</button>
      <h3 id="outfit-title">${esc(name)} · ${label}</h3>${body}${outfitDescription(o, part)}${always}</div>`;
  document.getElementById('modal').classList.add('on');
  document.body.style.overflow = 'hidden';
  document.querySelectorAll('body > aside, body > main').forEach(node => node.inert = true);
  document.querySelector('#modal .close').focus({preventScroll: true});
}

// 请求失败时给出明确提示，不能留下看起来像已加载的破图。
document.addEventListener('load', e => {
  if (e.target instanceof HTMLImageElement && e.target.closest('.outfit-picture')) {
    e.target.closest('.outfit-picture').dataset.status = 'ready';
  }
}, true);
document.addEventListener('error', e => {
  const img = e.target;
  if (!(img instanceof HTMLImageElement) || !img.closest('.outfit-picture, #modal')) return;
  const message = document.createElement('p');
  message.className = 'outfit-image-error';
  message.textContent = '设定图加载失败，请刷新页面重试';
  const picture = img.closest('.outfit-picture');
  if (picture) picture.dataset.status = 'error';
  img.replaceWith(message);
}, true);

// 日程逻辑日的起点（分钟）。骨架各天首段都是 02:00。
const DAY_START = 2 * 60;

// 「现在」一律按 Mittes 那边的时间（JST）算，不看打开页面的人在哪个时区。
// 原来的写法把两个时区混着用：日期取 toISOString（UTC）、时刻取 getHours（浏览器本地），
// 结果连在这台 JST 机器上都是错的——00:00~09:00 JST 之间 UTC 还停在前一天。
function jstNow() {
  const p = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Asia/Tokyo',
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', hour12: false,
  }).formatToParts(new Date()).reduce((o, x) => (o[x.type] = x.value, o), {});
  // 逻辑日：日程从 02:00 起算，跨零点那段在骨架里写成 24:00-26:00。
  // 所以 00:00~02:00 属于**前一天**，分钟数要 +1440 才落得进那个区间。
  const raw = (+p.hour % 24) * 60 + +p.minute;
  const wrapped = raw < DAY_START;
  const d = new Date(`${p.year}-${p.month}-${p.day}T00:00:00Z`);
  if (wrapped) d.setUTCDate(d.getUTCDate() - 1);
  return {
    date: d.toISOString().slice(0, 10),
    minutes: wrapped ? raw + 1440 : raw,
    clock: `${(+p.hour % 24) < 10 ? '0' : ''}${+p.hour % 24}:${p.minute}`,
    wrapped,
  };
}
// 点穿搭名弹层；点空白或 × 关掉
document.addEventListener('click', e => {
  const page = e.target.closest('#cal [data-cal]');
  if (page) {
    if (page.dataset.cal === 'today') {
      calYM = jstNow().date.slice(0, 7);
    } else {
      const step = page.dataset.cal === 'next' ? 1 : -1;
      calYM = isoOf(new Date(Date.UTC(+calYM.slice(0, 4), +calYM.slice(5, 7) - 1 + step, 1))).slice(0, 7);
    }
    renderCal();
    return;
  }
  const jump = e.target.closest('.band-cell');
  if (jump) {
    const target = document.querySelector(`.seg[data-anchor="${CSS.escape(jump.dataset.jump)}"]`);
    if (target) target.scrollIntoView({ block: 'center', behavior: 'smooth' });
    return;
  }

  const cell = e.target.closest('.style-cell');
  if (cell) {
    stylePick = {mood: cell.dataset.mood, energy: cell.dataset.energy};
    document.querySelectorAll('.style-cell').forEach(node => node.classList.toggle(
      'on', node.dataset.mood === stylePick.mood && node.dataset.energy === stylePick.energy));
    document.getElementById('style-out').innerHTML = styleText(stylePick);
    return;
  }

  const more = e.target.closest('#more-days');
  if (more) { toggleOlder(document.getElementById('older-days').hidden); return; }

  const btn = e.target.closest('.outfit, .outfit-picture');
  if (btn) { showOutfit(btn.dataset.o, btn.dataset.part || 'outer'); return; }

  const save = e.target.closest('.save-expression');
  if (save) {
    const box = save.closest('.expression');
    const status = box.querySelector('.job-status');
    const manner = box.querySelector('[data-expression]').value.trim();
    enqueue({action: 'set_expression', date: save.dataset.day, slot: save.dataset.slot, manner}, status, save)
      .catch(() => {});
    return;
  }

  const regenDay = e.target.closest('#regen-day');
  if (regenDay) {
    const status = document.getElementById('day-job-status');
    enqueue({action: 'regenerate_day', date: regenDay.dataset.day}, status, regenDay)
      .then(() => refreshDay(regenDay.dataset.day)).catch(() => {});
    return;
  }

  const regenTopics = e.target.closest('#regen-topics');
  if (regenTopics) {
    const status = document.getElementById('day-job-status');
    enqueue({action: 'regenerate_topics', date: regenTopics.dataset.day}, status, regenTopics)
      .then(() => refreshDay(regenTopics.dataset.day)).catch(() => {});
    return;
  }

  const regenExpressions = e.target.closest('#regen-expressions');
  if (regenExpressions) {
    const status = document.getElementById('day-job-status');
    enqueue({action: 'regenerate_expressions', date: regenExpressions.dataset.day}, status, regenExpressions)
      .then(() => refreshDay(regenExpressions.dataset.day)).catch(() => {});
    return;
  }

  const modal = document.getElementById('modal');
  if (e.target.closest('.close') || e.target === modal) closeOutfit();
});
document.getElementById('generate-day').addEventListener('click', () => {
  const day = document.getElementById('generate-date').value;
  const status = document.getElementById('generate-status');
  const button = document.getElementById('generate-day');
  if (!day) {
    status.classList.add('error');
    status.textContent = '请先选择日期';
    return;
  }
  enqueue({action: 'generate_day', date: day}, status, button)
    .then(() => refreshDay(day)).catch(() => {});
});
document.addEventListener('keydown', e => {
  const modal = document.getElementById('modal');
  if (!modal.classList.contains('on')) return;
  if (e.key === 'Escape') closeOutfit();
  if (e.key === 'Tab') {
    const focusable = [...modal.querySelectorAll('button, a, summary')];
    const first = focusable[0], last = focusable.at(-1);
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  }
});

boot();
</script>
</body>
</html>
"""


def _page_bytes() -> bytes:
    """把表达方式表嵌进页面再发。

    ``<`` 转义掉是因为这段 JSON 落在 ``<script>`` 里：正文出现 ``</script>`` 会提前
    截断脚本。现在的文案里没有尖括号，但这张表是随时会改的文案，不能指望它一直没有。
    """
    table = json.dumps(
        {
            "voice": reply_style.VOICE,
            "fixed": reply_style.FIXED,
            "stamina": reply_style.STAMINA,
            "enabled": _reply_style_enabled(CONFIG_PATH),
            "moods": list(reply_style.MOODS),
            "energies": list(reply_style.ENERGIES),
        },
        ensure_ascii=False,
    ).replace("<", "\\u003c")
    return PAGE.replace("__STYLE_TABLE__", table).encode("utf-8")


def _lan_address() -> str:
    """取本机在局域网里的地址。

    连一个不存在的外部地址不会真的发包，但内核会为此选好出口网卡，
    再从 socket 上读回本地地址——比解析主机名可靠。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        try:
            probe.connect(("10.255.255.255", 1))
            return str(probe.getsockname()[0])
        except OSError:
            return ""


def main() -> int:
    parser = argparse.ArgumentParser(description="日程归档浏览器")
    parser.add_argument("--port", type=int, default=8765, help="监听端口，默认 8765")
    parser.add_argument("--db", type=Path, default=DB_PATH, help="归档库路径")
    parser.add_argument("--wardrobe", type=Path, default=WARDROBE_PATH, help="衣柜文件路径")
    parser.add_argument(
        "--lan",
        action="store_true",
        help="监听 0.0.0.0，局域网内可访问。无鉴权，别映射到公网",
    )
    parser.add_argument("--host", default="", help="自定义监听地址，优先级高于 --lan")
    args = parser.parse_args()

    if not args.db.exists():
        print(f"归档库不存在：{args.db}", file=sys.stderr)
        print("等 12:00 的批次跑完，或在聊天里用 /status batch today 手动生成一次。", file=sys.stderr)
        return 1

    host = args.host or ("0.0.0.0" if args.lan else "127.0.0.1")  # noqa: S104 —— --lan 就是要对局域网开放

    Handler.db_path = args.db
    Handler.wardrobe_path = args.wardrobe
    today = date.today()
    # flush 是必要的：输出重定向到文件或被 systemd 接管时，
    # 不 flush 的话这几行会一直卡在缓冲区里，看不到访问地址
    lines = ["日程归档浏览器"]
    if host in ("0.0.0.0", "::"):
        lan = _lan_address()
        if lan:
            lines.append(f"　局域网：http://{lan}:{args.port}")
        lines.append(f"　本机　：http://127.0.0.1:{args.port}")
        lines.append("　注意：没有鉴权，局域网用户可查看、编辑并触发生成")
    else:
        lines.append(f"　http://{host}:{args.port}")
    lines.append(f"　库：{args.db}")
    lines.append(f"　今天是 {today.isoformat()} 周{WEEKDAY_NAMES[today.weekday()]}　（Ctrl-C 退出）")
    print("\n".join(lines), flush=True)

    with ThreadingHTTPServer((host, args.port), Handler) as server:
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\n已退出")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
