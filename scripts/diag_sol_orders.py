"""診斷 SOL 殘留條件單（-4130 SL/TP 補掛每輪失敗）。
列出 SOL 交易所端所有未結訂單（含 closePosition STOP/TP），對照本地持倉應有的 SL/TP。
用法：docker compose cp scripts/diag_sol_orders.py coin-monitor:/app/diag_sol_orders.py
      docker compose exec coin-monitor python /app/diag_sol_orders.py
"""
import os
import json
import ccxt

SYMBOL = 'SOL/USDT:USDT'

ex = ccxt.binance({
    'apiKey':          os.getenv('BINANCE_API_KEY', ''),
    'secret':          os.getenv('BINANCE_SECRET_KEY', ''),
    'enableRateLimit': True,
    'options':         {'defaultType': 'future'},
})
ex.enable_demo_trading(True)

# 1) 本地持倉記錄
try:
    pos = json.load(open('positions_altcoin.json'))
    sp = pos.get(SYMBOL, {})
    print(f"[本地] SOL dir={sp.get('direction')} ep={sp.get('entry_price')} "
          f"amt={sp.get('amount')} sl_id={sp.get('sl_order_id')} tp_id={sp.get('tp_order_id')} "
          f"breakeven={sp.get('breakeven')}")
except Exception as e:
    print(f"[本地] 讀持倉失敗: {e}")

# 2) 交易所實際倉位
try:
    for p in ex.fetch_positions([SYMBOL]):
        if abs(p.get('contracts') or 0) > 0:
            print(f"[交易所倉位] {p['symbol']} contracts={p.get('contracts')} "
                  f"side={p.get('side')} entry={p.get('entryPrice')}")
except Exception as e:
    print(f"[交易所倉位] 查詢失敗: {e}")

print("-" * 70)

# 3) fetch_open_orders（demo 對條件單常漏）
print("[fetch_open_orders]")
try:
    oo = ex.fetch_open_orders(SYMBOL)
    if not oo:
        print("  （空）")
    for o in oo:
        print(f"  id={o['id']} type={o.get('type')} side={o.get('side')} "
              f"stop={o.get('stopPrice')} reduceOnly={o.get('reduceOnly')} "
              f"closePos={o.get('info',{}).get('closePosition')} status={o.get('status')}")
except Exception as e:
    print(f"  失敗: {e}")

# 4) raw fapi openOrders（最可靠，直接打 API）
print("[raw fapiPrivateGetOpenOrders symbol=SOLUSDT]")
try:
    raw = ex.fapiPrivateGetOpenOrders({'symbol': 'SOLUSDT'})
    if not raw:
        print("  （空）")
    for o in raw:
        print(f"  orderId={o.get('orderId')} type={o.get('type')} side={o.get('side')} "
              f"stopPrice={o.get('stopPrice')} closePosition={o.get('closePosition')} "
              f"reduceOnly={o.get('reduceOnly')} status={o.get('status')}")
except Exception as e:
    print(f"  失敗: {e}")
