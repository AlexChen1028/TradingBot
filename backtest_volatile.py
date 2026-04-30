"""
爆量追蹤策略回測 — 針對前 20 名莊家幣
策略：成交量突然爆量 + 價格突破近期高點 → 進場做多
出場：止損 / 追蹤止盈 / 持倉超時

用法：
  docker exec crypto-bot-trading-bot-1 python backtest_volatile.py
"""

import time
import ccxt
import numpy as np
import pandas as pd
from datetime import datetime, timedelta

TOP_COINS = [
    'AIOT/USDT:USDT', 'SIREN/USDT:USDT', 'FHE/USDT:USDT',  'LAB/USDT:USDT',
    'TNSR/USDT:USDT', 'BLESS/USDT:USDT', 'LIGHT/USDT:USDT', 'JELLYJELLY/USDT:USDT',
    'ORDI/USDT:USDT', 'UAI/USDT:USDT',   'TAKE/USDT:USDT',  'AKE/USDT:USDT',
    'ARIA/USDT:USDT', 'BULLA/USDT:USDT', 'SAPIEN/USDT:USDT','PIEVERSE/USDT:USDT',
    'BOB/USDT:USDT',  'STO/USDT:USDT',   'PIPPIN/USDT:USDT','ON/USDT:USDT',
]

# ── 策略參數 ──────────────────────────────────────────────────────────────────
VOL_WINDOW      = 20    # 均量計算窗口（天）
VOL_THRESHOLD   = 5.0   # 爆量倍數門檻
BREAKOUT_WINDOW = 20    # 突破近幾天高點
STOP_LOSS_PCT   = 0.05  # 止損 5%
TRAILING_STOP   = 0.10  # 追蹤止盈（從最高點回落 10%）
MAX_HOLD_DAYS   = 7     # 最多持倉天數
FEE_PCT         = 0.0005  # 單邊手續費 0.05%（taker）
LOOKBACK_DAYS   = 180

def fetch_ohlcv(exchange, symbol):
    since = int((datetime.utcnow() - timedelta(days=LOOKBACK_DAYS + 5)).timestamp() * 1000)
    try:
        raw = exchange.fetch_ohlcv(symbol, '1h', since=since, limit=(LOOKBACK_DAYS + 5) * 24)
        if not raw or len(raw) < 200:
            return None
        df = pd.DataFrame(raw, columns=['ts', 'open', 'high', 'low', 'close', 'volume'])
        df['ts'] = pd.to_datetime(df['ts'], unit='ms')
        return df.reset_index(drop=True)
    except Exception:
        return None

def backtest(df):
    df = df.copy()
    window_h = VOL_WINDOW * 24

    df['vol_ma']    = df['volume'].rolling(window_h).mean()
    df['vol_ratio'] = df['volume'] / df['vol_ma']
    df['high_max']  = df['high'].rolling(BREAKOUT_WINDOW * 24).max().shift(1)

    trades = []
    in_pos = False
    entry_price = peak_price = entry_idx = 0

    for i in range(window_h + 1, len(df)):
        row = df.iloc[i]

        if not in_pos:
            if (row['vol_ratio'] >= VOL_THRESHOLD
                    and row['close'] > row['high_max']
                    and row['vol_ma'] > 0):
                in_pos = True
                entry_price = peak_price = row['close']
                entry_idx = i
        else:
            peak_price = max(peak_price, row['high'])
            held_h = i - entry_idx

            sl       = row['close'] < entry_price * (1 - STOP_LOSS_PCT)
            trail    = row['close'] < peak_price  * (1 - TRAILING_STOP)
            timeout  = held_h >= MAX_HOLD_DAYS * 24

            if sl or trail or timeout:
                ret    = (row['close'] / entry_price - 1) - FEE_PCT * 2
                reason = 'stop_loss' if sl else ('trailing' if trail else 'timeout')
                trades.append({
                    'entry': df.iloc[entry_idx]['ts'],
                    'exit':  row['ts'],
                    'ret':   ret,
                    'held_h': held_h,
                    'reason': reason,
                })
                in_pos = False

    if len(trades) < 2:
        return None

    t = pd.DataFrame(trades)
    eq = (1 + t['ret']).cumprod()

    total_ret = eq.iloc[-1] - 1
    win_rate  = (t['ret'] > 0).mean()
    n         = len(t)
    max_dd    = ((eq - eq.cummax()) / eq.cummax()).min()
    avg_win   = t[t['ret'] > 0]['ret'].mean() if (t['ret'] > 0).any() else 0
    avg_loss  = t[t['ret'] < 0]['ret'].mean() if (t['ret'] < 0).any() else 0
    avg_held  = t['held_h'].mean()

    # 夏普比率：以平均持倉時間換算年化因子，無風險利率設 0
    ann_factor = np.sqrt(365 * 24 / avg_held) if avg_held > 0 else 1.0
    sharpe     = t['ret'].mean() / t['ret'].std() * ann_factor if t['ret'].std() > 0 else 0.0

    return {
        'n_trades':     n,
        'win_rate':     win_rate,
        'total_return': total_ret,
        'max_dd':       max_dd,
        'sharpe':       sharpe,
        'avg_win':      avg_win,
        'avg_loss':     avg_loss,
        'avg_held_h':   avg_held,
    }

def main():
    exchange = ccxt.binance({'enableRateLimit': True})

    print("=" * 90)
    print("  爆量追蹤策略回測（過去 180 天，1h K 線）")
    print(f"  進場：成交量 > {VOL_THRESHOLD}x 均量 + 突破 {BREAKOUT_WINDOW} 日高點")
    print(f"  出場：止損 {STOP_LOSS_PCT:.0%} | 追蹤止盈 {TRAILING_STOP:.0%} | 超時 {MAX_HOLD_DAYS} 天")
    print("=" * 90)

    results = []
    for idx, symbol in enumerate(TOP_COINS, 1):
        print(f"[{idx:02d}/{len(TOP_COINS)}] {symbol:<30}", end=' ', flush=True)
        df = fetch_ohlcv(exchange, symbol)
        if df is None:
            print("資料不足，跳過")
            continue
        res = backtest(df)
        if res is None:
            print("交易次數不足，跳過")
            continue
        res['symbol'] = symbol
        results.append(res)
        print(
            f"總報酬 {res['total_return']:+.1%}  "
            f"勝率 {res['win_rate']:.0%}  "
            f"次數 {res['n_trades']}  "
            f"夏普 {res['sharpe']:+.2f}  "
            f"最大回撤 {res['max_dd']:.1%}"
        )
        time.sleep(0.3)

    if not results:
        print("\n沒有可用結果。")
        return

    df_r = (pd.DataFrame(results)
            .sort_values('total_return', ascending=False)
            .reset_index(drop=True))

    print(f"\n{'='*105}")
    print("  最終排名（依總報酬）")
    print(f"{'='*105}")
    print(f"  {'幣種':<25} {'總報酬':>8} {'勝率':>6} {'次數':>5} {'夏普':>6} {'最大回撤':>8} {'平均獲利':>8} {'平均虧損':>8} {'均持倉h':>7}")
    print(f"  {'-'*98}")
    for _, row in df_r.iterrows():
        print(
            f"  {row['symbol']:<25} "
            f"{row['total_return']:>+8.1%} "
            f"{row['win_rate']:>6.0%} "
            f"{int(row['n_trades']):>5} "
            f"{row['sharpe']:>+6.2f} "
            f"{row['max_dd']:>8.1%} "
            f"{row['avg_win']:>+8.1%} "
            f"{row['avg_loss']:>+8.1%} "
            f"{row['avg_held_h']:>7.1f}"
        )

    out = 'backtest_volatile.csv'
    df_r.to_csv(out, index=False)
    print(f"\n結果已儲存到 {out}")

if __name__ == '__main__':
    main()
