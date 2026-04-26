"""
ML Trading Bot — supports BTC, ETH, or any trained coin.

Configuration via environment variables:
  SYMBOL          : trading pair base (default: BTC)  e.g. BTC or ETH
  LONG_FLAT_ONLY  : set to 'true' to disable shorting (recommended for ETH)
  BINANCE_API_KEY / BINANCE_SECRET_KEY
  TELEGRAM_TOKEN / TELEGRAM_CHAT_ID

Usage:
  # BTC bot (default)
  python main.py

  # ETH bot (long/flat only)
  SYMBOL=ETH LONG_FLAT_ONLY=true python main.py
"""

import os
import sys
import json
import time
import logging
import warnings
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import torch
import torch.nn as nn
import joblib
import ccxt
import requests as _requests

warnings.filterwarnings('ignore')

from data import (
    fetch_btc, fetch_us_market, fetch_fear_greed, fetch_funding_rate,
    fetch_news_sentiment, merge_context, add_features, ETH_EXTRA_COLS,
)

# ── Config from env vars ──────────────────────────────────────────────────────
_COIN          = os.getenv('SYMBOL', 'BTC').upper()          # e.g. BTC, ETH
SYMBOL         = f'{_COIN}/USDT:USDT'                        # futures symbol
SPOT_SYMBOL    = f'{_COIN}/USDT'                             # for OHLCV fetch
LONG_FLAT_ONLY = os.getenv('LONG_FLAT_ONLY', 'false').lower() == 'true'

MODEL_PATH  = f'{_COIN.lower()}_model_wf.pt'
SCALER_PATH = f'{_COIN.lower()}_scaler_wf.pkl'
STATE_FILE  = f'{_COIN.lower()}_state.json'
LOG_FILE    = f'{_COIN.lower()}_bot.log'

MAX_POS_PCT    = 0.20
MIN_HOLD_HOURS = 6
THRESHOLD      = 0.50
LOOKBACK_DAYS  = 60
INTERVAL_SECS  = 3600
STOP_LOSS_PCT  = 0.05

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILE, encoding='utf-8'),
    ]
)
log = logging.getLogger(__name__)

# ── Telegram ──────────────────────────────────────────────────────────────────
_TG_TOKEN   = os.getenv('TELEGRAM_TOKEN',   '')
_TG_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '')

def tg_send(text: str):
    if not _TG_TOKEN or not _TG_CHAT_ID:
        return
    try:
        _requests.post(
            f'https://api.telegram.org/bot{_TG_TOKEN}/sendMessage',
            json={'chat_id': _TG_CHAT_ID, 'text': text, 'parse_mode': 'HTML'},
            timeout=10,
        )
    except Exception as e:
        log.warning(f'Telegram error: {e}')


# ── Model definition ──────────────────────────────────────────────────────────
class TransformerPredictor(nn.Module):
    def __init__(self, n_features, d_model, nhead, num_layers, dropout, seq_len):
        super().__init__()
        self.input_proj = nn.Linear(n_features, d_model)
        self.pos_embed  = nn.Parameter(torch.zeros(1, seq_len, d_model))
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
        x = self.input_proj(x) + self.pos_embed
        x = self.transformer(x)
        x = self.norm(x)
        return self.head((x.mean(1) + x[:, -1, :]) / 2).squeeze(-1)


# ── State persistence ─────────────────────────────────────────────────────────
_DEFAULT_STATE = {
    'direction':    0,
    'amount_coin':  0.0,
    'entry_time':   None,
    'entry_price':  None,
}

def load_state() -> dict:
    p = Path(STATE_FILE)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    return _DEFAULT_STATE.copy()

def save_state(s: dict):
    Path(STATE_FILE).write_text(json.dumps(s, indent=2))


# ── Data fetch ────────────────────────────────────────────────────────────────
def fetch_tick_data(exchange_pub, feature_cols: list) -> pd.DataFrame:
    since = (datetime.utcnow() - timedelta(days=LOOKBACK_DAYS + 5)).strftime('%Y-%m-%d')
    limit = (LOOKBACK_DAYS + 5) * 24

    raw  = exchange_pub.fetch_ohlcv(SPOT_SYMBOL, '1h', limit=limit)
    ohlcv = pd.DataFrame(raw, columns=['ts', 'open', 'high', 'low', 'close', 'volume'])
    ohlcv['ts'] = pd.to_datetime(ohlcv['ts'], unit='ms').dt.tz_localize(None)

    mkt  = fetch_us_market(start=since)
    fng  = fetch_fear_greed()
    fr   = fetch_funding_rate(symbol=SYMBOL, since_iso=f"{since}T00:00:00Z")
    news = fetch_news_sentiment()

    df = merge_context(ohlcv, mkt, fng, fr, news)

    # Fetch BTC reference data if model uses cross-asset features
    ref_btc = None
    if any(c in feature_cols for c in ETH_EXTRA_COLS):
        log.info("Fetching BTC reference data for cross-asset features ...")
        raw_btc = exchange_pub.fetch_ohlcv('BTC/USDT', '1h', limit=limit)
        ref_btc = pd.DataFrame(raw_btc, columns=['ts', 'open', 'high', 'low', 'close', 'volume'])
        ref_btc['ts'] = pd.to_datetime(ref_btc['ts'], unit='ms').dt.tz_localize(None)

    df = add_features(df, ref_btc=ref_btc)
    return df


# ── Model inference ───────────────────────────────────────────────────────────
def predict(model, scaler, df: pd.DataFrame, feature_cols: list, seq_len: int):
    df_c = df.replace([float('inf'), float('-inf')], float('nan')
                      ).dropna(subset=feature_cols)

    if len(df_c) < seq_len:
        raise ValueError(f"Not enough data: {len(df_c)} rows (need {seq_len})")

    X = scaler.transform(df_c[feature_cols].values[-seq_len:].astype('float32'))
    with torch.no_grad():
        logit = model(torch.from_numpy(X).unsqueeze(0)).item()
        prob  = float(torch.sigmoid(torch.tensor(logit)))

    if prob > THRESHOLD:
        direction = 1
    elif prob < (1 - THRESHOLD) and not LONG_FLAT_ONLY:
        direction = -1
    else:
        direction = 0

    return prob, direction


# ── Attribution explanation ───────────────────────────────────────────────────
def explain_prediction(model, scaler, df: pd.DataFrame, feature_cols: list, seq_len: int):
    try:
        df_c  = df.replace([float('inf'), float('-inf')], float('nan')).dropna(subset=feature_cols)
        X_all = scaler.transform(df_c[feature_cols].values.astype('float32'))
        x     = torch.from_numpy(X_all[-seq_len:][np.newaxis])
        x.requires_grad_(True)
        model(x).sum().backward()
        scores  = (x.grad.detach().numpy()[0] * x.detach().numpy()[0]).mean(axis=0)
        top_idx = np.argsort(np.abs(scores))[::-1][:10]
        log.info("Top features:")
        for i in top_idx:
            arrow = '↑' if scores[i] > 0 else '↓'
            log.info(f"  {arrow} {feature_cols[i]:30s} {scores[i]:+.4f}")
    except Exception as e:
        log.warning(f"Attribution explain skipped: {e}")


# ── Exchange helpers ───────────────────────────────────────────────────────────
def get_balance(exchange) -> float:
    bal = exchange.fetch_balance({'type': 'future'})
    return float(bal.get('USDT', {}).get('free', 0))

def get_price(exchange) -> float:
    return float(exchange.fetch_ticker(SYMBOL)['last'])

def check_stop_loss(exchange, state: dict) -> bool:
    if state['direction'] == 0 or not state.get('entry_price'):
        return False
    price   = get_price(exchange)
    ep      = state['entry_price']
    pnl_pct = (price - ep) / ep * state['direction']
    if pnl_pct < -STOP_LOSS_PCT:
        side_str = 'LONG' if state['direction'] == 1 else 'SHORT'
        msg = (f"[{_COIN}] STOP-LOSS on {side_str} | "
               f"entry={ep:,.2f}  now={price:,.2f}  loss={pnl_pct*100:+.2f}%")
        log.warning(msg)
        tg_send(f"🚨 {msg}")
        close_position(exchange, state)
        return True
    return False

def close_position(exchange, state: dict):
    if state['direction'] == 0 or state['amount_coin'] == 0:
        return
    amt      = state['amount_coin']
    price    = get_price(exchange)
    ep       = state.get('entry_price') or price
    pnl_pct  = (price - ep) / ep * state['direction'] * 100
    side_str = 'LONG' if state['direction'] == 1 else 'SHORT'
    try:
        params = {'reduceOnly': True}
        if state['direction'] == 1:
            exchange.create_market_sell_order(SYMBOL, amt, params=params)
        else:
            exchange.create_market_buy_order(SYMBOL, amt, params=params)
        msg = f"[{_COIN}] CLOSED {side_str} | {amt} {_COIN} | PnL {pnl_pct:+.2f}%"
        log.info(msg)
        tg_send(f"🔒 {msg}")
    except Exception as e:
        log.error(f"Failed to close position: {e}")

def open_position(exchange, direction: int, balance: float):
    if direction == 0:
        return 0.0, 0.0
    price    = get_price(exchange)
    usdt     = balance * MAX_POS_PCT
    amount   = max(round(usdt / price, 4), 0.001)
    side_str = 'LONG' if direction == 1 else 'SHORT'
    try:
        if direction == 1:
            exchange.create_market_buy_order(SYMBOL, amount)
        else:
            exchange.create_market_sell_order(SYMBOL, amount)
        msg = (f"[{_COIN}] OPENED {side_str} | {amount} {_COIN} @ ~{price:,.2f} USDT "
               f"(${usdt:,.0f} = {MAX_POS_PCT*100:.0f}% balance)")
        log.info(msg)
        tg_send(f"{'🟢' if direction == 1 else '🔴'} {msg}")
        return amount, price
    except Exception as e:
        log.error(f"Failed to open position: {e}")
        return 0.0, price


# ── Main loop ─────────────────────────────────────────────────────────────────
def main():
    for p in (MODEL_PATH, SCALER_PATH):
        if not Path(p).exists():
            sys.exit(f"[ERROR] {p} not found — run train_wf.py first.")

    ckpt  = torch.load(MODEL_PATH, map_location='cpu', weights_only=False)
    cfg   = ckpt['config']
    model = TransformerPredictor(
        cfg['n_features'], cfg['d_model'], cfg['nhead'],
        cfg['num_layers'], cfg['dropout'], cfg['seq_len'],
    )
    model.load_state_dict(ckpt['model_state'])
    model.eval()

    scaler       = joblib.load(SCALER_PATH)
    feature_cols = cfg['feature_cols']
    seq_len      = cfg['seq_len']

    mode_str = 'Long/Flat' if LONG_FLAT_ONLY else 'Long/Short'
    log.info(f"Coin   : {_COIN}  ({mode_str} mode)")
    log.info(f"Model  : {MODEL_PATH}  ({cfg['n_features']} features, seq_len={seq_len})")

    exchange = ccxt.binance({
        'apiKey':  os.getenv('BINANCE_API_KEY', ''),
        'secret':  os.getenv('BINANCE_SECRET_KEY', ''),
        'enableRateLimit': True,
        'options': {'defaultType': 'future'},
    })
    exchange.enable_demo_trading(True)
    exchange_pub = ccxt.binance({'enableRateLimit': True})

    log.info("Exchange: Binance Futures (DEMO MODE)")
    log.info(f"Config  : max_pos={MAX_POS_PCT*100:.0f}%  min_hold={MIN_HOLD_HOURS}h  threshold={THRESHOLD}")

    state = load_state()
    log.info(f"State   : dir={state['direction']}  entry={state.get('entry_time', '—')}")

    while True:
        tick_start = time.time()
        now        = datetime.utcnow()
        log.info(f"\n{'='*55}")
        log.info(f"  [{_COIN}] {now.strftime('%Y-%m-%d %H:%M UTC')}")
        log.info(f"{'='*55}")

        try:
            log.info("Fetching market data ...")
            df = fetch_tick_data(exchange_pub, feature_cols)
            prob, direction = predict(model, scaler, df, feature_cols, seq_len)
            dir_label = {1: 'LONG', -1: 'SHORT', 0: 'FLAT'}[direction]
            log.info(f"Signal  : {dir_label}  (prob={prob:.4f}  conf={abs(prob-0.5)*200:.1f}%)")
            explain_prediction(model, scaler, df, feature_cols, seq_len)

            if check_stop_loss(exchange, state):
                state.update(_DEFAULT_STATE.copy())
                save_state(state)

            locked = False
            if state['direction'] != 0 and state.get('entry_time'):
                held_h = (now - datetime.fromisoformat(state['entry_time'])).total_seconds() / 3600
                remain = max(0, MIN_HOLD_HOURS - held_h)
                log.info(f"Held    : {held_h:.1f}h  |  Lock remaining: {remain:.1f}h")
                if remain > 0 and direction != state['direction']:
                    log.info("Min-hold active — maintaining current position")
                    locked = True

            if not locked and direction != state['direction']:
                balance = get_balance(exchange)
                log.info(f"Balance : {balance:,.2f} USDT")

                if state['direction'] != 0:
                    close_position(exchange, state)
                    state.update(_DEFAULT_STATE.copy())
                    save_state(state)
                    time.sleep(1)

                if direction != 0:
                    amt, price = open_position(exchange, direction, balance)
                    state.update({
                        'direction':   direction,
                        'amount_coin': amt,
                        'entry_time':  now.isoformat(),
                        'entry_price': price,
                    })
                    save_state(state)
                else:
                    log.info("Signal is FLAT — staying out of market")
                    save_state(state)
            else:
                cur_label = {1: 'LONG', -1: 'SHORT', 0: 'FLAT'}[state['direction']]
                log.info(f"No action — holding {cur_label}")

            # 每小時持倉報告
            try:
                cur_price = get_price(exchange)
                balance   = get_balance(exchange)
                if state['direction'] != 0 and state.get('entry_price'):
                    ep      = state['entry_price']
                    amt     = state['amount_coin']
                    pnl_pct = (cur_price - ep) / ep * state['direction'] * 100
                    held_h  = (now - datetime.fromisoformat(state['entry_time'])).total_seconds() / 3600
                    dir_emoji = '🟢 LONG' if state['direction'] == 1 else '🔴 SHORT'
                    tg_send(
                        f"📋 <b>[{_COIN}] 每小時持倉報告</b>\n"
                        f"⏰ {now.strftime('%Y-%m-%d %H:%M UTC')}\n\n"
                        f"方向：{dir_emoji}\n"
                        f"數量：{amt} {_COIN}\n"
                        f"進場價：{ep:,.2f} USDT\n"
                        f"現價：{cur_price:,.2f} USDT\n"
                        f"未實現盈虧：{pnl_pct:+.2f}%\n"
                        f"持倉時間：{held_h:.1f} 小時\n"
                        f"帳戶餘額：{balance:,.2f} USDT"
                    )
                else:
                    tg_send(
                        f"📋 <b>[{_COIN}] 每小時持倉報告</b>\n"
                        f"⏰ {now.strftime('%Y-%m-%d %H:%M UTC')}\n\n"
                        f"方向：⚪ 空倉\n"
                        f"帳戶餘額：{balance:,.2f} USDT"
                    )
            except Exception as e:
                log.warning(f"Status report error: {e}")

        except KeyboardInterrupt:
            log.info("Bot stopped by user.")
            tg_send(f"⚠️ [{_COIN}] Bot stopped by user.")
            break
        except Exception as e:
            log.error(f"Tick error: {e}", exc_info=True)
            tg_send(f"❌ [{_COIN}] Tick error: {e}")

        elapsed    = time.time() - tick_start
        sleep_time = max(0, INTERVAL_SECS - elapsed)
        log.info(f"Next tick in {sleep_time / 60:.0f} min")
        time.sleep(sleep_time)


if __name__ == '__main__':
    main()
