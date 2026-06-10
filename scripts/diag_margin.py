"""ETH/BTC/SOL 保證金 + 槓桿診斷（排查 -2019 Margin is insufficient）。
用法：docker compose exec coin-monitor python scripts/diag_margin.py
"""
import os
import ccxt

ex = ccxt.binance({
    'apiKey':          os.getenv('BINANCE_API_KEY', ''),
    'secret':          os.getenv('BINANCE_SECRET_KEY', ''),
    'enableRateLimit': True,
    'options':         {'defaultType': 'future'},
})
ex.enable_demo_trading(True)

b = ex.fetch_balance()
u = b['USDT']
print(f"USDT  free: {u['free']}  |  used: {u['used']}  |  total: {u['total']}")
print("-" * 60)

symbols = ['ETH/USDT:USDT', 'BTC/USDT:USDT', 'SOL/USDT:USDT']
for p in ex.fetch_positions(symbols):
    print(
        f"{p['symbol']:<16} lev: {p.get('leverage')}  "
        f"mode: {p.get('marginMode')}  contracts: {p.get('contracts')}  "
        f"notional: {p.get('notional')}"
    )

print("-" * 60)
# 用 raw API 直接看每個 symbol 的槓桿設定（不依賴是否有持倉）
try:
    risk = ex.fapiPrivateV2GetPositionRisk()
    for r in risk:
        if r['symbol'] in ('ETHUSDT', 'BTCUSDT', 'SOLUSDT'):
            print(
                f"{r['symbol']:<10} leverage: {r['leverage']}  "
                f"marginType: {r.get('marginType')}  "
                f"isolatedWallet: {r.get('isolatedWallet')}  "
                f"positionAmt: {r.get('positionAmt')}"
            )
except Exception as e:
    print(f"positionRisk 查詢失敗: {e}")
