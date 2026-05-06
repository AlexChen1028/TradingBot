"""
莊家幣監控 + 自動小倉位交易
每 15 分鐘掃描高波動幣種，3+ 個信號自動開倉
每小時發送 Binance 全市場漲跌幅榜（無交易通知）
"""

import os, json, time, ccxt, requests, numpy as np, pandas as pd
import feedparser
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer as _VSA
from datetime import datetime, timedelta, timezone
from pathlib import Path

def now8(): return datetime.utcnow() + timedelta(hours=8)

# ── 設定 ──────────────────────────────────────────────────────────────────────
SCAN_INTERVAL        = 15 * 60      # 每 15 分鐘掃一次信號
UPDATE_INTERVAL      = 2  * 60 * 60 # 每 2 小時更新幣種清單
LEADERBOARD_INTERVAL = 60 * 60      # 每小時發漲跌幅榜

MIN_SIGNALS           = 3   # 一般掃描門檻
MIN_LEADERBOARD_SIGNALS = 2   # 漲跌幅榜幣種（寬鬆）
LEADERBOARD_MIN_PCT   = 3.0  # 24h 漲跌超過 3% 才考慮
LEADERBOARD_TOP_N     = 5    # 漲幅/跌幅各取前幾名
MAX_POSITIONS  = 999    # 無上限（原3→5）
MARGIN_USDT    = 60     # 每筆保證金（USDT）（原50）
LEVERAGE       = 20     # 槓桿倍數
TRAILING_SL_PCT = 0.035  # 追蹤止損回調率 3.5%（交易所 TRAILING_STOP_MARKET）
TP_PCT          = 0.15   # 固定止盈天花板 15%（讓追蹤止損先跑，暴漲才觸發）
TRAILING_PCT    = 0.035  # 軟體追蹤備援（與交易所設定一致，交易所掛單失敗時生效）
MAX_HOLD_HOURS = 36     # 最長持倉時間（原48，縮短避免套牢）
TOP_N          = 20
MIN_VOL_USDT   = 1_000_000
POSITIONS_FILE      = 'positions_altcoin.json'
ALTCOIN_TRADES_FILE = 'altcoin_trades.jsonl'
TAKER_FEE           = 0.0005  # Binance futures taker fee 0.05%
NEWS_SEEN_FILE      = 'news_seen.json'

# 重大新聞偵測：含以下關鍵字才考慮
NEWS_KEYWORDS = [
    'crash', 'plunge', 'surge', 'rally', 'hack', 'exploit', 'breach', 'stolen',
    'ban', 'sec', 'cftc', 'regulation', 'lawsuit', 'crackdown', 'arrest',
    'bankrupt', 'insolvent', 'collapse', 'freeze', 'suspend', 'delist',
    'federal reserve', 'rate cut', 'rate hike', 'recession', 'inflation',
    'emergency', 'breaking', 'liquidat', 'rug pull', 'scam', 'billion',
]
NEWS_SENTIMENT_MIN = 0.25   # |vader compound| 低於此值不發
NEWS_FEEDS = [
    'https://www.coindesk.com/arc/outboundfeeds/rss/',
    'https://cointelegraph.com/rss',
    'https://decrypt.co/feed',
]
_news_analyzer = _VSA()

# ── Telegram ──────────────────────────────────────────────────────────────────
TG_TOKEN    = os.getenv('MONITOR_TOKEN',   '')
TG_CHAT_IDS = [i.strip() for i in os.getenv('MONITOR_CHAT_ID', '').split(',') if i.strip()]

# Trading Bot 1（用於週報 / P&L 通知）
TRADING_TOKEN    = os.getenv('TELEGRAM_TOKEN',   '')
TRADING_CHAT_IDS = [i.strip() for i in os.getenv('TELEGRAM_CHAT_ID', '').split(',') if i.strip()]

def tg_trading(text):
    """透過 Trading Bot 1 發送（週報 / 盈虧通知）"""
    if not TRADING_TOKEN or not TRADING_CHAT_IDS:
        return
    for chat_id in TRADING_CHAT_IDS:
        try:
            requests.post(
                f'https://api.telegram.org/bot{TRADING_TOKEN}/sendMessage',
                json={'chat_id': chat_id, 'text': text, 'parse_mode': 'HTML'},
                timeout=10,
            )
        except Exception:
            pass

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

# ── 重大新聞偵測 ──────────────────────────────────────────────────────────────
def check_breaking_news():
    """掃描加密貨幣 RSS，偵測含重大關鍵字且情緒強烈的新聞，發到 TG 群組。"""
    # 載入已發送記錄，清除 24 小時前的舊條目
    seen_data: dict = {}
    p = Path(NEWS_SEEN_FILE)
    if p.exists():
        try:
            seen_data = json.loads(p.read_text())
        except Exception:
            pass
    cutoff = (now8() - timedelta(hours=24)).isoformat()
    seen_data = {url: ts for url, ts in seen_data.items() if ts > cutoff}
    seen_urls = set(seen_data)

    sent = 0
    for feed_url in NEWS_FEEDS:
        try:
            feed = feedparser.parse(feed_url)
            for entry in (feed.entries or [])[:8]:
                link = entry.get('link') or entry.get('id', '')
                if not link or link in seen_urls:
                    continue
                title   = entry.get('title', '')
                summary = entry.get('summary', '')[:400]
                text    = f"{title} {summary}".lower()

                # 關鍵字過濾
                hit = next((kw for kw in NEWS_KEYWORDS if kw in text), None)
                if not hit:
                    continue

                # 情緒強度過濾
                score = _news_analyzer.polarity_scores(text)['compound']
                if abs(score) < NEWS_SENTIMENT_MIN:
                    continue

                # 決定情緒標籤
                if score <= -0.4:
                    tag = '🔴 利空'
                elif score >= 0.4:
                    tag = '🟢 利多'
                else:
                    tag = '⚠️ 重要'

                source  = feed_url.split('/')[2].replace('www.', '')
                excerpt = summary[:220] + ('...' if len(summary) > 220 else '')
                tg(
                    f"📰 <b>重大市場新聞 {tag}</b>\n"
                    f"🔗 {source}\n\n"
                    f"<b>{title}</b>\n\n"
                    f"{excerpt}"
                )
                seen_data[link] = now8().isoformat()
                seen_urls.add(link)
                sent += 1
                time.sleep(0.5)
        except Exception as e:
            print(f"  ⚠️ 新聞來源失敗 {feed_url}: {e}")

    Path(NEWS_SEEN_FILE).write_text(json.dumps(seen_data))
    if sent:
        print(f"  📰 重大新聞：發送 {sent} 則")


# ── 交易記錄 ──────────────────────────────────────────────────────────────────
def log_altcoin_trade(symbol, direction, entry_price, close_price, amount, entry_time, reason):
    pnl_usdt = amount * (close_price - entry_price) * direction
    fee_usdt = amount * (entry_price + close_price) * TAKER_FEE
    record = {
        'coin':         symbol.split('/')[0],
        'direction':    direction,
        'entry_price':  entry_price,
        'close_price':  close_price,
        'amount':       amount,
        'pnl_usdt':     round(pnl_usdt, 4),
        'fee_usdt':     round(fee_usdt, 4),
        'net_pnl_usdt': round(pnl_usdt - fee_usdt, 4),
        'margin_usdt':  MARGIN_USDT,
        'entry_time':   entry_time,
        'close_time':   now8().isoformat(),
        'reason':       reason,
    }
    with open(ALTCOIN_TRADES_FILE, 'a', encoding='utf-8') as f:
        f.write(json.dumps(record) + '\n')


def send_hourly_position_report():
    """整點：讀取 BTC/ETH/SOL status 檔 + altcoin 持倉，合併發一則報告。"""
    now = now8().strftime('%Y-%m-%d %H:%M +08')
    lines = []
    balance = None

    for coin in ['BTC', 'ETH', 'SOL']:
        p = Path(f'{coin.lower()}_status.json')
        if not p.exists():
            continue
        try:
            s = json.loads(p.read_text())
            d = s.get('direction', 0)
            if balance is None:
                balance = s.get('balance')
            if d == 0:
                lines.append(f"⚪ <b>{coin}</b>  空倉")
            else:
                side  = '🟢 LONG' if d == 1 else '🔴 SHORT'
                lines.append(
                    f"{side} <b>{coin}</b> 20x逐倉\n"
                    f"   進場 {s['entry_price']:,.2f} → 現價 {s['cur_price']:,.2f}\n"
                    f"   價格 {s['price_pct']:+.2f}%  保證金盈虧 {s['pnl_pct']:+.2f}%  持倉 {s['held_h']:.1f}h"
                )
        except Exception:
            continue

    # altcoin 持倉
    positions = load_positions()
    for sym, pos in positions.items():
        coin = sym.split('/')[0]
        d    = pos['direction']
        ep   = pos['entry_price']
        side = '🟢 LONG' if d == 1 else '🔴 SHORT'
        try:
            cur = float(requests.get(
                f'https://api.binance.com/api/v3/ticker/price?symbol={coin}USDT',
                timeout=5,
            ).json()['price'])
            pnl = (cur - ep) / ep * d * 100 * LEVERAGE
            lines.append(
                f"{side} <b>{coin}</b> 20x逐倉（山寨）\n"
                f"   進場 {ep:.4f} → 現價 {cur:.4f}  保證金盈虧 {pnl:+.2f}%"
            )
        except Exception:
            lines.append(f"{side} <b>{coin}</b> 20x逐倉（山寨）  進場 {ep:.4f}")

    if not lines:
        return  # 全空倉就不發

    body = '\n\n'.join(lines)
    bal_str = f"\n\n💰 帳戶餘額：{balance:,.2f} USDT" if balance else ''
    tg_trading(
        f"📋 <b>每小時持倉公告</b>\n"
        f"⏰ {now}\n\n"
        f"{body}"
        f"{bal_str}"
    )


def send_performance_report():
    """讀取過去7天所有幣種交易記錄，計算淨利潤和報酬率，發送到TG"""
    cutoff = now8() - timedelta(days=7)
    trade_files = {
        'BTC':  'btc_trades.jsonl',
        'ETH':  'eth_trades.jsonl',
        'SOL':  'sol_trades.jsonl',
        '山寨': ALTCOIN_TRADES_FILE,
    }

    coin_stats = {}
    total_gross = total_fee = total_net = total_margin = total_trades = 0

    for label, fname in trade_files.items():
        p = Path(fname)
        if not p.exists():
            continue
        rows = []
        for line in p.read_text(encoding='utf-8').splitlines():
            if not line.strip():
                continue
            try:
                r = json.loads(line)
                if datetime.fromisoformat(r.get('close_time', '1970-01-01')) < cutoff:
                    continue
                rows.append(r)
            except Exception:
                continue
        if not rows:
            continue

        gross  = sum(r.get('pnl_usdt', 0) for r in rows)
        # 舊記錄若沒有 fee_usdt 欄位，自動估算
        fee    = sum(
            r.get('fee_usdt') if r.get('fee_usdt') is not None
            else r.get('amount', 0) * (r.get('entry_price', 0) + r.get('close_price', 0)) * TAKER_FEE
            for r in rows
        )
        net    = gross - fee
        margin = sum(r.get('margin_usdt', MARGIN_USDT) for r in rows)

        coin_stats[label] = {'n': len(rows), 'gross': gross, 'fee': fee, 'net': net, 'margin': margin}
        total_gross  += gross
        total_fee    += fee
        total_net    += net
        total_margin += margin
        total_trades += len(rows)

    if total_trades == 0:
        tg_trading(f"📊 <b>週績效報告（過去7天）</b>\n⏰ {now8().strftime('%Y-%m-%d %H:%M +08')}\n\n尚無已完成交易記錄。")
        return

    roi = total_net / total_margin * 100 if total_margin > 0 else 0
    emoji = '📈' if total_net >= 0 else '📉'

    coin_lines = '\n'.join(
        f"{'🟢' if s['net'] >= 0 else '🔴'} {label:<4}  {s['n']}筆  "
        f"淨利 <b>{s['net']:+.2f} U</b>  費 {s['fee']:.2f} U"
        for label, s in coin_stats.items()
    )

    msg = (
        f"{emoji} <b>週績效報告（過去7天）</b>\n"
        f"⏰ {now8().strftime('%Y-%m-%d %H:%M +08')}\n\n"
        f"{coin_lines}\n\n"
        f"總交易：{total_trades} 筆\n"
        f"毛利潤：{total_gross:+.2f} U\n"
        f"手續費：-{total_fee:.2f} U\n"
        f"<b>淨利潤：{total_net:+.2f} U</b>\n"
        f"投入保證金：{total_margin:.0f} U\n"
        f"<b>週報酬率：{roi:+.2f}%</b>"
    )
    tg_trading(msg)
    print(f"  📊 週績效報告已發送：淨利 {total_net:+.2f} U  ROI {roi:+.2f}%")


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
        price   = float(exchange.fetch_ticker(symbol)['last'])
        amount  = round(MARGIN_USDT * LEVERAGE / price, 4)
        sl_side = 'sell' if direction == 1 else 'buy'

        if direction == 1:
            exchange.create_market_buy_order(symbol, amount)
        else:
            exchange.create_market_sell_order(symbol, amount)

        sl_id = tp_id = None

        # 追蹤止損（TRAILING_STOP_MARKET）；失敗退回固定 STOP_MARKET
        try:
            tsl_order = exchange.create_order(symbol, 'trailing_stop_market', sl_side, amount, None, {
                'callbackRate': TRAILING_SL_PCT * 100,
                'closePosition': True,
                'workingType':  'MARK_PRICE',
            })
            sl_id = tsl_order['id']
            print(f"  ✅ 追蹤止損 {TRAILING_SL_PCT*100:.1f}% 已掛")
        except Exception as e:
            print(f"  ⚠️ 追蹤止損失敗 ({e})，退回固定止損")
            try:
                sl_price = round(
                    price * (1 - TRAILING_SL_PCT) if direction == 1 else price * (1 + TRAILING_SL_PCT), 4
                )
                sl_order = exchange.create_order(symbol, 'stop_market', sl_side, amount, None, {
                    'stopPrice': sl_price, 'closePosition': True, 'workingType': 'MARK_PRICE',
                })
                sl_id = sl_order['id']
            except Exception as e2:
                print(f"  ❌ 固定止損也失敗 {symbol}: {e2}")

        # 固定止盈天花板（TAKE_PROFIT_MARKET）
        try:
            tp_price = round(
                price * (1 + TP_PCT) if direction == 1 else price * (1 - TP_PCT), 4
            )
            tp_order = exchange.create_order(symbol, 'take_profit_market', sl_side, amount, None, {
                'stopPrice': tp_price, 'closePosition': True, 'workingType': 'MARK_PRICE',
            })
            tp_id = tp_order['id']
        except Exception as e:
            print(f"  ⚠️ TP 訂單失敗 {symbol}: {e}")

        positions[symbol] = {
            'direction':   direction,
            'entry_price': price,
            'entry_time':  now8().isoformat(),
            'peak_price':  price,
            'amount':      amount,
            'sl_order_id': sl_id,
            'tp_order_id': tp_id,
        }
        save_positions(positions)
        side = 'LONG' if direction == 1 else 'SHORT'
        coin = symbol.split('/')[0]
        sl_label = f"追蹤止損 {TRAILING_SL_PCT*100:.1f}% {'✅' if sl_id else '⚠️'}"
        tp_label = f"止盈天花板 {TP_PCT*100:.0f}% {'✅' if tp_id else '⚠️'}"
        print(f"  ✅ 開倉 {side} {coin} | {amount} @ {price:.4f} | ${MARGIN_USDT}×{LEVERAGE}x | {sl_label} {tp_label}")
        tg(
            f"{'🟢' if direction==1 else '🔴'} <b>開倉 {side} {coin}</b>\n"
            f"進場：{price:.4f} USDT\n"
            f"數量：{amount}  保證金：${MARGIN_USDT}×{LEVERAGE}x 逐倉\n"
            f"{sl_label} | {tp_label}"
        )
    except Exception as e:
        print(f"  ❌ 開倉失敗 {symbol}: {e}")

def close_pos(exchange, symbol, positions, reason):
    pos = positions.get(symbol)
    if not pos:
        return
    try:
        # 取消交易所止損/止盈掛單
        for key in ('sl_order_id', 'tp_order_id'):
            oid = pos.get(key)
            if oid:
                try:
                    exchange.cancel_order(oid, symbol)
                except Exception:
                    pass

        amt = pos['amount']
        if pos['direction'] == 1:
            exchange.create_market_sell_order(symbol, amt, params={'reduceOnly': True})
        else:
            exchange.create_market_buy_order(symbol, amt, params={'reduceOnly': True})

        price     = float(exchange.fetch_ticker(symbol)['last'])
        ep        = pos['entry_price']
        pnl_pct   = (price - ep) / ep * pos['direction'] * 100 * LEVERAGE
        pnl_usdt  = amt * (price - ep) * pos['direction']
        fee_usdt  = amt * (ep + price) * TAKER_FEE
        net_usdt  = pnl_usdt - fee_usdt
        side      = 'LONG' if pos['direction'] == 1 else 'SHORT'
        coin      = symbol.split('/')[0]
        print(f"  🔒 平倉 {side} {coin} | 淨利 {net_usdt:+.2f} U (費 {fee_usdt:.2f} U) | {reason}")
        tg(
            f"🔒 <b>平倉 {side} {coin}</b>\n"
            f"進場：{ep:.4f} → 現價：{price:.4f}\n"
            f"保證金盈虧：{pnl_pct:+.2f}%\n"
            f"毛利：{pnl_usdt:+.2f} U  手續費：-{fee_usdt:.2f} U\n"
            f"<b>淨利：{net_usdt:+.2f} U</b>  原因：{reason}"
        )
        log_altcoin_trade(symbol, pos['direction'], ep, price, amt, pos.get('entry_time', ''), reason)
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

            # 偵測交易所是否已透過 SL/TP 自動平倉
            try:
                ex_pos = exchange.fetch_positions([symbol])
                still_open = any(
                    abs(p.get('contracts') or 0) > 0
                    for p in ex_pos if p.get('symbol') == symbol
                )
                if not still_open:
                    ep       = pos['entry_price']
                    amt      = pos['amount']
                    pnl_usdt = amt * (price - ep) * d
                    fee_usdt = amt * (ep + price) * TAKER_FEE
                    net_usdt = pnl_usdt - fee_usdt
                    pnl_pct  = (price - ep) / ep * d * 100 * LEVERAGE
                    side     = 'LONG' if d == 1 else 'SHORT'
                    coin     = symbol.split('/')[0]
                    trigger  = '🎯 止盈' if pnl_usdt > 0 else '🛑 止損'
                    reason   = f'交易所{("止盈" if pnl_usdt > 0 else "止損")}'
                    print(f"  {trigger} 交易所平倉 {side} {coin} | 淨利 {net_usdt:+.2f} U")
                    tg(
                        f"⚡ <b>{trigger} {side} {coin}（交易所自動）</b>\n"
                        f"進場：{ep:.4f} → 現價：{price:.4f}\n"
                        f"保證金盈虧：{pnl_pct:+.2f}%\n"
                        f"毛利：{pnl_usdt:+.2f} U  手續費：-{fee_usdt:.2f} U\n"
                        f"<b>淨利：{net_usdt:+.2f} U</b>"
                    )
                    log_altcoin_trade(symbol, d, ep, price, amt, pos.get('entry_time', ''), reason)
                    del positions[symbol]
                    save_positions(positions)
                    continue
            except Exception:
                pass

            if d == 1:
                positions[symbol]['peak_price'] = max(pos['peak_price'], price)
            else:
                positions[symbol]['peak_price'] = min(pos['peak_price'], price)

            peak    = positions[symbol]['peak_price']
            held_h  = (now - datetime.fromisoformat(pos['entry_time'])).total_seconds() / 3600

            # 追蹤止損備援（交易所掛單失敗時的軟體保險）
            trail   = (price < peak * (1 - TRAILING_PCT)) if d == 1 else (price > peak * (1 + TRAILING_PCT))
            timeout = held_h >= MAX_HOLD_HOURS

            if trail or timeout:
                reason = '追蹤止損備援' if trail else '超時平倉'
                close_pos(exchange, symbol, positions, reason)
        except Exception as e:
            print(f"  ⚠️ 檢查倉位 {symbol} 失敗: {e}")
    save_positions(positions)

# ── 漲跌幅榜 ──────────────────────────────────────────────────────────────────
def send_leaderboard(exchange, top_n=10):
    """發送漲跌幅榜，並回傳符合條件的交易候選幣種 [(futures_symbol, direction, pct)]"""
    candidates = []
    try:
        tickers = exchange.fetch_tickers()
        rows = [
            {'symbol': s.split('/')[0], 'pct': t.get('percentage')}
            for s, t in tickers.items()
            if s.endswith('/USDT')
            and not s.endswith(':USDT')
            and t.get('percentage') is not None
            and (t.get('quoteVolume') or 0) >= 5_000_000
        ]
        if not rows:
            print("  漲跌幅榜：無資料")
            return candidates
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

        # 挑選交易候選：漲超過 3% → 做多，跌超過 3% → 做空
        markets = exchange.markets or {}
        top_g = df[df['pct'] >  LEADERBOARD_MIN_PCT].head(LEADERBOARD_TOP_N)
        top_l = df[df['pct'] < -LEADERBOARD_MIN_PCT].tail(LEADERBOARD_TOP_N)
        for _, row in pd.concat([top_g, top_l]).iterrows():
            sym = row['symbol']
            fut = f"{sym}/USDT:USDT"
            direction = 1 if row['pct'] > 0 else -1
            candidates.append((fut, direction, row['pct']))
    except Exception as e:
        print(f"  漲跌幅榜錯誤：{e}")
    return candidates

# ── 主迴圈 ────────────────────────────────────────────────────────────────────
def scan_leaderboard(exchange_pub, exchange_priv, candidates, positions):
    """根據漲跌幅榜候選幣種嘗試開倉（寬鬆門檻：2 個信號）"""
    if not candidates:
        return
    print(f"  📊 漲跌幅榜候選：{len(candidates)} 個幣種")
    for symbol, lb_direction, pct in candidates:
        if symbol in positions or len(positions) >= MAX_POSITIONS:
            break
        result = analyze(exchange_pub, symbol)
        if result is None:
            continue
        # 寬鬆門檻：只需 2 個信號，方向由漲跌幅榜決定
        if result['n'] >= MIN_LEADERBOARD_SIGNALS:
            sym_name = symbol.split('/')[0]
            dir_str  = '做多' if lb_direction == 1 else '做空'
            print(f"  🎯 {sym_name} {pct:+.1f}% | {result['n']} 個信號 → {dir_str}")
            tg(f"🎯 <b>漲跌幅榜進場</b>\n{sym_name} {pct:+.1f}% | {result['n']}/4 信號\n方向：{'🟢 做多' if lb_direction==1 else '🔴 做空'}")
            open_pos(exchange_priv, symbol, lb_direction, positions)
        time.sleep(0.2)


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
    print(f"  每筆 ${MARGIN_USDT}×{LEVERAGE}x  追蹤SL {TRAILING_SL_PCT:.1%}  TP {TP_PCT:.0%}  備援 {TRAILING_PCT:.1%}")
    print(f"  最多 {MAX_POSITIONS} 個倉位  門檻 {MIN_SIGNALS}/4 個信號")
    print("=" * 60)

    positions        = load_positions()
    watch_coins      = get_top_coins(exchange_pub)
    last_update      = time.time()
    last_leaderboard = 0  # 立刻發第一次
    last_report_date = None  # 每天發一次週績效報告
    last_report_hour = -1   # 整點持倉公告

    while True:
        try:
            if time.time() - last_update >= UPDATE_INTERVAL:
                new_list = get_top_coins(exchange_pub)
                if new_list:
                    watch_coins = new_list
                    last_update = time.time()

            lb_candidates = []
            if time.time() - last_leaderboard >= LEADERBOARD_INTERVAL:
                lb_candidates = send_leaderboard(exchange_pub)
                last_leaderboard = time.time()
                if lb_candidates:
                    scan_leaderboard(exchange_pub, exchange_priv, lb_candidates, positions)

            # 整點發彙總持倉報告（分鐘數 < 2 且這小時還沒發過）
            _now = now8()
            if _now.minute < 2 and _now.hour != last_report_hour:
                send_hourly_position_report()
                last_report_hour = _now.hour

            # 每天 00:00 +08 發送週績效報告
            today = _now.date()
            if today != last_report_date:
                send_performance_report()
                last_report_date = today

            check_breaking_news()
            scan(exchange_pub, exchange_priv, watch_coins, positions)

        except KeyboardInterrupt:
            print("\n監控停止。")
            break
        except Exception as e:
            print(f"掃描錯誤：{e}")
        time.sleep(SCAN_INTERVAL)

if __name__ == '__main__':
    main()
