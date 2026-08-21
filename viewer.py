"""日程归档浏览器。

起一个只读的小服务，把 ``data/schedule.db`` 里的日程按天翻出来看。

    uv run python plugins/04_a_day_with_mittes/viewer.py          # 只有本机能开
    uv run python plugins/04_a_day_with_mittes/viewer.py --lan    # 局域网内可开

只用标准库，不引任何依赖；数据库以只读方式打开，不会跟正在运行的 bot 抢写锁。

**``--lan`` 没有任何鉴权**：同一局域网里知道地址的人都能看到全部日程内容。
这是自用小工具的取舍，别往公网端口映射。
"""

from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import argparse
import json
import socket
import sqlite3
import sys
import tomllib


DB_PATH = Path(__file__).parent / "data" / "schedule.db"
WARDROBE_PATH = Path(__file__).parent / "wardrobe.toml"
WEEKDAY_NAMES = ["一", "二", "三", "四", "五", "六", "日"]


def _connect(db_path: Path) -> sqlite3.Connection:
    """以只读方式打开归档库。"""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
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
        "shares": grouped,
    }


def _load_wardrobe(path: Path) -> dict[str, Any]:
    """读衣柜。**每次请求都重读**——它是手写资产，改完刷新页面就该看到，
    不该为了省一次文件读取而要求重启 viewer。文件不到 10KB。
    """
    try:
        with path.open("rb") as handle:
            raw = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return {"error": f"读不了 wardrobe.toml：{exc}"}
    return {
        "always": raw.get("always") or {},
        "wardrobe": raw.get("wardrobe") or {},
    }


class Handler(BaseHTTPRequestHandler):
    conn: sqlite3.Connection
    wardrobe_path: Path

    def do_GET(self) -> None:  # noqa: N802 —— BaseHTTPRequestHandler 的固定签名
        path = urlparse(self.path).path
        if path == "/":
            self._send(200, "text/html; charset=utf-8", PAGE.encode("utf-8"))
            return
        if path == "/api/wardrobe":
            self._send_json(_load_wardrobe(self.wardrobe_path))
            return
        if path == "/api/days":
            self._send_json(_list_days(self.conn))
            return
        if path.startswith("/api/day/"):
            day = path.rsplit("/", 1)[-1]
            data = _load_day(self.conn, day)
            if data is None:
                self._send_json({"error": "没有这一天的记录"}, status=404)
                return
            self._send_json(data)
            return
        self._send(404, "text/plain; charset=utf-8", "Not Found".encode("utf-8"))

    def _send_json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(status, "application/json; charset=utf-8", body)

    def _send(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
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
  --bg: #faf9f7; --panel: #fff; --line: #e6e2dc; --text: #2c2a28;
  --muted: #8a8378; --accent: #c96a5a; --badge: #f0ece6;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #1c1b19; --panel: #252320; --line: #37342f; --text: #e8e4dd;
    --muted: #9a9288; --accent: #e08b78; --badge: #322e29;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--text);
  font: 15px/1.7 -apple-system, "Noto Sans CJK SC", "Microsoft YaHei", sans-serif;
  display: flex; min-height: 100vh;
}
aside {
  width: 210px; flex: none; border-right: 1px solid var(--line);
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
main { flex: 1; padding: 28px 32px 64px; max-width: 900px; }
.dayhead { border-bottom: 1px solid var(--line); padding-bottom: 14px; margin-bottom: 24px; }
.dayhead h2 { margin: 0 0 6px; font-size: 22px; }
.dayhead .meta { color: var(--muted); font-size: 13px; }
.digest {
  background: var(--panel); border: 1px solid var(--line); border-radius: 8px;
  padding: 10px 14px; margin-top: 12px; font-size: 14px; color: var(--muted);
}
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

/* 骨架行里可点的穿搭名 */
.skeleton .outfit {
  font: inherit; color: var(--accent); background: none; border: 0; padding: 0;
  cursor: pointer; border-bottom: 1px dashed currentColor;
}
.skeleton .outfit:hover { background: var(--badge); }

/* 穿搭细节弹层 */
#modal {
  display: none; position: fixed; inset: 0; z-index: 20;
  background: rgba(0,0,0,.38); padding: 24px; overflow-y: auto;
}
#modal.on { display: flex; align-items: flex-start; justify-content: center; }
#modal .sheet {
  background: var(--panel); border: 1px solid var(--line); border-radius: 10px;
  padding: 20px 22px; max-width: 620px; width: 100%; margin: auto;
  box-shadow: 0 10px 40px rgba(0,0,0,.25); position: relative;
}
#modal .sheet h3 { margin: 0 0 12px; font-size: 17px; }
#modal .close {
  position: absolute; top: 10px; right: 12px; font-size: 22px; line-height: 1;
  background: none; border: 0; color: var(--muted); cursor: pointer;
}
#modal .close:hover { color: var(--text); }
#modal .always { margin-top: 14px; padding-top: 12px; border-top: 1px solid var(--line); }
#modal .always b { font-size: 12.5px; color: var(--muted); }
#modal .none { color: var(--muted); font-style: italic; }
.wardrobe-link { margin-top: 10px; border-top: 1px solid var(--line); padding-top: 12px !important; }
@media (max-width: 640px) {
  body { flex-direction: column; }
  aside { width: auto; height: auto; position: static; border-right: 0; border-bottom: 1px solid var(--line); }
  main { padding: 20px 16px 48px; }
}
</style>
</head>
<body>
<aside><h1>Mittes 的一天</h1><nav id="days"></nav></aside>
<main id="main"><p class="empty">加载中…</p></main>
<div id="modal"></div>
<script>
const WD = ['一','二','三','四','五','六','日'];
const esc = s => (s ?? '').replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));

async function boot() {
  const days = await (await fetch('/api/days')).json();
  const nav = document.getElementById('days');
  if (!days.length) {
    document.getElementById('main').innerHTML =
      '<p class="empty">归档库里还没有数据。等 12:00 的批次跑完，或者用 /status batch today 手动生成。</p>';
    return;
  }
  nav.innerHTML = days.map(d => {
    const bad = d.aborted ? '中止' : (d.ok_count < d.total_count ? `${d.ok_count}/${d.total_count}` : '');
    const neg = d.negative_count ? ' ※' : '';
    return `<a href="#${d.date}" data-d="${d.date}">${d.date.slice(5)} 周${WD[d.weekday]}
      <span class="sub">${bad}${neg}</span></a>`;
  }).join('') + `<a href="#wardrobe" data-d="wardrobe" class="wardrobe-link">衣柜　<span class="sub">全部搭配</span></a>`;
  window.addEventListener('hashchange', route);
  route();
}

async function route() {
  if (location.hash === '#wardrobe') {
    document.querySelectorAll('#days a').forEach(a => a.classList.toggle('on', a.dataset.d === 'wardrobe'));
    renderWardrobe(await wardrobe());
    window.scrollTo(0, 0);
    return;
  }
  const known = [...document.querySelectorAll('#days a')].map(a => a.dataset.d).filter(d => d !== 'wardrobe');
  const today = jstNow().date;
  const day = location.hash.slice(1)
    || (known.includes(today) ? today : known[0]);
  document.querySelectorAll('#days a').forEach(a => a.classList.toggle('on', a.dataset.d === day));
  const data = await (await fetch('/api/day/' + day)).json();
  if (data.error) { document.getElementById('main').innerHTML = `<p class="empty">${esc(data.error)}</p>`; return; }
  render(day, data);
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

function render(day, data) {
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

  const segs = data.segments.map(s => {
    const [a, b] = s.slot.split('-').map(t => +t.slice(0,2) * 60 + +t.slice(3));
    const now = nowSlot >= a && nowSlot < b;
    return `<article class="seg${now ? ' now' : ''}">
      <h3><span class="slot">${s.slot}</span>${esc(s.title)}
        ${s.generated ? '' : '<span class="badge">底稿</span>'}
        ${s.negative_level ? `<span class="badge neg">不顺心 · ${esc(s.negative_level)}</span>` : ''}
      </h3>
      <div class="skeleton">${esc(s.place)}　/　<button class="outfit" data-o="${esc(s.outfit)}">${esc(s.outfit)}</button>　/　${esc(s.company)}　/　${esc(s.kind)}</div>
      ${trail(s, now ? nowSlot : -1)}
      <p class="story">${esc(s.story)}</p>
      <dl class="fields">
        <dt>心情</dt><dd>${esc(s.mood)}</dd>
        <dt>说话方式</dt><dd>${esc(s.manner)}</dd>
      </dl>
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
    </div>${segs}`;
}

// 地点时段轴。当前那一段高亮——只在"现在"落在这个时段里时才标。
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
  let keys = [];
  try { keys = JSON.parse(s.topic_keys || '[]'); } catch (e) { keys = []; }
  const rows = shares.map(r => {
    if (!r.shared_at) {
      return `<div class="share">注入 ${r.injected} 次，还没说出口</div>`;
    }
    const hit = r.hit_key ? `　命中「${esc(r.hit_key)}」` : '';
    const said = r.reply_text
      ? `<div class="said">${esc(r.reply_text)}</div>`
      : '<div class="said none">（这条记录早于回复留存，没有原文）</div>';
    return `<div class="share"><b>已说出口 ${esc(r.shared_at.slice(11, 16))}</b>`
      + `　注入 ${r.injected} 次${hit}${said}</div>`;
  }).join('');
  return `<div class="topic">
    <span class="tag">可说</span>${esc(s.topic)}
    <div class="keys">关键词：${keys.map(esc).join('、') || '—'}</div>
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
                     ['legs','腿'],['feet','脚'],['accessories','配饰'],['optional','有时会加']];
const ALWAYS_LABELS = [['hair_base','头发底子'],['nails','美甲'],['props','道具']];

function outfitRows(o) {
  return PART_LABELS.filter(([k]) => o[k])
    .map(([k, label]) => `<dt>${label}</dt><dd>${esc(o[k])}</dd>`).join('');
}

// 点段落里的穿搭名 → 弹出这一套的从头到脚
async function showOutfit(name) {
  const w = await wardrobe();
  const o = (w.wardrobe || {})[name];
  const body = o
    ? `<dl class="fields">${outfitRows(o)}</dl>`
    : `<p class="none">这一身不在衣柜里——当天临时定的衣服（品牌方或角色的），每次都不一样。</p>`;
  const always = (o && w.always) ? `<div class="always"><b>不分场合一直有的</b>
      <dl class="fields">${ALWAYS_LABELS.filter(([k]) => w.always[k])
        .map(([k, label]) => `<dt>${label}</dt><dd>${esc(w.always[k])}</dd>`).join('')}</dl></div>` : '';
  document.getElementById('modal').innerHTML =
    `<div class="sheet"><button class="close">×</button><h3>${esc(name)}</h3>${body}${always}</div>`;
  document.getElementById('modal').classList.add('on');
}

function renderWardrobe(w) {
  if (w.error) { document.getElementById('main').innerHTML = `<p class="empty">${esc(w.error)}</p>`; return; }
  const sets = Object.entries(w.wardrobe || {}).map(([name, o]) => `
    <article class="seg">
      <h3>${esc(name)}</h3>
      <dl class="fields">${outfitRows(o)}</dl>
    </article>`).join('');
  const always = `<article class="seg">
      <h3>不分场合一直有的</h3>
      <dl class="fields">${ALWAYS_LABELS.filter(([k]) => (w.always||{})[k])
        .map(([k, label]) => `<dt>${label}</dt><dd>${esc(w.always[k])}</dd>`).join('')}</dl>
    </article>`;
  document.getElementById('main').innerHTML =
    `<div class="dayhead"><h2>衣柜</h2>
       <div class="meta">${Object.keys(w.wardrobe || {}).length} 套　·　骨架的穿搭栏填的就是这些名字</div>
     </div>${sets}${always}`;
}

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
  const btn = e.target.closest('.outfit');
  if (btn) { showOutfit(btn.dataset.o); return; }
  const modal = document.getElementById('modal');
  if (e.target.closest('.close') || e.target === modal) modal.classList.remove('on');
});
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') document.getElementById('modal').classList.remove('on');
});

boot();
</script>
</body>
</html>
"""


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

    Handler.conn = _connect(args.db)
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
        lines.append("　注意：没有鉴权，同一局域网里知道地址的人都能看到全部日程内容")
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
