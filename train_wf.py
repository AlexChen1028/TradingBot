"""
Walk-Forward Training for BTC price direction prediction.

Instead of training once on all historical data (which causes distribution
shift / overfitting to old regimes), this script:
  1. Splits the timeline into rolling train/test windows
  2. Re-trains a fresh model on each train window
  3. Predicts only on the out-of-sample test window
  4. Concatenates all OOS predictions and runs a full backtest

This gives a realistic, leak-free estimate of real-world performance.

Timeline (example, 18-month train / 3-month test / 3-month step):
  Window 1:  train 2019-10 ~ 2021-03  |  test 2021-04 ~ 2021-06
  Window 2:  train 2020-01 ~ 2021-06  |  test 2021-07 ~ 2021-09
  ...
  Window N:  train 2024-10 ~ 2026-01  |  test 2026-02 ~ 2026-04

Usage:
    python train_wf.py                     # default settings
    python train_wf.py --train_months 12   # shorter train window
    python train_wf.py --fee 0.0002        # maker fee for backtest
"""

import sys
sys.stdout.reconfigure(encoding='utf-8')

import argparse
import math
import numpy as np
import pandas as pd
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.data import Dataset, DataLoader
from sklearn.preprocessing import StandardScaler
import matplotlib
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import matplotlib.gridspec as gridspec
import joblib

from data import (fetch_btc, fetch_us_market, fetch_fear_greed,
                  fetch_funding_rate, fetch_news_sentiment,
                  merge_context, add_features,
                  compute_sample_weights,
                  FEATURE_COLS, ETH_EXTRA_COLS, TARGET_AHEAD)

matplotlib.rcParams['font.family'] = 'DejaVu Sans'

# ── Args ──────────────────────────────────────────────────────────────────────
parser = argparse.ArgumentParser()
parser.add_argument('--symbol',       type=str,   default='BTC/USDT')
parser.add_argument('--train_months', type=int,   default=18)
parser.add_argument('--test_months',  type=int,   default=3)
parser.add_argument('--step_months',  type=int,   default=3)
parser.add_argument('--since',        type=str,   default='2019-10-01')
parser.add_argument('--epochs',       type=int,   default=60)
parser.add_argument('--patience',     type=int,   default=10)
parser.add_argument('--fee',          type=float, default=0.0004)
parser.add_argument('--threshold',    type=float, default=0.50)
parser.add_argument('--min_hold',     type=int,   default=24)
parser.add_argument('--sizing',       type=str,   default='kelly',
                    choices=['fixed','kelly','half_kelly'])
parser.add_argument('--d_model',      type=int,   default=128)
parser.add_argument('--nhead',        type=int,   default=8)
parser.add_argument('--n_layers',     type=int,   default=3)
parser.add_argument('--seq_len',      type=int,   default=60)
parser.add_argument('--dropout',      type=float, default=0.2)
parser.add_argument('--target_ahead', type=int,   default=6,
                    help='Hours ahead to predict (default 6)')
parser.add_argument('--min_move',     type=float, default=0.0,
                    help='Min price move to count as signal (e.g. 0.01 = 1%%)')
parser.add_argument('--balance_classes', action='store_true',
                    help='Oversample minority class to balance up/down labels')
args = parser.parse_args()

device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
print(f"Device: {device}")
if device.type == 'cuda':
    print(f"  GPU : {torch.cuda.get_device_name(0)}")

HOURS_PER_MONTH = 24 * 30   # approximate
SEQ_LEN  = args.seq_len
D_MODEL  = args.d_model
NHEAD    = args.nhead
N_LAYERS = args.n_layers
DROPOUT  = args.dropout
LR       = 3e-4
LR_MIN   = 1e-5
LABEL_SMOOTH = 0.1
BATCH    = 256


# ── Model ─────────────────────────────────────────────────────────────────────
class TransformerPredictor(nn.Module):
    def __init__(self, n_features, d_model=D_MODEL, nhead=NHEAD,
                 num_layers=N_LAYERS, dropout=DROPOUT, seq_len=SEQ_LEN):
        super().__init__()
        self.input_proj = nn.Linear(n_features, d_model)
        self.pos_embed  = nn.Parameter(torch.zeros(1, seq_len, d_model))
        nn.init.trunc_normal_(self.pos_embed, std=0.02)
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
        x = self.input_proj(x) + self.pos_embed
        x = self.transformer(x)
        x = self.norm(x)
        return self.head((x.mean(1) + x[:,-1,:]) / 2).squeeze(-1)


def weighted_bce(logits, targets, weights, smoothing=LABEL_SMOOTH):
    t    = targets * (1 - smoothing) + 0.5 * smoothing
    loss = F.binary_cross_entropy_with_logits(logits, t, reduction='none')
    return (loss * weights).mean()

def get_lr(epoch, total_epochs):
    if epoch < 5:
        return LR * (epoch + 1) / 5
    p = (epoch - 5) / max(1, total_epochs - 5)
    return LR_MIN + 0.5 * (LR - LR_MIN) * (1 + math.cos(math.pi * p))


# ── Dataset ───────────────────────────────────────────────────────────────────
class BTCDataset(Dataset):
    def __init__(self, X, y, w):
        self.X, self.y, self.w = map(torch.from_numpy, (X, y, w))
    def __len__(self):        return len(self.X)
    def __getitem__(self, i): return self.X[i], self.y[i], self.w[i]


# ── Sequences ─────────────────────────────────────────────────────────────────
def make_sequences(df_slice: pd.DataFrame, scaler=None, fit_scaler=True,
                   feature_cols=None):
    """
    Build (X, y, w) arrays from a DataFrame slice.
    If fit_scaler=True, fit a new StandardScaler on this slice.
    If fit_scaler=False, transform using the provided scaler (test window).
    """
    if feature_cols is None:
        feature_cols = FEATURE_COLS
    df = df_slice.replace([np.inf, -np.inf], np.nan)
    df = df.dropna(subset=feature_cols + ['target']).reset_index(drop=True)

    raw_X = df[feature_cols].values.astype(np.float32)
    raw_y = df['target'].values.astype(np.float32)
    raw_w = compute_sample_weights(df)

    if fit_scaler:
        scaler = StandardScaler()
        raw_X  = scaler.fit_transform(raw_X)
    else:
        raw_X = scaler.transform(raw_X)

    X, y, w = [], [], []
    for i in range(SEQ_LEN, len(raw_X)):
        X.append(raw_X[i - SEQ_LEN:i])
        y.append(raw_y[i - 1])
        w.append(raw_w[i - 1])

    X = np.array(X, np.float32)
    y = np.array(y, np.float32)
    w = np.array(w, np.float32)

    # Oversample minority class so up/down counts are equal
    if fit_scaler and getattr(args, 'balance_classes', False):
        idx_up   = np.where(y == 1)[0]
        idx_down = np.where(y == 0)[0]
        n_min    = min(len(idx_up), len(idx_down))
        if n_min > 0 and len(idx_up) != len(idx_down):
            rng      = np.random.default_rng(42)
            idx_up   = rng.choice(idx_up,   n_min, replace=False)
            idx_down = rng.choice(idx_down, n_min, replace=False)
            idx      = np.sort(np.concatenate([idx_up, idx_down]))
            X, y, w  = X[idx], y[idx], w[idx]

    return X, y, w, scaler


# ── Train one window ──────────────────────────────────────────────────────────
def train_window(X_tr, y_tr, w_tr, X_va, y_va, w_va,
                 window_id: int, total_windows: int) -> nn.Module:
    tr_loader = DataLoader(BTCDataset(X_tr, y_tr, w_tr), BATCH,
                           shuffle=True, pin_memory=(device.type=='cuda'), num_workers=0)
    va_loader = DataLoader(BTCDataset(X_va, y_va, w_va), BATCH,
                           shuffle=False, pin_memory=(device.type=='cuda'), num_workers=0)

    model = TransformerPredictor(X_tr.shape[2]).to(device)
    optim = torch.optim.AdamW(model.parameters(), lr=LR, weight_decay=1e-4)

    best_acc   = 0.0
    best_state = None
    no_improv  = 0

    for epoch in range(1, args.epochs + 1):
        lr_now = get_lr(epoch - 1, args.epochs)
        for pg in optim.param_groups:
            pg['lr'] = lr_now

        model.train()
        tl = tc = 0
        for xb, yb, wb in tr_loader:
            xb, yb, wb = xb.to(device), yb.to(device), wb.to(device)
            optim.zero_grad()
            logits = model(xb)
            loss   = weighted_bce(logits, yb, wb)
            loss.backward()
            nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            optim.step()
            tl += loss.item()
            tc += ((logits > 0) == yb.bool()).float().sum().item()

        model.eval()
        vc = 0
        with torch.no_grad():
            for xb, yb, wb in va_loader:
                xb, yb = xb.to(device), yb.to(device)
                vc += ((model(xb) > 0) == yb.bool()).float().sum().item()

        va_acc = vc / len(X_va)
        if va_acc > best_acc:
            best_acc  = va_acc
            best_state = {k: v.cpu().clone() for k, v in model.state_dict().items()}
            no_improv = 0
        else:
            no_improv += 1
        if no_improv >= args.patience:
            break

    print(f"  Window {window_id:2d}/{total_windows} | "
          f"Train: {len(X_tr):,}  Val: {len(X_va):,}  "
          f"Best val acc: {best_acc:.4f}  (stopped ep {epoch})")

    model.load_state_dict(best_state)
    return model, best_acc


# ── Predict on test slice (batched) ───────────────────────────────────────────
def predict_slice(model, X_test: np.ndarray, batch_size=512) -> np.ndarray:
    model.eval()
    probs = []
    with torch.no_grad():
        for s in range(0, len(X_test), batch_size):
            b = torch.from_numpy(X_test[s:s+batch_size]).to(device)
            probs.append(torch.sigmoid(model(b)).cpu().numpy())
    return np.concatenate(probs)


# ── Backtest helpers (same as backtest.py) ────────────────────────────────────
HPY = 24 * 365

def calc_size(prob, direction, sizing):
    if direction == 0 or sizing == 'fixed':
        return float(direction)
    confidence = abs(prob - 0.5) * 2
    size = confidence if sizing == 'kelly' else confidence * 0.5
    return size * direction

def simulate(prices, probs, timestamps, threshold, fee, mode, min_hold, sizing='kelly'):
    n        = len(prices)
    log_rets = np.concatenate([[0], np.log(prices[1:] / prices[:-1])])
    position = np.zeros(n, dtype=np.float64)
    trades   = 0
    prev_dir = 0
    hold_cnt = 0

    for i in range(1, n):
        p = probs[i]
        if np.isnan(p):
            position[i] = calc_size(0.5, prev_dir, sizing)
            hold_cnt += 1; continue

        d = (1 if p > threshold
             else (-1 if p < (1 - threshold) and mode == 'long_short' else 0))

        if d != prev_dir and prev_dir != 0 and hold_cnt < min_hold:
            d = prev_dir
        if d != prev_dir:
            trades += 1; hold_cnt = 0
        else:
            hold_cnt += 1
        position[i] = calc_size(p, d, sizing)
        prev_dir = d

    ret  = position[:-1] * log_rets[1:]
    chg  = np.abs(np.diff(np.concatenate([[0.0], position[:-1]])))
    ret -= chg * fee
    bah  = log_rets[1:]

    out = pd.DataFrame({'ts': timestamps[1:], 'price': prices[1:],
                        'prob': probs[1:], 'position': position[:-1],
                        'strat_ret': ret, 'bah_ret': bah})
    out['_trades'] = trades
    return out


def calc_metrics(df, ret_col):
    r   = df[ret_col].values
    cum = np.exp(np.cumsum(r))
    tot = cum[-1] - 1
    ann = (1 + tot) ** (HPY / len(r)) - 1
    vol = r.std() * np.sqrt(HPY)
    shp = (ann - 0.04) / vol if vol > 0 else 0
    dd  = cum / np.maximum.accumulate(cum) - 1
    im  = df['position'].abs() > 1e-6
    wr  = (df.loc[im, 'strat_ret'] > 0).sum() / im.sum() if im.sum() else float('nan')
    avsz = df.loc[im, 'position'].abs().mean() if im.sum() else float('nan')
    return dict(total=tot, ann=ann, sharpe=shp, max_dd=dd.min(),
                win_rate=wr, avg_size=avsz,
                n_trades=int(df['_trades'].iloc[0]),
                drawdowns=dd, cum=cum)


def print_row(label, m):
    wr   = f"{m['win_rate']:.2%}"  if not np.isnan(m.get('win_rate',  float('nan'))) else "   —  "
    avsz = f"{m['avg_size']:.3f}" if not np.isnan(m.get('avg_size', float('nan'))) else "  —   "
    print(f"  {label:<14} | {m['total']:>+8.2%} | {m['ann']:>+10.2%} | "
          f"{m['sharpe']:>7.2f} | {m['max_dd']:>8.2%} | {wr:>7} | {m['n_trades']:>7} | {avsz:>7}")


def plot_wf(df_ls, m_ls, m_lf, m_bah, window_dates, coin='BTC/USDT'):
    fig = plt.figure(figsize=(18, 12))
    fig.patch.set_facecolor('#0d1117')
    gs  = gridspec.GridSpec(4, 1, figure=fig, hspace=0.05,
                            height_ratios=[2.5, 0.7, 1.5, 1.0])
    axes = [fig.add_subplot(gs[i]) for i in range(4)]
    for ax in axes:
        ax.set_facecolor('#0d1117'); ax.tick_params(colors='#8b949e')
        ax.spines[:].set_color('#30363d'); ax.yaxis.label.set_color('#8b949e')

    ax1, ax2, ax3, ax4 = axes
    ts = df_ls['ts'].values

    ax1.plot(ts, df_ls['price'], color='#58a6ff', lw=0.6)
    lm = df_ls['position'] ==  1
    sm = df_ls['position'] == -1
    ax1.scatter(ts[lm], df_ls['price'][lm], marker='^', color='#3fb950', s=5, zorder=3)
    ax1.scatter(ts[sm], df_ls['price'][sm], marker='v', color='#f85149', s=5, zorder=3)
    # mark window boundaries
    for d in window_dates:
        ax1.axvline(pd.Timestamp(d), color='#f0883e', lw=0.6, alpha=0.4, ls='--')
    ax1.set_ylabel('Price (USDT)')
    prefix = coin.split('/')[0]
    ax1.set_title(f'{prefix} Walk-Forward Backtest  |  Train {args.train_months}m / Test {args.test_months}m  |  '
                  f'Fee {args.fee*100:.3f}%  |  Min hold {args.min_hold}h',
                  color='white', pad=8)

    ax2.plot(ts, df_ls['prob'], color='#d2a8ff', lw=0.5)
    ax2.axhline(args.threshold,   color='#3fb950', lw=0.8, ls='--', alpha=0.6)
    ax2.axhline(1-args.threshold, color='#f85149', lw=0.8, ls='--', alpha=0.6)
    ax2.axhline(0.5, color='#8b949e', lw=0.5, ls=':')
    ax2.set_ylim(0, 1); ax2.set_ylabel('Prob')

    ax3.plot(ts, m_bah['cum'], color='#8b949e', lw=0.9, ls='--', label='Buy & Hold')
    ax3.plot(ts, m_ls['cum'],  color='#f0883e', lw=1.1, label='Long/Short')
    ax3.plot(ts, m_lf['cum'],  color='#58a6ff', lw=1.1, label='Long/Flat')
    ax3.axhline(1.0, color='#30363d', lw=0.5)
    ax3.set_ylabel('Equity'); ax3.set_yscale('log')
    ax3.legend(loc='upper left', facecolor='#161b22', labelcolor='white', fontsize=8)

    ax4.fill_between(ts, m_bah['drawdowns'], 0, color='#8b949e', alpha=0.3, label='B&H')
    ax4.fill_between(ts, m_ls['drawdowns'],  0, color='#f0883e', alpha=0.4, label='L/S')
    ax4.fill_between(ts, m_lf['drawdowns'],  0, color='#58a6ff', alpha=0.4, label='L/F')
    ax4.set_ylabel('Drawdown')
    ax4.legend(loc='lower left', facecolor='#161b22', labelcolor='white', fontsize=8)

    ax4.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m'))
    ax4.xaxis.set_major_locator(mdates.MonthLocator(interval=6))
    plt.setp(ax4.get_xticklabels(), rotation=30, ha='right', color='#8b949e')
    for ax in (ax1, ax2, ax3):
        plt.setp(ax.get_xticklabels(), visible=False)

    out = f'{coin.split("/")[0].lower()}_backtest_wf.png'
    plt.savefig(out, dpi=150, bbox_inches='tight', facecolor='#0d1117')
    print(f"Chart saved -> {out}")
    plt.show()


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    # ── fetch all data once ──
    coin     = args.symbol  # e.g. 'BTC/USDT' or 'ETH/USDT'
    is_btc   = coin.upper().startswith('BTC')
    ohlcv    = fetch_btc(symbol=coin, since_iso=f"{args.since}T00:00:00Z")
    mkt      = fetch_us_market(start=args.since)
    fng      = fetch_fear_greed()
    fr       = fetch_funding_rate(symbol=f"{coin}:USDT")
    news     = fetch_news_sentiment()
    df       = merge_context(ohlcv, mkt, fng, fr, news)

    # For non-BTC coins fetch BTC as cross-asset reference
    ref_btc = None
    if not is_btc:
        print("[Cross-asset] Fetching BTC/USDT as reference ...")
        ref_btc = fetch_btc(symbol='BTC/USDT', since_iso=f"{args.since}T00:00:00Z")

    df = add_features(df, ref_btc=ref_btc,
                      target_ahead=args.target_ahead,
                      min_move=args.min_move)
    df = df.replace([np.inf, -np.inf], np.nan).reset_index(drop=True)

    feature_cols = FEATURE_COLS + (ETH_EXTRA_COLS if ref_btc is not None else [])

    total_bars   = len(df)
    train_bars   = args.train_months * HOURS_PER_MONTH
    test_bars    = args.test_months  * HOURS_PER_MONTH
    step_bars    = args.step_months  * HOURS_PER_MONTH

    # First test window starts after first training window
    first_test_start = train_bars
    n_windows = max(1, (total_bars - train_bars - test_bars) // step_bars + 1)

    print(f"\nTotal bars: {total_bars:,}")
    print(f"Train: {args.train_months}m ({train_bars:,}h)  "
          f"Test: {args.test_months}m ({test_bars:,}h)  "
          f"Step: {args.step_months}m ({step_bars:,}h)")
    print(f"Windows: {n_windows}")
    print("─" * 65)

    all_probs  = []
    all_prices = []
    all_ts     = []
    all_pos    = []
    window_dates = []
    window_accs  = []

    for w in range(n_windows):
        tr_start = w * step_bars
        tr_end   = tr_start + train_bars
        te_start = tr_end
        te_end   = min(te_start + test_bars, total_bars)

        if te_end <= te_start:
            break

        df_train = df.iloc[tr_start:tr_end].copy()
        df_test  = df.iloc[te_start:te_end].copy()

        # ── 80/20 split inside train window for early stopping ──
        val_split = int(len(df_train) * 0.85)
        df_tr = df_train.iloc[:val_split]
        df_va = df_train.iloc[val_split:]

        X_tr, y_tr, w_tr, scaler = make_sequences(df_tr, fit_scaler=True, feature_cols=feature_cols)
        X_va, y_va, w_va, _      = make_sequences(df_va, scaler=scaler, fit_scaler=False, feature_cols=feature_cols)
        X_te, y_te, w_te, _      = make_sequences(df_test, scaler=scaler, fit_scaler=False, feature_cols=feature_cols)

        if len(X_tr) == 0 or len(X_te) == 0:
            continue

        window_dates.append(df_test['ts'].iloc[0])
        model, best_acc = train_window(X_tr, y_tr, w_tr, X_va, y_va, w_va,
                                       w + 1, n_windows)
        window_accs.append(best_acc)

        probs = predict_slice(model, X_te)

        # Pad with NaN so len(probs_with_pad) == len(df_test)
        # Can't assume exactly SEQ_LEN rows were dropped (some may be NaN rows too)
        n_pad = len(df_test) - len(probs)
        nan_pad = np.full(max(n_pad, 0), np.nan)

        all_probs.extend(np.concatenate([nan_pad, probs[:len(df_test) - len(nan_pad)]]))
        all_prices.extend(df_test['close'].values)
        all_ts.extend(df_test['ts'].values)

    # ── Concatenate and backtest ──
    prices     = np.array(all_prices, dtype=np.float64)
    probs_arr  = np.array(all_probs,  dtype=np.float64)
    timestamps = np.array(all_ts)

    print(f"\n{'='*65}")
    print(f"  Walk-Forward OOS Coverage: {pd.Timestamp(timestamps[0]).date()} "
          f"-> {pd.Timestamp(timestamps[-1]).date()}  ({len(prices):,} bars)")
    print(f"  Avg window val accuracy: {np.mean(window_accs):.4f}")
    print(f"  Fee: {args.fee*100:.3f}%  |  Threshold: {args.threshold}  "
          f"|  Min hold: {args.min_hold}h")
    print(f"{'='*65}")

    df_ls = simulate(prices, probs_arr, timestamps,
                     args.threshold, args.fee, 'long_short', args.min_hold, args.sizing)
    df_lf = simulate(prices, probs_arr, timestamps,
                     args.threshold, args.fee, 'long_flat',  args.min_hold, args.sizing)

    m_ls  = calc_metrics(df_ls, 'strat_ret')
    m_lf  = calc_metrics(df_lf, 'strat_ret')
    m_bah = calc_metrics(df_ls, 'bah_ret')
    m_bah.update(win_rate=float('nan'), n_trades=0)

    print(f"  {'Strategy':<14} | {'Return':>8} | {'Ann.Return':>10} | "
          f"{'Sharpe':>7} | {'Max DD':>8} | {'WinRate':>7} | {'Trades':>7} | {'AvgSize':>7}")
    print(f"  {'-'*80}")
    print_row('Long/Short', m_ls)
    print_row('Long/Flat',  m_lf)
    print_row('Buy & Hold', m_bah)
    print(f"{'='*65}")

    # save final model (trained on last window's full training data)
    print("\nSaving final model (last window) ...")
    torch.save({
        'model_state': model.state_dict(),
        'config': {
            'n_features': len(feature_cols),
            'd_model': D_MODEL, 'nhead': NHEAD,
            'num_layers': N_LAYERS, 'dropout': DROPOUT,
            'seq_len': SEQ_LEN, 'target_ahead': TARGET_AHEAD,
            'feature_cols': feature_cols,
        },
    }, f'{coin.split("/")[0].lower()}_model_wf.pt')
    joblib.dump(scaler, f'{coin.split("/")[0].lower()}_scaler_wf.pkl')
    prefix = coin.split("/")[0].lower()
    print(f"Saved: {prefix}_model_wf.pt  {prefix}_scaler_wf.pkl")

    plot_wf(df_ls, m_ls, m_lf, m_bah, window_dates, coin=coin)


if __name__ == '__main__':
    main()
