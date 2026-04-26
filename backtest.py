"""
Backtest the trained BTC model (with US market + sentiment features).

Usage:
    python backtest.py
    python backtest.py --since 2020-01-01
    python backtest.py --threshold 0.55
    python backtest.py --fee 0.0002
"""
import sys
sys.stdout.reconfigure(encoding='utf-8')

import argparse
import os
import numpy as np
import pandas as pd
import torch
import torch.nn as nn
import joblib
import matplotlib
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import matplotlib.gridspec as gridspec

from data import (fetch_btc, fetch_us_market, fetch_fear_greed,
                  fetch_funding_rate, fetch_news_sentiment,
                  merge_context, add_features, FEATURE_COLS)

matplotlib.rcParams['font.family'] = 'DejaVu Sans'

parser = argparse.ArgumentParser()
parser.add_argument('--since',     default='2017-09-01')
parser.add_argument('--threshold', type=float, default=0.50)
parser.add_argument('--fee',       type=float, default=0.0004)
parser.add_argument('--symbol',    default='BTC/USDT')
parser.add_argument('--min_hold',  type=int,   default=24,
                    help='Min bars to hold a position before switching (default 24h)')
parser.add_argument('--sizing',    type=str,   default='kelly',
                    choices=['fixed', 'kelly', 'half_kelly'],
                    help='Position sizing: fixed=binary ±1, kelly=full Kelly, half_kelly=half Kelly')
args = parser.parse_args()


# ── Model ─────────────────────────────────────────────────────────────────────
class TransformerPredictor(nn.Module):
    def __init__(self, n_features, d_model, nhead, num_layers, dropout, seq_len):
        super().__init__()
        self.input_proj = nn.Linear(n_features, d_model)
        self.pos_embed  = nn.Parameter(torch.zeros(1, seq_len, d_model))
        enc = nn.TransformerEncoderLayer(d_model=d_model, nhead=nhead,
                                         dim_feedforward=d_model*4,
                                         dropout=dropout, batch_first=True,
                                         norm_first=True)
        self.transformer = nn.TransformerEncoder(enc, num_layers=num_layers)
        self.norm = nn.LayerNorm(d_model)
        self.head = nn.Sequential(
            nn.Linear(d_model, d_model//2), nn.GELU(),
            nn.Dropout(dropout), nn.Linear(d_model//2, 1),
        )

    def forward(self, x):
        x      = self.input_proj(x) + self.pos_embed
        x      = self.transformer(x)
        x      = self.norm(x)
        pooled = (x.mean(1) + x[:, -1, :]) / 2
        return self.head(pooled).squeeze(-1)


# ── Load model ────────────────────────────────────────────────────────────────
def load_model():
    for p in ('btc_model.pt', 'btc_scaler.pkl'):
        if not os.path.exists(p):
            sys.exit(f"[ERROR] {p} not found — run train.py first.")
    ckpt  = torch.load('btc_model.pt', map_location='cpu', weights_only=False)
    cfg   = ckpt['config']
    model = TransformerPredictor(cfg['n_features'], cfg['d_model'], cfg['nhead'],
                                 cfg['num_layers'], cfg['dropout'], cfg['seq_len'])
    model.load_state_dict(ckpt['model_state'])
    model.eval()
    return model, joblib.load('btc_scaler.pkl'), cfg


# ── Batched inference ─────────────────────────────────────────────────────────
def predict(model, scaler, df, feature_cols, seq_len, batch_size=512):
    df_c  = df.replace([float('inf'), float('-inf')], float('nan')
                       ).dropna(subset=feature_cols).copy().reset_index(drop=True)
    X_raw = scaler.transform(df_c[feature_cols].values.astype('float32'))
    seqs  = np.stack([X_raw[i - seq_len:i] for i in range(seq_len, len(X_raw))])

    probs_list = []
    total = len(seqs)
    with torch.no_grad():
        for s in range(0, total, batch_size):
            b = torch.from_numpy(seqs[s:s + batch_size])
            probs_list.append(torch.sigmoid(model(b)).numpy())
            print(f"  Inference {min(s+batch_size, total):,}/{total:,}", end='\r')
    print()

    probs         = np.concatenate(probs_list)
    out           = np.full(len(df_c), float('nan'))
    out[seq_len:] = probs
    result        = np.full(len(df), float('nan'))
    result[df.index.isin(df_c.index)] = out
    return result


# ── Position sizing ───────────────────────────────────────────────────────────
def calc_size(prob: float, direction: int, sizing: str) -> float:
    """
    Scale position size based on model confidence.

    sizing='fixed'     → always ±1 (binary)
    sizing='kelly'     → full Kelly: f* = |2p-1|, range [0,1]
    sizing='half_kelly'→ half Kelly: f* = |2p-1|/2, range [0,0.5]

    The Kelly criterion for a binary 1:1 bet with win prob p is:
        f* = p - (1-p) = 2p - 1
    High confidence (p=0.7) → 40% of capital.
    Low  confidence (p=0.52) →  4% of capital.
    This naturally reduces exposure when the model is uncertain.
    """
    if direction == 0 or sizing == 'fixed':
        return float(direction)

    confidence = abs(prob - 0.5) * 2          # [0, 1] — 0 at 50%, 1 at 100%
    if sizing == 'kelly':
        size = confidence                      # full Kelly
    else:                                      # half_kelly
        size = confidence * 0.5
    return size * direction                    # signed


# ── Trade simulation ──────────────────────────────────────────────────────────
def simulate(prices, probs, timestamps, threshold, fee,
             mode='long_short', min_hold=24, sizing='kelly'):
    """
    min_hold : minimum bars to hold a direction before switching.
    sizing   : 'fixed' | 'kelly' | 'half_kelly'
               With Kelly sizing, position size ∝ model confidence,
               reducing risk when uncertain and amplifying high-conviction trades.
    """
    n        = len(prices)
    log_rets = np.concatenate([[0], np.log(prices[1:] / prices[:-1])])
    position = np.zeros(n, dtype=np.float64)   # float — allows fractional sizes
    trades   = 0
    prev_dir = 0        # direction: -1, 0, 1
    hold_cnt = 0

    for i in range(1, n):
        p = probs[i]
        if np.isnan(p):
            position[i] = calc_size(0.5, prev_dir, sizing)  # hold current
            hold_cnt += 1
            continue

        # Desired direction
        if p > threshold:
            d = 1
        elif p < (1 - threshold) and mode == 'long_short':
            d = -1
        else:
            d = 0

        # Enforce min_hold on direction change (not size change)
        if d != prev_dir and prev_dir != 0 and hold_cnt < min_hold:
            d = prev_dir          # stay in current direction

        if d != prev_dir:
            trades += 1
            hold_cnt = 0
        else:
            hold_cnt += 1

        position[i] = calc_size(p, d, sizing)
        prev_dir    = d

    ret  = position[:-1] * log_rets[1:]
    # Fee proportional to change in position size (covers partial re-sizing)
    chg  = np.abs(np.diff(np.concatenate([[0.0], position[:-1]])))
    ret -= chg * fee
    bah  = log_rets[1:]

    out = pd.DataFrame({'ts': timestamps[1:], 'price': prices[1:],
                        'prob': probs[1:], 'position': position[:-1],
                        'strat_ret': ret, 'bah_ret': bah})
    out['_trades'] = trades
    return out


# ── Metrics ───────────────────────────────────────────────────────────────────
HPY = 24 * 365

def calc_metrics(df, ret_col):
    r   = df[ret_col].values
    cum = np.exp(np.cumsum(r))
    tot = cum[-1] - 1
    ann = (1 + tot) ** (HPY / len(r)) - 1
    vol = r.std() * np.sqrt(HPY)
    shp = (ann - 0.04) / vol if vol > 0 else 0
    dd  = cum / np.maximum.accumulate(cum) - 1
    im  = df['position'].abs() > 1e-6          # any non-zero position
    wr  = (df.loc[im, 'strat_ret'] > 0).sum() / im.sum() if im.sum() else float('nan')
    avg_size = df.loc[im, 'position'].abs().mean() if im.sum() else float('nan')
    return dict(total=tot, ann=ann, sharpe=shp, max_dd=dd.min(),
                win_rate=wr, avg_size=avg_size,
                n_trades=int(df['_trades'].iloc[0]),
                drawdowns=dd, cum=cum)

def print_row(label, m):
    wr   = f"{m['win_rate']:.2%}"  if not np.isnan(m.get('win_rate', float('nan')))  else "   —  "
    avsz = f"{m['avg_size']:.3f}" if not np.isnan(m.get('avg_size', float('nan')))  else "  —   "
    print(f"  {label:<14} | {m['total']:>+8.2%} | {m['ann']:>+10.2%} | "
          f"{m['sharpe']:>7.2f} | {m['max_dd']:>8.2%} | {wr:>7} | {m['n_trades']:>7} | {avsz:>7}")


# ── Plot ──────────────────────────────────────────────────────────────────────
def plot(df, m_ls, m_lf, m_bah):
    fig = plt.figure(figsize=(18, 12))
    fig.patch.set_facecolor('#0d1117')
    gs  = gridspec.GridSpec(4, 1, figure=fig, hspace=0.05,
                            height_ratios=[2.5, 0.7, 1.5, 1.0])
    axes = [fig.add_subplot(gs[i]) for i in range(4)]
    for ax in axes:
        ax.set_facecolor('#0d1117')
        ax.tick_params(colors='#8b949e')
        ax.spines[:].set_color('#30363d')
        ax.yaxis.label.set_color('#8b949e')

    ax1, ax2, ax3, ax4 = axes
    ts = df['ts'].values

    ax1.plot(ts, df['price'], color='#58a6ff', lw=0.6)
    # Background shading: green=long, red=short, intensity = position size
    pos = df['position'].values
    for i in range(1, len(ts)):
        if pos[i-1] > 1e-6:
            ax1.axvspan(ts[i-1], ts[i], alpha=float(pos[i-1])*0.3, color='#3fb950', lw=0)
        elif pos[i-1] < -1e-6:
            ax1.axvspan(ts[i-1], ts[i], alpha=float(abs(pos[i-1]))*0.3, color='#f85149', lw=0)
    ax1.set_ylabel('Price (USDT)')
    ax1.set_title(f'BTC/USDT Backtest  |  Fee {args.fee*100:.3f}%  |  '
                  f'Min hold {args.min_hold}h  |  Sizing: {args.sizing}  |  '
                  f'Threshold {args.threshold}',
                  color='white', pad=8)

    # Panel 2: position size + prob
    ax2b = ax2.twinx()
    ax2.plot(ts, df['prob'], color='#d2a8ff', lw=0.5, alpha=0.7)
    ax2b.fill_between(ts, df['position'].clip(lower=0), 0, color='#3fb950', alpha=0.5)
    ax2b.fill_between(ts, df['position'].clip(upper=0), 0, color='#f85149', alpha=0.5)
    ax2b.set_ylim(-1.1, 1.1); ax2b.axhline(0, color='#8b949e', lw=0.5, ls=':')
    ax2b.tick_params(colors='#8b949e'); ax2b.set_ylabel('Size', color='#8b949e')
    ax2.axhline(args.threshold,     color='#3fb950', lw=0.6, ls='--', alpha=0.6)
    ax2.axhline(1-args.threshold,   color='#f85149', lw=0.8, ls='--', alpha=0.6)
    ax2.axhline(0.5, color='#8b949e', lw=0.5, ls=':')
    ax2.set_ylim(0, 1); ax2.set_ylabel('Prob')

    ax3.plot(ts, m_bah['cum'], color='#8b949e', lw=0.9, ls='--', label='Buy & Hold')
    ax3.plot(ts, m_ls['cum'],  color='#f0883e', lw=1.1, label='Long/Short')
    ax3.plot(ts, m_lf['cum'],  color='#58a6ff', lw=1.1, label='Long/Flat')
    ax3.axhline(1.0, color='#30363d', lw=0.5)
    ax3.set_ylabel('Equity'); ax3.set_yscale('log')
    ax3.legend(loc='upper left', facecolor='#161b22', labelcolor='white', fontsize=8)

    ax4.fill_between(ts, m_bah['drawdowns'], 0, color='#8b949e', alpha=0.3, label='B&H DD')
    ax4.fill_between(ts, m_ls['drawdowns'],  0, color='#f0883e', alpha=0.4, label='L/S DD')
    ax4.fill_between(ts, m_lf['drawdowns'],  0, color='#58a6ff', alpha=0.4, label='L/F DD')
    ax4.set_ylabel('Drawdown')
    ax4.legend(loc='lower left', facecolor='#161b22', labelcolor='white', fontsize=8)

    ax4.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m'))
    ax4.xaxis.set_major_locator(mdates.MonthLocator(interval=6))
    plt.setp(ax4.get_xticklabels(), rotation=30, ha='right', color='#8b949e')
    for ax in (ax1, ax2, ax3):
        plt.setp(ax.get_xticklabels(), visible=False)

    out = 'backtest_result.png'
    plt.savefig(out, dpi=150, bbox_inches='tight', facecolor='#0d1117')
    print(f"Chart saved -> {out}")
    plt.show()


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    model, scaler, cfg = load_model()
    feature_cols = cfg['feature_cols']
    seq_len      = cfg['seq_len']

    btc  = fetch_btc(since_iso=f"{args.since}T00:00:00Z")
    mkt  = fetch_us_market(start=args.since)
    fng  = fetch_fear_greed()
    fr   = fetch_funding_rate()
    news = fetch_news_sentiment()
    df   = merge_context(btc, mkt, fng, fr, news)
    df   = add_features(df)

    probs      = predict(model, scaler, df, feature_cols, seq_len)
    prices     = df['close'].values
    timestamps = df['ts'].values

    df_ls = simulate(prices, probs, timestamps, args.threshold, args.fee, 'long_short', args.min_hold, args.sizing)
    df_lf = simulate(prices, probs, timestamps, args.threshold, args.fee, 'long_flat',  args.min_hold, args.sizing)

    m_ls  = calc_metrics(df_ls, 'strat_ret')
    m_lf  = calc_metrics(df_lf, 'strat_ret')
    m_bah = calc_metrics(df_ls, 'bah_ret')
    m_bah.update(win_rate=float('nan'), n_trades=0)

    start = str(df['ts'].iloc[0])[:10]
    end   = str(df['ts'].iloc[-1])[:10]

    print(f"\n{'='*75}")
    print(f"  {args.symbol} Backtest  |  {start} -> {end}  ({len(df_ls):,} bars)")
    print(f"  Fee: {args.fee*100:.3f}%  |  Threshold: {args.threshold}  |  Min hold: {args.min_hold}h  |  Features: {len(feature_cols)}")
    print(f"{'='*75}")
    print(f"  {'Strategy':<14} | {'Return':>8} | {'Ann.Return':>10} | "
          f"{'Sharpe':>7} | {'Max DD':>8} | {'WinRate':>7} | {'Trades':>7} | {'AvgSize':>7}")
    print(f"  {'-'*80}")
    print_row('Long/Short', m_ls)
    print_row('Long/Flat',  m_lf)
    print_row('Buy & Hold', m_bah)
    print(f"{'='*75}")

    plot(df_ls, m_ls, m_lf, m_bah)


if __name__ == '__main__':
    main()
