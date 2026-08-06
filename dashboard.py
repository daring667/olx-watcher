"""Локальная панель OLX-помощника.

Главный экран — не избранное (оно удобнее в Telegram), а отсеянные объявления
с причиной отсева. Из 546 просмотренных до Telegram доходит около 56, и без
этого экрана невозможно понять, что именно съедают фильтры.
"""
from __future__ import annotations

import os
import sqlite3
import time
from html import escape
from pathlib import Path

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
a{color:#1769e0}small{color:#666}
form.inline{display:flex;gap:5px;flex-wrap:wrap}
input,select,button{font:inherit;padding:5px 7px;border:1px solid #ccd;border-radius:5px;background:#fff}
button{cursor:pointer;background:#1769e0;color:#fff;border-color:#1769e0}
.reasons{display:flex;gap:9px;flex-wrap:wrap;margin:12px 0}
.reason{background:#fff;border:1px solid #e6e8ee;border-radius:8px;padding:9px 13px;text-decoration:none;color:#1a1d24}
.reason b{display:block;font-size:20px}
.ok{color:#0a7d33;font-weight:600}
@media(prefers-color-scheme:dark){
 body{background:#14161c;color:#e6e8ee}table,.reason,input,select{background:#1d2029;color:#e6e8ee}
 th{background:#242833}td,th,.reason{border-color:#2c3140}nav a.on{color:#e6e8ee}small{color:#98a}
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
                             ("is_mining", "INTEGER NOT NULL DEFAULT 0")):
        if name not in existing:
            con.execute(f"ALTER TABLE ads ADD COLUMN {name} {definition}")
            con.commit()
    return con


def page(title: str, active: str, body: str) -> str:
    tabs = {"/": "Отсеянные", "/passed": "Прошли фильтр", "/mining": "⛏ Майнинговые",
            "/all": "Все объявления"}
    nav = "".join(f"<a href='{href}' class='{'on' if href == active else ''}'>{escape(name)}</a>"
                  for href, name in tabs.items())
    return f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>" \
           f"<title>{escape(title)}</title>{STYLE}<h1>{escape(title)}</h1><nav>{nav}</nav>{body}"


def rows_table(rows: list[sqlite3.Row], notes: dict[str, str], show_reason: bool) -> str:
    head = "<tr><th>Раздел</th><th>Объявление</th><th>Модель</th><th>Цена</th>" \
           + ("<th>Причина отсева</th>" if show_reason else "<th>Статус</th>") \
           + "<th>Продавец</th><th>Риски</th></tr>"
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
        cells.append(
            f"<tr><td>{escape(r['profile_id'])}</td>"
            f"<td><a href='{escape(r['url'], quote=True)}' target=_blank rel=noopener>{escape(r['title'] or '—')}</a>"
            f"{f'<br><small>{escape(note)}</small>' if note else ''}</td>"
            f"<td>{escape(r['model'] or '—')}"
            # Майнинговые видны и в общих списках, а не только на своей вкладке:
            # они проходят фильтр, и скрывать их молча было бы неверно.
            f"{'<br><small>⛏ майнинг</small>' if r['is_mining'] else ''}"
            f"{'' if r['telegram_message_id'] else '<br><small>не отправлено</small>'}</td>"
            f"<td>{price}<br><a href='/price/{escape(r['ad_id'], quote=True)}' target=_blank><small>история</small></a></td>"
            f"<td>{status}</td><td>{escape(r['seller_name'] or '—')}</td><td>{escape(r['risk_flags'] or '—')}</td></tr>")
    return f"<table>{head}{''.join(cells)}</table>"


def listing_page(title: str, active: str, where: str, params: list, show_reason: bool) -> str:
    query = request.args.get("q", "").strip()
    clauses, values = [where] if where else [], list(params)
    if query:
        clauses.append("(title LIKE ? OR model LIKE ? OR seller_name LIKE ?)")
        values += [f"%{query}%"] * 3
    clause = " WHERE " + " AND ".join(clauses) if clauses else ""
    con = connect()
    rows = con.execute("SELECT * FROM ads" + clause +
                       " ORDER BY price_kzt IS NULL, price_kzt, first_seen DESC LIMIT 500", values).fetchall()
    notes = {r["ad_id"]: r["body"] for r in
             con.execute("SELECT ad_id,body FROM notes WHERE id IN (SELECT MAX(id) FROM notes GROUP BY ad_id)")}
    con.close()
    # value экранируется: раньше кавычка в поиске ломала форму и пускала разметку.
    search = (f"<form class=inline><input name=q value='{escape(query, quote=True)}' "
              f"placeholder='модель, продавец, заголовок…'><button>Искать</button></form>")
    return page(title, active, f"{search}<p>Показано: {len(rows)}</p>{rows_table(rows, notes, show_reason)}")


@app.get("/")
def rejected():
    """Что фильтры съели и почему — сгруппировано по причине."""
    reason = request.args.get("reason", "")
    con = connect()
    groups = con.execute(
        "SELECT reject_reason r, COUNT(*) c FROM ads WHERE reject_reason != '' GROUP BY r ORDER BY c DESC").fetchall()
    con.close()
    chips = "".join(
        f"<a class=reason href='/?reason={escape(g['r'], quote=True)}'><b>{g['c']}</b>{escape(g['r'])}</a>"
        for g in groups)
    total = sum(g["c"] for g in groups)
    header = f"<div class=reasons><a class=reason href='/'><b>{total}</b>все причины</a>{chips}</div>"
    where, params = ("reject_reason = ?", [reason]) if reason else ("reject_reason != ''", [])
    body = listing_page("Отсеянные объявления", "/", where, params, show_reason=True)
    return body.replace("<p>Показано:", header + "<p>Показано:", 1)


@app.get("/passed")
def passed():
    """Всё, что проходит фильтр сейчас — включая не отправленное.

    Отправку не требуем: после смены правил часть объявлений проходит, но
    просмотрена была при старых фильтрах и в Telegram не попала. Такие видны
    здесь с пометкой «не отправлено» — иначе они пропали бы из виду совсем.
    """
    return listing_page("Прошли фильтр", "/passed", "reject_reason = ''", [], show_reason=False)


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
    con.close()
    if not rows:
        return page("История цены", "", "<p>История цены пока отсутствует.</p>"), 404
    values = [r["price_kzt"] for r in rows]
    lo, hi = min(values), max(values)
    spread = max(hi - lo, 1)
    points = " ".join(f"{20 + i * 560 / max(len(rows) - 1, 1):.0f},{180 - (v - lo) * 140 / spread:.0f}"
                      for i, v in enumerate(values))
    return page("История цены", "", f"<svg width=600 height=220><polyline points='{points}' fill=none "
                                    f"stroke='#1769e0' stroke-width=3/></svg>"
                                    f"<p>Минимум: {lo:,} ₸ · максимум: {hi:,} ₸ · замеров: {len(values)}</p>")


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
