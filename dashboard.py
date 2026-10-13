"""Панель «Радара объявлений».

Два раздела:
- 💼 Платный бот (главная) — обзор с цифрами и графиками, пользователи с
  карточками и продлением доступа, оплаты с подтверждением чеков, поиски и
  популярные запросы, лента отправленного, журнал действий;
- 🎮 Личный режим (/gpu/…) — видеокарты: отсеянные с причиной, прошедшие
  фильтр, майнинговые, рынок по моделям, история цены.

Панель слушает только localhost и адрес Tailscale (DASHBOARD_LISTEN), в
интернет не выставлена. Действия (подтвердить чек, продлить доступ) шлют
пользователю те же сообщения, что и бот.
"""
from __future__ import annotations

import json
import math
import os
import sqlite3
import time
from datetime import datetime, timedelta
from html import escape
from pathlib import Path
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo

from flask import Flask, abort, redirect, request

ROOT = Path(__file__).parent
DB = ROOT / "data" / "watcher.sqlite3"
TZ = ZoneInfo("Asia/Almaty")  # сервер живёт в UTC, а смотрим мы по алматинскому времени
app = Flask(__name__)

# --- Оформление -------------------------------------------------------------
STYLE = """<style>
:root{color-scheme:light;
 --bg:#f4f4f2;--surface:#fcfcfb;--surface-2:#f0efec;--line:#e4e3df;
 --text:#0b0b0b;--text-2:#52514e;--muted:#8a8984;
 --accent:#2a78d6;--accent-soft:#e3eefb;--series:#2a78d6;
 --good:#1a7f37;--good-soft:#e2f3e6;--warn:#9a6700;--warn-soft:#fbf0d5;--bad:#c42b2b;--bad-soft:#fbe4e3;
 --shadow:0 1px 2px rgba(0,0,0,.04),0 1px 8px rgba(0,0,0,.04)}
@media(prefers-color-scheme:dark){:root:where(:not([data-theme=light])){color-scheme:dark;
 --bg:#121211;--surface:#1a1a19;--surface-2:#242422;--line:#2e2e2b;
 --text:#ffffff;--text-2:#c3c2b7;--muted:#8f8e86;
 --accent:#3987e5;--accent-soft:#1d2c40;--series:#3987e5;
 --good:#4cc76d;--good-soft:#173322;--warn:#e0a83a;--warn-soft:#352a12;--bad:#ef6b6b;--bad-soft:#3a1c1c;
 --shadow:none}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.5 -apple-system,BlinkMacSystemFont,"SF Pro Text","Segoe UI",Roboto,sans-serif}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
.layout{display:flex;min-height:100vh}
aside{width:232px;flex:none;background:var(--surface);border-right:1px solid var(--line);padding:18px 12px;position:sticky;top:0;height:100vh;overflow-y:auto}
.brand{display:flex;align-items:center;gap:10px;font-weight:700;font-size:15px;padding:0 8px 16px}
.brand span{width:30px;height:30px;border-radius:9px;display:grid;place-items:center;background:linear-gradient(135deg,#1c1f4a,#40207a);color:#3cf0b4}
.section{font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);padding:14px 10px 6px}
aside a{display:flex;align-items:center;gap:9px;padding:7px 10px;border-radius:8px;color:var(--text-2);font-weight:500}
aside a:hover{background:var(--surface-2);text-decoration:none}
aside a.on{background:var(--accent-soft);color:var(--accent)}
aside .count{margin-left:auto;background:var(--bad);color:#fff;border-radius:99px;font-size:11px;padding:0 7px;font-weight:600}
main{flex:1;min-width:0;padding:24px 28px 48px;max-width:1280px}
h1{font-size:22px;margin:0 0 4px}.sub{color:var(--text-2);margin:0 0 20px}
h2{font-size:15px;margin:26px 0 10px}
.grid{display:grid;gap:12px}.g4{grid-template-columns:repeat(4,minmax(0,1fr))}.g3{grid-template-columns:repeat(3,minmax(0,1fr))}.g2{grid-template-columns:repeat(2,minmax(0,1fr))}
.card{background:var(--surface);border:1px solid var(--line);border-radius:14px;padding:16px;box-shadow:var(--shadow);min-width:0}
.tile .v{font-size:26px;font-weight:700;letter-spacing:-.01em;line-height:1.15}
.tile .l{color:var(--text-2);font-size:13px;margin-top:2px}.tile .d{color:var(--muted);font-size:12px;margin-top:6px}
.tile.hero{background:linear-gradient(135deg,var(--accent-soft),var(--surface))}
.chart{width:100%;height:170px;position:relative}.chart svg{width:100%;height:100%;display:block;overflow:visible}
.chart-title{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:8px}
.chart-title b{font-size:14px}.chart-title span{color:var(--muted);font-size:12px}
.tip{position:fixed;pointer-events:none;background:var(--text);color:var(--surface);padding:6px 9px;border-radius:8px;font-size:12px;white-space:nowrap;z-index:9;display:none}
.table-wrap{overflow-x:auto;border:1px solid var(--line);border-radius:14px;background:var(--surface);box-shadow:var(--shadow)}
table{width:100%;border-collapse:collapse}
th,td{padding:10px 12px;text-align:left;vertical-align:top;border-bottom:1px solid var(--line)}
tr:last-child td{border-bottom:0}
th{background:var(--surface-2);font-size:12px;font-weight:600;color:var(--text-2);position:sticky;top:0;white-space:nowrap}
th a{color:inherit}td.num,th.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
tbody tr:hover td{background:var(--surface-2)}
.muted{color:var(--muted)}.small{font-size:12px}.nowrap{white-space:nowrap}
.pill{display:inline-flex;align-items:center;gap:4px;padding:1px 8px;border-radius:99px;font-size:12px;font-weight:600;white-space:nowrap;background:var(--surface-2);color:var(--text-2)}
.pill.good{background:var(--good-soft);color:var(--good)}.pill.warn{background:var(--warn-soft);color:var(--warn)}
.pill.bad{background:var(--bad-soft);color:var(--bad)}.pill.accent{background:var(--accent-soft);color:var(--accent)}
.toolbar{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:12px}
.toolbar .chips a{padding:6px 11px;border-radius:99px;background:var(--surface);border:1px solid var(--line);color:var(--text-2);font-weight:500;display:inline-block;margin:0 4px 4px 0}
.toolbar .chips a.on{background:var(--accent);border-color:var(--accent);color:#fff}
input,select,textarea{font:inherit;color:var(--text);background:var(--surface);border:1px solid var(--line);border-radius:9px;padding:7px 10px}
button,.btn{font:inherit;font-weight:600;border:0;border-radius:9px;padding:7px 12px;cursor:pointer;background:var(--accent);color:#fff;display:inline-block}
button.ghost,.btn.ghost{background:var(--surface-2);color:var(--text)}button.danger{background:var(--bad-soft);color:var(--bad)}
form.inline{display:inline-flex;gap:6px;align-items:center;flex-wrap:wrap;margin:0}
.kv{display:grid;grid-template-columns:150px 1fr;gap:6px 14px}.kv dt{color:var(--text-2)}.kv dd{margin:0}
.funnel .row{display:grid;grid-template-columns:160px 1fr 70px;gap:10px;align-items:center;margin:8px 0}
.funnel .bar{height:22px;border-radius:0 4px 4px 0;background:var(--series);min-width:2px}
.list{margin:0;padding:0;list-style:none}.list li{padding:8px 0;border-bottom:1px solid var(--line);display:flex;gap:10px;justify-content:space-between}
.list li:last-child{border-bottom:0}
.notice{padding:10px 14px;border-radius:10px;background:var(--good-soft);color:var(--good);margin-bottom:14px;font-weight:500}
.reasons{display:flex;gap:8px;flex-wrap:wrap;margin:0 0 14px}
.reason{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:8px 12px;color:var(--text)}
.reason b{display:block;font-size:18px}
details.reason summary{cursor:pointer;list-style:none}details.reason summary::-webkit-details-marker{display:none}
.subs{display:flex;flex-direction:column;margin-top:6px;max-height:260px;overflow:auto}.subs a{padding:3px 0;font-size:13px}
tr.gone td{opacity:.5}
.empty{padding:28px;text-align:center;color:var(--muted)}
@media(max-width:900px){.layout{display:block}aside{position:static;width:auto;height:auto;display:flex;overflow-x:auto;padding:10px;gap:4px;border-right:0;border-bottom:1px solid var(--line)}
 .brand,.section{display:none}aside a{white-space:nowrap}main{padding:16px}.g4,.g3{grid-template-columns:repeat(2,minmax(0,1fr))}.g2{grid-template-columns:1fr}
 .kv{grid-template-columns:1fr}.funnel .row{grid-template-columns:110px 1fr 50px}}
</style>"""

# Маленькая библиотека графиков: столбцы и линия в SVG, подсказка при наведении.
# Один ряд — один цвет (--series), подписи — цветом текста, сетка — приглушённо.
CHARTS = """<div class=tip id=tip></div><script>
const tip=document.getElementById('tip');
function showTip(e,html){tip.innerHTML=html;tip.style.display='block';tip.style.left=(e.clientX+12)+'px';tip.style.top=(e.clientY-34)+'px'}
function hideTip(){tip.style.display='none'}
function nice(max){if(max<=0)return 1;const p=Math.pow(10,Math.floor(Math.log10(max)));for(const m of[1,2,2.5,5,10])if(m*p>=max)return m*p;return 10*p}
function chart(el){
 const d=JSON.parse(el.dataset.points),kind=el.dataset.kind||'bar',fmt=el.dataset.fmt||'',W=Math.max(el.clientWidth,200),H=el.clientHeight||170,L=46,B=22,T=8;
 const max=nice(Math.max(...d.map(p=>p[1]),0)),n=d.length,step=(W-L)/n,gap=Math.min(4,step*.25);
 const f=v=>fmt==='money'?v.toLocaleString('ru-RU')+' ₸':v.toLocaleString('ru-RU');
 const y=v=>T+(H-T-B)*(1-v/max);let s='';
 for(let i=0;i<=2;i++){const v=max*i/2,yy=y(v);s+=`<line x1=${L} x2=${W} y1=${yy} y2=${yy} stroke="var(--line)"/><text x=${L-6} y=${yy+4} text-anchor=end font-size=11 fill="var(--muted)">${fmt==='money'&&v>=1000?(v/1000).toLocaleString('ru-RU')+'к':v.toLocaleString('ru-RU')}</text>`}
 d.forEach((p,i)=>{if(i%Math.ceil(n/6)===0||i===n-1)s+=`<text x=${L+step*i+step/2} y=${H-5} text-anchor=middle font-size=11 fill="var(--muted)">${p[0]}</text>`});
 if(kind==='line'){const pts=d.map((p,i)=>`${L+step*i+step/2},${y(p[1])}`).join(' ');
  s+=`<polyline points="${pts}" fill=none stroke="var(--series)" stroke-width=2 stroke-linejoin=round stroke-linecap=round/>`;
  d.forEach((p,i)=>s+=`<circle cx=${L+step*i+step/2} cy=${y(p[1])} r=4 fill="var(--series)" stroke="var(--surface)" stroke-width=2 opacity=${i===n-1?1:0} class=pt />`)}
 else d.forEach((p,i)=>{const h=Math.max(H-B-y(p[1]),p[1]>0?2:0),x=L+step*i+gap/2,w=Math.max(step-gap,1),top=H-B-h,r=Math.min(4,w/2,h);
  s+=h?`<path d="M${x},${H-B}V${top+r}Q${x},${top} ${x+r},${top}H${x+w-r}Q${x+w},${top} ${x+w},${top+r}V${H-B}Z" fill="var(--series)"/>`:''});
 d.forEach((p,i)=>s+=`<rect x=${L+step*i} y=${T} width=${step} height=${H-T-B} fill=transparent data-i=${i} />`);
 el.innerHTML=`<svg viewBox="0 0 ${W} ${H}" role=img aria-label="${el.dataset.label||''}">${s}</svg>`;
 el.querySelectorAll('rect[data-i]').forEach(r=>{const p=d[+r.dataset.i];
  r.addEventListener('mousemove',e=>showTip(e,`<b>${f(p[1])}</b> · ${p[2]||p[0]}`));r.addEventListener('mouseleave',hideTip)})}
const drawAll=()=>document.querySelectorAll('.chart[data-points]').forEach(chart);drawAll();
let rz;addEventListener('resize',()=>{clearTimeout(rz);rz=setTimeout(drawAll,150)});
</script>"""

NAV = [
    ("💼 Платный бот", [("/", "📊", "Обзор"), ("/users", "👥", "Пользователи"), ("/payments", "💳", "Оплаты"),
                        ("/searches", "🔎", "Поиски"), ("/deliveries", "📨", "Отправленное"), ("/events", "🧾", "Журнал")]),
    ("🎮 Личный режим", [("/gpu", "🚫", "Отсеянные"), ("/gpu/passed", "✅", "Прошли фильтр"),
                         ("/gpu/today", "🆕", "За сегодня"), ("/gpu/mining", "⛏", "Майнинговые"),
                         ("/gpu/market", "📈", "Рынок"), ("/gpu/all", "📋", "Все объявления")]),
]


def layout(title: str, active: str, body: str, subtitle: str = "") -> str:
    pending = 0
    try:
        con = connect()
        if table_exists(con, "payments"):
            pending = con.execute("SELECT COUNT(*) FROM payments WHERE status='pending'").fetchone()[0]
        con.close()
    except sqlite3.Error:
        pass
    links = []
    for section, items in NAV:
        links.append(f"<div class=section>{escape(section)}</div>")
        for href, icon, name in items:
            badge = f"<span class=count>{pending}</span>" if href == "/payments" and pending else ""
            links.append(f"<a href='{href}' class='{'on' if href == active else ''}'>{icon} {escape(name)}{badge}</a>")
    notice = request.args.get("ok", "")
    return (f"<!doctype html><html lang=ru><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{escape(title)} · Радар</title>{STYLE}<div class=layout><aside><div class=brand><span>◎</span>Радар объявлений</div>"
            f"{''.join(links)}</aside><main><h1>{escape(title)}</h1>"
            f"{f'<p class=sub>{subtitle}</p>' if subtitle else '<div style=height:14px></div>'}"
            f"{f'<div class=notice>✅ {escape(notice)}</div>' if notice else ''}{body}</main></div>{CHARTS}</html>")


# --- Данные -----------------------------------------------------------------
def connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB, timeout=10)
    # Бот пишет в базу почти непрерывно. Без WAL читатель и писатель встают
    # в очередь друг к другу и ловят "database is locked".
    con.execute("PRAGMA journal_mode=WAL")
    con.row_factory = sqlite3.Row
    # Колонки заводит бот при старте, но панель может подняться раньше него —
    # тогда без этой проверки все страницы падали бы на отсутствующем столбце.
    if table_exists(con, "ads"):
        existing = {r[1] for r in con.execute("PRAGMA table_info(ads)")}
        for name, definition in (("reject_reason", "TEXT NOT NULL DEFAULT ''"),
                                 ("is_mining", "INTEGER NOT NULL DEFAULT 0"),
                                 ("missing_cycles", "INTEGER NOT NULL DEFAULT 0"),
                                 ("is_gone", "INTEGER NOT NULL DEFAULT 0"),
                                 ("model_rank", "INTEGER")):
            if name not in existing:
                con.execute(f"ALTER TABLE ads ADD COLUMN {name} {definition}")
                con.commit()
    return con


def table_exists(con: sqlite3.Connection, name: str) -> bool:
    return con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def feed_db():
    """База платного бота (FeedDB создаёт недостающие таблицы и колонки)."""
    from feed_db import FeedDB
    return FeedDB(connect())


def money(value) -> str:
    return f"{int(value):,} ₸".replace(",", " ") if value is not None else "—"


def num(value) -> str:
    return f"{int(value or 0):,}".replace(",", " ")


def dt(ts, fmt: str = "%d.%m %H:%M") -> str:
    return datetime.fromtimestamp(ts, TZ).strftime(fmt) if ts else "—"


def ago(ts) -> str:
    if not ts:
        return "—"
    s = time.time() - ts
    if s < 60:
        return "только что"
    if s < 3600:
        return f"{int(s // 60)} мин назад"
    if s < 86400:
        return f"{int(s // 3600)} ч назад"
    return dt(ts, "%d.%m.%Y")


def days_back(n: int) -> list[datetime]:
    today = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    return [today - timedelta(days=i) for i in range(n - 1, -1, -1)]


def daily(con, sql: str, n: int = 30) -> list:
    """[(подпись, значение, полная дата)] за n дней; sql отдаёт (ts, value) построчно."""
    days = days_back(n)
    buckets = {d.date(): 0 for d in days}
    since = int(days[0].timestamp())
    for ts, value in con.execute(sql, (since,)):
        key = datetime.fromtimestamp(ts, TZ).date()
        if key in buckets:
            buckets[key] += value or 0
    return [(d.strftime("%d.%m"), buckets[d.date()], d.strftime("%d %B").lstrip("0")) for d in days]


def chart(title: str, points: list, kind: str = "bar", fmt: str = "", note: str = "") -> str:
    total = sum(p[1] for p in points)
    shown = money(total) if fmt == "money" else num(total)
    return (f"<div class=card><div class=chart-title><b>{escape(title)}</b><span>{note or f'{shown} за {len(points)} дн.'}</span></div>"
            f"<div class=chart data-kind={kind} data-fmt={fmt} data-label='{escape(title, quote=True)}' "
            f"data-points='{escape(json.dumps(points, ensure_ascii=False), quote=True)}'></div></div>")


def tile(label: str, value: str, detail: str = "", hero: bool = False, href: str = "") -> str:
    inner = (f"<div class=v>{value}</div><div class=l>{escape(label)}</div>"
             f"{f'<div class=d>{detail}</div>' if detail else ''}")
    body = f"<a href='{href}' style='color:inherit;text-decoration:none'>{inner}</a>" if href else inner
    return f"<div class='card tile{' hero' if hero else ''}'>{body}</div>"


def owner_ids() -> set[str]:
    from olx_watcher import load_dotenv
    load_dotenv(ROOT / ".env")
    return {c.strip() for c in os.environ.get("TELEGRAM_CHAT_ID", "").split(",") if c.strip()}


def access_pill(user, owners: set[str] | None = None) -> str:
    now = time.time()
    if owners and user["chat_id"] in owners:
        return "<span class='pill accent'>👑 владелец</span>"
    if user["blocked"]:
        return "<span class='pill bad'>🚫 заблокировал</span>"
    if user["paid_until"] <= now:
        return "<span class='pill'>⏳ истёк</span>"
    if user["plan"] == "paid":
        return f"<span class='pill good'>💰 до {dt(user['paid_until'], '%d.%m')}</span>"
    # plan='trial' и после бонусных дней (за друга, подарок): денег не было, но доступ есть.
    label = "пробный" if user["paid_until"] - user["joined_at"] <= 3 * 86400 + 60 else "бесплатно"
    return f"<span class='pill warn'>🎁 {label} до {dt(user['paid_until'], '%d.%m')}</span>"


def person_link(chat_id, name) -> str:
    """Ссылка на карточку пользователя; после «удалить мои данные» его уже нет."""
    if not chat_id:
        return "—"
    if chat_id == "deleted":
        return "<span class=muted>удалил данные</span>"
    return f"<a href='/users/{escape(chat_id, quote=True)}'>{escape(name or chat_id)}</a>"


def user_name(user) -> str:
    name = " ".join(filter(None, [user["first_name"], user["last_name"]])) or "—"
    tag = f" <span class=muted>@{escape(user['username'])}</span>" if user["username"] else ""
    return f"<a href='/users/{escape(user['chat_id'], quote=True)}'>{escape(name)}</a>{tag}"


def table(head: list[str], rows: list[str], empty: str = "Пусто") -> str:
    if not rows:
        return f"<div class=table-wrap><div class=empty>{escape(empty)}</div></div>"
    cells = "".join(f"<th{' class=num' if h.startswith('#') else ''}>{escape(h.lstrip('#'))}</th>" for h in head)
    return f"<div class=table-wrap><table><thead><tr>{cells}</tr></thead><tbody>{''.join(rows)}</tbody></table></div>"


EVENT_LABELS = {
    "start": "👋 пришёл в бота", "watch_add": "➕ добавил поиск", "watch_delete": "🗑 удалил поиск",
    "watch_edit": "✏️ изменил поиск", "watch_pause": "⏸ поставил на паузу", "watch_resume": "▶️ возобновил",
    "filter": "🎯 сменил фильтр", "favorite_add": "⭐ в избранное", "receipt": "📸 прислал чек",
    "grant": "✅ доступ продлён", "expired": "⏳ доступ истёк", "stars_paid": "⭐ оплатил Stars",
    "referral_join": "🎁 пришёл друг", "referral_reward": "🎁 награда за друга", "broadcast": "📣 рассылка",
    "revoke": "⛔ доступ снят",
}


# --- Защита действий ------------------------------------------------------------
@app.before_request
def same_origin_posts():
    """POST — только со страниц самой панели: чужая вкладка не продлит доступ за вас."""
    if request.method == "POST":
        origin = request.headers.get("Origin") or request.headers.get("Referer") or ""
        if origin and request.host not in origin:
            abort(403)


def back(path: str, message: str):
    return redirect(f"{path}{'&' if '?' in path else '?'}ok={quote(message)}")


# --- 💼 Обзор -------------------------------------------------------------------
@app.get("/")
def overview():
    db = feed_db()
    con, s = db.c, db.stats()
    revenue = daily(con, "SELECT decided_at, amount_kzt FROM payments WHERE status='approved' AND decided_at>=?")
    signups = daily(con, "SELECT joined_at, 1 FROM users WHERE joined_at>=?")
    sent = daily(con, "SELECT sent_at, 1 FROM deliveries WHERE sent_at>=?")
    users = s["users"] or 0
    with_search = con.execute("SELECT COUNT(DISTINCT chat_id) FROM watches").fetchone()[0]
    stages = [("Пришли в бота", users), ("Добавили поиск", with_search), ("Платили хоть раз", s["ever_paid"]),
              ("Платят сейчас", s["paid"])]
    top = max(users, 1)
    funnel = "".join(
        f"<div class=row><span>{escape(name)}</span><div><div class=bar style='width:{max(value / top * 100, 0.5):.1f}%'></div></div>"
        f"<b class=num>{num(value)}</b></div>" for name, value in stages)
    conversion = f"{s['ever_paid'] / users * 100:.0f}% пришедших заплатили" if users else "пока нет пользователей"
    events = con.execute("SELECT e.*, u.first_name, u.username FROM events e LEFT JOIN users u USING(chat_id) "
                         "ORDER BY e.id DESC LIMIT 12").fetchall()
    event_items = "".join(
        f"<li><span>{EVENT_LABELS.get(e['kind'], escape(e['kind']))} · "
        f"<a href='/users/{escape(e['chat_id'] or '', quote=True)}'>{escape(e['first_name'] or e['username'] or e['chat_id'] or '—')}</a></span>"
        f"<span class='muted small nowrap'>{ago(e['at'])}</span></li>" for e in events) or "<li class=muted>Пока пусто</li>"
    queries = popular_queries(con, 8)
    query_items = "".join(f"<li><span>{escape(q)}</span><b>{n}</b></li>" for q, n in queries) or "<li class=muted>Пока нет поисков</li>"
    pending = s["pending"]
    body = (
        f"<div class='grid g4'>"
        f"{tile('Выручка за 30 дней', money(s['revenue_30d']), f'всего {money(s["revenue_total"])}', hero=True)}"
        f"{tile('Платят сейчас', num(s['paid']), f'пробный доступ: {num(s["trial"])}', href='/users?f=paid')}"
        f"{tile('Пользователей', num(users), f'+{num(s["new_7d"])} за неделю, сегодня +{num(s["new_today"])}', href='/users')}"
        f"{tile('Чеки на проверке', num(pending), 'нужно подтвердить' if pending else 'всё обработано', href='/payments')}"
        f"</div><div class='grid g3' style='margin-top:12px'>"
        f"{chart('Выручка по дням', revenue, 'bar', 'money')}{chart('Новые пользователи', signups)}{chart('Отправлено объявлений', sent, 'line')}"
        f"</div><div class='grid g3' style='margin-top:12px'>"
        f"<div class='card funnel'><div class=chart-title><b>Воронка</b><span>{conversion}</span></div>{funnel}</div>"
        f"<div class=card><div class=chart-title><b>Популярные запросы</b><a class=small href='/searches'>все поиски →</a></div><ul class=list>{query_items}</ul></div>"
        f"<div class=card><div class=chart-title><b>Последние действия</b><a class=small href='/events'>журнал →</a></div><ul class=list>{event_items}</ul></div>"
        f"</div><div class='grid g4' style='margin-top:12px'>"
        f"{tile('Заходили за 7 дней', num(s['active_7d']))}{tile('Поисков', num(s['watches']), f'разных выдач в опросе: {num(s["urls"])}', href='/searches')}"
        f"{tile('Отправлено сегодня', num(s['sent_today']), f'за неделю: {num(s["sent_7d"])}', href='/deliveries')}"
        f"{tile('Заблокировали бота', num(s['blocked']))}</div>")
    con.close()
    return layout("Обзор", "/", body, "Платный бот: деньги, пользователи и как работает поиск. Время — Алматы.")


def popular_queries(con, limit: int) -> list[tuple[str, int]]:
    """Что ищут чаще всего: текст из «q-…» в ссылках поисков."""
    from olx_feed import parse_search
    counts: dict[str, int] = {}
    for (url,) in con.execute("SELECT search_url FROM watches"):
        query = parse_search(url)["query"] or "по ссылке (без запроса)"
        counts[query] = counts.get(query, 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]


# --- 👥 Пользователи ------------------------------------------------------------
USER_FILTERS = {"all": "Все", "paid": "💰 Платят", "trial": "🎁 Пробный", "expired": "⏳ Истёк", "blocked": "🚫 Заблокировали"}


@app.get("/users")
def users():
    db, owners = feed_db(), owner_ids()
    query, kind = request.args.get("q", "").strip(), request.args.get("f", "all")
    now = int(time.time())
    where = {"paid": "paid_until>? AND plan='paid'", "trial": "paid_until>? AND plan='trial'",
             "expired": "paid_until<=?", "blocked": "blocked=1 AND ?>0"}.get(kind, "?>0")
    params: list = [now]
    if query:
        where += " AND (chat_id LIKE ? OR username LIKE ? OR first_name LIKE ? OR last_name LIKE ?)"
        params += [f"%{query.lstrip('@')}%"] * 4
    rows = db.c.execute(
        "SELECT u.*, (SELECT COUNT(*) FROM watches w WHERE w.chat_id=u.chat_id) watches_n, "
        "(SELECT COUNT(*) FROM deliveries d WHERE d.chat_id=u.chat_id) sent_n, "
        "(SELECT COALESCE(SUM(amount_kzt),0) FROM payments p WHERE p.chat_id=u.chat_id AND p.status='approved') paid_sum "
        f"FROM users u WHERE {where} ORDER BY COALESCE(last_seen_at, joined_at) DESC LIMIT 500", params).fetchall()
    chips = "".join(f"<a href='/users?{urlencode({'f': k, 'q': query})}' class='{'on' if k == kind else ''}'>{v}</a>"
                    for k, v in USER_FILTERS.items())
    toolbar = (f"<div class=toolbar><form class=inline><input name=q value='{escape(query, quote=True)}' "
               f"placeholder='Имя, @username или ID' style='width:260px'><input type=hidden name=f value='{escape(kind, quote=True)}'>"
               f"<button>Найти</button></form><div class=chips>{chips}</div></div>")
    lines = [f"<tr><td>{user_name(r)}<div class='muted small'>ID {escape(r['chat_id'])}</div></td>"
             f"<td>{access_pill(r, owners)}</td><td class=num>{r['watches_n']}</td><td class=num>{num(r['sent_n'])}</td>"
             f"<td class=num>{money(r['paid_sum']) if r['paid_sum'] else '—'}</td>"
             f"<td class=nowrap>{dt(r['joined_at'], '%d.%m.%Y')}</td><td class=nowrap>{ago(r['last_seen_at'])}</td></tr>" for r in rows]
    db.c.close()
    return layout("Пользователи", "/users", toolbar + table(
        ["Пользователь", "Доступ", "#Поисков", "#Получил", "#Заплатил", "Пришёл", "Был"], lines, "Никого не нашлось"),
        f"Показано: {len(rows)}")


@app.get("/users/<chat_id>")
def user_card(chat_id: str):
    db, owners = feed_db(), owner_ids()
    user = db.user(chat_id)
    if not user:
        abort(404)
    invited, invited_paid = db.referral_stats(chat_id)
    referrer = db.user(user["referrer"]) if user["referrer"] else None
    watches = db.watches(chat_id)
    payments = db.payments_of(chat_id, 50)
    sent = db.recent_deliveries(chat_id, 20)
    events = db.c.execute("SELECT * FROM events WHERE chat_id=? ORDER BY id DESC LIMIT 25", (chat_id,)).fetchall()
    from olx_feed import settings
    from olx_watcher import load_config
    cfg = settings(load_config())
    grant_form = (f"<form class=inline method=post action='/users/{escape(chat_id, quote=True)}/grant'>"
                  f"<select name=preset><option value='{cfg['period_days']}:1'>💰 +{cfg['period_days']} дней — оплата {money(cfg['price_kzt'])}</option>"
                  f"<option value='3:0'>🎁 +3 дня бонус</option><option value='7:0'>🎁 +7 дней бонус</option>"
                  f"<option value='-1:0'>➖ минус 1 день</option><option value='revoke'>⛔ снять доступ сейчас</option></select>"
                  f"<button>Применить</button></form>")
    info = (f"<dl class=kv><dt>Доступ</dt><dd>{access_pill(user, owners)} <span class=muted>· {dt(user['paid_until'], '%d.%m.%Y %H:%M')}</span></dd>"
            f"<dt>Пришёл</dt><dd>{dt(user['joined_at'], '%d.%m.%Y %H:%M')}{f' · по приглашению {user_name(referrer)}' if referrer else ''}</dd>"
            f"<dt>Последний раз</dt><dd>{ago(user['last_seen_at'])}</dd>"
            f"<dt>Фильтр выгоды</dt><dd>{'все новые' if not user['min_discount'] else f'от {user["min_discount"]}% дешевле рынка'}</dd>"
            f"<dt>Пригласил</dt><dd>{invited} чел., оплатили {invited_paid}</dd>"
            f"<dt>Избранное</dt><dd>{db.count('favorites', chat_id)}</dd>"
            f"<dt>ID</dt><dd><code>{escape(chat_id)}</code></dd></dl>")
    watch_rows = [f"<tr><td><a href='{escape(w['search_url'], quote=True)}' target=_blank rel=noopener>{escape(w['title'])}</a>"
                  f"{f'<div class=small>🚫 {escape(w["exclude_words"])}</div>' if w['exclude_words'] else ''}</td>"
                  f"<td>{'<span class=pill>⏸ пауза</span>' if w['paused'] else '<span class=\"pill good\">● активен</span>'}</td>"
                  f"<td class=num>{w['sent_count']}</td><td class=nowrap>{ago(w['last_sent_at'])}</td></tr>" for w in watches]
    pay_rows = [f"<tr><td class=nowrap>{dt(p['created_at'])}</td><td>{escape(p['method'])}</td><td>{status_pill(p['status'])}</td>"
                f"<td class=num>{p['days']}</td><td class=num>{money(p['amount_kzt'])}</td></tr>" for p in payments]
    sent_rows = [f"<tr><td><a href='{escape(d['url'] or '', quote=True)}' target=_blank rel=noopener>{escape(d['title'] or '—')}</a></td>"
                 f"<td class=num>{money(d['price_kzt'])}</td><td>{discount_pill(d['discount_pct'])}</td><td class=nowrap>{ago(d['sent_at'])}</td></tr>" for d in sent]
    event_rows = [f"<li><span>{EVENT_LABELS.get(e['kind'], escape(e['kind']))} <span class=muted>· {event_detail(db, e)}</span></span>"
                  f"<span class='muted small nowrap'>{dt(e['at'])}</span></li>" for e in events]
    name = " ".join(filter(None, [user["first_name"], user["last_name"]])) or chat_id
    body = (f"<div class='grid g2'><div class=card>{info}</div>"
            f"<div class=card><div class=chart-title><b>Доступ</b><span>пользователь получит сообщение от бота</span></div>{grant_form}"
            f"<h2 style='margin-top:18px'>Журнал</h2><ul class=list>{''.join(event_rows) or '<li class=muted>Пусто</li>'}</ul></div></div>"
            f"<h2>Поиски · {len(watches)}</h2>{table(['Поиск', 'Статус', '#Прислано', 'Последнее'], watch_rows, 'Поисков нет')}"
            f"<h2>Оплаты</h2>{table(['Когда', 'Способ', 'Статус', '#Дней', '#Сумма'], pay_rows, 'Оплат не было')}"
            f"<h2>Последние присланные</h2>{table(['Объявление', '#Цена', 'Рынок', 'Когда'], sent_rows, 'Пока ничего')}")
    db.c.close()
    sub = f"@{escape(user['username'])}" if user["username"] else ""
    return layout(name, "/users", body, sub)


@app.post("/users/<chat_id>/grant")
def user_grant(chat_id: str):
    import olx_feed as feed
    from olx_watcher import load_config
    db = feed_db()
    cfg = feed.settings(load_config())
    preset = request.form.get("preset", "")
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    owner_ids()  # подгружает .env
    token = os.environ.get("TELEGRAM_BOT_TOKEN", token)
    if preset == "revoke":
        db.set_user(chat_id, paid_until=int(time.time()), expiring_notified=1, expired_notified=0)
        db.log(chat_id, "revoke", "доступ снят в панели")
        db.c.close()
        return back(f"/users/{quote(chat_id)}", "Доступ снят — бот сообщит, что он закончился")
    days, _, paid = preset.partition(":")
    if not days.lstrip("-").isdigit():
        abort(400)
    days_n = int(days)
    amount = round(int(cfg["price_kzt"]) * days_n / int(cfg["period_days"])) if paid == "1" else 0
    user = db.grant(chat_id, days_n, by="dashboard", amount_kzt=amount)
    if not user:
        abort(404)
    if days_n > 0:
        feed.notify_granted(db, token, user)
    db.c.close()
    return back(f"/users/{quote(chat_id)}", f"Доступ до {dt(user['paid_until'], '%d.%m.%Y %H:%M')}")


def event_detail(db, event) -> str:
    """Подробность события: у реферальных там ID друга — показываем имя со ссылкой."""
    detail = event["detail"] or ""
    if event["kind"] in {"referral_join", "referral_reward"} and detail:
        friend = db.user(detail)
        return person_link(detail, (friend["first_name"] or friend["username"]) if friend else None)
    return escape(detail[:60])


def status_pill(status: str) -> str:
    return {"approved": "<span class='pill good'>✅ подтверждён</span>", "pending": "<span class='pill warn'>⏳ на проверке</span>",
            "rejected": "<span class='pill bad'>❌ отклонён</span>", "refunded": "<span class=pill>↩️ возврат</span>"}.get(
        status, f"<span class=pill>{escape(status)}</span>")


def discount_pill(pct) -> str:
    if pct is None:
        return "<span class='muted small'>—</span>"
    if pct >= 60:
        return f"<span class='pill warn'>⚠️ −{pct}%</span>"
    if pct >= 10:
        return f"<span class='pill good'>🔥 −{pct}%</span>"
    if pct <= -10:
        return f"<span class='pill bad'>+{-pct}%</span>"
    return "<span class=pill>≈ рынок</span>"


# --- 💳 Оплаты ------------------------------------------------------------------
@app.get("/payments")
def payments():
    db = feed_db()
    con = db.c
    pending = db.pending_payments()
    pend_rows = [f"<tr><td class=nowrap>#{p['id']} · {dt(p['created_at'])}</td>"
                 f"<td><a href='/users/{escape(p['chat_id'], quote=True)}'>{escape(p['first_name'] or p['username'] or p['chat_id'])}</a>"
                 f"{f' <span class=muted>@{escape(p["username"])}</span>' if p['username'] else ''}</td>"
                 f"<td class=muted>фото чека — в Telegram у бота</td>"
                 f"<td class=nowrap><form class=inline method=post action='/payments/{p['id']}/approve'><button>✅ Подтвердить</button></form> "
                 f"<form class=inline method=post action='/payments/{p['id']}/reject'><button class=danger>Отклонить</button></form></td></tr>"
                 for p in pending]
    method = request.args.get("m", "")
    where, params = ("WHERE p.method=?", [method]) if method else ("", [])
    history = con.execute(f"SELECT p.*, u.first_name, u.username FROM payments p LEFT JOIN users u USING(chat_id) "
                          f"{where} ORDER BY p.created_at DESC LIMIT 300", params).fetchall()
    by_method = con.execute("SELECT method, COUNT(*) n, COALESCE(SUM(amount_kzt),0) s FROM payments "
                            "WHERE status='approved' GROUP BY method ORDER BY s DESC").fetchall()
    names = {"receipt": "📸 Kaspi-чек", "stars": "⭐ Stars", "manual": "✋ вручную", "referral": "🎁 за друга"}
    tiles = "".join(tile(names.get(r["method"], r["method"]), money(r["s"]), f"{r['n']} шт.") for r in by_method)
    chips = "".join(f"<a href='/payments{('?m=' + k) if k else ''}' class='{'on' if k == method else ''}'>{v}</a>"
                    for k, v in [("", "Все"), *names.items()])
    hist_rows = [f"<tr><td class=nowrap>{dt(p['created_at'])}</td><td>"
                 f"{person_link(p['chat_id'], p['first_name'] or p['username'])}</td>"
                 f"<td>{names.get(p['method'], escape(p['method']))}</td><td>{status_pill(p['status'])}</td>"
                 f"<td class=num>{p['days']}</td><td class=num>{money(p['amount_kzt'])}</td></tr>" for p in history]
    revenue = daily(con, "SELECT decided_at, amount_kzt FROM payments WHERE status='approved' AND decided_at>=?", 60)
    body = (f"<h2 style='margin-top:0'>На проверке · {len(pending)}</h2>"
            f"{table(['Чек', 'Пользователь', '', ''], pend_rows, 'Нет чеков на проверке 👌')}"
            f"<div class='grid g4' style='margin-top:16px'>{tiles}</div>"
            f"<div style='margin-top:12px'>{chart('Выручка по дням', revenue, 'bar', 'money')}</div>"
            f"<h2>История</h2><div class=toolbar><div class=chips>{chips}</div></div>"
            f"{table(['Когда', 'Пользователь', 'Способ', 'Статус', '#Дней', '#Сумма'], hist_rows, 'Оплат пока не было')}")
    con.close()
    return layout("Оплаты", "/payments", body, "Kaspi-чеки подтверждаются здесь или кнопкой в Telegram — пользователь получит сообщение.")


@app.post("/payments/<int:payment_id>/<decision>")
def payment_decision(payment_id: int, decision: str):
    import olx_feed as feed
    from olx_watcher import load_config
    owner_ids()
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    db = feed_db()
    cfg = feed.settings(load_config())
    payment = db.payment(payment_id)
    if not payment or payment["status"] != "pending":
        db.c.close()
        return back("/payments", "Этот чек уже обработан")
    if decision == "approve":
        user = db.grant(payment["chat_id"], int(cfg["period_days"]), by="dashboard",
                        amount_kzt=int(cfg["price_kzt"]), method="receipt", payment_id=payment_id)
        feed.notify_granted(db, token, user)
        message = f"Чек #{payment_id} подтверждён, доступ до {dt(user['paid_until'], '%d.%m.%Y')}"
    elif decision == "reject":
        db.reject_payment(payment_id, "dashboard")
        feed.safe_send(db, token, payment["chat_id"],
                       "❌ <b>Оплата не подтверждена.</b>\nПроверьте сумму и реквизиты и отправьте чек ещё раз.")
        message = f"Чек #{payment_id} отклонён"
    else:
        abort(404)
    db.c.close()
    return back("/payments", message)


# --- 🔎 Поиски ------------------------------------------------------------------
@app.get("/searches")
def searches():
    db = feed_db()
    con = db.c
    query = request.args.get("q", "").strip()
    where, params = ("WHERE w.title LIKE ? OR w.search_url LIKE ?", [f"%{query}%"] * 2) if query else ("", [])
    rows = con.execute(
        "SELECT w.*, u.first_name, u.username, u.chat_id uid, f.last_polled, f.baseline_done FROM watches w "
        "LEFT JOIN users u ON u.chat_id=w.chat_id LEFT JOIN feed_urls f ON f.search_url=w.search_url "
        f"{where} ORDER BY w.sent_count DESC, w.id DESC LIMIT 500", params).fetchall()
    lines = []
    for w in rows:
        median, samples = db.market(w["search_url"], 30)
        lines.append(
            f"<tr><td><a href='{escape(w['search_url'], quote=True)}' target=_blank rel=noopener>{escape(w['title'])}</a>"
            f"{f'<div class=small>🚫 {escape(w["exclude_words"])}</div>' if w['exclude_words'] else ''}</td>"
            f"<td><a href='/users/{escape(w['uid'] or '', quote=True)}'>{escape(w['first_name'] or w['username'] or w['chat_id'])}</a></td>"
            f"<td>{'<span class=pill>⏸ пауза</span>' if w['paused'] else '<span class=\"pill good\">● активен</span>'}</td>"
            f"<td class=num>{w['sent_count']}</td><td class=num>{money(median) if median and samples >= 8 else '<span class=muted>мало данных</span>'}"
            f"<div class='muted small'>{samples} объявл.</div></td><td class=nowrap>{ago(w['last_polled'])}</td></tr>")
    queries = popular_queries(con, 20)
    top = max((n for _, n in queries), default=1)
    query_bars = "".join(
        f"<div class=row><span title='{escape(q, quote=True)}' style='overflow:hidden;text-overflow:ellipsis;white-space:nowrap'>{escape(q)}</span>"
        f"<div><div class=bar style='width:{n / top * 100:.1f}%'></div></div><b class=num>{n}</b></div>" for q, n in queries)
    body = (f"<div class='grid g2'><div class='card funnel'><div class=chart-title><b>Популярные запросы</b><span>сколько поисков</span></div>"
            f"{query_bars or '<div class=muted>Пока нет</div>'}</div>"
            f"<div class=card><div class=chart-title><b>Как это работает</b></div><p class=muted style=margin:0>Одинаковые ссылки разных людей "
            f"опрашиваются одним запросом. «Рынок» — медиана цен этой выдачи за 30 дней; с ней сравнивается каждое новое объявление. "
            f"«Опрошен» — когда бот последний раз читал эту выдачу.</p></div></div>"
            f"<h2>Все поиски · {len(rows)}</h2><div class=toolbar><form class=inline><input name=q value='{escape(query, quote=True)}' "
            f"placeholder='Запрос или часть ссылки' style='width:260px'><button>Найти</button></form></div>"
            f"{table(['Поиск', 'Чей', 'Статус', '#Прислано', '#Рынок', 'Опрошен'], lines, 'Поисков нет')}")
    con.close()
    return layout("Поиски", "/searches", body)


# --- 📨 Отправленное ------------------------------------------------------------
@app.get("/deliveries")
def deliveries():
    db = feed_db()
    con = db.c
    rows = con.execute("SELECT d.*, u.first_name, u.username, w.title watch_title FROM deliveries d "
                       "LEFT JOIN users u ON u.chat_id=d.chat_id LEFT JOIN watches w ON w.id=d.watch_id "
                       "ORDER BY d.sent_at DESC LIMIT 300").fetchall()
    lines = [f"<tr><td><a href='{escape(d['url'] or '', quote=True)}' target=_blank rel=noopener>{escape(d['title'] or '—')}</a>"
             f"<div class='muted small'>{escape(d['watch_title'] or 'поиск удалён')}</div></td>"
             f"<td><a href='/users/{escape(d['chat_id'], quote=True)}'>{escape(d['first_name'] or d['username'] or d['chat_id'])}</a></td>"
             f"<td class=num>{money(d['price_kzt'])}</td><td>{discount_pill(d['discount_pct'])}</td><td class=nowrap>{ago(d['sent_at'])}</td></tr>"
             for d in rows]
    sent = daily(con, "SELECT sent_at, 1 FROM deliveries WHERE sent_at>=?")
    con.close()
    return layout("Отправленное", "/deliveries", chart("Отправлено по дням", sent, "line") + "<h2>Последние 300</h2>" +
                  table(["Объявление", "Кому", "#Цена", "Рынок", "Когда"], lines, "Пока ничего не отправлено"))


# --- 🧾 Журнал ------------------------------------------------------------------
@app.get("/events")
def events():
    con = feed_db().c
    kind = request.args.get("k", "")
    where, params = ("WHERE e.kind=?", [kind]) if kind else ("", [])
    rows = con.execute(f"SELECT e.*, u.first_name, u.username FROM events e LEFT JOIN users u USING(chat_id) "
                       f"{where} ORDER BY e.id DESC LIMIT 400", params).fetchall()
    kinds = con.execute("SELECT kind, COUNT(*) n FROM events GROUP BY kind ORDER BY n DESC").fetchall()
    chips = (f"<a href='/events' class='{'on' if not kind else ''}'>Все</a>" +
             "".join(f"<a href='/events?k={quote(k['kind'])}' class='{'on' if k['kind'] == kind else ''}'>"
                     f"{EVENT_LABELS.get(k['kind'], escape(k['kind']))} · {k['n']}</a>" for k in kinds))
    lines = [f"<tr><td class=nowrap>{dt(e['at'], '%d.%m %H:%M:%S')}</td><td>{EVENT_LABELS.get(e['kind'], escape(e['kind']))}</td>"
             f"<td>{person_link(e['chat_id'], e['first_name'] or e['username'])}</td>"
             f"<td class='muted small' style='max-width:420px;overflow:hidden;text-overflow:ellipsis'>{escape(e['detail'] or '')}</td></tr>"
             for e in rows]
    con.close()
    return layout("Журнал", "/events", f"<div class=toolbar><div class=chips>{chips}</div></div>" +
                  table(["Когда", "Что", "Кто", "Подробности"], lines, "Журнал пуст"),
                  "Все действия пользователей и владельца — для разбора «почему так вышло».")


# --- 🎮 Личный режим: видеокарты --------------------------------------------------
COLUMNS = {
    "profile": ("profile_id", "Раздел"),
    "title": ("title", "Объявление"),
    # Сортируем по рангу поколения, а не по имени: строкой «GTX 1050» встало бы
    # раньше «GTX 960», хотя карта новее на четыре поколения.
    "model": ("model_rank", "Модель"),
    "price": ("price_kzt", "Цена"),
    "reason": ("reject_reason", "Причина отсева"),
    "status": ("status", "Статус"),
    "seller": ("seller_name", "Продавец"),
    "risk": ("risk_flags", "Риски"),
    "new": ("first_seen", "Появилось"),
}


def order_by(sort: str, direction: str) -> str:
    # Пустые значения всегда внизу при любом направлении.
    column = COLUMNS.get(sort, COLUMNS["price"])[0]
    way = "DESC" if direction == "desc" else "ASC"
    return f"{column} IS NULL, {column} {way}, first_seen DESC"


def sort_link(key: str, label: str, sort: str, direction: str) -> str:
    """Заголовок-ссылка: первый клик сортирует, повторный разворачивает."""
    active = key == sort
    nxt = "desc" if active and direction == "asc" else "asc"
    params = {k: v for k, v in request.args.items() if k not in ("sort", "dir")}
    params.update(sort=key, dir=nxt)
    arrow = (" ▲" if direction == "asc" else " ▼") if active else ""
    return f"<th><a href='{request.path}?{urlencode(params)}'>{escape(label)}{arrow}</a></th>"


def rows_table(rows, notes: dict[str, str], show_reason: bool, sort: str, direction: str) -> str:
    order = ["title", "model", "price", "reason" if show_reason else "status", "seller", "risk", "new"]
    head = "<tr>" + "".join(sort_link(key, COLUMNS[key][1], sort, direction) for key in order) + "</tr>"
    cells = []
    for r in rows:
        note = notes.get(r["ad_id"], "")
        price = money(r["price_kzt"]) if r["price_kzt"] is not None else "<span class=muted>не распознана</span>"
        if show_reason:
            status = f"<span class='pill bad'>{escape(r['reject_reason'])}</span>" if r["reject_reason"] else "<span class='pill good'>прошло</span>"
        else:
            status = (f"<form class=inline method=post action=/gpu/update>"
                      f"<input type=hidden name=id value='{escape(r['ad_id'], quote=True)}'>"
                      f"<select name=status>{''.join(f'<option{" selected" if r["status"] == s else " "}>{s}</option>' for s in ('new', 'interested', 'ignored'))}</select>"
                      f"<select name=deal>{''.join(f'<option{" selected" if r["deal_status"] == d else " "}>{d}</option>' for d in ('new', 'позвонить', 'написал', 'договорился', 'купил', 'архив'))}</select>"
                      f"<input name=note placeholder='заметка' style='width:120px'><button class=ghost>Сохранить</button></form>")
        flags = "".join(flag for flag, on in (("<div class=small>⛏ майнинг</div>", r["is_mining"]),
                                              ("<div class=small>🚫 снято с публикации</div>", r["is_gone"]),
                                              ("<div class='small muted'>не отправлено</div>", not r["telegram_message_id"])) if on)
        cells.append(
            f"<tr{' class=gone' if r['is_gone'] else ''}><td><a href='{escape(r['url'], quote=True)}' target=_blank rel=noopener>"
            f"{escape(r['title'] or '—')}</a>{f'<div class=small>📝 {escape(note)}</div>' if note else ''}</td>"
            f"<td class=nowrap>{escape(r['model'] or '—')}{flags}</td>"
            f"<td class='num'>{price}<div><a class=small href='/gpu/price/{escape(r['ad_id'], quote=True)}'>история</a></div></td>"
            f"<td>{status}</td><td>{escape(r['seller_name'] or '—')}</td><td class=small>{escape(r['risk_flags'] or '—')}</td>"
            f"<td class=nowrap>{ago(r['first_seen'])}</td></tr>")
    return f"<div class=table-wrap><table><thead>{head}</thead><tbody>{''.join(cells)}</tbody></table></div>"


def gpu_listing(title: str, active: str, where: str, params: list, show_reason: bool,
                default_sort: str = "price", before: str = "") -> str:
    query = request.args.get("q", "").strip()
    model_filter = request.args.get("model", "").strip()
    price_min = request.args.get("price_min", "").strip()
    price_max = request.args.get("price_max", "").strip()
    sort = request.args.get("sort", default_sort)
    direction = request.args.get("dir", "desc" if sort == "new" else "asc")
    clauses, values = [where] if where else [], list(params)
    if query:
        clauses.append("(title LIKE ? OR model LIKE ? OR seller_name LIKE ?)")
        values += [f"%{query}%"] * 3
    if model_filter:
        clauses.append("model = ?")
        values.append(model_filter)
    # Значения из адресной строки могут быть чем угодно: ?price_min=abc роняло
    # страницу на int() с 500-й ошибкой. Нечисловой фильтр просто игнорируем.
    for raw, sql in ((price_min, "price_kzt >= ?"), (price_max, "price_kzt <= ?")):
        if not raw:
            continue
        try:
            values.append(int(raw.replace(" ", "").replace(" ", "")))
        except ValueError:
            continue
        clauses.append(sql)
    clause = " WHERE " + " AND ".join(clauses) if clauses else ""
    con = connect()
    if not table_exists(con, "ads"):
        con.close()
        return layout(title, active, "<div class='card empty'>Режим видеокарт ещё не запускался.</div>")
    rows = con.execute(f"SELECT * FROM ads{clause} ORDER BY {order_by(sort, direction)} LIMIT 500", values).fetchall()
    notes = {r["ad_id"]: r["body"] for r in
             con.execute("SELECT ad_id,body FROM notes WHERE id IN (SELECT MAX(id) FROM notes GROUP BY ad_id)")}
    models = [r[0] for r in con.execute("SELECT DISTINCT model FROM ads WHERE model IS NOT NULL ORDER BY model_rank")]
    paused = con.execute("SELECT value FROM meta WHERE key='watcher_paused'").fetchone()
    last = con.execute("SELECT MAX(first_seen) FROM ads").fetchone()[0]
    con.close()
    options = "".join(f"<option value='{escape(m, quote=True)}'{' selected' if m == model_filter else ''}>{escape(m)}</option>" for m in models)
    toolbar = (f"<div class=toolbar><form class=inline><input name=q value='{escape(query, quote=True)}' placeholder='Название, модель, продавец'>"
               f"<select name=model><option value=''>Все модели</option>{options}</select>"
               f"<input name=price_min value='{escape(price_min, quote=True)}' placeholder='от ₸' style='width:90px'>"
               f"<input name=price_max value='{escape(price_max, quote=True)}' placeholder='до ₸' style='width:90px'>"
               f"<input type=hidden name=sort value='{escape(sort, quote=True)}'><input type=hidden name=dir value='{escape(direction, quote=True)}'>"
               f"<button>Показать</button></form></div>")
    state = ("<span class='pill warn'>⏸ на паузе — снять: /menu в Telegram</span>" if paused and paused[0] == "1"
             else "<span class='pill good'>● работает</span>")
    subtitle = f"Личный обход видеокарт {state} · последнее объявление {ago(last)} · показано {len(rows)}"
    return layout(title, active, before + toolbar + rows_table(rows, notes, show_reason, sort, direction), subtitle)


def reason_chips(groups) -> str:
    """Причины отсева, свёрнутые по категориям («устаревшая серия: …» — их десятки)."""
    categories: dict[str, list[tuple[str, int]]] = {}
    for row in groups:
        category = row["r"].split(":", 1)[0] if ":" in row["r"] else row["r"]
        categories.setdefault(category, []).append((row["r"], row["c"]))
    chips = [f"<a class=reason href='/gpu'><b>{sum(g['c'] for g in groups)}</b>все причины</a>"]
    for category, items in sorted(categories.items(), key=lambda kv: -sum(c for _, c in kv[1])):
        total = sum(count for _, count in items)
        if len(items) == 1 and items[0][0] == category:
            chips.append(f"<a class=reason href='/gpu?reason={quote(category)}'><b>{total}</b>{escape(category)}</a>")
            continue
        inner = "".join(f"<a href='/gpu?reason={quote(reason)}'>{escape(reason.split(': ', 1)[-1])} · {count}</a>" for reason, count in items)
        chips.append(f"<details class=reason><summary><b>{total}</b>{escape(category)} <span class=muted>· {len(items)}</span></summary>"
                     f"<div class=subs><a href='/gpu?group={quote(category)}'>показать все {total}</a>{inner}</div></details>")
    return f"<div class=reasons>{''.join(chips)}</div>"


@app.get("/gpu")
def gpu_rejected():
    """Что фильтры съели и почему — сгруппировано по причине."""
    reason, group = request.args.get("reason", ""), request.args.get("group", "")
    con = connect()
    groups = con.execute("SELECT reject_reason r, COUNT(*) c FROM ads WHERE reject_reason != '' "
                         "GROUP BY r ORDER BY c DESC").fetchall() if table_exists(con, "ads") else []
    con.close()
    if reason:
        where, params = "reject_reason = ?", [reason]
    elif group:
        where, params = "reject_reason LIKE ?", [f"{group}:%"]
    else:
        where, params = "reject_reason != ''", []
    return gpu_listing("Отсеянные", "/gpu", where, params, True, before=reason_chips(groups) if groups else "")


@app.get("/gpu/passed")
def gpu_passed():
    """Всё, что проходит фильтр сейчас, — включая не отправленное после смены правил."""
    return gpu_listing("Прошли фильтр", "/gpu/passed", "reject_reason = ''", [], False)


@app.get("/gpu/today")
def gpu_today():
    midnight = int(datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    return gpu_listing("Новые за сегодня", "/gpu/today", "first_seen >= ?", [midnight], True, "new")


@app.get("/gpu/mining")
def gpu_mining():
    """Карты без видеовыходов (P106/P104, CMP): формально NVIDIA, но смотреть отдельно."""
    return gpu_listing("Майнинговые карты", "/gpu/mining", "is_mining = 1", [], True)


@app.get("/gpu/all")
def gpu_all():
    return gpu_listing("Все объявления", "/gpu/all", "", [], True)


@app.post("/gpu/update")
def gpu_update():
    ad_id = request.form["id"]
    con = connect()
    con.execute("UPDATE ads SET status=?, deal_status=? WHERE ad_id=?",
                (request.form["status"], request.form["deal"], ad_id))
    note = request.form.get("note", "").strip()
    if note:
        con.execute("INSERT INTO notes(ad_id,created_at,body) VALUES(?,?,?)", (ad_id, int(time.time()), note))
    con.commit()
    con.close()
    return redirect(request.referrer or "/gpu/passed")


@app.get("/gpu/price/<ad_id>")
def gpu_price(ad_id: str):
    con = connect()
    rows = con.execute("SELECT recorded_at,price_kzt FROM price_history WHERE ad_id=? AND price_kzt IS NOT NULL "
                       "ORDER BY recorded_at", (ad_id,)).fetchall()
    ad = con.execute("SELECT title, url FROM ads WHERE ad_id=?", (ad_id,)).fetchone()
    con.close()
    if not rows:
        return layout("История цены", "", "<div class='card empty'>История цены пока отсутствует.</div>"), 404
    points = [(dt(r["recorded_at"], "%d.%m"), r["price_kzt"], dt(r["recorded_at"])) for r in rows]
    prices = [r["price_kzt"] for r in rows]
    body = (f"<div class='grid g3'>{tile('Сейчас', money(prices[-1]))}{tile('Минимум', money(min(prices)))}{tile('Максимум', money(max(prices)))}</div>"
            f"<div style='margin-top:12px'>{chart('Цена', points, 'line', 'money', f'{len(rows)} замеров')}</div>"
            + (f"<p><a href='{escape(ad['url'], quote=True)}' target=_blank rel=noopener>Открыть на OLX →</a></p>" if ad else ""))
    return layout((ad["title"] if ad else ad_id)[:80], "", body, "История цены")


@app.get("/gpu/market")
def gpu_market():
    """Обзор рынка: цены по моделям среди прошедших фильтр."""
    con = connect()
    models = con.execute(
        "SELECT model, COUNT(*) cnt, CAST(AVG(price_kzt) AS INTEGER) avg_price, MIN(price_kzt) min_price, "
        "MAX(price_kzt) max_price, MIN(model_rank) rnk FROM ads WHERE model IS NOT NULL AND price_kzt IS NOT NULL "
        "AND reject_reason = '' GROUP BY model ORDER BY rnk").fetchall() if table_exists(con, "ads") else []
    con.close()
    if not models:
        return layout("Рынок видеокарт", "/gpu/market", "<div class='card empty'>Пока нет данных.</div>")
    points = [(r["model"], r["avg_price"], f"{r['model']}: средняя, {r['cnt']} объявл.") for r in models]
    lines = [f"<tr><td>{escape(r['model'])}</td><td class=num>{r['cnt']}</td><td class=num>{money(r['min_price'])}</td>"
             f"<td class=num><b>{money(r['avg_price'])}</b></td><td class=num>{money(r['max_price'])}</td></tr>" for r in models]
    body = (chart("Средняя цена по моделям", points, "bar", "money", "по прошедшим фильтр") +
            "<h2>По моделям</h2>" + table(["Модель", "#Объявлений", "#Мин", "#Средняя", "#Макс"], lines))
    return layout("Рынок видеокарт", "/gpu/market", body, "Модели — по поколениям, от старых к новым.")


# Старые адреса личного режима — на новые, чтобы закладки не ломались.
for old, new in {"/passed": "/gpu/passed", "/today": "/gpu/today", "/mining": "/gpu/mining",
                 "/all": "/gpu/all", "/market": "/gpu/market"}.items():
    app.add_url_rule(old, f"legacy_{old.strip('/')}", (lambda target: lambda: redirect(target))(new))
app.add_url_rule("/price/<ad_id>", "legacy_price", lambda ad_id: redirect(f"/gpu/price/{ad_id}"))


if __name__ == "__main__":
    # Список адресов через пробел. По умолчанию только localhost; на сервере
    # systemd добавляет адрес Tailscale — панель открывается с Mac и телефона,
    # но не выставлена ни в интернет, ни в домашнюю сеть.
    listen = os.getenv("DASHBOARD_LISTEN", "127.0.0.1:5050")
    from waitress import serve
    print(f"Панель слушает: {listen}")
    serve(app, listen=listen, threads=4)
