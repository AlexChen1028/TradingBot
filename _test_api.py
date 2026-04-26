import os, ccxt

key    = os.getenv('BINANCE_API_KEY', '')
secret = os.getenv('BINANCE_SECRET_KEY', '')

print(f"Key loaded: {'YES' if key else 'NO'} ({len(key)} chars)")
print(f"Secret loaded: {'YES' if secret else 'NO'} ({len(secret)} chars)")

ex = ccxt.binance({
    'apiKey': key,
    'secret': secret,
    'enableRateLimit': True,
    'options': {'defaultType': 'future'},
})
ex.enable_demo_trading(True)

print(f"\nEndpoint: {ex.urls['api']['fapiPublic']}")
print("Testing balance fetch...")
try:
    bal = ex.fetch_balance({'type': 'future'})
    usdt = bal.get('USDT', {})
    print(f"SUCCESS — USDT free: {usdt.get('free')}, total: {usdt.get('total')}")
except Exception as e:
    print(f"FAILED — {type(e).__name__}: {e}")
