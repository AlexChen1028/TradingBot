import sys
sys.stdout.reconfigure(encoding='utf-8')

import math
import numpy as np
import pandas as pd
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.data import Dataset, DataLoader
from sklearn.preprocessing import StandardScaler
import joblib

from data import (fetch_btc, fetch_us_market, fetch_fear_greed,
                  fetch_funding_rate, fetch_news_sentiment,
                  merge_context, add_features,
                  compute_sample_weights,
                  FEATURE_COLS, TARGET_AHEAD)

# ── GPU ───────────────────────────────────────────────────────────────────────
device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
print(f"Device: {device}")
if device.type == 'cuda':
    print(f"  GPU : {torch.cuda.get_device_name(0)}")
    print(f"  VRAM: {torch.cuda.get_device_properties(0).total_memory/1e9:.1f} GB")

# ── Config ────────────────────────────────────────────────────────────────────
SINCE_ISO  = '2017-09-01T00:00:00Z'
SEQ_LEN    = 60
BATCH_SIZE = 256
EPOCHS     = 200
LR         = 3e-4
LR_MIN     = 1e-5
WARMUP     = 10
PATIENCE   = 25
D_MODEL    = 128
NHEAD      = 8
N_LAYERS   = 3
DROPOUT    = 0.2
LABEL_SMOOTH = 0.1


# ── Dataset ───────────────────────────────────────────────────────────────────
def make_sequences(df: pd.DataFrame):
    df    = df.replace([np.inf, -np.inf], np.nan).dropna(subset=FEATURE_COLS + ['target'])
    df    = df.reset_index(drop=True)
    raw_X = df[FEATURE_COLS].values.astype(np.float32)
    raw_y = df['target'].values.astype(np.float32)

    # compute sample weights BEFORE scaling (needs original feature values)
    raw_w = compute_sample_weights(df)

    scaler = StandardScaler()
    raw_X  = scaler.fit_transform(raw_X)

    X, y, w = [], [], []
    for i in range(SEQ_LEN, len(raw_X)):
        X.append(raw_X[i - SEQ_LEN:i])
        y.append(raw_y[i - 1])
        w.append(raw_w[i - 1])   # weight of the prediction bar

    return (np.array(X, np.float32),
            np.array(y, np.float32),
            np.array(w, np.float32),
            scaler)


class BTCDataset(Dataset):
    def __init__(self, X, y, w):
        self.X = torch.from_numpy(X)
        self.y = torch.from_numpy(y)
        self.w = torch.from_numpy(w)

    def __len__(self):        return len(self.X)
    def __getitem__(self, i): return self.X[i], self.y[i], self.w[i]


# ── Model ─────────────────────────────────────────────────────────────────────
class TransformerPredictor(nn.Module):
    """Pre-LN Transformer encoder for time-series binary classification."""
    def __init__(self, n_features, d_model=D_MODEL, nhead=NHEAD,
                 num_layers=N_LAYERS, dropout=DROPOUT, seq_len=SEQ_LEN):
        super().__init__()
        self.input_proj = nn.Linear(n_features, d_model)
        self.pos_embed  = nn.Parameter(torch.zeros(1, seq_len, d_model))
        nn.init.trunc_normal_(self.pos_embed, std=0.02)
        enc = nn.TransformerEncoderLayer(
            d_model=d_model, nhead=nhead, dim_feedforward=d_model * 4,
            dropout=dropout, batch_first=True, norm_first=True,
        )
        self.transformer = nn.TransformerEncoder(enc, num_layers=num_layers)
        self.norm = nn.LayerNorm(d_model)
        self.head = nn.Sequential(
            nn.Linear(d_model, d_model // 2), nn.GELU(),
            nn.Dropout(dropout), nn.Linear(d_model // 2, 1),
        )

    def forward(self, x):
        x      = self.input_proj(x) + self.pos_embed
        x      = self.transformer(x)
        x      = self.norm(x)
        pooled = (x.mean(1) + x[:, -1, :]) / 2
        return self.head(pooled).squeeze(-1)


# ── Weighted loss with label smoothing ────────────────────────────────────────
def weighted_bce(logits, targets, weights, smoothing=LABEL_SMOOTH):
    """
    BCEWithLogitsLoss with per-sample weights and label smoothing.
    Extreme-sentiment samples get higher weight, forcing the model to
    learn contrarian signals more carefully.
    """
    t    = targets * (1 - smoothing) + 0.5 * smoothing
    loss = F.binary_cross_entropy_with_logits(logits, t, reduction='none')
    return (loss * weights).mean()


# ── LR schedule ───────────────────────────────────────────────────────────────
def get_lr(epoch: int) -> float:
    if epoch < WARMUP:
        return LR * (epoch + 1) / WARMUP
    p = (epoch - WARMUP) / max(1, EPOCHS - WARMUP)
    return LR_MIN + 0.5 * (LR - LR_MIN) * (1 + math.cos(math.pi * p))


# ── Training ──────────────────────────────────────────────────────────────────
def train():
    # ── fetch all data sources ──
    btc  = fetch_btc(since_iso=SINCE_ISO)
    mkt  = fetch_us_market(start=SINCE_ISO[:10])
    fng  = fetch_fear_greed()
    fr   = fetch_funding_rate()
    news = fetch_news_sentiment()

    df = merge_context(btc, mkt, fng, fr, news)
    df = add_features(df)

    X, y, w, scaler = make_sequences(df)

    split   = int(len(X) * 0.8)
    X_tr, X_va = X[:split], X[split:]
    y_tr, y_va = y[:split], y[split:]
    w_tr, w_va = w[:split], w[split:]

    pos_rate = float(y_tr.mean())
    print(f"\nTrain: {len(X_tr):,}  Val: {len(X_va):,}  "
          f"Pos: {pos_rate:.3f}  Features: {len(FEATURE_COLS)}")
    print(f"Avg sample weight: {w_tr.mean():.3f}  Max: {w_tr.max():.3f}")

    tr_loader = DataLoader(BTCDataset(X_tr, y_tr, w_tr), BATCH_SIZE,
                           shuffle=True,  pin_memory=(device.type=='cuda'), num_workers=0)
    va_loader = DataLoader(BTCDataset(X_va, y_va, w_va), BATCH_SIZE,
                           shuffle=False, pin_memory=(device.type=='cuda'), num_workers=0)

    model = TransformerPredictor(len(FEATURE_COLS)).to(device)
    optim = torch.optim.AdamW(model.parameters(), lr=LR, weight_decay=1e-4)

    print(f"Params: {sum(p.numel() for p in model.parameters() if p.requires_grad):,}")
    print(f"\n{'Epoch':>6} {'TR-Loss':>9} {'TR-Acc':>8} {'VA-Loss':>9} {'VA-Acc':>8}  LR")
    print("─" * 62)

    best_acc  = 0.0
    no_improv = 0

    for epoch in range(1, EPOCHS + 1):
        lr_now = get_lr(epoch - 1)
        for pg in optim.param_groups:
            pg['lr'] = lr_now

        # ── train ──
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

        # ── validate (unweighted for fair accuracy reading) ──
        model.eval()
        vl = vc = 0
        with torch.no_grad():
            for xb, yb, wb in va_loader:
                xb, yb, wb = xb.to(device), yb.to(device), wb.to(device)
                out = model(xb)
                vl += weighted_bce(out, yb, wb).item()
                vc += ((out > 0) == yb.bool()).float().sum().item()

        tr_acc = tc / len(X_tr)
        va_acc = vc / len(X_va)
        flag   = ""

        if va_acc > best_acc:
            best_acc  = va_acc
            no_improv = 0
            torch.save({
                'model_state': model.state_dict(),
                'config': {
                    'n_features': len(FEATURE_COLS),
                    'd_model': D_MODEL, 'nhead': NHEAD,
                    'num_layers': N_LAYERS, 'dropout': DROPOUT,
                    'seq_len': SEQ_LEN, 'target_ahead': TARGET_AHEAD,
                    'feature_cols': FEATURE_COLS,
                },
            }, 'btc_model.pt')
            joblib.dump(scaler, 'btc_scaler.pkl')
            flag = "  [saved]"
        else:
            no_improv += 1

        if epoch % 5 == 0 or flag:
            print(f"{epoch:6d} {tl/len(tr_loader):9.4f} {tr_acc:8.4f} "
                  f"{vl/len(va_loader):9.4f} {va_acc:8.4f}  {lr_now:.2e}{flag}")

        if no_improv >= PATIENCE:
            print(f"\nEarly stop at epoch {epoch}")
            break

    print(f"\nDone. Best val accuracy: {best_acc:.4f}")
    print("Saved: btc_model.pt  btc_scaler.pkl")


if __name__ == '__main__':
    train()
