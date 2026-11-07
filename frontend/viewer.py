"""日程归档与管理页面。

把 ``data/schedule.db`` 里的日程按天翻出来看，也能通过任务队列编辑表达方式、
生成日程和重跑 topic。任务由正在运行的插件领取，viewer 自己不调用 LLM。

    uv run python plugins/04_a_day_with_mittes/frontend/viewer.py          # 只有本机能开
    uv run python plugins/04_a_day_with_mittes/frontend/viewer.py --lan    # 局域网内可开

只用标准库，不引任何依赖；每个请求短连接 SQLite，WAL 模式下不会长期占写锁。

**``--lan`` 没有任何鉴权**：同一局域网里知道地址的人都能看到全部日程内容。
这是自用小工具的取舍，别往公网端口映射。
"""

from contextlib import contextmanager
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator
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


@contextmanager
def _connect(db_path: Path, *, readonly: bool = True) -> Iterator[sqlite3.Connection]:
    """每个 HTTP 请求使用自己的短连接，避免 ThreadingHTTPServer 共享连接。

    必须用完即关：sqlite3.Connection 自带的 ``with`` 只提交/回滚事务、不关闭连接，
    曾因此每个请求漏一对 db/wal 句柄，跑一周撞满 1024 个 fd 后 /api/days 全部失败。
    """
    if readonly:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    else:
        conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        yield conn
    finally:
        conn.close()


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


def _active_jobs(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """排队/执行中的任务，加上 90 秒内刚结束的，供页面刷新后接着显示进度（面板顶栏也读它）。"""
    rows = conn.execute(
        "SELECT * FROM admin_jobs WHERE status IN ('pending', 'running') "
        "OR (finished_at != '' AND finished_at >= datetime('now', '-90 seconds')) ORDER BY id"
    ).fetchall()
    return [dict(row) for row in rows]


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
        if path == "/api/jobs/active":
            with _connect(self.db_path) as conn:
                self._send_json({"jobs": _active_jobs(conn)})
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
  display: grid; grid-template-columns: 284px minmax(0, 1fr); min-height: 100vh;
}
button { color: inherit; }

/* ── 侧栏：日历是主导航，最近几天带状态标记，生成表单压在最底下 ──
   原来左边日期列表、右边日历各占一栏，两边都是在选日子；合成一栏，正文拿到整块宽度。 */
#rail {
  border-right: 1px solid var(--line); background: var(--bg);
  height: 100vh; position: sticky; top: 0; overflow-y: auto;
  padding: 18px 16px 28px; display: flex; flex-direction: column; gap: 16px;
}
#rail h1 { font-size: 15px; margin: 0; letter-spacing: .05em; }
.rail-sec > summary, .rail-label {
  font-size: 11px; font-weight: 600; letter-spacing: .08em; color: var(--muted);
  margin-bottom: 8px; list-style: none;
}
.rail-sec > summary { cursor: pointer; }
.rail-sec > summary::-webkit-details-marker { display: none; }
#days { display: flex; flex-direction: column; gap: 1px; }
#days a {
  display: flex; align-items: baseline; gap: 6px; padding: 5px 9px; border-radius: 6px;
  color: var(--text); text-decoration: none; font-variant-numeric: tabular-nums; font-size: 13.5px;
}
#days a:hover { background: var(--badge); }
#days a.on { background: var(--accent); color: var(--band-ink); }
#days .sub { margin-left: auto; color: var(--muted); font-size: 11.5px; }
#days a.on .sub { color: inherit; opacity: .85; }
#more-days {
  font: inherit; font-size: 12px; color: var(--muted); text-align: left;
  background: none; border: 0; padding: 6px 9px 0; cursor: pointer;
}
#more-days:hover { color: var(--text); }
.create-day { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 6px; }
.create-day input { min-width: 0; padding: 5px 8px; font-size: 13px; }
.create-day button { font-size: 13px; padding: 5px 10px; }
.create-day .job-status { grid-column: 1 / -1; margin: 0; }
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
#cal .cal-more { margin-top: 12px; border-top: 1px solid var(--line); padding-top: 8px; }
#cal .cal-more > summary { font-size: 12px; color: var(--muted); cursor: pointer; }
#cal .cal-more > summary:hover { color: var(--text); }

main { min-width: 0; padding: 26px 36px 72px; max-width: 1240px; width: 100%; }

/* ── 当日抬头 ── */
.dayhead { margin-bottom: 22px; }
.dayhead-top { display: flex; flex-wrap: wrap; align-items: flex-start; gap: 10px 20px; }
.dayhead-title { flex: 1; min-width: 240px; }
.dayhead h2 { margin: 0; font-size: 24px; line-height: 1.35; font-variant-numeric: tabular-nums; }
.dayhead h2 .wd { color: var(--muted); font-weight: 500; margin-left: 6px; font-size: 18px; }
.pill {
  display: inline-block; vertical-align: 4px; margin-left: 8px; padding: 0 8px; border-radius: 999px;
  font-size: 11.5px; line-height: 20px; font-weight: 600; background: var(--accent); color: var(--band-ink);
}
.pill.warn { background: var(--kind-busy); }
.chips { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
.chip {
  font-size: 12px; color: var(--muted); background: var(--badge); border-radius: 6px;
  padding: 1px 8px; font-variant-numeric: tabular-nums;
}
.chip.strong { color: var(--text); }
.day-actions { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; }
.day-actions .action { font-size: 12.5px; padding: 4px 10px; color: var(--muted); background: transparent; }
.day-actions .action:hover { color: var(--accent); }
.day-actions .job-status { margin: 0; flex-basis: 100%; text-align: right; }
.notice {
  margin-top: 12px; padding: 9px 12px; border-radius: 8px; font-size: 13.5px;
  background: var(--hol-bg); color: var(--text);
}
/* 脉络默认收成两行：它是写手的构思，翻日程时偶尔才需要通读 */
.outline {
  margin: 14px 0 0; font-size: 13.5px; color: var(--muted); cursor: pointer;
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
}
.outline.open { display: block; }
.outline b { color: var(--text); font-weight: 600; margin-right: 6px; }
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

/* ── 正文：左边议程，右边当日穿搭（宽屏）；窄了穿搭挪到议程下面横排 ── */
.day-body { display: grid; grid-template-columns: minmax(0, 1fr) 248px; gap: 28px; align-items: start; }
.sec-head { display: flex; align-items: baseline; gap: 10px; margin: 0 0 10px; }
.sec-head h3 { margin: 0; font-size: 15px; }
.sec-head .count { color: var(--muted); font-size: 12px; }
.sec-head .link {
  margin-left: auto; font: inherit; font-size: 12.5px; color: var(--muted);
  background: none; border: 0; padding: 0; cursor: pointer;
}
.sec-head .link:hover { color: var(--accent); }

/* 一段一行。summary 只放扫一眼要的东西：时间、做什么、在哪跟谁、几个标记；
   story 和 topic 收在里面，现在那一段默认展开。左边那条色带就是时间条上的 kind 颜色。 */
.seg {
  background: var(--panel); border: 1px solid var(--line); border-radius: 10px;
  margin-bottom: 6px; box-shadow: inset 4px 0 0 var(--kind-idle);
}
.seg.sleep { box-shadow: inset 4px 0 0 var(--kind-sleep); }
.seg.busy { box-shadow: inset 4px 0 0 var(--kind-busy); }
.seg.now { border-color: var(--accent); }
.seg > summary {
  display: grid; grid-template-columns: 58px minmax(0, 1fr) auto; gap: 4px 14px; align-items: center;
  padding: 10px 14px 10px 18px; cursor: pointer; list-style: none;
}
.seg > summary::-webkit-details-marker { display: none; }
.seg > summary:hover { background: color-mix(in srgb, var(--badge) 55%, transparent); border-radius: 10px; }
.seg[open] > summary { border-bottom: 1px dashed var(--line); border-radius: 10px 10px 0 0; }
.seg-time { display: flex; flex-direction: column; line-height: 1.3; font-variant-numeric: tabular-nums; }
.seg-time b { font-size: 14.5px; }
.seg-time i { font-style: normal; font-size: 11.5px; color: var(--muted); }
.seg-main { min-width: 0; display: flex; flex-direction: column; line-height: 1.45; }
.seg-title { font-weight: 600; font-size: 14.5px; }
.seg-sub { font-size: 12px; color: var(--muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.seg-flags { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 4px; max-width: 220px; }
.seg-flags .state-level { margin-left: 0; }
.flag {
  display: inline-block; border-radius: 4px; padding: 0 6px; font-size: 11.5px; line-height: 19px;
  background: var(--badge); color: var(--muted); white-space: nowrap;
}
.flag.now { background: var(--accent); color: var(--band-ink); font-weight: 600; }
.flag.say { color: var(--accent); background: transparent; box-shadow: inset 0 0 0 1px var(--accent); }
.flag.say.used { color: var(--muted); box-shadow: inset 0 0 0 1px var(--line); }
.flag.neg { background: var(--accent); color: #fff; }
.seg-body { padding: 12px 18px 16px 18px; }
.seg-body .story { margin: 10px 0 12px; }
.seg-body .fields { margin: 0; }
.fields { display: grid; grid-template-columns: 62px 1fr; gap: 4px 10px; font-size: 13.5px; }
.fields dt { color: var(--muted); }
.fields dd { margin: 0; }
.trail { display: flex; flex-wrap: wrap; gap: 6px; }
.trail .leg {
  font-size: 12px; color: var(--muted); background: var(--badge);
  padding: 1px 8px; border-radius: 999px; white-space: nowrap; font-variant-numeric: tabular-nums;
}
.trail .leg.on { color: var(--band-ink); background: var(--accent); font-weight: 600; }
.seg-body .outfit {
  font: inherit; color: var(--accent); background: none; border: 0; padding: 0;
  cursor: pointer; border-bottom: 1px dashed currentColor;
}
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
.topic.used .tag { background: transparent; color: var(--muted); box-shadow: inset 0 0 0 1px var(--line); }
.topic.used .topic-text { color: var(--muted); text-decoration: line-through; text-decoration-color: var(--line); }
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
.job-status { color: var(--muted); font-size: 12px; margin-top: 6px; min-height: 1.4em; }
.job-status.error { color: var(--accent); }
/* 排队/执行中：状态文字前一个转圈；刷新页面后会从 /api/jobs/active 接着显示 */
.job-status.busy::before, button.action.busy::before {
  content: ''; display: inline-block; width: 10px; height: 10px; margin-right: 6px; vertical-align: -1px;
  border: 1.5px solid currentColor; border-right-color: transparent; border-radius: 50%;
  animation: job-spin .8s linear infinite;
}
.job-status.done { color: #3a9a6a; }
@keyframes job-spin { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) { .job-status.busy::before, button.action.busy::before { animation-duration: 2.4s; } }
.expression { margin-top: 12px; border-top: 1px solid var(--line); padding-top: 11px; }
.expression label { display: block; color: var(--muted); font-size: 12.5px; margin-bottom: 5px; }
.expression textarea {
  display: block; width: 100%; min-height: 62px; resize: vertical; line-height: 1.55;
}
.expression-foot { display: flex; align-items: center; gap: 9px; margin-top: 7px; }
.expression-foot .job-status { margin: 0; flex: 1; }

/* ── 当日穿搭：缩略图，点开是原来的大图弹层 ── */
.outfits { position: sticky; top: 20px; }
.fits { display: flex; flex-direction: column; gap: 12px; }
.fit { background: var(--panel); border: 1px solid var(--line); border-radius: 10px; overflow: hidden; }
.fit.now { border-color: var(--accent); }
.fit .outfit-picture img { aspect-ratio: 3 / 2; }
.fit-info { padding: 8px 12px 10px; }
.fit-info h4 { margin: 0; font-size: 13px; line-height: 1.5; overflow-wrap: anywhere; }
.fit-info .when { font-size: 11.5px; color: var(--muted); font-variant-numeric: tabular-nums; }
.fit-info .now-tag { color: var(--accent); font-size: 11px; font-weight: 600; margin-right: 6px; }
.fit-links { display: flex; gap: 12px; margin-top: 4px; }
.fit-links .outfit {
  font: inherit; font-size: 12px; color: var(--muted); background: none; border: 0; padding: 0; cursor: pointer;
}
.fit-links .outfit:hover { color: var(--accent); }
.fit .outfit-missing { aspect-ratio: 3 / 2; margin: 0; display: grid; place-items: center;
  font-size: 12px; color: var(--muted); background: var(--badge); padding: 10px; text-align: center; }
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
#modal .part-switch { display: flex; gap: 6px; margin: -4px 0 12px; }
#modal .part-switch .outfit {
  font: inherit; font-size: 12.5px; padding: 2px 10px; border-radius: 999px; cursor: pointer;
  border: 1px solid var(--line); background: none; color: var(--muted);
}
#modal .part-switch .outfit.on { background: var(--accent); border-color: var(--accent); color: var(--band-ink); }

@media (max-width: 1280px) {
  .day-body { grid-template-columns: minmax(0, 1fr); }
  .outfits { position: static; }
  .fits { flex-direction: row; overflow-x: auto; padding-bottom: 6px; scroll-snap-type: x proximity; }
  .fit { flex: 0 0 220px; scroll-snap-align: start; }
}
/* 手机：先看正文。侧栏收成顶上一条——最近几天横排，日历折叠 */
@media (max-width: 860px) {
  body { display: block; }
  #rail { height: auto; position: static; border-right: 0; border-bottom: 1px solid var(--line); padding: 14px 16px; gap: 12px; }
  #days { flex-direction: row; overflow-x: auto; gap: 4px; padding-bottom: 4px; }
  #days a { flex: none; background: var(--badge); }
  #days a .sub { margin-left: 4px; }
  #days #older-days, #more-days { display: none; }
  main { padding: 18px 16px 48px; }
  .dayhead h2 { font-size: 21px; }
  .day-actions .job-status { text-align: left; }
  /* 手机上一天十几段挤在 360px 里，刻度只会叠成一团；时刻看各段的悬停提示和议程 */
  .band-axis { display: none; }
  .seg > summary { grid-template-columns: 50px minmax(0, 1fr); padding: 10px 12px 10px 16px; }
  .seg-flags { grid-column: 2; justify-content: flex-start; max-width: none; }
  .seg-body { padding: 10px 14px 14px; }
  .outfit-picture .zoom-hint { opacity: 1; }
  #modal { padding: 12px; }
  #modal .sheet { padding: 16px; }
}
</style>
</head>
<body>
<aside id="rail">
  <h1>Mittes 的一天</h1>
  <details class="rail-sec" id="cal-fold"><summary>日历</summary><div id="cal"></div></details>
  <div class="rail-sec"><div class="rail-label">最近</div><nav id="days" aria-label="最近的日程"></nav></div>
  <div class="rail-sec">
    <label class="rail-label" for="generate-date">生成指定日期</label>
    <div class="create-day">
      <input id="generate-date" type="date">
      <button id="generate-day" class="action primary">生成</button>
      <div id="generate-status" class="job-status"></div>
    </div>
  </div>
</aside>
<main id="main"><p class="empty">加载中…</p></main>
<div id="modal"></div>
<script>
const WD = ['一','二','三','四','五','六','日'];
const esc = s => (s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

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
  // 归档只会越来越长，近七天是日常真正会点的范围，更早的收进折叠区（days 已按日期倒序）。
  const older = days.slice(7);
  const foldout = older.length
    ? `<button type="button" id="more-days" aria-expanded="false">更早的 ${older.length} 天</button>
       <div id="older-days" hidden>${older.map(link).join('')}</div>`
    : '';
  // 衣柜整页已移到 mittes-panel 的 /wardrobe 单独维护，这里只留当日穿搭
  document.getElementById('days').innerHTML = days.slice(0, 7).map(link).join('') + foldout;
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
let calMoreOpen = false; // 祝日说明的折叠状态，翻月重画时保留

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
    <details class="cal-more"${calMoreOpen ? ' open' : ''}><summary>本月祝日与连休 · 图例</summary>
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
    </div></details>`;
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
  // 宽屏日历常开；手机上先看正文，日历收起来要用再点
  document.getElementById('cal-fold').open = matchMedia('(min-width: 861px)').matches;
  await loadDays();
  window.addEventListener('hashchange', route);
  await route();
  await resumeJobs();
}

async function route() {
  const today = jstNow().date;
  const day = location.hash.slice(1)
    || (knownDays.includes(today) ? today : knownDays[0]);
  if (!day) {
    document.getElementById('main').innerHTML =
      '<p class="empty">归档库里还没有数据，可以在侧栏指定日期生成。</p>';
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
  const jst = jstNow();
  const isToday = jst.date === day;
  const nowSlot = isToday ? jst.minutes : -1;
  const busy = (data.gate || {}).busy_kinds || [];
  const shares = data.shares || {};

  const chips = [
    m.weather && esc(m.weather),
    m.holiday && esc(m.holiday),
    `${m.ok_count}/${m.total_count} 段生成`,
    m.batch_reason && esc(m.batch_reason),
    m.batch_elapsed ? `耗时 ${Math.round(m.batch_elapsed)}s` : '',
  ].filter(Boolean).map(t => `<span class="chip">${t}</span>`).join('')
    + (isToday ? `<span class="chip strong">现在 ${jst.clock} JST${jst.wrapped ? '（跨零点，仍算这一天）' : ''}</span>` : '');

  const segs = data.segments.map(s => {
    const [a, b] = slotMinutes(s.slot);
    const now = nowSlot >= a && nowSlot < b;
    const cls = s.kind === '睡眠' ? 'sleep' : (busy.includes(s.kind) ? 'busy' : 'idle');
    const used = (shares[s.slot] || []).some(r => r.shared_at);
    const flags = [
      now ? '<span class="flag now">现在</span>' : '',
      s.generated ? '' : '<span class="flag">底稿</span>',
      s.negative_level ? `<span class="flag neg">不顺心 · ${esc(s.negative_level)}</span>` : '',
      s.topic ? `<span class="flag say${used ? ' used' : ''}">${used ? '已说' : '可说'}</span>` : '',
      stateLevel(s.mood_level),
      stateLevel(s.energy_level),
    ].join('');
    const [from, to] = s.slot.split('-');
    return `<details class="seg ${cls}${now ? ' now' : ''}" data-anchor="${esc(s.slot)}"${now ? ' open' : ''}>
      <summary>
        <span class="seg-time"><b>${esc(from)}</b><i>${esc(to)}</i></span>
        <span class="seg-main"><span class="seg-title">${esc(s.title)}</span>
          <span class="seg-sub">${esc(s.place)} · ${esc(s.company)} · ${esc(s.kind)}</span></span>
        <span class="seg-flags">${flags}</span>
      </summary>
      <div class="seg-body">
        ${trail(s, now ? nowSlot : -1)}
        <p class="story">${esc(s.story) || '<span style="color:var(--muted)">（这段没有生成内容）</span>'}</p>
        <dl class="fields">
          <dt>心情</dt><dd>${esc(s.mood)}${stateLevel(s.mood_level)}</dd>
          <dt>体力</dt><dd>${esc(s.physical_state || '—')}${stateLevel(s.energy_level)}</dd>
          <dt>穿搭</dt><dd><button class="outfit" data-o="${esc(s.outfit)}">${esc(s.outfit)}</button></dd>
        </dl>
        ${data.manner_enabled === false ? '' : `<div class="expression">
          <label>表达方式</label>
          <textarea maxlength="200" data-expression>${esc(s.manner)}</textarea>
          <div class="expression-foot">
            <button class="action save-expression" data-day="${day}" data-slot="${esc(s.slot)}">保存</button>
            <span class="job-status"></span>
          </div>
        </div>`}
        ${topicBlock(s, shares[s.slot] || [])}
      </div>
    </details>`;
  }).join('');

  const outline = m.outline ? `<p class="outline" title="点击展开 / 收起"><b>脉络</b>${esc(m.outline)}</p>`
    : m.day_digest ? `<p class="outline" title="点击展开 / 收起"><b>当日概要</b>${esc(m.day_digest)}</p>` : '';

  document.getElementById('main').innerHTML = `
    <header class="dayhead">
      <div class="dayhead-top">
        <div class="dayhead-title">
          <h2>${+day.slice(5, 7)} 月 ${+day.slice(8)} 日<span class="wd">${day.slice(0, 4)} · 周${WD[m.weekday]}</span>${isToday ? '<span class="pill">今天</span>' : ''}${m.aborted ? '<span class="pill warn">批次中止</span>' : ''}</h2>
          <div class="chips">${chips}</div>
        </div>
        <div class="day-actions">
          <button class="action" id="regen-day" data-day="${day}">重新生成日程</button>
          <button class="action" id="regen-topics" data-day="${day}">重新生成 topic</button>
          ${data.manner_enabled === false ? '' : `<button class="action" id="regen-expressions" data-day="${day}">重新生成表达方式</button>`}
          <span id="day-job-status" class="job-status"></span>
        </div>
      </div>
      ${m.aborted ? `<div class="notice">中止原因：${esc(m.aborted)}</div>` : ''}
      ${outline}
      ${timeline(data, nowSlot)}
    </header>
    <div class="day-body">
      <section class="agenda" aria-label="当日日程">
        <div class="sec-head"><h3>日程</h3><span class="count">${data.segments.length} 段</span>
          <button type="button" class="link" id="toggle-all">全部展开</button></div>
        ${segs}
      </section>
      ${dayOutfits(data.segments, nowSlot, w)}
    </div>`;
  paintJobs();
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

// 任务进度按任务编号记在这里，不挂在某个 DOM 节点上：
// 日程区每次切换日期都会整块重画，刷新页面后也要能接着显示，所以每次轮询后重新找节点上色。
const jobs = new Map();
const JOB_TEXT = {pending: '已排队，等待 bot 处理…', running: 'bot 正在处理…'};

function jobTargets(job) {
  if (job.action === 'generate_day') {
    return {status: document.getElementById('generate-status'), button: document.getElementById('generate-day')};
  }
  if (job.action === 'set_expression') {
    const button = [...document.querySelectorAll('.save-expression')]
      .find(node => node.dataset.day === job.target_date && node.dataset.slot === job.slot);
    return {status: button?.closest('.expression')?.querySelector('.job-status'), button};
  }
  const ids = {regenerate_day: 'regen-day', regenerate_topics: 'regen-topics', regenerate_expressions: 'regen-expressions'};
  const button = document.getElementById(ids[job.action]);
  if (!button || button.dataset.day !== job.target_date) return {};
  return {status: document.getElementById('day-job-status'), button};
}

function paintJobs() {
  for (const job of jobs.values()) {
    const {status, button} = jobTargets(job);
    const busy = job.status === 'pending' || job.status === 'running';
    if (status) {
      status.classList.toggle('busy', busy);
      status.classList.toggle('error', job.status === 'failed');
      status.classList.toggle('done', job.status === 'completed');
      status.textContent = busy ? JOB_TEXT[job.status] : job.status === 'failed' ? (job.error || '任务执行失败') : (job.result || '已完成');
    }
    if (button) {
      button.disabled = busy;
      button.classList.toggle('busy', busy);
    }
  }
}

function follow(job) {
  return new Promise((resolve, reject) => {
    jobs.set(job.id, job);
    paintJobs();
    const tick = async () => {
      try {
        const next = await api('/api/job/' + job.id);
        jobs.set(job.id, next);
        paintJobs();
        if (next.status === 'completed') { finish(next); resolve(next); return; }
        if (next.status === 'failed') { finish(next); reject(new Error(next.error || '任务执行失败')); return; }
      } catch (error) { /* 临时读不到就下一秒再试 */ }
      setTimeout(tick, 1000);
    };
    setTimeout(tick, 1000);
  });
}

function finish(job) {
  // 结果留一会儿让人看到，然后清掉，免得以后切到这一天还显示旧消息
  setTimeout(() => { if (jobs.get(job.id) === job) jobs.delete(job.id); }, 15000);
}

async function enqueue(payload, statusNode, button) {
  statusNode.classList.remove('error', 'done');
  statusNode.classList.add('busy');
  statusNode.textContent = JOB_TEXT.pending;
  button.disabled = true;
  let created;
  try {
    created = await api('/api/jobs', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload),
    });
  } catch (error) {
    statusNode.classList.remove('busy');
    statusNode.classList.add('error');
    statusNode.textContent = error.message;
    button.disabled = false;
    throw error;
  }
  return follow({id: created.id, action: payload.action, target_date: payload.date, slot: payload.slot || '', status: 'pending'});
}

/** 打开或刷新页面时接上还没结束的任务；结束后同样刷新对应日期 */
async function resumeJobs() {
  try {
    const {jobs: active} = await api('/api/jobs/active');
    for (const job of active) {
      if (job.status === 'pending' || job.status === 'running') {
        follow(job).then(() => job.action !== 'set_expression' && refreshDay(job.target_date)).catch(() => {});
      } else if (!jobs.has(job.id)) {
        jobs.set(job.id, job);
        finish(job);
      }
    }
    paintJobs();
  } catch (error) { /* 旧版插件没有这个接口时不影响其他功能 */ }
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
    const used = ((data.shares || {})[s.slot] || []).some(r => r.shared_at);
    const tip = `${s.slot}　${s.title}（${s.kind}）${hasTopic ? (used ? '　可说的事已用掉' : '　有可说的事') : ''}`;
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
  // 「用掉」现在由小模型判定回复讲到了这件事，hit_key 是「判定：依据」；
  // 更早的记录里是关键词或"工具调用"，都照原样显示
  const used = shares.some(r => r.shared_at);
  const rows = shares.map(r => {
    if (!r.shared_at) {
      return `<div class="share">注入 ${r.injected} 次${r.pitched ? `　取材 ${r.pitched} 次` : ''}，还没用掉</div>`;
    }
    const hit = r.hit_key ? `　${esc(r.hit_key)}` : '';
    const said = r.reply_text ? `<div class="said">${esc(r.reply_text)}</div>` : '';
    return `<div class="share"><b>已用掉 ${esc(r.shared_at.slice(11, 16))}</b>`
      + `　注入 ${r.injected} 次${r.pitched ? `　取材 ${r.pitched} 次` : ''}${hit}${said}</div>`;
  }).join('');
  return `<div class="topic${used ? ' used' : ''}">
    <span class="tag">${used ? '已用掉' : '可说'}</span><span class="topic-text">${esc(s.topic)}</span>
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

function outfitThumb(name, w) {
  const url = outfitImageUrl(name, w, 'outer');
  const o = (w.wardrobe || {})[name];
  return url ? `<button type="button" class="outfit-picture" data-status="loading" data-o="${esc(name)}" data-part="outer" aria-label="放大查看${esc(name)}">
      <img src="${esc(url)}" alt="${esc(name)}设定图" width="1536" height="1024" loading="lazy" decoding="async">
      <span class="zoom-hint" aria-hidden="true">查看大图 ↗</span></button>`
    : `<p class="outfit-missing">${o ? '设定图尚未添加' : '当日临时搭配 · 暂无设定图'}</p>`;
}

// 同一套连着穿的几段合成一个时间范围，读起来是「什么时候穿的」而不是一串时段
function outfitSpans(segments, name) {
  const spans = [];
  segments.forEach(s => {
    if (s.outfit !== name) return;
    const [from, to] = s.slot.split('-');
    const last = spans.at(-1);
    if (last && last[1] === from) last[1] = to; else spans.push([from, to]);
  });
  return spans.map(([a, b]) => `${a}–${b}`).join('　');
}

function dayOutfits(segments, nowSlot, w) {
  const names = [...new Set(segments.map(s => s.outfit).filter(Boolean))];
  if (!names.length) return '';
  const head = `<div class="sec-head"><h3>当日穿搭</h3><span class="count">${names.length} 套</span></div>`;
  if (w.error) return `<aside class="outfits">${head}<p class="empty">${esc(w.error)}</p></aside>`;
  const current = segments.find(s => {
    const [a, b] = slotMinutes(s.slot);
    return nowSlot >= a && nowSlot < b;
  });
  const hasInner = name => (w.inner_images || []).includes(name);
  const cards = names.map(name => {
    const now = name === current?.outfit;
    return `<article class="fit${now ? ' now' : ''}">${outfitThumb(name, w)}
      <div class="fit-info">
        <h4>${now ? '<span class="now-tag">此刻</span>' : ''}${esc(name)}</h4>
        <div class="when">${outfitSpans(segments, name)}</div>
        <div class="fit-links">
          <button type="button" class="outfit" data-o="${esc(name)}" data-part="outer">外搭</button>
          ${hasInner(name) ? `<button type="button" class="outfit" data-o="${esc(name)}" data-part="inner">内搭</button>` : ''}
        </div>
      </div></article>`;
  }).join('');
  return `<aside class="outfits" aria-label="当日穿搭">${head}<div class="fits">${cards}</div></aside>`;
}

let outfitTrigger = null;
function closeOutfit() {
  document.getElementById('modal').classList.remove('on');
  document.body.style.overflow = '';
  document.querySelectorAll('#rail, #main').forEach(node => node.inert = false);
  if (outfitTrigger?.isConnected) outfitTrigger.focus({preventScroll: true});
}

// 点段落里的穿搭名或画廊图片 → 先看完整设定图，文字默认收起。
async function showOutfit(name, part = 'outer') {
  if (!document.getElementById('modal').classList.contains('on')) outfitTrigger = document.activeElement;
  const w = await wardrobe();
  const o = (w.wardrobe || {})[name];
  const url = outfitImageUrl(name, w, part);
  const label = part === 'inner' ? '内搭' : '外搭';
  const body = w.error ? `<p class="none">${esc(w.error)}</p>`
    : url ? `<img class="outfit-full" src="${esc(url)}" alt="${esc(name)}${label}设定图" width="1536" height="1024">
        <a class="original" href="${esc(url)}" target="_blank" rel="noopener">查看原图 ↗</a>`
    : `<p class="none">${o ? '这套穿搭尚未添加设定图。' : '这一身不在衣柜里，是当天临时定的搭配，暂无设定图。'}</p>`;
  const always = o ? alwaysDescription(w) : '';
  const parts = [['outer', '外搭'], ['inner', '内搭']]
    .filter(([key]) => key === 'outer' || (w.inner_images || []).includes(name))
    .map(([key, text]) => `<button type="button" class="outfit${key === part ? ' on' : ''}" data-o="${esc(name)}" data-part="${key}">${text}</button>`).join('');
  document.getElementById('modal').innerHTML =
    `<div class="sheet" role="dialog" aria-modal="true" aria-labelledby="outfit-title">
      <button type="button" class="close" aria-label="关闭服饰详情">×</button>
      <h3 id="outfit-title">${esc(name)} · ${label}</h3>
      ${parts.includes('data-part="inner"') ? `<div class="part-switch">${parts}</div>` : ''}${body}${outfitDescription(o, part)}${always}</div>`;
  document.getElementById('modal').classList.add('on');
  document.body.style.overflow = 'hidden';
  document.querySelectorAll('#rail, #main').forEach(node => node.inert = true);
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
    if (target) {
      target.open = true;
      target.scrollIntoView({ block: 'start', behavior: 'smooth' });
    }
    return;
  }

  const all = e.target.closest('#toggle-all');
  if (all) {
    const segs = [...document.querySelectorAll('.seg')];
    const open = segs.some(seg => !seg.open);
    segs.forEach(seg => { seg.open = open; });
    all.textContent = open ? '全部收起' : '全部展开';
    return;
  }

  const outline = e.target.closest('.outline');
  if (outline) { outline.classList.toggle('open'); return; }

  const calMore = e.target.closest('#cal .cal-more > summary');
  if (calMore) { calMoreOpen = !calMore.parentElement.open; return; }

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
    return PAGE.encode("utf-8")


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
