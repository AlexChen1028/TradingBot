"""修復 ETH 逐倉錢包卡住的負餘額（-2019 Margin is insufficient）。
做法：cross 切換讓負餘額結算進主錢包，再切回 isolated（bot 預期模式）。
僅在 ETH 無持倉時安全執行。
用法：docker compose cp scripts/fix_eth_margin.py coin-monitor:/app/fix_eth_margin.py
      docker compose exec coin-monitor python /app/fix_eth_margin.py
"""
import os
import ccxt

SYMBOL = 'ETH/USDT:USDT'

ex = ccxt.binance({
    'apiKey':          os.getenv('BINANCE_API_KEY', ''),
    'secret':          os.getenv('BINANCE_SECRET_KEY', ''),
    'enableRateLimit': True,
    'options':         {'defaultType': 'future'},
})
ex.enable_demo_trading(True)


def eth_wallet():
    for r in ex.fapiPrivateV2GetPositionRisk():
        if r['symbol'] == 'ETHUSDT':
            return r
    return {}


r0 = eth_wallet()
print(f"[前] ETH  isolatedWallet={r0.get('isolatedWallet')}  "
      f"marginType={r0.get('marginType')}  positionAmt={r0.get('positionAmt')}")

if abs(float(r0.get('positionAmt', 0) or 0)) > 0:
    print("⛔ ETH 目前有持倉，為安全起見不執行。請先平倉再跑此腳本。")
    raise SystemExit(1)

# 先撤掉 ETH 殘留掛單（避免擋住切換）
try:
    for o in ex.fetch_open_orders(SYMBOL):
        try:
            ex.cancel_order(o['id'], SYMBOL)
            print(f"  撤單 {o['id']}")
        except Exception as e:
            print(f"  撤單失敗 {o['id']}: {e}")
except Exception as e:
    print(f"  查掛單失敗（忽略）: {e}")

# 切 cross → 讓負逐倉餘額結算進主錢包
try:
    ex.set_margin_mode('cross', SYMBOL)
    print("  ✅ 已切換 ETH → cross")
except Exception as e:
    print(f"  set_margin_mode cross: {e}")

r1 = eth_wallet()
print(f"[中] ETH  isolatedWallet={r1.get('isolatedWallet')}  marginType={r1.get('marginType')}")

# 切回 isolated（bot 每次開倉都會設 isolated，這裡先還原成乾淨的 isolated/0）
try:
    ex.set_margin_mode('isolated', SYMBOL)
    print("  ✅ 已切回 ETH → isolated")
except Exception as e:
    print(f"  set_margin_mode isolated: {e}")

r2 = eth_wallet()
print(f"[後] ETH  isolatedWallet={r2.get('isolatedWallet')}  "
      f"marginType={r2.get('marginType')}  positionAmt={r2.get('positionAmt')}")

w = float(r2.get('isolatedWallet', 0) or 0)
if w >= 0:
    print("🎯 ETH 逐倉錢包已回正常（≥0），下次開倉應不再 -2019。")
else:
    print(f"⚠️ ETH 逐倉錢包仍為負（{w}）。可能需在交易所網頁端手動補入逐倉保證金或重置 demo 錢包。")
