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

MIN_SIGNALS           = 3   # 一般掃描門檻（2026-05-23 KOL: 提高至3+，降低假突破頻率）
MIN_LEADERBOARD_SIGNALS = 2   # 漲跌幅榜幣種（寬鬆）
LEADERBOARD_MIN_PCT   = 4.0  # 24h 漲跌超過 4% 才考慮（2026-05-23 KOL: 震盪雜訊多→提高至4%）
SHORT_BIAS            = True  # 2026-05-25 KOL: 三方共識大級別偏空（弱勢反彈），LONG 需額外 +1 信號
LEADERBOARD_TOP_N     = 5    # 漲幅/跌幅各取前幾名
MAX_POSITIONS  = 999    # 無上限（原3→5）
MARGIN_USDT         = 60    # 預設保證金（fallback）
MARGIN_BY_SIGNALS   = {2: 60, 3: 80, 4: 100}  # 動態保證金（依信號數量）
LIMIT_ORDER_TIMEOUT = 15      # 限價掛單等待秒數，超過改市價
LIMIT_SLIPPAGE      = 0.0002  # 限價優化幅度（做多掛低 / 做空掛高 0.02%）
LEVERAGE       = 20     # 槓桿倍數（山寨幣）
STOP_LOSS_PCT   = 0.035  # 固定止損 3.5%（山寨幣）
TP_PCT          = 0.07   # 固定止盈 7%（山寨幣）
# 主流幣（BTC/ETH/SOL）專用參數
MAJOR_LEVERAGE  = 50     # 槓桿倍數
MAJOR_SL_PCT    = 0.01   # 固定止損 1%
MAJOR_TP_PCT    = 0.02   # 固定止盈 2%
TRAILING_PCT    = 0.15   # 軟體追蹤止盈備援（從最佳價格回落 15%）
MAX_HOLD_HOURS = 36     # 最長持倉時間（原48，縮短避免套牢）
TOP_N          = 20
MIN_VOL_USDT   = 1_000_000
# 這三個幣永遠在掃描清單內，不依賴波動性排名
WATCH_ALWAYS   = ['BTC/USDT:USDT', 'ETH/USDT:USDT', 'SOL/USDT:USDT']
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
_MAJOR_TRADE_FILES = {
    'BTC': 'btc_trades.jsonl',
    'ETH': 'eth_trades.jsonl',
    'SOL': 'sol_trades.jsonl',
}

def log_altcoin_trade(symbol, direction, entry_price, close_price, amount, entry_time, reason, margin_usdt=None):
    pnl_usdt = amount * (close_price - entry_price) * direction
    fee_usdt = amount * (entry_price + close_price) * TAKER_FEE
    coin = symbol.split('/')[0]
    record = {
        'coin':         coin,
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
    # 主流幣寫獨立檔案，山寨幣寫 altcoin_trades.jsonl
    trade_file = _MAJOR_TRADE_FILES.get(coin, ALTCOIN_TRADES_FILE)
    with open(trade_file, 'a', encoding='utf-8') as f:
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
            _lev = MAJOR_LEVERAGE if sym in set(WATCH_ALWAYS) else LEVERAGE
            pnl = (cur - ep) / ep * d * 100 * _lev
            _type = '主流' if sym in set(WATCH_ALWAYS) else '山寨'
            lines.append(
                f"{side} <b>{coin}</b> {_lev}x逐倉（{_type}）\n"
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
    """讀取交易記錄（依 STATS_FROM 起算），計算主流幣 vs 山寨幣分區淨利潤，發送到TG"""
    cutoff = now8() - timedelta(days=90)  # 最遠回溯 90 天，STATS_FROM 為主要起算點
    # 主流幣 / 山寨幣分區
    major_files = {
        'BTC': 'btc_trades.jsonl',
        'ETH': 'eth_trades.jsonl',
        'SOL': 'sol_trades.jsonl',
    }
    alt_files = {
        '山寨': ALTCOIN_TRADES_FILE,
    }

    def _load_stats(files_dict):
        stats = {}
        for label, fname in files_dict.items():
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
            stats[label] = {'n': len(rows), 'gross': gross, 'fee': fee,
                            'net': net, 'margin': margin, 'wins': wins}
        return stats

    major_stats = _load_stats(major_files)
    alt_stats   = _load_stats(alt_files)
    all_stats   = {**major_stats, **alt_stats}

    total_trades = sum(s['n']      for s in all_stats.values())
    total_gross  = sum(s['gross']  for s in all_stats.values())
    total_fee    = sum(s['fee']    for s in all_stats.values())
    total_net    = sum(s['net']    for s in all_stats.values())
    total_margin = sum(s['margin'] for s in all_stats.values())
    total_wins   = sum(s['wins']   for s in all_stats.values())

    since_str = STATS_FROM if STATS_FROM else (now8() - timedelta(days=30)).strftime('%Y-%m-%d')
    title_tag = f"（{since_str} 起）"

    if total_trades == 0:
        tg_trading(f"📊 <b>績效報告{title_tag}</b>\n"
                   f"⏰ {now8().strftime('%Y-%m-%d %H:%M +08')}\n\n尚無已完成交易記錄。")
        return

    def _section_lines(stats):
        lines = []
        for label, s in stats.items():
            wr = s['wins'] / s['n'] * 100 if s['n'] > 0 else 0
            e  = '🟢' if s['net'] >= 0 else '🔴'
            lines.append(
                f"{e} {label:<4}  {s['n']}筆  勝率{wr:.0f}%  "
                f"淨利 <b>{s['net']:+.2f} U</b>"
            )
        return '\n'.join(lines)

    def _subtotal(stats):
        n   = sum(s['n']   for s in stats.values())
        net = sum(s['net'] for s in stats.values())
        return n, net

    major_n, major_net = _subtotal(major_stats)
    alt_n,   alt_net   = _subtotal(alt_stats)

    roi      = total_net / total_margin * 100 if total_margin > 0 else 0
    total_wr = total_wins / total_trades * 100 if total_trades > 0 else 0
    emoji    = '📈' if total_net >= 0 else '📉'

    parts = [f"{emoji} <b>績效報告{title_tag}</b>\n⏰ {now8().strftime('%Y-%m-%d %H:%M +08')}"]

    if major_stats:
        me = '🟢' if major_net >= 0 else '🔴'
        parts.append(
            f"\n<b>── 主流幣 ──</b>\n"
            f"{_section_lines(major_stats)}\n"
            f"{me} 小計  {major_n}筆  淨利 <b>{major_net:+.2f} U</b>"
        )

    if alt_stats:
        ae = '🟢' if alt_net >= 0 else '🔴'
        parts.append(
            f"\n<b>── 山寨幣 ──</b>\n"
            f"{_section_lines(alt_stats)}\n"
            f"{ae} 小計  {alt_n}筆  淨利 <b>{alt_net:+.2f} U</b>"
        )

    parts.append(
        f"\n<b>── 合計 ──</b>\n"
        f"總交易：{total_trades}筆  勝率 <b>{total_wr:.0f}%</b>\n"
        f"毛利潤：{total_gross:+.2f} U\n"
        f"手續費：-{total_fee:.2f} U\n"
        f"<b>淨利潤：{total_net:+.2f} U</b>\n"
        f"投入保證金：{total_margin:.0f} U\n"
        f"<b>報酬率：{roi:+.2f}%</b>"
    )

    msg = '\n'.join(parts)
    tg_trading(msg)
    print(f"  📊 績效報告已發送：淨利 {total_net:+.2f} U  ROI {roi:+.2f}%")


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
    for sym in WATCH_ALWAYS:
        if sym not in top:
            top.append(sym)
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

    # 4. 資金費率信號：劇變 或 由正轉負（嘎空燃料）
    if fr_now is not None and fr_prev is not None:
        fr_change = abs(fr_now - fr_prev)
        if fr_change >= 0.0002:
            signals.append('funding')
            details.append(f"資金費率劇變 {fr_now*100:+.4f}%")
        elif fr_prev >= -0.0001 and fr_now < -0.0001:
            signals.append('funding')
            details.append(f"資費轉負（嘎空燃料）{fr_now*100:+.4f}%")

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


def analyze_major(exchange, symbol):
    """
    主流幣（BTC/ETH/SOL）趨勢跟隨分析。
    與山寨幣的差異：不依賴大幅壓縮突破，改用趨勢指標組合。

    信號 1 — EMA 排列：EMA9/21/50 全多頭 or 全空頭
    信號 2 — MACD 動能：histogram 連續 3 根同向（走揚 or 走弱）
    信號 3 — 成交量確認：近 2 根均量 ≥ 20 期均量 × 1.3（主流幣門檻低於山寨）
    信號 4 — 資金費率：劇變 or 翻負（嘎空燃料，與山寨相同）
    方向 — 多數決：bull 信號 > bear 信號 → 做多，反之做空，平手預設做多
    """
    df = fetch_1h(exchange, symbol)
    if df is None:
        return None

    fr_now, fr_prev = fetch_funding(exchange, symbol)
    c = df['close']

    ema9  = c.ewm(span=9,  adjust=False).mean()
    ema21 = c.ewm(span=21, adjust=False).mean()
    ema50 = c.ewm(span=50, adjust=False).mean()

    macd_fast  = c.ewm(span=12, adjust=False).mean()
    macd_slow  = c.ewm(span=26, adjust=False).mean()
    macd_hist  = (macd_fast - macd_slow) - (macd_fast - macd_slow).ewm(span=9, adjust=False).mean()

    bull, bear, details = [], [], []

    # 1. EMA 排列
    if ema9.iloc[-1] > ema21.iloc[-1] > ema50.iloc[-1]:
        bull.append('ema')
        details.append("EMA9>21>50 多頭排列")
    elif ema9.iloc[-1] < ema21.iloc[-1] < ema50.iloc[-1]:
        bear.append('ema')
        details.append("EMA9<21<50 空頭排列")

    # 2. MACD 動能（近 3 根 histogram 同向）
    h3 = macd_hist.iloc[-3:].values
    if h3[-1] > 0 and h3[0] <= h3[1] <= h3[2]:
        bull.append('macd')
        details.append(f"MACD 柱狀圖走揚 ({macd_hist.iloc[-1]:.5f})")
    elif h3[-1] < 0 and h3[0] >= h3[1] >= h3[2]:
        bear.append('macd')
        details.append(f"MACD 柱狀圖走弱 ({macd_hist.iloc[-1]:.5f})")

    # 3. 成交量確認（偏多趨勢 → bull，否則 bear）
    vol_20  = df['volume'].iloc[-20:].mean()
    vol_now = df['volume'].iloc[-2:].mean()
    if vol_20 > 0 and vol_now / vol_20 >= 1.3:
        if ema21.iloc[-1] >= ema50.iloc[-1]:
            bull.append('vol')
            details.append(f"成交量放大 {vol_now/vol_20:.1f}x（偏多趨勢）")
        else:
            bear.append('vol')
            details.append(f"成交量放大 {vol_now/vol_20:.1f}x（偏空趨勢）")

    # 日線 EMA200（供後面 KOL 訊號使用，每次獨立抓取）
    ema200_daily = None
    try:
        raw_1d = exchange.fetch_ohlcv(symbol, '1d', limit=210)
        if raw_1d and len(raw_1d) >= 50:
            dc = pd.DataFrame(raw_1d, columns=['ts','open','high','low','close','volume'])['close']
            ema200_daily = float(dc.ewm(span=200, adjust=False).mean().iloc[-1])
    except Exception:
        pass

    # 4. 資金費率（同山寨幣邏輯）
    if fr_now is not None and fr_prev is not None:
        fr_change = abs(fr_now - fr_prev)
        if fr_change >= 0.0002:
            (bull if fr_now < 0 else bear).append('funding')
            details.append(f"資金費率劇變 {fr_now*100:+.4f}%")
        elif fr_prev >= -0.0001 and fr_now < -0.0001:
            bull.append('funding')
            details.append(f"資費轉負（嘎空燃料）{fr_now*100:+.4f}%")

    # 5. RSI 動能：RSI 方向 × 50 線位置（KOL 常用：50 以上看多動能）
    rsi_series = _rsi(c, 14)
    rsi_now  = float(rsi_series.iloc[-1])
    rsi_prev = float(rsi_series.iloc[-4])   # 4 小時前
    if rsi_now > 50 and rsi_now > rsi_prev:
        bull.append('rsi_mom')
        details.append(f"RSI {rsi_now:.0f} 站上 50 且上升")
    elif rsi_now < 50 and rsi_now < rsi_prev:
        bear.append('rsi_mom')
        details.append(f"RSI {rsi_now:.0f} 跌破 50 且下降")

    # 6. 布林帶位置：突破上軌（多頭動能）
    #    KOL（BTC飛揚）：地板不追空 → 移除跌破下軌的空頭信號
    bm  = c.rolling(20).mean()
    bstd = c.rolling(20).std()
    bb_upper = bm + 2 * bstd
    bb_lower = bm - 2 * bstd
    bb_pct   = float((c.iloc[-1] - bb_lower.iloc[-1]) / (bb_upper.iloc[-1] - bb_lower.iloc[-1] + 1e-9))
    bb_mid_val = float(bm.iloc[-1])
    price = float(c.iloc[-1])
    if bb_pct > 0.85:
        bull.append('bb')
        details.append(f"突破布林上軌（bb_pct={bb_pct:.2f}）")

    # 7. bb_mid 支阻互換（BTC歐陽）：
    #    前一根收盤在中軌下方 + 現在反抽到中軌附近 (±0.3%) = 壓力位做空
    prev_close = float(c.iloc[-2])
    if prev_close < bb_mid_val and abs(price - bb_mid_val) / (bb_mid_val + 1e-9) < 0.003:
        bear.append('bb_mid_flip')
        details.append(f"反抽布林中軌（支阻互換）mid={bb_mid_val:,.0f}")

    # 8. 日線 EMA200 牛熊分界（加密龐克）
    if ema200_daily is not None:
        if price > ema200_daily:
            bull.append('ema200')
            details.append(f"站上 200 日均線 EMA200={ema200_daily:,.0f}")
        else:
            bear.append('ema200')
            details.append(f"跌破 200 日均線 EMA200={ema200_daily:,.0f}")

    # 9. 嘎空燃料強化（加密龐克）：接近 EMA200 + 資費轉負 = 組合多頭訊號
    #    不重複計算（若 signal 4 已加 funding 則跳過）
    if (ema200_daily is not None and fr_now is not None
            and price > ema200_daily * 0.97
            and fr_now < -0.0001
            and 'funding' not in bull):
        bull.append('squeeze_fuel')
        details.append(f"嘎空燃料：近 EMA200 且資費轉負 {fr_now*100:+.4f}%")

    # 10. 多頭過熱過濾（加密龐克）：資費 > 0.05% = 假突破風險，削減多頭信心
    if fr_now is not None and fr_now > 0.0005:
        bear.append('fr_overheat')
        details.append(f"多頭資費過熱 {fr_now*100:+.4f}%，假突破風險")

    # 多數決方向
    if len(bull) > len(bear):
        direction, aligned = 1, bull
    elif len(bear) > len(bull):
        direction, aligned = -1, bear
    else:
        direction, aligned = 1, bull  # 平手預設多

    rsi   = float(_rsi(c, 14).iloc[-1])
    trend = 1 if price > float(ema50.iloc[-1]) else -1

    return {
        'symbol':    symbol,
        'price':     price,
        'signals':   aligned,
        'details':   details,
        'n':         len(aligned),
        'direction': direction,
        'rsi':       rsi,
        'trend':     trend,
        'ema200':    ema200_daily,
        'weekend':   now8().weekday() >= 5,   # BTC飛揚：週末量能萎縮
    }


def analyze_dispatch(exchange, symbol):
    """主流幣用 analyze_major，山寨幣用 analyze。"""
    if symbol in set(WATCH_ALWAYS):
        return analyze_major(exchange, symbol)
    return analyze(exchange, symbol)


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
        is_major  = symbol in set(WATCH_ALWAYS)
        lev       = MAJOR_LEVERAGE  if is_major else LEVERAGE
        sl_pct    = MAJOR_SL_PCT    if is_major else STOP_LOSS_PCT
        tp_pct    = MAJOR_TP_PCT    if is_major else TP_PCT
        coin_type = '主流' if is_major else '山寨'

        exchange.set_margin_mode('isolated', symbol)
        exchange.set_leverage(lev, symbol, params={'marginMode': 'isolated'})
        ref_price = float(exchange.fetch_ticker(symbol)['last'])
        margin    = MARGIN_BY_SIGNALS.get(n_signals, MARGIN_USDT)
        amount    = round(margin * lev / ref_price, 4)
        sl_side   = 'sell' if direction == 1 else 'buy'

        price = _enter_position(exchange, symbol, direction, amount)

        sl_id = tp_id = None

        # 固定止損（STOP_MARKET）
        try:
            sl_price = round(
                price * (1 - sl_pct) if direction == 1 else price * (1 + sl_pct), 8
            )
            sl_order = exchange.create_order(symbol, 'stop_market', sl_side, amount, None, {
                'stopPrice': sl_price, 'closePosition': True, 'workingType': 'MARK_PRICE',
            })
            sl_id = sl_order['id']
            print(f"  ✅ 固定止損 {sl_pct*100:.1f}% @ {sl_price:.6g} 已掛")
        except Exception as e:
            print(f"  ❌ 止損訂單失敗 {symbol}: {e}")

        # 固定止盈天花板（TAKE_PROFIT_MARKET）
        try:
            tp_price = round(
                price * (1 + tp_pct) if direction == 1 else price * (1 - tp_pct), 4
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
        sl_label = f"止損 {sl_pct*100:.1f}% {'✅' if sl_id else '⚠️'}"
        tp_label = f"止盈天花板 {tp_pct*100:.0f}% {'✅' if tp_id else '⚠️'}"
        print(f"  ✅ 開倉 {side} {coin} | {amount} @ {price:.4f} | ${margin}×{lev}x ({n_signals}信號) | {sl_label} {tp_label}")
        tg(
            f"{'🟢' if direction==1 else '🔴'} <b>開倉 {side} {coin}</b>\n"
            f"進場：{price:.4f} USDT\n"
            f"數量：{amount}  保證金：${margin}×{lev}x 逐倉（{coin_type}，{n_signals} 信號）\n"
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
_bias_cache   = {'bias': 0, 'reason': '初始化', 'ts': 0}
_btc_kol_cache = {'fake_breakout': False, 'squeeze_fuel': False, 'ema200': 0, 'fr_raw': 0, 'ts': 0}

def get_btc_kol_gate(exchange_pub):
    """
    每小時更新 BTC EMA200 狀態，作為山寨幣開倉的市場結構門檻。
    fake_breakout: BTC 資費過熱 + 緊貼 EMA200 → 全市場 LONG 降溫（避免假突破進場）
    squeeze_fuel:  BTC 資費轉負 + 接近 EMA200  → 市場嘎空動能建立，LONG 有利
    """
    global _btc_kol_cache
    if time.time() - _btc_kol_cache['ts'] < 3600:
        return _btc_kol_cache
    try:
        raw    = exchange_pub.fetch_ohlcv('BTC/USDT', '1d', limit=210)
        daily_c = pd.DataFrame(raw, columns=['ts','open','high','low','close','volume'])['close']
        ema200  = float(daily_c.ewm(span=200, adjust=False).mean().iloc[-1])
        fr_data = exchange_pub.fetch_funding_rate('BTC/USDT:USDT')
        fr_raw  = float(fr_data.get('fundingRate', 0) or 0)
        btc_px  = float(exchange_pub.fetch_ticker('BTC/USDT')['last'])
        fake_breakout = bool(fr_raw > 0.0005 and ema200 * 0.995 <= btc_px <= ema200 * 1.01)
        squeeze_fuel  = bool(btc_px > ema200 * 0.98 and fr_raw < -0.0001)
        _btc_kol_cache.update({
            'fake_breakout': fake_breakout, 'squeeze_fuel': squeeze_fuel,
            'ema200': ema200, 'fr_raw': fr_raw, 'ts': time.time(),
        })
        label = '⚠️ 假突破' if fake_breakout else ('🔥 嘎空燃料' if squeeze_fuel else '—')
        print(f"  📡 BTC KOL：EMA200={ema200:,.0f}  fr={fr_raw:.5f}  {label}")
    except Exception as e:
        print(f"  BTC KOL gate error: {e}")
    return _btc_kol_cache

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
        result = analyze_dispatch(exchange_pub, symbol)
        if result is None:
            continue
        if lb_direction == 1 and result.get('rsi', 50) >= 80:
            continue  # RSI 超買，跳過做多
        if lb_direction == -1 and result.get('rsi', 50) <= 20:
            continue  # RSI 超賣，跳過做空
        if lb_direction != result.get('trend', lb_direction):
            continue  # 方向不符 EMA50 趨勢
        if lb_direction == 1 and get_btc_kol_gate(exchange_pub).get('fake_breakout'):
            print(f"  ⚠️ KOL: BTC 假突破風險，跳過 {symbol.split('/')[0]} 漲幅榜做多")
            continue
        if lb_direction == 1 and SHORT_BIAS and result['n'] < MIN_LEADERBOARD_SIGNALS + 1:
            print(f"  📉 SHORT_BIAS: {symbol.split('/')[0]} 漲幅榜 LONG 信號不足（需 {MIN_LEADERBOARD_SIGNALS+1}，有 {result['n']}），跳過")
            continue
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
        result = analyze_dispatch(exchange_pub, symbol)
        # BTC飛揚：週末量能萎縮，主流幣門檻提高 1
        # 2026-05-25 KOL SHORT_BIAS：三方共識大級別偏空，LONG 需額外 +1 信號
        min_sig = MIN_SIGNALS + (1 if result and result.get('weekend') else 0)
        if result and result['n'] >= min_sig:
            d = result['direction']
            if d == 1 and SHORT_BIAS and result['n'] < min_sig + 1:
                print(f"  📉 SHORT_BIAS: {symbol.split('/')[0]} LONG 信號不足（需 {min_sig+1}，有 {result['n']}），跳過")
                continue
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
            if d == 1 and get_btc_kol_gate(exchange_pub).get('fake_breakout'):
                print(f"  ⚠️ KOL: BTC 假突破風險，跳過 {symbol.split('/')[0]} 做多")
                continue
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
    print(f"  山寨: ${MARGIN_USDT}×{LEVERAGE}x  SL {STOP_LOSS_PCT:.1%}  TP {TP_PCT:.0%}")
    print(f"  主流: ${MARGIN_USDT}×{MAJOR_LEVERAGE}x  SL {MAJOR_SL_PCT:.1%}  TP {MAJOR_TP_PCT:.0%}  備援 {TRAILING_PCT:.0%}")
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
