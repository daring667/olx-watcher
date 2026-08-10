"""Локальная панель OLX-помощника.

Главный экран — не избранное (оно удобнее в Telegram), а отсеянные объявления
с причиной отсева. Из 546 просмотренных до Telegram доходит около 56, и без
этого экрана невозможно понять, что именно съедают фильтры.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from html import escape
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlencode

from flask import Flask, redirect, request

DB = Path(__file__).parent / "data" / "watcher.sqlite3"
app = Flask(__name__)

STYLE = """<style>
body{font:14px/1.45 system-ui,-apple-system,sans-serif;margin:0;padding:24px;background:#f6f7fb;color:#1a1d24}
h1{font-size:20px;margin:0 0 4px}
nav{margin:14px 0}nav a{margin-right:14px;color:#1769e0;text-decoration:none}nav a.on{font-weight:600;color:#1a1d24}
table{width:100%;border-collapse:collapse;background:#fff;border-radius:8px;overflow:hidden}
td,th{padding:9px 11px;border-bottom:1px solid #e6e8ee;text-align:left;vertical-align:top}
th{background:#eef0f6;font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.03em}
th a.sortable{color:inherit;text-decoration:none;white-space:nowrap;cursor:pointer}
th a.sortable:hover{color:#1769e0}
a{color:#1769e0}small{color:#666}
form.inline{display:flex;gap:5px;flex-wrap:wrap}
input,select,button{font:inherit;padding:5px 7px;border:1px solid #ccd;border-radius:5px;background:#fff}
button{cursor:pointer;background:#1769e0;color:#fff;border-color:#1769e0}
.reasons{display:flex;gap:9px;flex-wrap:wrap;margin:12px 0;align-items:flex-start}
.reason{background:#fff;border:1px solid #e6e8ee;border-radius:8px;padding:9px 13px;text-decoration:none;color:#1a1d24}
.reason b{display:block;font-size:20px}
details.reason{padding:0;overflow:hidden}
details.reason summary{padding:9px 13px;cursor:pointer;list-style:none;user-select:none}
details.reason summary::-webkit-details-marker{display:none}
details.reason summary::after{content:" ▾";color:#888}
details[open].reason summary::after{content:" ▴"}
.hint{color:#888;font-size:12px}
.sub{display:flex;flex-direction:column;border-top:1px solid #e6e8ee;max-height:280px;overflow-y:auto}
.sub a{padding:6px 13px;text-decoration:none;color:#1a1d24;display:flex;justify-content:space-between;gap:16px;font-size:13px}
.sub a:hover{background:#eef0f6}
.sub a:first-child{color:#1769e0;font-weight:600}
.ok{color:#0a7d33;font-weight:600}
tr.gone{opacity:.5}tr.gone a{text-decoration:line-through}
@media(prefers-color-scheme:dark){
 body{background:#14161c;color:#e6e8ee}table,.reason,input,select{background:#1d2029;color:#e6e8ee}
 th{background:#242833}td,th,.reason{border-color:#2c3140}nav a.on{color:#e6e8ee}small{color:#98a}
 .sub{border-color:#2c3140}.sub a{color:#e6e8ee}.sub a:hover{background:#242833}
 .sub a:first-child{color:#6ea8fe}
}
</style>"""


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB, timeout=10)
    # Бот пишет в базу почти непрерывно. Без WAL читатель и писатель встают
    # в очередь друг к другу и ловят "database is locked".
    con.execute("PRAGMA journal_mode=WAL")
    con.row_factory = sqlite3.Row
    # Колонку заводит бот при старте, но панель может подняться раньше него —
    # тогда без этой проверки все страницы падали бы на отсутствующем столбце.
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


def page(title: str, active: str, body: str) -> str:
    tabs = {"/": "Отсеянные", "/passed": "Прошли фильтр", "/today": "Новые за сегодня",
            "/mining": "⛏ Майнинговые", "/market": "📊 Рынок", "/all": "Все объявления"}
    nav = "".join(f"<a href='{href}' class='{'on' if href == active else ''}'>{escape(name)}</a>"
                  for href, name in tabs.items())
    return f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>" \
           f"<title>{escape(title)}</title>{STYLE}<h1>{escape(title)}</h1><nav>{nav}</nav>{body}"


def rows_table(rows: list[sqlite3.Row], notes: dict[str, str], show_reason: bool,
               sort: str = "price", direction: str = "asc") -> str:
    order = ["profile", "title", "model", "price", "reason" if show_reason else "status",
             "seller", "risk"]
    head = "<tr>" + "".join(
        sort_link(key, COLUMNS[key][1], sort, direction) for key in order) + "</tr>"
    cells = []
    for r in rows:
        note = notes.get(r["ad_id"], "")
        price = f"{r['price_kzt']:,} ₸" if r["price_kzt"] is not None else "<small>не распознана</small>"
        if show_reason:
            status = escape(r["reject_reason"] or "") or "<span class=ok>прошло</span>"
        else:
            status = (f"<form class=inline method=post action=/update>"
                      f"<input type=hidden name=id value='{escape(r['ad_id'], quote=True)}'>"
                      f"<select name=status>{''.join(f'<option{" selected" if r["status"] == s else ""}>{s}</option>' for s in ('new', 'interested', 'ignored'))}</select>"
                      f"<select name=deal>{''.join(f'<option{" selected" if r["deal_status"] == d else ""}>{d}</option>' for d in ('new', 'позвонить', 'написал', 'договорился', 'купил', 'архив'))}</select>"
                      f"<input name=note placeholder='заметка' value=''><button>Сохранить</button></form>")
        gone = " class=gone" if r["is_gone"] else ""
        cells.append(
            f"<tr{gone}><td>{escape(r['profile_id'])}</td>"
            f"<td><a href='{escape(r['url'], quote=True)}' target=_blank rel=noopener>{escape(r['title'] or '—')}</a>"
            f"{f'<br><small>{escape(note)}</small>' if note else ''}</td>"
            f"<td>{escape(r['model'] or '—')}"
            # Майнинговые видны и в общих списках, а не только на своей вкладке:
            # они проходят фильтр, и скрывать их молча было бы неверно.
            f"{'<br><small>⛏ майнинг</small>' if r['is_mining'] else ''}"
            f"{'<br><small>🚫 снято с публикации</small>' if r['is_gone'] else ''}"
            f"{'' if r['telegram_message_id'] else '<br><small>не отправлено</small>'}</td>"
            f"<td>{price}<br><a href='/price/{escape(r['ad_id'], quote=True)}' target=_blank><small>история</small></a></td>"
            f"<td>{status}</td><td>{escape(r['seller_name'] or '—')}</td><td>{escape(r['risk_flags'] or '—')}</td></tr>")
    return f"<table>{head}{''.join(cells)}</table>"


# Колонка -> (выражение для ORDER BY, заголовок). Пустые значения всегда внизу
# при любом направлении: объявление без цены не должно возглавлять список.
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
    return (f"<th><a class=sortable href='{request.path}?{urlencode(params)}'>"
            f"{escape(label)}{arrow}</a></th>")


def listing_page(title: str, active: str, where: str, params: list, show_reason: bool,
                 default_sort: str = "price") -> str:
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
            values.append(int(raw.replace(" ", "").replace("\u00a0", "")))
        except ValueError:
            continue
        clauses.append(sql)
    clause = " WHERE " + " AND ".join(clauses) if clauses else ""
    con = connect()
    rows = con.execute(f"SELECT * FROM ads{clause} ORDER BY {order_by(sort, direction)} LIMIT 500",
                       values).fetchall()
    notes = {r["ad_id"]: r["body"] for r in
             con.execute("SELECT ad_id,body FROM notes WHERE id IN (SELECT MAX(id) FROM notes GROUP BY ad_id)")}
    # Список моделей для выпадающего фильтра
    all_models = con.execute("SELECT DISTINCT model FROM ads WHERE model IS NOT NULL ORDER BY model").fetchall()
    con.close()
    model_options = "".join(
        f"<option value='{escape(r['model'], quote=True)}'{' selected' if r['model'] == model_filter else ''}>"
        f"{escape(r['model'])}</option>" for r in all_models)
    search = (f"<form class=inline>"
              f"<input name=q value='{escape(query, quote=True)}' placeholder='поиск…'>"
              f"<select name=model><option value=''>Все модели</option>{model_options}</select>"
              f"<input name=price_min value='{escape(price_min, quote=True)}' placeholder='от ₸' style='width:80px'>"
              f"<input name=price_max value='{escape(price_max, quote=True)}' placeholder='до ₸' style='width:80px'>"
              f"<input type=hidden name=sort value='{escape(sort, quote=True)}'>"
              f"<input type=hidden name=dir value='{escape(direction, quote=True)}'>"
              f"<button>Фильтр</button></form>")
    return page(title, active, f"{search}<p>Показано: {len(rows)} · сортировка по столбцу, "
                               f"повторный клик разворачивает</p>"
                               f"{rows_table(rows, notes, show_reason, sort, direction)}")


def reason_chips(groups: list[sqlite3.Row]) -> str:
    """Причины отсева, свёрнутые по категориям.

    Одних «устаревшая серия: …» набирается больше двадцати штук, и плоский
    список чипов невозможно читать. Категория — часть причины до двоеточия;
    внутри раскрывашка со списком конкретных серий и количеством.
    """
    categories: dict[str, list[tuple[str, int]]] = {}
    for row in groups:
        category = row["r"].split(":", 1)[0] if ":" in row["r"] else row["r"]
        categories.setdefault(category, []).append((row["r"], row["c"]))

    chips = [f"<a class=reason href='/'><b>{sum(g['c'] for g in groups)}</b>все причины</a>"]
    for category, items in sorted(categories.items(), key=lambda kv: -sum(c for _, c in kv[1])):
        total = sum(count for _, count in items)
        if len(items) == 1 and items[0][0] == category:
            chips.append(f"<a class=reason href='/?reason={quote(category)}'>"
                         f"<b>{total}</b>{escape(category)}</a>")
            continue
        inner = "".join(
            f"<a href='/?reason={quote(reason)}'>"
            f"{escape(reason.split(': ', 1)[-1])} <b>{count}</b></a>"
            for reason, count in items)
        chips.append(
            f"<details class=reason><summary><b>{total}</b>{escape(category)}"
            f"<span class=hint> · {len(items)}</span></summary>"
            f"<div class=sub><a href='/?group={quote(category)}'>показать все {total}</a>{inner}</div>"
            f"</details>")
    return f"<div class=reasons>{''.join(chips)}</div>"


@app.get("/")
def rejected():
    """Что фильтры съели и почему — сгруппировано по причине."""
    reason, group = request.args.get("reason", ""), request.args.get("group", "")
    con = connect()
    groups = con.execute("SELECT reject_reason r, COUNT(*) c FROM ads WHERE reject_reason != '' "
                         "GROUP BY r ORDER BY c DESC").fetchall()
    con.close()
    if reason:
        where, params = "reject_reason = ?", [reason]
    elif group:
        where, params = "reject_reason LIKE ?", [f"{group}:%"]
    else:
        where, params = "reject_reason != ''", []
    body = listing_page("Отсеянные объявления", "/", where, params, show_reason=True)
    return body.replace("<p>Показано:", reason_chips(groups) + "<p>Показано:", 1)


@app.get("/passed")
def passed():
    """Всё, что проходит фильтр сейчас — включая не отправленное.

    Отправку не требуем: после смены правил часть объявлений проходит, но
    просмотрена была при старых фильтрах и в Telegram не попала. Такие видны
    здесь с пометкой «не отправлено» — иначе они пропали бы из виду совсем.
    """
    return listing_page("Прошли фильтр", "/passed", "reject_reason = ''", [], show_reason=False)


@app.get("/today")
def today():
    """Появившиеся за сегодня — и прошедшие фильтр, и отсеянные.

    Отсеянные тоже показываем: по ним видно, что фильтры съели сегодня, а не
    вообще за всё время.
    """
    midnight = int(datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    return listing_page("Новые за сегодня", "/today", "first_seen >= ?", [midnight],
                        show_reason=True, default_sort="new")


@app.get("/mining")
def mining():
    """Карты без видеовыходов: P106/P104, CMP 30HX-90HX.

    Из выборки они не исключены — цена бывает заманчивой, а формально это
    NVIDIA. Но смотреть их стоит отдельно от игровых.
    """
    return listing_page("Майнинговые карты", "/mining", "is_mining = 1", [], show_reason=True)


@app.get("/all")
def every():
    return listing_page("Все объявления", "/all", "", [], show_reason=True)


@app.post("/update")
def update():
    ad_id = request.form["id"]
    con = connect()
    con.execute("UPDATE ads SET status=?, deal_status=? WHERE ad_id=?",
                (request.form["status"], request.form["deal"], ad_id))
    note = request.form.get("note", "").strip()
    if note:
        con.execute("INSERT INTO notes(ad_id,created_at,body) VALUES(?,?,?)", (ad_id, int(time.time()), note))
    con.commit(); con.close()
    return redirect(request.referrer or "/passed")


@app.get("/price/<ad_id>")
def price(ad_id: str):
    con = connect()
    rows = con.execute("SELECT recorded_at,price_kzt FROM price_history WHERE ad_id=? AND price_kzt IS NOT NULL "
                       "ORDER BY recorded_at", (ad_id,)).fetchall()
    ad = con.execute("SELECT title, model FROM ads WHERE ad_id=?", (ad_id,)).fetchone()
    con.close()
    if not rows:
        return page("История цены", "", "<p>История цены пока отсутствует.</p>"), 404
    title = (ad["title"] or ad_id) if ad else ad_id
    labels = json.dumps([datetime.fromtimestamp(r["recorded_at"]).strftime("%d.%m %H:%M") for r in rows])
    values = json.dumps([r["price_kzt"] for r in rows])
    chart = f"""
    <h2>{escape(title[:80])}</h2>
    <canvas id="priceChart" width="700" height="300"></canvas>
    <script src="https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"></script>
    <script>
    new Chart(document.getElementById('priceChart'), {{
      type: 'line',
      data: {{
        labels: {labels},
        datasets: [{{
          label: 'Цена (₸)',
          data: {values},
          borderColor: '#1769e0',
          backgroundColor: 'rgba(23,105,224,0.1)',
          fill: true,
          tension: 0.3,
          pointRadius: 4,
        }}]
      }},
      options: {{
        responsive: true,
        plugins: {{ legend: {{ display: false }} }},
        scales: {{
          y: {{ ticks: {{ callback: v => v.toLocaleString() + ' ₸' }} }},
          x: {{ ticks: {{ maxTicksLimit: 10 }} }}
        }}
      }}
    }});
    </script>
    <p>Минимум: {min(r['price_kzt'] for r in rows):,} ₸ · максимум: {max(r['price_kzt'] for r in rows):,} ₸ · замеров: {len(rows)}</p>
    """
    return page("История цены", "", chart)


@app.get("/market")
def market():
    """Обзор рынка: средние/медианные цены по моделям и количество объявлений."""
    con = connect()
    models = con.execute(
        "SELECT model, COUNT(*) cnt, "
        "CAST(AVG(price_kzt) AS INTEGER) avg_price, "
        "MIN(price_kzt) min_price, MAX(price_kzt) max_price "
        "FROM ads WHERE model IS NOT NULL AND price_kzt IS NOT NULL AND reject_reason = '' "
        "GROUP BY model ORDER BY avg_price").fetchall()
    con.close()
    if not models:
        return page("Обзор рынка", "/market", "<p>Пока нет данных.</p>")
    labels = json.dumps([r["model"] for r in models])
    avg_data = json.dumps([r["avg_price"] for r in models])
    min_data = json.dumps([r["min_price"] for r in models])
    max_data = json.dumps([r["max_price"] for r in models])
    counts = json.dumps([r["cnt"] for r in models])
    chart = f"""
    <canvas id="marketChart" height="400"></canvas>
    <script src="https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"></script>
    <script>
    new Chart(document.getElementById('marketChart'), {{
      type: 'bar',
      data: {{
        labels: {labels},
        datasets: [
          {{ label: 'Мин', data: {min_data}, backgroundColor: 'rgba(10,125,51,0.6)' }},
          {{ label: 'Средняя', data: {avg_data}, backgroundColor: 'rgba(23,105,224,0.7)' }},
          {{ label: 'Макс', data: {max_data}, backgroundColor: 'rgba(220,53,69,0.5)' }}
        ]
      }},
      options: {{
        responsive: true,
        plugins: {{ legend: {{ position: 'top' }} }},
        scales: {{
          y: {{ ticks: {{ callback: v => v.toLocaleString() + ' ₸' }} }}
        }}
      }}
    }});
    </script>
    <table><tr><th>Модель</th><th>Кол-во</th><th>Мин</th><th>Средняя</th><th>Макс</th></tr>
    {''.join(f"<tr><td>{escape(r['model'])}</td><td>{r['cnt']}</td><td>{r['min_price']:,} ₸</td><td>{r['avg_price']:,} ₸</td><td>{r['max_price']:,} ₸</td></tr>" for r in models)}
    </table>
    """
    return page("Обзор рынка", "/market", chart)


if __name__ == "__main__":
    # Список адресов через пробел. По умолчанию только localhost; на сервере
    # systemd добавляет адрес Tailscale — панель открывается с Mac и телефона,
    # но не выставлена ни в интернет, ни в домашнюю сеть. Localhost остаётся
    # в списке, чтобы healthcheck в CI мог достучаться без Tailscale.
    listen = os.getenv("DASHBOARD_LISTEN", "127.0.0.1:5050")
    try:
        from waitress import serve
        print(f"Панель слушает: {listen}")
        serve(app, listen=listen, threads=4)
    except ImportError:
        host, _, port = listen.split()[0].rpartition(":")
        app.run(host=host, port=int(port), debug=False)
