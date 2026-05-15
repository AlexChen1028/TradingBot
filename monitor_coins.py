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

MIN_SIGNALS           = 2   # 一般掃描門檻
MIN_LEADERBOARD_SIGNALS = 2   # 漲跌幅榜幣種（寬鬆）
LEADERBOARD_MIN_PCT   = 3.0  # 24h 漲跌超過 3% 才考慮
LEADERBOARD_TOP_N     = 5    # 漲幅/跌幅各取前幾名
MAX_POSITIONS  = 999    # 無上限（原3→5）
MARGIN_USDT         = 60    # 預設保證金（fallback）
MARGIN_BY_SIGNALS   = {2: 60, 3: 80, 4: 100}  # 動態保證金（依信號數量）
LIMIT_ORDER_TIMEOUT = 15      # 限價掛單等待秒數，超過改市價
LIMIT_SLIPPAGE      = 0.0002  # 限價優化幅度（做多掛低 / 做空掛高 0.02%）
LEVERAGE       = 20     # 槓桿倍數
STOP_LOSS_PCT   = 0.035  # 固定止損 3.5%（交易所 STOP_MARKET）
TP_PCT          = 0.07   # 固定止盈 7%（交易所 TAKE_PROFIT_MARKET）
TRAILING_PCT    = 0.15   # 軟體追蹤止盈備援（從最佳價格回落 15%）
MAX_HOLD_HOURS = 36     # 最長持倉時間（原48，縮短避免套牢）
TOP_N          = 20
MIN_VOL_USDT   = 1_000_000
POSITIONS_FILE      = 'positions_altcoin.json'
ALTCOIN_TRADES_FILE = 'altcoin_trades.jsonl'
TAKER_FEE           = 0.0005  # Binance futures taker fee 0.05%
NEWS_SEEN_FILE      = 'news_seen.json'
STATS_FROM          = os.getenv('STATS_FROM', '')  # e.g. '2026-05-16' — ignore trades before this date

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
def log_altcoin_trade(symbol, direction, entry_price, close_price, amount, entry_time, reason, margin_usdt=None):
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
        'margin_usdt':  margin_usdt if margin_usdt is not None else MARGIN_USDT,
        'entry_time':   entry_time,
        'close_time':   now8().isoformat(),
        'reason':       reason,
    }
    with open(ALTCOIN_TRADES_FILE, 'a', encoding='utf-8') as f:
        f.write(json.dumps(record) + '\n')


def send_hourly_position_report():
    """整點：altcoin 持倉彙總報告。"""
    now = now8().strftime('%Y-%m-%d %H:%M +08')
    lines = []
    balance = None

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

    # 今日累積盈虧
    today_start = now8().replace(hour=0, minute=0, second=0, microsecond=0)
    day_net = 0
    p_trades = Path(ALTCOIN_TRADES_FILE)
    if p_trades.exists():
        for line in p_trades.read_text(encoding='utf-8').splitlines():
            if not line.strip(): continue
            try:
                r = json.loads(line)
                ct = r.get('close_time', '1970-01-01')
                if datetime.fromisoformat(ct) >= today_start:
                    if not STATS_FROM or ct >= STATS_FROM:
                        day_net += r.get('net_pnl_usdt', 0)
            except Exception:
                pass

    if not lines and day_net == 0:
        return  # 全空倉且今日無交易就不發

    body    = '\n\n'.join(lines) if lines else '（目前無持倉）'
    day_str = f"\n\n📅 今日累積：<b>{'🟢' if day_net >= 0 else '🔴'} {day_net:+.2f} U</b>"
    tg(
        f"📋 <b>山寨幣持倉公告</b>\n"
        f"⏰ {now}\n\n"
        f"{body}"
        f"{day_str}"
    )


def send_performance_report():
    """讀取過去7天山寨幣交易記錄，計算淨利潤和報酬率，發送到TG"""
    cutoff = now8() - timedelta(days=7)
    trade_files = {
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
                ct = r.get('close_time', '1970-01-01')
                if datetime.fromisoformat(ct) < cutoff:
                    continue
                if STATS_FROM and ct < STATS_FROM:
                    continue
                rows.append(r)
            except Exception:
                continue
        if not rows:
            continue

        gross  = sum(r.get('pnl_usdt', 0) for r in rows)
        fee    = sum(
            r.get('fee_usdt') if r.get('fee_usdt') is not None
            else r.get('amount', 0) * (r.get('entry_price', 0) + r.get('close_price', 0)) * TAKER_FEE
            for r in rows
        )
        net    = gross - fee
        margin = sum(r.get('margin_usdt', MARGIN_USDT) for r in rows)
        wins   = sum(1 for r in rows if r.get('net_pnl_usdt', r.get('pnl_usdt', 0)) > 0)

        coin_stats[label] = {'n': len(rows), 'gross': gross, 'fee': fee, 'net': net, 'margin': margin, 'wins': wins}
        total_gross  += gross
        total_fee    += fee
        total_net    += net
        total_margin += margin
        total_trades += len(rows)

    if total_trades == 0:
        tg_trading(f"📊 <b>週績效報告（過去7天）</b>\n⏰ {now8().strftime('%Y-%m-%d %H:%M +08')}\n\n尚無已完成交易記錄。")
        return

    roi         = total_net / total_margin * 100 if total_margin > 0 else 0
    total_wins  = sum(s['wins'] for s in coin_stats.values())
    total_wr    = total_wins / total_trades * 100 if total_trades > 0 else 0
    emoji       = '📈' if total_net >= 0 else '📉'

    coin_lines = '\n'.join(
        f"{'🟢' if s['net'] >= 0 else '🔴'} {label:<4}  {s['n']}筆  "
        f"勝率 {s['wins']/s['n']*100:.0f}%  淨利 <b>{s['net']:+.2f} U</b>  費 {s['fee']:.2f} U"
        for label, s in coin_stats.items()
    )

    msg = (
        f"{emoji} <b>山寨幣週績效報告（過去7天）</b>\n"
        f"⏰ {now8().strftime('%Y-%m-%d %H:%M +08')}\n\n"
        f"{coin_lines}\n\n"
        f"總交易：{total_trades} 筆  勝率 <b>{total_wr:.0f}%</b>\n"
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

# ── 技術指標 ──────────────────────────────────────────────────────────────────
def _rsi(series, period=14):
    delta = series.diff()
    gain  = delta.clip(lower=0).rolling(period).mean()
    loss  = (-delta.clip(upper=0)).rolling(period).mean()
    rs    = gain / loss.replace(0, 1e-9)
    return 100 - 100 / (1 + rs)

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

    # RSI 14 + EMA50 趨勢
    rsi   = float(_rsi(df['close'], 14).iloc[-1])
    ema50 = float(df['close'].ewm(span=50, adjust=False).mean().iloc[-1])
    trend = 1 if cur_price > ema50 else -1

    return {
        'symbol':    symbol,
        'price':     cur_price,
        'signals':   signals,
        'details':   details,
        'n':         len(signals),
        'direction': direction,
        'rsi':       rsi,
        'trend':     trend,
    }

# ── 開倉 / 平倉 ───────────────────────────────────────────────────────────────
def _enter_position(exchange, symbol, direction, amount):
    """嘗試限價單進場，LIMIT_ORDER_TIMEOUT 秒未成交改市價。回傳實際成交均價。"""
    try:
        ref_price   = float(exchange.fetch_ticker(symbol)['last'])
        limit_price = round(
            ref_price * (1 - LIMIT_SLIPPAGE) if direction == 1
            else ref_price * (1 + LIMIT_SLIPPAGE), 8
        )
        side  = 'buy' if direction == 1 else 'sell'
        order = exchange.create_limit_order(symbol, side, amount, limit_price)
        oid   = order['id']
        print(f"  📋 限價單 {limit_price:.6g} 掛出，等待成交…")

        deadline = time.time() + LIMIT_ORDER_TIMEOUT
        while time.time() < deadline:
            time.sleep(3)
            o = exchange.fetch_order(oid, symbol)
            if o['status'] == 'closed':
                fill = float(o['average'] or limit_price)
                print(f"  ✅ 限價成交 @ {fill:.6g}")
                return fill
            if o['status'] == 'canceled':
                break

        try:
            exchange.cancel_order(oid, symbol)
        except Exception:
            pass
        print("  ⏱ 限價逾時，改市價")
    except Exception as e:
        print(f"  ⚠️ 限價單失敗 ({e})，改市價")

    side = 'buy' if direction == 1 else 'sell'
    if direction == 1:
        exchange.create_market_buy_order(symbol, amount)
    else:
        exchange.create_market_sell_order(symbol, amount)
    return float(exchange.fetch_ticker(symbol)['last'])


def open_pos(exchange, symbol, direction, positions, n_signals=3):
    try:
        exchange.set_margin_mode('isolated', symbol)
        exchange.set_leverage(LEVERAGE, symbol, params={'marginMode': 'isolated'})
        ref_price = float(exchange.fetch_ticker(symbol)['last'])
        margin    = MARGIN_BY_SIGNALS.get(n_signals, MARGIN_USDT)
        amount    = round(margin * LEVERAGE / ref_price, 4)
        sl_side   = 'sell' if direction == 1 else 'buy'

        price = _enter_position(exchange, symbol, direction, amount)

        sl_id = tp_id = None

        # 固定止損（STOP_MARKET）
        try:
            sl_price = round(
                price * (1 - STOP_LOSS_PCT) if direction == 1 else price * (1 + STOP_LOSS_PCT), 8
            )
            sl_order = exchange.create_order(symbol, 'stop_market', sl_side, amount, None, {
                'stopPrice': sl_price, 'closePosition': True, 'workingType': 'MARK_PRICE',
            })
            sl_id = sl_order['id']
            print(f"  ✅ 固定止損 {STOP_LOSS_PCT*100:.1f}% @ {sl_price:.6g} 已掛")
        except Exception as e:
            print(f"  ❌ 止損訂單失敗 {symbol}: {e}")

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
            'margin_usdt': margin,
            'sl_order_id': sl_id,
            'tp_order_id': tp_id,
        }
        save_positions(positions)
        side = 'LONG' if direction == 1 else 'SHORT'
        coin = symbol.split('/')[0]
        sl_label = f"止損 {STOP_LOSS_PCT*100:.1f}% {'✅' if sl_id else '⚠️'}"
        tp_label = f"止盈天花板 {TP_PCT*100:.0f}% {'✅' if tp_id else '⚠️'}"
        print(f"  ✅ 開倉 {side} {coin} | {amount} @ {price:.4f} | ${margin}×{LEVERAGE}x ({n_signals}信號) | {sl_label} {tp_label}")
        tg(
            f"{'🟢' if direction==1 else '🔴'} <b>開倉 {side} {coin}</b>\n"
            f"進場：{price:.4f} USDT\n"
            f"數量：{amount}  保證金：${margin}×{LEVERAGE}x 逐倉（{n_signals} 信號）\n"
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
        log_altcoin_trade(symbol, pos['direction'], ep, price, amt, pos.get('entry_time', ''), reason, pos.get('margin_usdt'))
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
                    log_altcoin_trade(symbol, d, ep, price, amt, pos.get('entry_time', ''), reason, pos.get('margin_usdt'))
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
            ep      = pos['entry_price']
            held_h  = (now - datetime.fromisoformat(pos['entry_time'])).total_seconds() / 3600

            # 保本止損：獲利 ≥ 3% 後把止損移到進場價
            gain_pct = (price - ep) / ep * d
            if gain_pct >= 0.03 and not pos.get('breakeven'):
                sl_side = 'sell' if d == 1 else 'buy'
                try:
                    old_sl = pos.get('sl_order_id')
                    if old_sl:
                        try: exchange.cancel_order(old_sl, symbol)
                        except Exception: pass
                    be_price = round(ep * (1 + 0.0005) if d == 1 else ep * (1 - 0.0005), 8)
                    be_order = exchange.create_order(symbol, 'stop_market', sl_side, pos['amount'], None, {
                        'stopPrice': be_price, 'closePosition': True, 'workingType': 'MARK_PRICE',
                    })
                    positions[symbol]['sl_order_id'] = be_order['id']
                    positions[symbol]['breakeven']   = True
                    save_positions(positions)
                    coin = symbol.split('/')[0]
                    print(f"  🔒 {coin} 保本止損啟動（進場 {ep:.4f}，現價 {price:.4f}，獲利 {gain_pct:.1%}）")
                    tg(f"🔒 <b>保本止損啟動</b> {coin}\n獲利已達 {gain_pct:.1%}，止損移至進場價附近 {be_price:.4f}")
                except Exception as e:
                    print(f"  ⚠️ 保本止損失敗 {symbol}: {e}")

            sl      = (price < ep * (1 - STOP_LOSS_PCT)) if d == 1 else (price > ep * (1 + STOP_LOSS_PCT))
            trail   = (price < peak * (1 - TRAILING_PCT)) if d == 1 else (price > peak * (1 + TRAILING_PCT))
            timeout = held_h >= MAX_HOLD_HOURS

            if sl or trail or timeout:
                reason = '止損' if sl else ('追蹤止盈備援' if trail else '超時平倉')
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
        print(f"  → 漲跌幅榜掃描完成（{len(rows)} 幣種）")

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

# ── 市場大環境過濾 ────────────────────────────────────────────────────────────
_bias_cache = {'bias': 0, 'reason': '初始化', 'ts': 0}

def get_market_bias(exchange):
    """
    每小時更新市場大環境偏向。BTC + SPY + QQQ 三者投票，2 票以上才設方向。
    返回 (bias: int, reason: str)   1=偏多  -1=偏空  0=中性
    """
    global _bias_cache
    if time.time() - _bias_cache['ts'] < 3600:
        return _bias_cache['bias'], _bias_cache['reason']

    votes_bull, votes_bear, parts = 0, 0, []

    # BTC 24h 漲跌
    try:
        btc_pct = exchange.fetch_ticker('BTC/USDT').get('percentage') or 0
        if btc_pct > 2:
            votes_bull += 1; parts.append(f"BTC {btc_pct:+.1f}%↑")
        elif btc_pct < -2:
            votes_bear += 1; parts.append(f"BTC {btc_pct:+.1f}%↓")
        else:
            parts.append(f"BTC {btc_pct:+.1f}% —")
    except Exception:
        parts.append("BTC err")

    # SPY / QQQ 最新日漲跌
    try:
        import yfinance as yf
        for sym in ['SPY', 'QQQ']:
            df = yf.download(sym, period='3d', interval='1d', progress=False, auto_adjust=True)
            if df is not None and len(df) >= 2:
                close = df['Close'].squeeze()
                pct = float((close.iloc[-1] / close.iloc[-2] - 1) * 100)
                if pct > 0.5:
                    votes_bull += 1; parts.append(f"{sym} {pct:+.1f}%↑")
                elif pct < -0.5:
                    votes_bear += 1; parts.append(f"{sym} {pct:+.1f}%↓")
                else:
                    parts.append(f"{sym} {pct:+.1f}% —")
    except Exception as e:
        parts.append(f"SPY/QQQ err")

    if votes_bear >= 2:
        bias, label = -1, '🔴 偏空'
    elif votes_bull >= 2:
        bias, label = 1, '🟢 偏多'
    else:
        bias, label = 0, '⚪ 中性'

    reason = f"{label}（{' | '.join(parts)}）"
    _bias_cache.update({'bias': bias, 'reason': reason, 'ts': time.time()})
    print(f"  🌍 大環境：{reason}")
    return bias, reason


# ── 主迴圈 ────────────────────────────────────────────────────────────────────
def scan_leaderboard(exchange_pub, exchange_priv, candidates, positions, market_bias=0):
    """根據漲跌幅榜候選幣種嘗試開倉（寬鬆門檻：2 個信號）"""
    if not candidates:
        return
    print(f"  📊 漲跌幅榜候選：{len(candidates)} 個幣種")
    for symbol, lb_direction, pct in candidates:
        if symbol in positions or len(positions) >= MAX_POSITIONS:
            break
        if market_bias == -1 and lb_direction == 1:
            continue
        if market_bias == 1 and lb_direction == -1:
            continue
        result = analyze(exchange_pub, symbol)
        if result is None:
            continue
        if lb_direction == 1 and result.get('rsi', 50) >= 80:
            continue  # RSI 超買，跳過做多
        if lb_direction == -1 and result.get('rsi', 50) <= 20:
            continue  # RSI 超賣，跳過做空
        if lb_direction != result.get('trend', lb_direction):
            continue  # 方向不符 EMA50 趨勢
        if result['n'] >= MIN_LEADERBOARD_SIGNALS:
            sym_name = symbol.split('/')[0]
            dir_str  = '做多' if lb_direction == 1 else '做空'
            print(f"  🎯 {sym_name} {pct:+.1f}% | {result['n']} 個信號 → {dir_str}")
            tg(f"🎯 <b>漲跌幅榜進場</b>\n{sym_name} {pct:+.1f}% | {result['n']}/4 信號\n方向：{'🟢 做多' if lb_direction==1 else '🔴 做空'}")
            open_pos(exchange_priv, symbol, lb_direction, positions, result['n'])
        time.sleep(0.2)


def scan(exchange_pub, exchange_priv, watch_coins, positions, market_bias=0):
    now = now8().strftime('%Y-%m-%d %H:%M +08')
    print(f"\n[{now}] 掃描 {len(watch_coins)} 個幣種  倉位 {len(positions)}/{MAX_POSITIONS}")

    if positions:
        check_positions(exchange_priv, positions)

    for symbol in watch_coins:
        if symbol in positions or len(positions) >= MAX_POSITIONS:
            continue
        result = analyze(exchange_pub, symbol)
        if result and result['n'] >= MIN_SIGNALS:
            d = result['direction']
            if d == 1 and result.get('rsi', 50) >= 80:
                continue  # RSI 超買，跳過做多
            if d == -1 and result.get('rsi', 50) <= 20:
                continue  # RSI 超賣，跳過做空
            if d != result.get('trend', d):
                continue  # 方向不符 EMA50 趨勢
            if market_bias == -1 and d == 1:
                continue  # 大環境偏空，跳過做多
            if market_bias == 1 and d == -1:
                continue  # 大環境偏多，跳過做空
            print(f"  🔔 {symbol.split('/')[0]} {result['n']} 個信號  RSI {result['rsi']:.0f}  → 開倉")
            open_pos(exchange_priv, symbol, d, positions, result['n'])
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
    print(f"  每筆 ${MARGIN_USDT}×{LEVERAGE}x  SL {STOP_LOSS_PCT:.1%}  TP {TP_PCT:.0%}  備援 {TRAILING_PCT:.0%}")
    print(f"  最多 {MAX_POSITIONS} 個倉位  門檻 {MIN_SIGNALS}/4 個信號")
    print("=" * 60)

    positions        = load_positions()
    watch_coins      = get_top_coins(exchange_pub)
    last_update      = time.time()
    last_leaderboard = 0  # 立刻發第一次
    last_report_date = None  # 每天發一次週績效報告
    last_report_hour = -1   # 整點持倉公告
    market_bias      = 0    # 初始中性，啟動時立刻抓一次

    while True:
        try:
            if time.time() - last_update >= UPDATE_INTERVAL:
                new_list = get_top_coins(exchange_pub)
                if new_list:
                    watch_coins = new_list
                    last_update = time.time()

            lb_candidates = []
            if time.time() - last_leaderboard >= LEADERBOARD_INTERVAL:
                market_bias, _ = get_market_bias(exchange_pub)  # 每小時更新大環境
                lb_candidates  = send_leaderboard(exchange_pub)
                last_leaderboard = time.time()
                if lb_candidates:
                    scan_leaderboard(exchange_pub, exchange_priv, lb_candidates, positions, market_bias)

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
            scan(exchange_pub, exchange_priv, watch_coins, positions, market_bias)

        except KeyboardInterrupt:
            print("\n監控停止。")
            break
        except Exception as e:
            print(f"掃描錯誤：{e}")
        time.sleep(SCAN_INTERVAL)

if __name__ == '__main__':
    main()
