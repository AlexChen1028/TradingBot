import sys
import torch
import joblib
import numpy as np
import torch.nn as nn

ckpt = torch.load('btc_model_wf.pt', map_location='cpu', weights_only=False)
cfg  = ckpt['config']
print('n_features  :', cfg['n_features'])
print('seq_len     :', cfg['seq_len'])
print('feature_cols:', len(cfg['feature_cols']))

scaler = joblib.load('btc_scaler_wf.pkl')
print('scaler feat :', scaler.n_features_in_)

from data import FEATURE_COLS
match = cfg['feature_cols'] == FEATURE_COLS
print('cols match  :', match)
if not match:
    saved = set(cfg['feature_cols'])
    curr  = set(FEATURE_COLS)
    print('  in model, not in data.py:', saved - curr)
    print('  in data.py, not in model:', curr - saved)

class TransformerPredictor(nn.Module):
    def __init__(self, n_features, d_model, nhead, num_layers, dropout, seq_len):
        super().__init__()
        self.input_proj = nn.Linear(n_features, d_model)
        self.pos_embed  = nn.Parameter(torch.zeros(1, seq_len, d_model))
        enc = nn.TransformerEncoderLayer(
            d_model=d_model, nhead=nhead, dim_feedforward=d_model*4,
            dropout=dropout, batch_first=True, norm_first=True)
        self.transformer = nn.TransformerEncoder(enc, num_layers=num_layers)
        self.norm = nn.LayerNorm(d_model)
        self.head = nn.Sequential(
            nn.Linear(d_model, d_model//2), nn.GELU(),
            nn.Dropout(dropout), nn.Linear(d_model//2, 1))

    def forward(self, x):
        x = self.input_proj(x) + self.pos_embed
        x = self.transformer(x)
        x = self.norm(x)
        return self.head((x.mean(1) + x[:, -1, :]) / 2).squeeze(-1)

model = TransformerPredictor(
    cfg['n_features'], cfg['d_model'], cfg['nhead'],
    cfg['num_layers'], cfg['dropout'], cfg['seq_len'])
model.load_state_dict(ckpt['model_state'])
model.eval()

dummy = torch.from_numpy(
    np.zeros((1, cfg['seq_len'], cfg['n_features']), dtype=np.float32))
with torch.no_grad():
    out = torch.sigmoid(model(dummy)).item()
print('forward pass:', round(out, 4), ' <- model OK')
print('ALL GOOD')
