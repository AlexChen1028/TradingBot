"""
莊家幣監控 + 自動小倉位交易
每 15 分鐘掃描高波動幣種，3+ 個信號自動開倉
每小時發送 Binance 全市場漲跌幅榜（無交易通知）
"""

import os, json, time, ccxt, requests, numpy as np, pandas as pd
from datetime import datetime, timedelta, timezone
from pathlib import Path

def now8(): return datetime.utcnow() + timedelta(hours=8)

# ── 設定 ──────────────────────────────────────────────────────────────────────
SCAN_INTERVAL        = 15 * 60      # 每 15 分鐘掃一次信號
UPDATE_INTERVAL      = 2  * 60 * 60 # 每 2 小時更新幣種清單
LEADERBOARD_INTERVAL = 60 * 60      # 每小時發漲跌幅榜

MIN_SIGNALS    = 3      # 至少幾個信號才進場
MAX_POSITIONS  = 3      # 最多同時持有幾個倉位
MARGIN_USDT    = 50     # 每筆保證金（USDT）
LEVERAGE       = 20     # 槓桿倍數
STOP_LOSS_PCT  = 0.10   # 止損（相對進場價格）
TRAILING_PCT   = 0.15   # 追蹤止盈（從最佳價格回落）
MAX_HOLD_HOURS = 48     # 最長持倉時間
TOP_N          = 20
MIN_VOL_USDT   = 1_000_000
POSITIONS_FILE = 'positions_altcoin.json'

# ── Telegram ──────────────────────────────────────────────────────────────────
TG_TOKEN    = os.getenv('MONITOR_TOKEN',   '')
TG_CHAT_IDS = [i.strip() for i in os.getenv('MONITOR_CHAT_ID', '').split(',') if i.strip()]

def tg(text):
    if not TG_TOKEN or not TG_CHAT_IDS:
        return
    for chat_id in TG_CHAT_IDS:
        try:
            requests.post(
                f'https://api.telegram.org/bot{TG_TOKEN}/sendMessage',
                json={'chat_id': chat_id, 'text': text, 'parse_mode': 'HTML'},
                timeout=10,
            )
        except Exception:
            pass

# ── 倉位持久化 ────────────────────────────────────────────────────────────────
def load_positions():
    p = Path(POSITIONS_FILE)
    return json.loads(p.read_text()) if p.exists() else {}

def save_positions(pos):
    Path(POSITIONS_FILE).write_text(json.dumps(pos, indent=2))

# ── 幣種清單更新 ──────────────────────────────────────────────────────────────
def get_top_coins(exchange):
    print("正在更新幣種清單...")
    markets = exchange.load_markets()
    symbols = [s for s, m in markets.items()
               if m.get('quote') == 'USDT' and m.get('type') in ('swap', 'future')
               and m.get('active') and ':USDT' in s]

    since = int((datetime.now(timezone.utc) - timedelta(days=185)).timestamp() * 1000)
    results = []
    for sym in symbols:
        try:
            raw = exchange.fetch_ohlcv(sym, '1d', since=since, limit=185)
            if not raw or len(raw) < 30:
                continue
            df = pd.DataFrame(raw, columns=['ts', 'open', 'high', 'low', 'close', 'volume'])
            df['ret'] = df['close'].pct_change().abs()
            if (df['volume'] * df['close']).mean() < MIN_VOL_USDT:
                continue
            vol_mean = df['volume'].mean()
            results.append({
                'symbol':        sym,
                'max_move':      df['ret'].max(),
                'big_move_days': int((df['ret'] > 0.15).sum()),
                'vol_spike':     df['volume'].max() / vol_mean if vol_mean > 0 else 0,
                'realized_vol':  df['ret'].std() * np.sqrt(365),
            })
            time.sleep(0.08)
        except Exception:
            continue

    if not results:
        return []
    df_r = pd.DataFrame(results)
    df_r['score'] = (
        df_r['max_move'].rank(pct=True)      * 0.40 +
        df_r['big_move_days'].rank(pct=True) * 0.30 +
        df_r['vol_spike'].rank(pct=True)     * 0.20 +
        df_r['realized_vol'].rank(pct=True)  * 0.10
    )
    top = df_r.nlargest(TOP_N, 'score')['symbol'].tolist()
    print(f"清單更新完成：{[s.split('/')[0] for s in top]}")
    return top

# ── 資料抓取 ──────────────────────────────────────────────────────────────────
def fetch_1h(exchange, symbol, limit=7 * 24 + 10):
    try:
        raw = exchange.fetch_ohlcv(symbol, '1h', limit=limit)
        if not raw or len(raw) < 50:
            return None
        df = pd.DataFrame(raw, columns=['ts', 'open', 'high', 'low', 'close', 'volume'])
        df['ts'] = pd.to_datetime(df['ts'], unit='ms')
        return df
    except Exception:
        return None

def fetch_funding(exchange, symbol):
    try:
        rates = exchange.fetch_funding_rate_history(symbol, limit=10)
        if not rates or len(rates) < 2:
            return None, None
        return rates[-1]['fundingRate'], rates[-2]['fundingRate']
    except Exception:
        return None, None

# ── 信號分析 ──────────────────────────────────────────────────────────────────
def analyze(exchange, symbol):
    df = fetch_1h(exchange, symbol)
    if df is None:
        return None

    signals, details = [], []
    fr_now, fr_prev  = fetch_funding(exchange, symbol)
    cur_price = df['close'].iloc[-1]

    # 1. 成交量累積
    vol_4h  = df['volume'].iloc[-4:].mean()
    vol_24h = df['volume'].iloc[-24:].mean()
    if vol_24h > 0 and vol_4h / vol_24h >= 1.5:
        signals.append('vol')
        details.append(f"成交量累積 {vol_4h/vol_24h:.1f}x 均量")

    # 2. 價格壓縮
    avg_range = df.assign(r=(df['high'] - df['low']) / df['close'])['r'].iloc[-168:].mean()
    range_now = df.assign(r=(df['high'] - df['low']) / df['close'])['r'].iloc[-4:].mean()
    if avg_range > 0 and range_now / avg_range <= 0.5:
        signals.append('compress')
        details.append(f"價格壓縮（近4h波動僅均值的{range_now/avg_range:.0%}）")

    # 3. 接近突破
    high_14d = df['high'].iloc[-14 * 24:].max()
    gap_pct  = (high_14d - cur_price) / high_14d
    if gap_pct <= 0.03:
        signals.append('breakout')
        details.append(f"距14日高點僅 {gap_pct:.1%}")

    # 4. 資金費率劇變
    if fr_now is not None and fr_prev is not None:
        fr_change = abs(fr_now - fr_prev)
        if fr_change >= 0.0002:
            signals.append('funding')
            details.append(f"資金費率劇變 {fr_now*100:+.4f}%")

    # 判斷方向
    ret_1h = (df['close'].iloc[-1] - df['close'].iloc[-2]) / df['close'].iloc[-2]
    if 'breakout' in signals:
        direction = 1
    elif fr_now is not None and fr_now > 0.001:
        direction = -1  # 多方過度槓桿 → 做空
    elif fr_now is not None and fr_now < -0.001:
        direction = 1   # 空方過度槓桿 → 做多
    elif ret_1h < -0.02:
        direction = -1
    else:
        direction = 1

    return {
        'symbol':    symbol,
        'price':     cur_price,
        'signals':   signals,
        'details':   details,
        'n':         len(signals),
        'direction': direction,
    }

# ── 開倉 / 平倉 ───────────────────────────────────────────────────────────────
def open_pos(exchange, symbol, direction, positions):
    try:
        exchange.set_margin_mode('isolated', symbol)
        exchange.set_leverage(LEVERAGE, symbol, params={'marginMode': 'isolated'})
        price  = float(exchange.fetch_ticker(symbol)['last'])
        amount = round(MARGIN_USDT * LEVERAGE / price, 4)
        if direction == 1:
            exchange.create_market_buy_order(symbol, amount)
        else:
            exchange.create_market_sell_order(symbol, amount)
        positions[symbol] = {
            'direction':   direction,
            'entry_price': price,
            'entry_time':  now8().isoformat(),
            'peak_price':  price,
            'amount':      amount,
        }
        save_positions(positions)
        side = 'LONG' if direction == 1 else 'SHORT'
        print(f"  ✅ 開倉 {side} {symbol.split('/')[0]} | {amount} @ {price:.4f} | 保證金 ${MARGIN_USDT}")
    except Exception as e:
        print(f"  ❌ 開倉失敗 {symbol}: {e}")

def close_pos(exchange, symbol, positions, reason):
    pos = positions.get(symbol)
    if not pos:
        return
    try:
        amt = pos['amount']
        if pos['direction'] == 1:
            exchange.create_market_sell_order(symbol, amt, params={'reduceOnly': True})
        else:
            exchange.create_market_buy_order(symbol, amt, params={'reduceOnly': True})
        price   = float(exchange.fetch_ticker(symbol)['last'])
        pnl_pct = (price - pos['entry_price']) / pos['entry_price'] * pos['direction'] * 100
        side    = 'LONG' if pos['direction'] == 1 else 'SHORT'
        print(f"  🔒 平倉 {side} {symbol.split('/')[0]} | PnL {pnl_pct:+.2f}% | {reason}")
        del positions[symbol]
        save_positions(positions)
    except Exception as e:
        print(f"  ❌ 平倉失敗 {symbol}: {e}")

# ── 倉位檢查 ──────────────────────────────────────────────────────────────────
def check_positions(exchange, positions):
    now = now8()
    for symbol in list(positions.keys()):
        pos = positions[symbol]
        try:
            price = float(exchange.fetch_ticker(symbol)['last'])
            d     = pos['direction']

            if d == 1:
                positions[symbol]['peak_price'] = max(pos['peak_price'], price)
            else:
                positions[symbol]['peak_price'] = min(pos['peak_price'], price)

            peak    = positions[symbol]['peak_price']
            pnl_pct = (price - pos['entry_price']) / pos['entry_price'] * d
            held_h  = (now - datetime.fromisoformat(pos['entry_time'])).total_seconds() / 3600

            sl      = pnl_pct < -STOP_LOSS_PCT
            trail   = (price < peak * (1 - TRAILING_PCT)) if d == 1 else (price > peak * (1 + TRAILING_PCT))
            timeout = held_h >= MAX_HOLD_HOURS

            if sl or trail or timeout:
                reason = '止損' if sl else ('追蹤止盈' if trail else '超時平倉')
                close_pos(exchange, symbol, positions, reason)
        except Exception as e:
            print(f"  ⚠️ 檢查倉位 {symbol} 失敗: {e}")
    save_positions(positions)

# ── 漲跌幅榜 ──────────────────────────────────────────────────────────────────
def send_leaderboard(exchange, top_n=10):
    try:
        tickers = exchange.fetch_tickers()
        rows = [
            {'symbol': s.split('/')[0], 'pct': t.get('percentage')}
            for s, t in tickers.items()
            if s.endswith('/USDT')          # 用 spot 幣（不被 IP 封鎖）
            and not s.endswith(':USDT')
            and t.get('percentage') is not None
            and (t.get('quoteVolume') or 0) >= 5_000_000
        ]
        if not rows:
            print("  漲跌幅榜：無資料")
            return
        df = pd.DataFrame(rows).sort_values('pct', ascending=False)
        g  = df.head(top_n)
        l  = df.tail(top_n).iloc[::-1]
        now = now8().strftime('%Y-%m-%d %H:%M +08')
        g_lines = '\n'.join(f"  🟢 {r['symbol']:<12} {r['pct']:>+.2f}%" for _, r in g.iterrows())
        l_lines = '\n'.join(f"  🔴 {r['symbol']:<12} {r['pct']:>+.2f}%" for _, r in l.iterrows())
        tg(
            f"📊 <b>Binance 現貨漲跌榜（24h）</b>\n⏰ {now}\n\n"
            f"🏆 <b>漲幅前 {top_n} 名</b>\n{g_lines}\n\n"
            f"💀 <b>跌幅前 {top_n} 名</b>\n{l_lines}"
        )
        print(f"  → 漲跌幅榜已發送（{len(rows)} 幣種）")
    except Exception as e:
        print(f"  漲跌幅榜錯誤：{e}")

# ── 主迴圈 ────────────────────────────────────────────────────────────────────
def scan(exchange_pub, exchange_priv, watch_coins, positions):
    now = now8().strftime('%Y-%m-%d %H:%M +08')
    print(f"\n[{now}] 掃描 {len(watch_coins)} 個幣種  倉位 {len(positions)}/{MAX_POSITIONS}")

    if positions:
        check_positions(exchange_priv, positions)

    for symbol in watch_coins:
        if symbol in positions or len(positions) >= MAX_POSITIONS:
            continue
        result = analyze(exchange_pub, symbol)
        if result and result['n'] >= MIN_SIGNALS:
            print(f"  🔔 {symbol.split('/')[0]} {result['n']} 個信號 → 開倉")
            open_pos(exchange_priv, symbol, result['direction'], positions)
        time.sleep(0.15)

def main():
    exchange_pub  = ccxt.binance({'enableRateLimit': True})
    exchange_priv = ccxt.binance({
        'apiKey':          os.getenv('BINANCE_API_KEY', ''),
        'secret':          os.getenv('BINANCE_SECRET_KEY', ''),
        'enableRateLimit': True,
        'options':         {'defaultType': 'future'},
    })
    exchange_priv.enable_demo_trading(True)

    print("=" * 60)
    print("  莊家幣監控 + 自動交易啟動")
    print(f"  每筆 ${MARGIN_USDT}×{LEVERAGE}x  止損 {STOP_LOSS_PCT:.0%}  追蹤止盈 {TRAILING_PCT:.0%}")
    print(f"  最多 {MAX_POSITIONS} 個倉位  門檻 {MIN_SIGNALS}/4 個信號")
    print("=" * 60)

    positions        = load_positions()
    watch_coins      = get_top_coins(exchange_pub)
    last_update      = time.time()
    last_leaderboard = 0  # 立刻發第一次

    while True:
        try:
            if time.time() - last_update >= UPDATE_INTERVAL:
                new_list = get_top_coins(exchange_pub)
                if new_list:
                    watch_coins = new_list
                    last_update = time.time()

            if time.time() - last_leaderboard >= LEADERBOARD_INTERVAL:
                send_leaderboard(exchange_pub)
                last_leaderboard = time.time()

            scan(exchange_pub, exchange_priv, watch_coins, positions)

        except KeyboardInterrupt:
            print("\n監控停止。")
            break
        except Exception as e:
            print(f"掃描錯誤：{e}")
        time.sleep(SCAN_INTERVAL)

if __name__ == '__main__':
    main()
