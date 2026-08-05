"""Локальная панель OLX-помощника: http://127.0.0.1:5050"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from html import escape

from flask import Flask, redirect, request

DB = Path(__file__).parent / "data" / "watcher.sqlite3"
app = Flask(__name__)


@app.get("/")
def index():
    status = request.args.get("status", "all")
    query = request.args.get("q", "").strip()
    where, params = [], []
    if status != "all":
        where.append("status=?"); params.append(status)
    if query:
        where.append("(title LIKE ? OR model LIKE ? OR seller_name LIKE ?)"); params += [f"%{query}%"] * 3
    clause = " WHERE " + " AND ".join(where) if where else ""
    con = sqlite3.connect(DB); con.row_factory = sqlite3.Row
    rows = con.execute("SELECT * FROM ads" + clause + " ORDER BY price_kzt IS NULL, price_kzt, first_seen DESC LIMIT 500", params).fetchall()
    notes = {r["ad_id"]: r["body"] for r in con.execute("SELECT ad_id,body FROM notes WHERE id IN (SELECT MAX(id) FROM notes GROUP BY ad_id)")}
    con.close()
    table = "".join(
        f"<tr><td>{escape(r['profile_id'])}</td><td><a href='{escape(r['url'])}' target='_blank'>{escape(r['title'] or '—')}</a><br><small>{escape(notes.get(r['ad_id'], ''))}</small></td>"
        f"<td>{escape(r['model'] or '—')}</td><td>{r['price_kzt'] or '—'} ₸<br><a href='/price/{r['ad_id']}' target='_blank'>история</a></td>"
        f"<td><form method=post action=/update><input type=hidden name=id value='{r['ad_id']}'><select name=status><option>{r['status']}</option><option>interested</option><option>ignored</option><option>new</option></select><select name=deal><option>{r['deal_status']}</option><option>позвонить</option><option>написал</option><option>договорился</option><option>купил</option><option>архив</option></select><input name=note placeholder='заметка'><button>Сохранить</button></form></td><td>{escape(r['seller_name'] or '—')}</td><td>{escape(r['risk_flags'] or '—')}</td></tr>"
        for r in rows)
    return f"""<!doctype html><meta charset=utf-8><title>OLX помощник</title>
    <style>body{{font:14px system-ui;margin:24px;background:#f6f7fb}}table{{width:100%;border-collapse:collapse;background:white}}td,th{{padding:9px;border-bottom:1px solid #ddd;text-align:left}}a{{color:#1769e0}}small{{color:#666}}</style>
    <h1>OLX-помощник</h1><form><input name=q value='{query}' placeholder='модель, продавец…'><select name=status><option value=all>Все</option><option value=interested>Избранное</option><option value=new>Новые</option><option value=ignored>Архив</option></select><button>Фильтровать</button></form>
    <p>Показано: {len(rows)}</p><table><tr><th>Раздел</th><th>Объявление / заметка</th><th>Модель</th><th>Цена</th><th>Статус</th><th>Продавец</th><th>Риски</th></tr>{table}</table>"""


@app.post("/update")
def update():
    ad_id, status, deal, note = request.form["id"], request.form["status"], request.form["deal"], request.form.get("note", "").strip()
    con = sqlite3.connect(DB)
    con.execute("UPDATE ads SET status=?, deal_status=? WHERE ad_id=?", (status, deal, ad_id))
    if note:
        import time
        con.execute("INSERT INTO notes(ad_id,created_at,body) VALUES(?,?,?)", (ad_id, int(time.time()), note))
    con.commit(); con.close()
    return redirect(request.referrer or "/")


@app.get("/price/<ad_id>")
def price(ad_id: str):
    con = sqlite3.connect(DB)
    rows = con.execute("SELECT recorded_at,price_kzt FROM price_history WHERE ad_id=? AND price_kzt IS NOT NULL ORDER BY recorded_at", (ad_id,)).fetchall()
    con.close()
    if not rows:
        return "История цены пока отсутствует.", 404
    values = [r[1] for r in rows]; lo, hi = min(values), max(values); spread = max(hi - lo, 1)
    points = " ".join(f"{20 + i * 560 / max(len(rows)-1,1):.0f},{180 - (v-lo) * 140/spread:.0f}" for i, (_, v) in enumerate(rows))
    return f"<meta charset=utf-8><h2>История цены</h2><svg width=600 height=220 style='background:#f6f7fb'><polyline points='{points}' fill='none' stroke='#1769e0' stroke-width=3/></svg><p>Минимум: {lo:,} ₸ · максимум: {hi:,} ₸</p>"


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5050, debug=False)
