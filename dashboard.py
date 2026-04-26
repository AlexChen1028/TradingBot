"""
績效儀表板生成器
讀取 btc_trades.jsonl 和 eth_trades.jsonl，生成 dashboard.html

執行：python dashboard.py
然後在瀏覽器開啟 dashboard.html
"""

import json
import os
from pathlib import Path
from datetime import datetime, timedelta

COINS = ['btc', 'eth']


def load_trades(coin: str) -> list:
    p = Path(f'{coin}_trades.jsonl')
    if not p.exists():
        return []
    trades = []
    for line in p.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if line:
            try:
                trades.append(json.loads(line))
            except Exception:
                pass
    return trades


def load_state(coin: str) -> dict:
    p = Path(f'{coin}_state.json')
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except Exception:
        return {}


def calc_stats(trades: list) -> dict:
    if not trades:
        return {'total_pnl': 0, 'win_rate': 0, 'n_trades': 0, 'avg_pnl': 0, 'best': 0, 'worst': 0}
    pnls = [t.get('pnl_usdt', 0) for t in trades]
    wins = sum(1 for p in pnls if p > 0)
    return {
        'total_pnl': sum(pnls),
        'win_rate':  wins / len(pnls) * 100 if pnls else 0,
        'n_trades':  len(pnls),
        'avg_pnl':   sum(pnls) / len(pnls) if pnls else 0,
        'best':      max(pnls) if pnls else 0,
        'worst':     min(pnls) if pnls else 0,
    }


def trade_rows(trades: list, coin: str) -> str:
    rows = ''
    for t in reversed(trades[-50:]):
        pnl   = t.get('pnl_usdt', 0)
        color = '#3fb950' if pnl >= 0 else '#f85149'
        rows += f"""
        <tr>
            <td>{t.get('close_time', '')[:16]}</td>
            <td>{coin.upper()}</td>
            <td>{'LONG' if t.get('direction') == 1 else 'SHORT'}</td>
            <td>{t.get('entry_price', 0):,.2f}</td>
            <td>{t.get('close_price', 0):,.2f}</td>
            <td style="color:{color};font-weight:bold">{pnl:+.2f} U</td>
            <td>{t.get('reason', '-')}</td>
        </tr>"""
    return rows


def generate():
    now = datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')
    cards_html = ''
    all_rows   = ''

    for coin in COINS:
        trades = load_trades(coin)
        state  = load_state(coin)
        stats  = calc_stats(trades)
        color  = '#3fb950' if stats['total_pnl'] >= 0 else '#f85149'
        dir_map = {1: '🟢 LONG', -1: '🔴 SHORT', 0: '⚪ FLAT'}
        cur_dir = dir_map.get(state.get('direction', 0), '⚪ FLAT')

        cards_html += f"""
        <div class="card">
            <h2>{coin.upper()} Bot</h2>
            <div class="stat">當前倉位：{cur_dir}</div>
            <div class="stat">總盈虧：<span style="color:{color};font-weight:bold">{stats['total_pnl']:+.2f} U</span></div>
            <div class="stat">交易次數：{stats['n_trades']}</div>
            <div class="stat">勝率：{stats['win_rate']:.1f}%</div>
            <div class="stat">均盈虧：{stats['avg_pnl']:+.2f} U</div>
            <div class="stat">最佳單：{stats['best']:+.2f} U</div>
            <div class="stat">最差單：{stats['worst']:+.2f} U</div>
        </div>"""

        all_rows += trade_rows(trades, coin)

    html = f"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="300">
<title>Trading Bot Dashboard</title>
<style>
  body {{ background:#0d1117; color:#c9d1d9; font-family:sans-serif; margin:0; padding:20px; }}
  h1   {{ color:#58a6ff; margin-bottom:4px; }}
  .sub {{ color:#8b949e; margin-bottom:20px; }}
  .cards {{ display:flex; gap:16px; flex-wrap:wrap; margin-bottom:30px; }}
  .card {{ background:#161b22; border:1px solid #30363d; border-radius:8px; padding:20px; min-width:220px; }}
  .card h2 {{ color:#58a6ff; margin:0 0 12px; }}
  .stat {{ margin:6px 0; font-size:14px; }}
  table {{ width:100%; border-collapse:collapse; background:#161b22; border-radius:8px; overflow:hidden; }}
  th    {{ background:#21262d; color:#8b949e; padding:10px; text-align:left; font-size:13px; }}
  td    {{ padding:9px 10px; font-size:13px; border-top:1px solid #21262d; }}
  tr:hover td {{ background:#21262d; }}
</style>
</head>
<body>
<h1>📊 Trading Bot Dashboard</h1>
<div class="sub">更新：{now}（每 5 分鐘自動刷新）</div>
<div class="cards">{cards_html}</div>
<h2 style="color:#8b949e;font-size:15px">最近 50 筆交易</h2>
<table>
  <tr><th>時間</th><th>幣種</th><th>方向</th><th>進場</th><th>出場</th><th>盈虧</th><th>原因</th></tr>
  {all_rows if all_rows else '<tr><td colspan="7" style="text-align:center;color:#8b949e">尚無交易記錄</td></tr>'}
</table>
</body>
</html>"""

    Path('dashboard.html').write_text(html, encoding='utf-8')
    print(f"Dashboard saved → dashboard.html  ({now})")


if __name__ == '__main__':
    generate()
