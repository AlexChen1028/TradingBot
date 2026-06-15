"""掃描所有持倉幣 + 常見幣的殘留條件單，找出會引爆 -4130 的孤兒掛單。
孤兒單 = 交易所有 closePosition STOP/TP，但本地 positions 無對應持倉。
用法：docker compose cp scripts/diag_stale_orders.py coin-monitor:/app/diag_stale_orders.py
      docker compose exec coin-monitor python /app/diag_stale_orders.py
"""
import os
import json
import ccxt

ex = ccxt.binance({
    'apiKey':          os.getenv('BINANCE_API_KEY', ''),
    'secret':          os.getenv('BINANCE_SECRET_KEY', ''),
    'enableRateLimit': True,
    'options':         {'defaultType': 'future'},
})
ex.enable_demo_trading(True)

try:
    pos = json.load(open('positions_altcoin.json'))
except Exception:
    pos = {}
local_syms = {s.split('/')[0] for s in pos}
print(f"[本地持倉] {local_syms or '（無）'}")
print("-" * 70)

# 全帳戶未結訂單（raw，最可靠）
try:
    raw = ex.fapiPrivateGetOpenOrders()
except Exception as e:
    print(f"全帳戶 openOrders 查詢失敗: {e}")
    raise SystemExit(1)

by_sym = {}
for o in raw:
    by_sym.setdefault(o['symbol'], []).append(o)

if not by_sym:
    print("交易所端無任何未結訂單 ✅")
else:
    for sym, orders in by_sym.items():
        coin = sym.replace('USDT', '')
        orphan = coin not in local_syms
        tag = '🚨 孤兒(無對應持倉→會引爆 -4130)' if orphan else '✅ 有對應持倉'
        print(f"{sym}  {tag}")
        for o in orders:
            print(f"    orderId={o.get('orderId')} type={o.get('type')} side={o.get('side')} "
                  f"stopPrice={o.get('stopPrice')} closePosition={o.get('closePosition')} "
                  f"reduceOnly={o.get('reduceOnly')}")
