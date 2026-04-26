"""
Binance Futures 波動幣種掃描器
掃描過去 180 天內有過大波動（疑似被洗盤）的 USDT 永續合約幣種

用法：
  docker exec crypto-bot-trading-bot-1 python scan_coins.py
"""

import time
import ccxt
import numpy as np
import pandas as pd
from datetime import datetime, timedelta

LOOKBACK_DAYS   = 180     # 掃描幾天
MIN_DAILY_MOVE  = 0.15    # 單日漲跌門檻（15%）
MIN_VOL_USDT    = 1_000_000  # 最低日均成交量（1M USDT，過濾垃圾幣）
TOP_N           = 20      # 顯示前幾名

def fetch_daily(exchange, symbol, days):
    since = int((datetime.utcnow() - timedelta(days=days + 5)).timestamp() * 1000)
    try:
        raw = exchange.fetch_ohlcv(symbol, '1d', since=since, limit=days + 5)
        if not raw or len(raw) < 30:
            return None
        df = pd.DataFrame(raw, columns=['ts', 'open', 'high', 'low', 'close', 'volume'])
        df['ts'] = pd.to_datetime(df['ts'], unit='ms')
        return df.tail(days).reset_index(drop=True)
    except Exception:
        return None

def analyze(df):
    df = df.copy()
    df['ret']     = df['close'].pct_change()
    df['abs_ret'] = df['ret'].abs()

    max_daily_move = df['abs_ret'].max()
    big_move_days  = int((df['abs_ret'] > MIN_DAILY_MOVE).sum())
    max_pump       = df['ret'].max()
    max_dump       = df['ret'].min()

    # 成交量爆量倍數
    vol_mean  = df['volume'].mean()
    vol_spike = df['volume'].max() / vol_mean if vol_mean > 0 else 0

    # 已實現波動率（年化）
    realized_vol = df['ret'].std() * np.sqrt(365)

    # 最大回撤
    cum = (1 + df['ret'].fillna(0)).cumprod()
    dd  = (cum - cum.cummax()) / cum.cummax()
    max_dd = dd.min()

    # 日均成交額（USDT）
    avg_vol_usdt = (df['volume'] * df['close']).mean()

    return {
        'max_daily_move': max_daily_move,
        'big_move_days':  big_move_days,
        'vol_spike':      vol_spike,
        'realized_vol':   realized_vol,
        'max_dd':         max_dd,
        'max_pump':       max_pump,
        'max_dump':       max_dump,
        'avg_vol_usdt':   avg_vol_usdt,
    }

def main():
    exchange = ccxt.binance({'enableRateLimit': True})
    markets  = exchange.load_markets()

    # 只掃 USDT 永續合約
    symbols = sorted([
        s for s, m in markets.items()
        if m.get('quote') == 'USDT'
        and m.get('type') in ('swap', 'future')
        and m.get('active')
        and ':USDT' in s
    ])

    print(f"掃描 {len(symbols)} 個 USDT 永續合約，回溯 {LOOKBACK_DAYS} 天...")
    print("（這大約需要 2-5 分鐘）\n")

    results = []
    for i, symbol in enumerate(symbols, 1):
        df = fetch_daily(exchange, symbol, LOOKBACK_DAYS)
        if df is None:
            continue
        stats = analyze(df)
        if stats['avg_vol_usdt'] < MIN_VOL_USDT:
            continue
        stats['symbol'] = symbol
        results.append(stats)
        if i % 50 == 0:
            print(f"  進度：{i}/{len(symbols)}  已找到 {len(results)} 個候選幣種")
        time.sleep(0.08)

    if not results:
        print("沒有找到符合條件的幣種。")
        return

    df_res = pd.DataFrame(results)

    # 綜合評分（大波動 + 爆量 + 高波動率）
    df_res['score'] = (
        df_res['max_daily_move'].rank(pct=True) * 0.40 +
        df_res['big_move_days'].rank(pct=True)  * 0.30 +
        df_res['vol_spike'].rank(pct=True)       * 0.20 +
        df_res['realized_vol'].rank(pct=True)    * 0.10
    )

    top = df_res.nlargest(TOP_N, 'score').reset_index(drop=True)

    print(f"\n{'='*85}")
    print(f"  過去 {LOOKBACK_DAYS} 天波動最大前 {TOP_N} 名（疑似有莊家洗盤）")
    print(f"{'='*85}")
    print(f"{'#':>3}  {'幣種':<22} {'最大單日波動':>10} {'大波動天數':>9} {'爆量倍數':>9} {'最大拉盤':>8} {'最大砸盤':>8}")
    print(f"  {'-'*80}")
    for rank, row in top.iterrows():
        print(
            f"{rank+1:>3}  {row['symbol']:<22} "
            f"{row['max_daily_move']:>9.1%}  "
            f"{int(row['big_move_days']):>9}  "
            f"{row['vol_spike']:>8.1f}x  "
            f"{row['max_pump']:>7.1%}  "
            f"{row['max_dump']:>7.1%}"
        )

    out = 'volatile_coins.csv'
    top.to_csv(out, index=False)
    print(f"\n結果已儲存到 {out}")

if __name__ == '__main__':
    main()
