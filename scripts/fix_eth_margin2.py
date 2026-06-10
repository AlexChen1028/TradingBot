"""修復 ETH 逐倉負餘額 第二招：直接補保證金（positionMargin type=1）。
cross 切換被 -4051 擋住時改用此法 —— 從主錢包撥一筆進 ETH 逐倉錢包，
蓋過 -157 並留緩衝，使 isolatedWallet 轉正、解除 -2019 封鎖。
僅在 ETH 無持倉時執行。
用法：docker compose cp scripts/fix_eth_margin2.py coin-monitor:/app/fix_eth_margin2.py
      docker compose exec coin-monitor python /app/fix_eth_margin2.py
"""
import os
import ccxt

ADD_AMOUNT = 300  # 需 > 157 並留足夠開倉緩衝（主錢包有 ~4300，安全）

ex = ccxt.binance({
    'apiKey':          os.getenv('BINANCE_API_KEY', ''),
    'secret':          os.getenv('BINANCE_SECRET_KEY', ''),
    'enableRateLimit': True,
    'options':         {'defaultType': 'future'},
})
ex.enable_demo_trading(True)


def eth_risk():
    for r in ex.fapiPrivateV2GetPositionRisk():
        if r['symbol'] == 'ETHUSDT':
            return r
    return {}


r0 = eth_risk()
print(f"[前] ETH  isolatedWallet={r0.get('isolatedWallet')}  "
      f"marginType={r0.get('marginType')}  positionAmt={r0.get('positionAmt')}")

if abs(float(r0.get('positionAmt', 0) or 0)) > 0:
    print("⛔ ETH 目前有持倉，為安全起見不執行。請先平倉再跑。")
    raise SystemExit(1)

# 補保證金：type=1 加倉保證金，positionSide=BOTH（單向持倉）
attempts = [
    {'symbol': 'ETHUSDT', 'amount': ADD_AMOUNT, 'type': 1, 'positionSide': 'BOTH'},
    {'symbol': 'ETHUSDT', 'amount': ADD_AMOUNT, 'type': 1},  # 不帶 positionSide 的後備
]
ok = False
for params in attempts:
    try:
        resp = ex.fapiPrivatePostPositionMargin(params)
        print(f"  ✅ 補保證金成功（{params}）→ {resp}")
        ok = True
        break
    except Exception as e:
        print(f"  ✗ 補保證金失敗（{params}）: {e}")

r1 = eth_risk()
print(f"[後] ETH  isolatedWallet={r1.get('isolatedWallet')}  "
      f"marginType={r1.get('marginType')}  positionAmt={r1.get('positionAmt')}")

w = float(r1.get('isolatedWallet', 0) or 0)
if w >= 0:
    print(f"🎯 ETH 逐倉錢包已轉正（{w}），下次開倉應不再 -2019。")
elif ok:
    print(f"⚠️ API 回報成功但錢包仍為 {w}，可能需再跑一次或加大金額。")
else:
    print("⚠️ 補保證金未成功。最後手段：Binance testnet 網頁端手動補入 ETH 逐倉保證金，"
          "或重置 demo 錢包（會清空所有持倉/餘額）。")
