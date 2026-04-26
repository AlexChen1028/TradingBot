"""
Shared data fetching, feature engineering, and sample weighting.
Used by train.py and backtest.py.

Feature set (40 total):
  BTC technical  : 25
  US market      :  5
  F&G sentiment  :  2
  Funding rate   :  4  ← NEW
  News sentiment :  2  ← NEW
  Trend corr     :  2  ← NEW
"""

import time
import warnings
import requests
import feedparser
import numpy as np
import pandas as pd
import ccxt
import yfinance as yf
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

warnings.filterwarnings('ignore')

# ── Constants ─────────────────────────────────────────────────────────────────
FEATURE_COLS = [
    # BTC momentum
    'returns', 'log_returns', 'ret_4h', 'ret_8h', 'ret_24h',
    # volume
    'volume_change', 'volume_ratio',
    # trend
    'ema9_ratio', 'ema21_ratio', 'ema50_ratio',
    # oscillators
    'rsi', 'macd_hist_norm',
    # volatility / bands
    'bb_pos', 'bb_width', 'atr_norm', 'hl_ratio', 'realized_vol',
    # candle body
    'body_ratio', 'upper_shadow', 'lower_shadow',
    # market structure
    'obv_norm',
    # time seasonality
    'hour_sin', 'hour_cos', 'dow_sin', 'dow_cos',
    # US market
    'spy_ret', 'qqq_ret', 'vix_norm', 'gld_ret', 'us_session',
    # Fear & Greed
    'fng_norm', 'fng_mom',
    # Funding rate  ← NEW
    'fr_norm', 'fr_z', 'fr_ma', 'fr_cumsum',
    # News sentiment  ← NEW
    'news_sent', 'news_sent_ma',
    # Sentiment-trend correlation  ← NEW
    'fr_trend_corr', 'sent_trend_corr',
]  # 40 features

TARGET_AHEAD = 6

# RSS feeds to scrape for news sentiment
RSS_FEEDS = [
    'https://www.coindesk.com/arc/outboundfeeds/rss/',
    'https://cointelegraph.com/rss',
    'https://bitcoinmagazine.com/.rss/full/',
    'https://decrypt.co/feed',
]


# ── 1. BTC OHLCV ──────────────────────────────────────────────────────────────
def fetch_btc(symbol='BTC/USDT', timeframe='1h',
              since_iso='2017-09-01T00:00:00Z') -> pd.DataFrame:
    exchange = ccxt.binance({'enableRateLimit': True})
    since    = exchange.parse8601(since_iso)
    now      = exchange.milliseconds()
    rows     = []

    tag = symbol.split('/')[0]
    print(f"[{tag}] Fetching from {since_iso[:10]} ...")
    while since < now:
        batch = exchange.fetch_ohlcv(symbol, timeframe, since=since, limit=500)
        if not batch:
            break
        rows.extend(batch)
        since = batch[-1][0] + 1
        pct   = min(100, (batch[-1][0] - exchange.parse8601(since_iso)) /
                         (now - exchange.parse8601(since_iso)) * 100)
        print(f"  {len(rows):,} candles  ({pct:.1f}%)", end='\r')
        if len(batch) < 500:
            break
        time.sleep(exchange.rateLimit / 1000)

    print(f"\n[{tag}] Done: {len(rows):,} candles")
    df = pd.DataFrame(rows, columns=['ts', 'open', 'high', 'low', 'close', 'volume'])
    df['ts'] = pd.to_datetime(df['ts'], unit='ms').dt.tz_localize(None)
    return df.reset_index(drop=True)


# ── 2. US market (yfinance) ───────────────────────────────────────────────────
def fetch_us_market(start='2017-09-01') -> pd.DataFrame:
    tickers = {'SPY': 'spy', 'QQQ': 'qqq', '^VIX': 'vix', 'GLD': 'gld'}
    print("[Market] Fetching SPY / QQQ / VIX / GLD ...")
    parts = {}
    for ticker, col in tickers.items():
        raw = yf.download(ticker, start=start, interval='1d',
                          auto_adjust=True, progress=False)
        if raw.empty:
            continue
        close = raw['Close']
        if isinstance(close, pd.DataFrame):
            close = close.iloc[:, 0]
        parts[col] = close.squeeze()

    mkt = pd.DataFrame(parts)
    mkt.index = pd.to_datetime(mkt.index).tz_localize(None).normalize()
    mkt['spy_ret'] = np.log(mkt['spy'] / mkt['spy'].shift(1))
    mkt['qqq_ret'] = np.log(mkt['qqq'] / mkt['qqq'].shift(1))
    mkt['gld_ret'] = np.log(mkt['gld'] / mkt['gld'].shift(1))
    mkt['vix_norm'] = mkt['vix'] / 100
    print(f"[Market] Done: {len(mkt)} trading days")
    return mkt[['spy_ret', 'qqq_ret', 'vix_norm', 'gld_ret']]


# ── 3. Fear & Greed ───────────────────────────────────────────────────────────
def fetch_fear_greed() -> pd.DataFrame:
    print("[F&G] Fetching Fear & Greed Index ...")
    resp = requests.get('https://api.alternative.me/fng/?limit=0&format=json',
                        timeout=30)
    resp.raise_for_status()
    raw  = resp.json()['data']
    df   = pd.DataFrame(raw)[['timestamp', 'value']]
    df['ts']  = pd.to_datetime(df['timestamp'].astype(int), unit='s').dt.normalize()
    df['fng'] = df['value'].astype(float)
    df = df[['ts', 'fng']].sort_values('ts').set_index('ts')
    df = df[~df.index.duplicated(keep='last')]
    print(f"[F&G] Done: {len(df)} days ({df.index[0].date()} ~ {df.index[-1].date()})")
    return df


# ── 4. Funding Rate ───────────────────────────────────────────────────────────
def fetch_funding_rate(symbol='BTC/USDT:USDT',
                       since_iso='2019-09-13T00:00:00Z') -> pd.DataFrame:
    """
    Fetch BTC perpetual futures funding rate history from Binance.
    Funding is settled every 8h (00:00 / 08:00 / 16:00 UTC).
    Positive rate  → longs pay shorts (market is over-leveraged long, bearish signal).
    Negative rate  → shorts pay longs (market is over-leveraged short, bullish signal).
    """
    exchange = ccxt.binance({
        'enableRateLimit': True,
        'options': {'defaultType': 'future'},
    })
    since    = exchange.parse8601(since_iso)
    now      = exchange.milliseconds()
    records  = []

    print(f"[FundingRate] Fetching from {since_iso[:10]} ...")
    while since < now:
        try:
            batch = exchange.fetch_funding_rate_history(symbol, since=since, limit=500)
        except Exception as e:
            print(f"  Warning: {e}")
            break
        if not batch:
            break
        records.extend(batch)
        since = batch[-1]['timestamp'] + 1
        print(f"  {len(records):,} records", end='\r')
        if len(batch) < 500:
            break
        time.sleep(0.3)

    if not records:
        print("[FundingRate] No data — using zeros")
        return pd.DataFrame(columns=['ts', 'funding_rate'])

    df = pd.DataFrame([{
        'ts':           pd.Timestamp(r['timestamp'], unit='ms').tz_localize(None),
        'funding_rate': float(r['fundingRate']),
    } for r in records])
    df = df.sort_values('ts').reset_index(drop=True)
    print(f"\n[FundingRate] Done: {len(df):,} records "
          f"({df['ts'].iloc[0].date()} ~ {df['ts'].iloc[-1].date()})")
    return df


# ── 5. News RSS scraper + VADER ───────────────────────────────────────────────
def fetch_news_sentiment() -> pd.DataFrame:
    """
    Scrape crypto news RSS feeds and compute daily VADER compound sentiment.
    Returns DataFrame indexed by UTC-midnight date with column 'news_sent' (-1..1).
    RSS feeds typically cover the last 30-90 days.
    For historical training we fall back to F&G; this provides recent signal.
    """
    analyzer = SentimentIntensityAnalyzer()
    articles = []

    print("[News] Scraping RSS feeds ...")
    for url in RSS_FEEDS:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries:
                # parse publish date
                pub = entry.get('published_parsed') or entry.get('updated_parsed')
                if pub is None:
                    continue
                ts   = pd.Timestamp(*pub[:6]).normalize()
                text = f"{entry.get('title', '')} {entry.get('summary', '')[:300]}"
                score = analyzer.polarity_scores(text)['compound']
                articles.append({'ts': ts, 'score': score})
        except Exception as e:
            print(f"  Warning ({url}): {e}")

    if not articles:
        print("[News] No articles scraped — returning empty")
        return pd.DataFrame(columns=['news_sent'])

    df   = pd.DataFrame(articles).dropna()
    daily = (df.groupby('ts')['score'].mean()
               .rename('news_sent')
               .sort_index()
               .to_frame())
    daily = daily[~daily.index.duplicated(keep='last')]
    print(f"[News] Done: {len(daily)} days of sentiment "
          f"({daily.index[0].date()} ~ {daily.index[-1].date()})")
    return daily


# ── 6. Merge all daily sources to hourly ─────────────────────────────────────
def merge_context(btc: pd.DataFrame,
                  mkt: pd.DataFrame,
                  fng: pd.DataFrame,
                  fr:  pd.DataFrame,
                  news: pd.DataFrame) -> pd.DataFrame:
    """Forward-fill all daily/8h series onto hourly BTC timestamps."""
    df  = btc.copy().set_index('ts')
    idx = df.index

    def daily_to_hourly(src: pd.DataFrame) -> pd.DataFrame:
        src = src.copy()
        src = src[~src.index.duplicated(keep='last')]
        combined = src.reindex(src.index.union(idx)).ffill()
        return combined.reindex(idx)

    def event_to_hourly(src: pd.DataFrame, col: str) -> pd.Series:
        """For 8h event data (funding rate): forward-fill to hourly."""
        if src.empty:
            return pd.Series(np.nan, index=idx, name=col)
        src2 = src.set_index('ts')[[col]]
        src2 = src2[~src2.index.duplicated(keep='last')]
        combined = src2.reindex(src2.index.union(idx)).ffill()
        return combined.reindex(idx)[col]

    df = pd.concat([
        df,
        daily_to_hourly(mkt),
        daily_to_hourly(fng),
        daily_to_hourly(news) if not news.empty else pd.DataFrame(index=idx),
    ], axis=1)

    df['funding_rate_raw'] = event_to_hourly(fr, 'funding_rate')

    return df.reset_index().rename(columns={'index': 'ts'})


# ── 7. Feature engineering ────────────────────────────────────────────────────
def add_features(df: pd.DataFrame) -> pd.DataFrame:
    c, o, h, l = df['close'], df['open'], df['high'], df['low']

    # ── BTC technical ──────────────────────────────────────────────────────
    df['returns']     = c.pct_change()
    df['log_returns'] = np.log(c / c.shift(1))
    df['ret_4h']      = np.log(c / c.shift(4))
    df['ret_8h']      = np.log(c / c.shift(8))
    df['ret_24h']     = np.log(c / c.shift(24))
    df['volume_change'] = df['volume'].pct_change()
    df['volume_ratio']  = df['volume'] / df['volume'].rolling(20).mean()

    for span, col in [(9, 'ema9'), (21, 'ema21'), (50, 'ema50')]:
        df[f'{col}_ratio'] = c / c.ewm(span=span, adjust=False).mean() - 1

    delta = c.diff()
    gain  = delta.clip(lower=0).ewm(com=13, adjust=False).mean()
    loss  = (-delta.clip(upper=0)).ewm(com=13, adjust=False).mean()
    df['rsi'] = (100 - 100 / (1 + gain / loss)) / 100

    macd = c.ewm(span=12, adjust=False).mean() - c.ewm(span=26, adjust=False).mean()
    df['macd_hist_norm'] = (macd - macd.ewm(span=9, adjust=False).mean()) / c

    bm  = c.rolling(20).mean()
    bs  = c.rolling(20).std()
    bup, blo = bm + 2*bs, bm - 2*bs
    df['bb_pos']   = (c - blo) / (bup - blo + 1e-9)
    df['bb_width'] = (bup - blo) / bm

    tr = pd.concat([h - l, (h - c.shift(1)).abs(),
                    (l - c.shift(1)).abs()], axis=1).max(axis=1)
    df['atr_norm']     = tr.rolling(14).mean() / c
    df['hl_ratio']     = (h - l) / c
    df['realized_vol'] = df['log_returns'].rolling(24).std()

    df['body_ratio']   = (c - o) / (h - l + 1e-9)
    df['upper_shadow'] = (h - c.clip(lower=o))  / (h - l + 1e-9)
    df['lower_shadow'] = (c.clip(upper=o) - l)  / (h - l + 1e-9)

    obv = (np.sign(c.diff()) * df['volume']).cumsum()
    df['obv_norm'] = (obv - obv.rolling(50).mean()) / (obv.rolling(50).std() + 1e-9)

    hour = df['ts'].dt.hour
    dow  = df['ts'].dt.dayofweek
    df['hour_sin'] = np.sin(2 * np.pi * hour / 24)
    df['hour_cos'] = np.cos(2 * np.pi * hour / 24)
    df['dow_sin']  = np.sin(2 * np.pi * dow  / 7)
    df['dow_cos']  = np.cos(2 * np.pi * dow  / 7)

    # ── US market ──────────────────────────────────────────────────────────
    df['us_session'] = ((hour >= 13) & (hour < 20)).astype(float)

    # ── F&G ───────────────────────────────────────────────────────────────
    df['fng_norm'] = df['fng'] / 100
    df['fng_mom']  = df['fng_norm'].diff().fillna(0)

    # ── Funding rate features ──────────────────────────────────────────────
    fr_raw = df['funding_rate_raw'].fillna(0)      # 0 = neutral before perps
    df['fr_norm']   = fr_raw * 1000                # scale: ~-1..1 typical range
    # 7-day (21 periods of 8h) rolling z-score → how extreme is current FR?
    fr_roll_mean = fr_raw.rolling(21).mean()
    fr_roll_std  = fr_raw.rolling(21).std().clip(lower=1e-9)
    df['fr_z']    = (fr_raw - fr_roll_mean) / fr_roll_std
    # 24h MA (3 periods of 8h)
    df['fr_ma']   = fr_raw.rolling(3).mean() * 1000
    # 28-day cumulative funding cost (positive = longs paid out over period)
    df['fr_cumsum'] = fr_raw.rolling(84).sum() * 1000   # 84×8h = 28d

    # ── News sentiment features ────────────────────────────────────────────
    if 'news_sent' not in df.columns:
        df['news_sent'] = np.nan
    # For periods without scraped news, blend F&G as proxy (rescale -1..1)
    fng_proxy = (df['fng_norm'] - 0.5) * 2            # 0-1 → -1..1
    df['news_sent'] = df['news_sent'].fillna(fng_proxy)
    df['news_sent_ma'] = df['news_sent'].rolling(24).mean().fillna(df['news_sent'])

    # ── Trend correlation features ─────────────────────────────────────────
    # "trend" = 1 when price is above EMA21 (uptrend), -1 otherwise
    ema21       = c.ewm(span=21, adjust=False).mean()
    trend       = np.where(c > ema21, 1.0, -1.0)

    window = 168  # 7-day rolling window

    # corr(funding_rate, trend): positive → FR tracks trend (momentum regime)
    #                            negative → FR leads reversals (contrarian regime)
    df['fr_trend_corr'] = (
        pd.Series(fr_raw.values, index=df.index)
        .rolling(window)
        .corr(pd.Series(trend, index=df.index))
        .fillna(0)
    )

    # corr(news_sentiment, trend): same interpretation for news
    df['sent_trend_corr'] = (
        df['news_sent']
        .rolling(window)
        .corr(pd.Series(trend, index=df.index))
        .fillna(0)
    )

    # ── Target ────────────────────────────────────────────────────────────
    df['target'] = (c.shift(-TARGET_AHEAD) > c).astype(float)
    df.loc[df.index[-TARGET_AHEAD:], 'target'] = np.nan

    return df


# ── 8. Sample weights ─────────────────────────────────────────────────────────
def compute_sample_weights(df: pd.DataFrame, alpha: float = 1.5) -> np.ndarray:
    """
    Upweight training samples where sentiment signals are at extremes.

    Rationale: extreme funding rates and extreme F&G (fear/greed) carry
    stronger predictive information than neutral periods, so we want the
    model to learn those regimes more carefully.

    weight_i = 1 + alpha * sentiment_strength_i
    where sentiment_strength ∈ [0, 1]:
      - 0  = all signals neutral
      - 1  = all signals at maximum extreme
    """
    # |fr_z| clipped to [0, 3], normalised to [0, 1]
    fr_signal  = df['fr_z'].abs().clip(0, 3).fillna(0) / 3

    # F&G distance from neutral (0.5): 0=neutral, 1=extreme fear/greed
    fng_signal = ((df['fng_norm'] - 0.5).abs() * 2).clip(0, 1).fillna(0)

    # News sentiment absolute value
    news_signal = df['news_sent'].abs().clip(0, 1).fillna(0)

    strength = (fr_signal + fng_signal + news_signal) / 3   # average, 0-1
    weights  = 1.0 + alpha * strength
    return weights.values.astype(np.float32)
