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

def now8() -> datetime:
    return datetime.utcnow() + timedelta(hours=8)
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

MODEL_PATH    = f'{_COIN.lower()}_model_wf.pt'
SCALER_PATH   = f'{_COIN.lower()}_scaler_wf.pkl'
MODEL_4H_PATH = f'{_COIN.lower()}_4h_model_wf.pt'
SCALER_4H_PATH= f'{_COIN.lower()}_4h_scaler_wf.pkl'
STATE_FILE    = f'{_COIN.lower()}_state.json'
LOG_FILE      = f'{_COIN.lower()}_bot.log'
MULTI_TF      = os.getenv('MULTI_TF', 'true').lower() == 'true'

MAX_POS_PCT    = float(os.getenv('MAX_POS_PCT', '0.05'))  # 保證金佔餘額比例
MIN_HOLD_HOURS = 6
THRESHOLD      = 0.50
LOOKBACK_DAYS  = 60
INTERVAL_SECS  = 3600
LEVERAGE       = int(os.getenv('LEVERAGE', '20'))
SL_PCT         = float(os.getenv('SL_PCT', '0.03'))    # 固定止損距離 3%（追蹤止損模式下為初始距離）
TP_PCT         = float(os.getenv('TP_PCT', '0.05'))    # 止盈距離 5%
TRAILING_SL    = os.getenv('TRAILING_SL', 'true').lower() == 'true'  # 是否用追蹤止損
MAX_DD_PCT     = float(os.getenv('MAX_DD_PCT', '0.20'))  # 最大回撤保護：20%
DEMO_MODE      = os.getenv('DEMO_MODE', 'true').lower() == 'true'    # 模擬 / 實盤模式
CORR_PROTECT   = os.getenv('CORR_PROTECT', 'true').lower() == 'true' # BTC/ETH 相關性保護

# ── KOL 共識支撐/壓力區（notes/youtube-insights.md 2026-05-23 45支影片統整）──────
# 三個 KOL 交集的靜態 Zone，每輪 KOL 更新後手動調整。
# KEY_SUPPORT_ZONE    : 三方支撐共識（飛揚 MA/STH 動態、歐陽 75,500~76,000、龐克 STH 78,300）
#                       → 上沿改為 78,500（含 STH 成本線支撐區間）
# KEY_RESISTANCE_ZONE : 三方壓力共識（飛揚 78,700、歐陽 78,000、龐克 STH 78,300+200MA 82,000）
KEY_SUPPORT_ZONE    = (75_500, 78_500)   # 2026-05-23 三方支撐共識（下沿75,500 ~ STH78,300）
KEY_RESISTANCE_ZONE = (78_000, 82_000)   # 2026-05-23 三方壓力共識（飛揚78,700~歐陽78,000~200MA82,000）

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
_TG_TOKEN    = os.getenv('TELEGRAM_TOKEN',   '')
_TG_CHAT_IDS = [i.strip() for i in os.getenv('TELEGRAM_CHAT_ID', '').split(',') if i.strip()]

def tg_send(text: str):
    if not _TG_TOKEN or not _TG_CHAT_IDS:
        return
    for chat_id in _TG_CHAT_IDS:
        try:
            _requests.post(
                f'https://api.telegram.org/bot{_TG_TOKEN}/sendMessage',
                json={'chat_id': chat_id, 'text': text, 'parse_mode': 'HTML'},
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
    'direction':          0,
    'amount_coin':        0.0,
    'entry_time':         None,
    'entry_price':        None,
    'sl_order_id':        None,
    'tp_order_id':        None,
    'peak_balance':       None,   # 帳戶最高峰值（回撤保護用）
    'paused':             False,  # 是否因回撤暫停
    'daily_open_balance': None,   # 當天開始餘額（每日報告用）
    'daily_open_time':    None,   # 當天開始時間
    'last_heartbeat':     None,   # 上次健康檢查時間
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
_TF_MS = {'1h': 3600000, '4h': 14400000, '8h': 28800000, '1d': 86400000}

def _fetch_paginated_ohlcv(exchange, symbol, timeframe, since_ms):
    """從 since_ms 拉到現在，分頁累積（避免 Binance 不接受大 limit）"""
    tf_ms = _TF_MS.get(timeframe, 3600000)
    bars, cur = [], since_ms
    while True:
        batch = exchange.fetch_ohlcv(symbol, timeframe, since=cur, limit=1000)
        if not batch:
            break
        bars.extend(batch)
        if len(batch) < 1000:
            break
        cur = batch[-1][0] + tf_ms
    return bars


def fetch_tick_data(exchange_pub, feature_cols: list, timeframe: str = '1h') -> pd.DataFrame:
    tf_hours = {'1h': 1, '4h': 4, '8h': 8, '1d': 24}.get(timeframe, 1)
    bars_day = 24 // tf_hours

    # 需要 rolling(168) warmup + seq_len + buffer，+200 額外 bar 確保安全
    limit     = (LOOKBACK_DAYS + 5) * bars_day + 200
    days_back = (limit * tf_hours // 24) + 5
    since     = (now8() - timedelta(days=days_back)).strftime('%Y-%m-%d')
    since_ms  = int((now8() - timedelta(days=days_back)).timestamp() * 1000)

    raw   = _fetch_paginated_ohlcv(exchange_pub, SPOT_SYMBOL, timeframe, since_ms)
    ohlcv = pd.DataFrame(raw, columns=['ts', 'open', 'high', 'low', 'close', 'volume'])
    ohlcv['ts'] = pd.to_datetime(ohlcv['ts'], unit='ms').dt.tz_localize(None)
    log.info(f"OHLCV fetched: {len(ohlcv)} {timeframe} bars  ({days_back} days back)")

    mkt  = fetch_us_market(start=since)
    fng  = fetch_fear_greed()
    fr   = fetch_funding_rate(symbol=SYMBOL, since_iso=f"{since}T00:00:00Z")
    news = fetch_news_sentiment()

    df = merge_context(ohlcv, mkt, fng, fr, news)

    # BTC reference for cross-asset features，覆蓋整個 OHLCV 時間範圍
    ref_btc = None
    if any(c in feature_cols for c in ETH_EXTRA_COLS):
        log.info(f"Fetching BTC reference data ({days_back} days) ...")
        raw_btc = _fetch_paginated_ohlcv(exchange_pub, 'BTC/USDT', '1h', since_ms)
        ref_btc = pd.DataFrame(raw_btc, columns=['ts', 'open', 'high', 'low', 'close', 'volume'])
        ref_btc['ts'] = pd.to_datetime(ref_btc['ts'], unit='ms').dt.tz_localize(None)
        log.info(f"BTC reference: {len(ref_btc)} 1h bars")

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


# ── 市場狀態偵測 ──────────────────────────────────────────────────────────────
def detect_regime(df: pd.DataFrame) -> str:
    """
    返回 'trending'（趨勢市）/ 'ranging'（震盪市）/ 'neutral'。
    使用 ATR 比率：近期 ATR 相對於 50 根均值。

    對應市場觀點（KOL: 加密龐克, notes/youtube-insights.md §一）：
      • 'ranging' → 對應「熊市中罕見的強勢收斂」場景，價格被擠壓在關鍵均線
        與成本線之間。此時 open_position() 會將倉位降到 50%，避免在洗盤
        區間吃掉本金。
      • 'trending' → 對應「強力挑戰均線」突破場景，可全倉執行訊號。
      • 'neutral' → 過渡狀態，照原訊號執行但保留 MIN_HOLD_HOURS 保護。
    """
    try:
        h, l, c = df['high'], df['low'], df['close']
        tr  = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
        atr = tr.rolling(14).mean()
        if len(atr.dropna()) < 50:
            return 'neutral'
        ratio = atr.iloc[-1] / atr.iloc[-50:].mean()
        if ratio > 1.3:
            return 'trending'
        elif ratio < 0.7:
            return 'ranging'
        return 'neutral'
    except Exception:
        return 'neutral'


# ── KOL overlay filters (加密龐克, notes/youtube-insights.md) ────────────────
def compute_kol_filters(exchange_pub, df: pd.DataFrame) -> dict:
    """
    EMA200-based overlay filters derived from 加密龐克 KOL analysis.
    See notes/youtube-insights.md §C and §D for full signal logic.

    squeeze_fuel_up    : near EMA200 + shorts over-leveraged + RSI < 70 → 嘎空燃料
    fake_breakout_risk : testing EMA200 resistance + longs over-heated → 假突破洗盤
    right_side_long    : 3 daily closes above EMA200 → 右側交易高確定性窗口
    fr_flip_negative   : FR crossed from neutral/positive to negative → 軋空動能訊號
    near_support       : price below EMA200 × 1.02 → SHORT risky (in support zone)
    spy_qqq_declining  : US stocks down > 0.5% → 美股回調釋放流動性，留意多頭催化
    """
    default = {
        'ema200': None, 'ma200_ratio': 0.0,
        'squeeze_fuel_up': False, 'fake_breakout_risk': False,
        'right_side_long': False, 'fr_flip_negative': False,
        'near_support': False, 'spy_qqq_declining': False,
        'fr_raw': 0.0,
        'in_support_zone': False,    # 靜態 KEY_SUPPORT_ZONE
        'in_resistance_zone': False, # 靜態 KEY_RESISTANCE_ZONE
        'squeeze_short_risk': False, # 大幅負費率 + OI 顯著 → 嘎空風險
    }
    try:
        raw_daily  = exchange_pub.fetch_ohlcv(SPOT_SYMBOL, '1d', limit=210)
        daily_c    = pd.DataFrame(raw_daily,
                                  columns=['ts', 'open', 'high', 'low', 'close', 'volume'])['close']
        ema200     = float(daily_c.ewm(span=200, adjust=False).mean().iloc[-1])
        close      = float(df['close'].iloc[-1])
        # data.py computes rsi as (100 - ...) / 100, so it's already 0..1
        rsi        = float(df['rsi'].iloc[-1])
        fr_series  = df['funding_rate_raw'].fillna(0)
        fr_raw     = float(fr_series.iloc[-1])
        fr_prev    = float(fr_series.iloc[-2]) if len(fr_series) >= 2 else fr_raw
        ma200_ratio = close / ema200 - 1

        # SPY/QQQ daily returns are forward-filled to hourly in data.py
        spy_ret = float(df['spy_ret'].fillna(0).iloc[-1])
        qqq_ret = float(df['qqq_ret'].fillna(0).iloc[-1])

        squeeze_fuel_up = bool(
            close > ema200 * 0.98 and   # within 2% below EMA200 (or above)
            fr_raw < -0.0001 and        # shorts over-leveraged → squeeze fuel
            rsi < 0.70
        )
        fake_breakout_risk = bool(
            fr_raw > 0.0005 and                       # longs over-heated
            ema200 * 0.995 <= close <= ema200 * 1.01  # price testing EMA200 as resistance
        )
        right_side_long = bool(
            all(daily_c.tail(3).values > ema200) and  # 3 consecutive daily closes above EMA200
            close > ema200
        )
        fr_flip_negative = bool(
            fr_raw < -0.0001 and fr_prev >= -0.0001   # FR just crossed to negative
        )
        near_support = bool(close < ema200 * 1.02)    # at or below EMA200 — support zone
        spy_qqq_declining = bool(spy_ret < -0.005 or qqq_ret < -0.005)

        # 靜態 KOL Zone（KEY_SUPPORT_ZONE / KEY_RESISTANCE_ZONE，每輪 KOL 更新手動調整）
        in_support_zone    = bool(KEY_SUPPORT_ZONE[0] <= close <= KEY_SUPPORT_ZONE[1])
        in_resistance_zone = bool(KEY_RESISTANCE_ZONE[0] <= close <= KEY_RESISTANCE_ZONE[1])

        # 嘎空短線風險（加密龐克第二輪）：大幅負費率 + 統計顯著（OI 代理）→ 暫停做空
        # fr_z 是 rolling 21-period z-score；abs > 1.5 = 當前費率已超出歷史 1.5σ → 極端程度
        fr_z = float(df['fr_z'].fillna(0).iloc[-1]) if 'fr_z' in df.columns else 0.0
        squeeze_short_risk = bool(fr_raw < -0.0003 and abs(fr_z) > 1.5)

        return {
            'ema200': ema200, 'ma200_ratio': ma200_ratio,
            'squeeze_fuel_up': squeeze_fuel_up,
            'fake_breakout_risk': fake_breakout_risk,
            'right_side_long': right_side_long,
            'fr_flip_negative': fr_flip_negative,
            'near_support': near_support,
            'spy_qqq_declining': spy_qqq_declining,
            'fr_raw': fr_raw,
            'in_support_zone': in_support_zone,
            'in_resistance_zone': in_resistance_zone,
            'squeeze_short_risk': squeeze_short_risk,
        }
    except Exception as e:
        log.warning(f"compute_kol_filters failed: {e}")
        return default


# ── BTC/ETH 相關性保護 ────────────────────────────────────────────────────────
def get_correlated_direction() -> int:
    """讀取相關幣種的倉位方向，BTC 讀 ETH，ETH 讀 BTC。"""
    if not CORR_PROTECT:
        return 0
    other = 'eth' if _COIN == 'BTC' else 'btc'
    try:
        p = Path(f'{other}_state.json')
        if p.exists():
            return json.loads(p.read_text()).get('direction', 0)
    except Exception:
        pass
    return 0

def check_preemptive_reversal(exchange, state: dict, model_direction: int) -> bool:
    """
    若模型信號已反轉，且目前虧損超過 SL 距離的一半，
    提前平倉避免被止損，並準備反向開單。
    """
    if state['direction'] == 0 or not state.get('entry_price'):
        return False
    if state['direction'] == model_direction:
        return False  # 模型方向未變，不翻倉
    price      = get_price(exchange)
    ep         = state['entry_price']
    loss_pct   = (price - ep) / ep * state['direction']  # 正=獲利，負=虧損
    threshold  = -(SL_PCT * 0.5)  # 虧損超過止損距離一半才觸發
    if loss_pct < threshold:
        side = 'LONG' if state['direction'] == 1 else 'SHORT'
        next_label = {1: '🟢 做多', -1: '🔴 做空', 0: '⚪ 空倉'}[model_direction]
        msg = (f"[{_COIN}] ⚠️ 提前翻倉 | {side} 虧損 {loss_pct*100*LEVERAGE:+.1f}% (margin)\n"
               f"避免被止損，反向開 {next_label}")
        log.info(msg)
        tg_send(msg)
        cancel_sltp(exchange, state)
        close_position(exchange, state)
        return True
    return False


def check_sltp_triggered(exchange, state: dict, next_direction: int = 0) -> bool:
    """偵測交易所止損/止盈是否已觸發（倉位被清零）。"""
    if state['direction'] == 0:
        return False
    try:
        positions = exchange.fetch_positions([SYMBOL])
        for pos in positions:
            if pos.get('symbol') == SYMBOL and abs(pos.get('contracts') or 0) > 0:
                return False  # 倉位仍存在
        # 倉位已清零 → 被止損或止盈
        ep        = state.get('entry_price') or 0
        cur_price = get_price(exchange)
        side      = 'LONG' if state['direction'] == 1 else 'SHORT'
        if ep:
            price_pct = (cur_price - ep) / ep * state['direction']
            trigger   = '🎯 止盈' if price_pct > 0 else '🛑 止損'
            pnl_pct   = price_pct * LEVERAGE * 100
            amt       = state.get('amount_coin', 0)
            pnl_usdt  = amt * (cur_price - ep) * state['direction']
            next_label = {1: '🟢 做多', -1: '🔴 做空', 0: '⚪ 空倉'}[next_direction]
            msg = (f"[{_COIN}] {trigger} {side} 平倉\n"
                   f"進場：{ep:,.2f} → 現價：{cur_price:,.2f}\n"
                   f"保證金盈虧：{pnl_pct:+.1f}%  ({pnl_usdt:+.2f} U)\n"
                   f"下一單：{next_label}")
        else:
            msg = f"[{_COIN}] SL/TP triggered on {side}"
        log.info(msg)
        tg_send(f"⚡ {msg}")
        reason = 'tp' if (ep and (cur_price - ep) / ep * state['direction'] > 0) else 'sl'
        log_trade(state, cur_price, reason)
        return True
    except Exception as e:
        log.warning(f"check_sltp_triggered error: {e}")
        return False

_TAKER_FEE = 0.0005  # Binance futures taker fee 0.05%

def log_trade(state: dict, close_price: float, reason: str):
    """每次平倉寫一筆 JSON 到 {coin}_trades.jsonl 供儀表板使用。"""
    ep       = state.get('entry_price') or close_price
    amt      = state.get('amount_coin', 0)
    pnl_usdt = amt * (close_price - ep) * state['direction']
    fee_usdt = amt * (ep + close_price) * _TAKER_FEE     # 開 + 平各一次 taker
    net_pnl  = pnl_usdt - fee_usdt
    margin   = amt * ep / LEVERAGE
    record = {
        'coin':         _COIN,
        'direction':    state['direction'],
        'entry_price':  ep,
        'close_price':  close_price,
        'amount':       amt,
        'pnl_usdt':     round(pnl_usdt, 4),
        'fee_usdt':     round(fee_usdt, 4),
        'net_pnl_usdt': round(net_pnl,  4),
        'margin_usdt':  round(margin,   4),
        'entry_time':   state.get('entry_time'),
        'close_time':   now8().isoformat(),
        'reason':       reason,
    }
    with open(f'{_COIN.lower()}_trades.jsonl', 'a', encoding='utf-8') as f:
        f.write(json.dumps(record) + '\n')


def cancel_sltp(exchange, state: dict):
    for key in ('sl_order_id', 'tp_order_id'):
        oid = state.get(key)
        if oid:
            try:
                exchange.cancel_order(oid, SYMBOL)
            except Exception:
                pass

def close_position(exchange, state: dict):
    if state['direction'] == 0 or state['amount_coin'] == 0:
        return
    cancel_sltp(exchange, state)
    amt      = state['amount_coin']
    price    = get_price(exchange)
    ep       = state.get('entry_price') or price
    price_pct = (price - ep) / ep * state['direction'] * 100
    pnl_pct   = price_pct * LEVERAGE
    side_str  = 'LONG' if state['direction'] == 1 else 'SHORT'
    try:
        params = {'reduceOnly': True}
        if state['direction'] == 1:
            exchange.create_market_sell_order(SYMBOL, amt, params=params)
        else:
            exchange.create_market_buy_order(SYMBOL, amt, params=params)
        pnl_usdt = amt * (price - ep) * state['direction']
        msg = (f"[{_COIN}] CLOSED {side_str} | {amt} {_COIN}\n"
               f"保證金盈虧：{pnl_pct:+.2f}%  ({pnl_usdt:+.2f} U)")
        log.info(msg)
        tg_send(f"🔒 {msg}")
        log_trade(state, price, 'signal')
    except Exception as e:
        log.error(f"Failed to close position: {e}")

def ensure_isolated_margin(exchange) -> bool:
    """
    保險：開倉前確保使用「逐倉」。
    若已存在「全倉」倉位導致無法切換，先強制平掉再切換。
    回傳是否成功設成 isolated。
    """
    # 1. 先試直接切 isolated
    try:
        exchange.set_margin_mode('isolated', SYMBOL)
        return True
    except Exception as e:
        msg = str(e).lower()
        if 'no need to change' in msg or 'same as' in msg:
            return True  # 已經是 isolated

    # 2. 切換失敗 → 檢查是否因為有開倉
    try:
        positions = exchange.fetch_positions([SYMBOL])
        for pos in positions:
            if pos.get('symbol') != SYMBOL:
                continue
            contracts = abs(pos.get('contracts') or 0)
            if contracts <= 0:
                continue
            margin_mode = pos.get('marginMode', '').lower()
            if margin_mode == 'cross' or margin_mode == 'crossed':
                side = 'sell' if pos.get('side') == 'long' else 'buy'
                log.warning(f"[{_COIN}] ⚠️ 偵測到全倉持倉 {contracts} {_COIN}，強制平倉以切換為逐倉")
                tg_send(f"⚠️ [{_COIN}] 偵測到全倉持倉，強制平倉以切換逐倉（保險機制）")
                try:
                    exchange.create_order(SYMBOL, 'market', side, contracts, params={'reduceOnly': True})
                    time.sleep(2)
                except Exception as e2:
                    log.error(f"強制平倉失敗：{e2}")
                    return False
    except Exception as e:
        log.warning(f"fetch_positions 失敗：{e}")

    # 3. 再試一次切 isolated
    try:
        exchange.set_margin_mode('isolated', SYMBOL)
        return True
    except Exception as e:
        msg = str(e).lower()
        if 'no need to change' in msg or 'same as' in msg:
            return True
        log.error(f"切換 isolated 失敗：{e}")
        return False


def open_position(exchange, direction: int, balance: float, regime: str = 'neutral'):
    if direction == 0:
        return 0.0, 0.0, None, None

    # 相關性保護：相關幣種同方向時倉位減半
    corr_dir  = get_correlated_direction()
    pos_scale = 0.5 if (corr_dir == direction and corr_dir != 0) else 1.0
    if pos_scale < 1.0:
        log.info(f"Correlation protection: same direction as correlated coin, scaling to 50%")

    # 震盪市倉位減半
    if regime == 'ranging':
        pos_scale *= 0.5
        log.info("Ranging market detected: scaling position to 50%")

    price    = get_price(exchange)
    margin   = balance * MAX_POS_PCT * pos_scale
    amount   = max(round(margin * LEVERAGE / price, 4), 0.001)
    side_str = 'LONG' if direction == 1 else 'SHORT'
    sl_id = tp_id = None

    try:
        # 保險：強制 isolated（若有全倉持倉會先平掉）
        if not ensure_isolated_margin(exchange):
            log.error(f"[{_COIN}] 無法切換為逐倉，跳過本次開倉")
            tg_send(f"❌ [{_COIN}] 無法切換為逐倉，本次開倉中止")
            return 0.0, price, None, None
        try:
            exchange.set_leverage(LEVERAGE, SYMBOL, params={'marginMode': 'isolated'})
        except Exception as e:
            log.warning(f"Leverage setup: {e}")

        if direction == 1:
            exchange.create_market_buy_order(SYMBOL, amount)
        else:
            exchange.create_market_sell_order(SYMBOL, amount)

        sl_side = 'sell' if direction == 1 else 'buy'

        # 止損：先試追蹤止損；若失敗（Demo 不支援）→ 退回固定 STOP_MARKET
        sl_order = None
        if TRAILING_SL:
            try:
                sl_order = exchange.create_order(SYMBOL, 'trailing_stop_market', sl_side, amount, None, {
                    'callbackRate':   SL_PCT * 100,
                    'closePosition':  True,
                    'workingType':    'MARK_PRICE',
                })
            except Exception as e:
                log.warning(f"Trailing SL failed ({e}) — falling back to fixed STOP_MARKET")
        if sl_order is None:
            try:
                sl_price = round(price * (1 - SL_PCT) if direction == 1 else price * (1 + SL_PCT), 2)
                sl_order = exchange.create_order(SYMBOL, 'stop_market', sl_side, amount, None, {
                    'stopPrice': sl_price, 'closePosition': True, 'workingType': 'MARK_PRICE',
                })
            except Exception as e:
                log.error(f"Fixed SL also failed: {e}")
        sl_id = sl_order['id'] if sl_order else None
        if sl_id is None:
            tg_send(f"⚠️ [{_COIN}] 止損訂單未掛上，請手動處理！")

        # 固定止盈
        try:
            tp_price = round(price * (1 + TP_PCT) if direction == 1 else price * (1 - TP_PCT), 2)
            tp_order = exchange.create_order(SYMBOL, 'take_profit_market', sl_side, amount, None, {
                'stopPrice': tp_price, 'closePosition': True, 'workingType': 'MARK_PRICE',
            })
            tp_id = tp_order['id']
        except Exception as e:
            log.warning(f"TP order failed: {e}")

        sl_desc = f"Trailing {SL_PCT*100:.0f}%" if TRAILING_SL else f"SL {SL_PCT*100:.0f}%"
        msg = (f"[{_COIN}] OPENED {side_str} {LEVERAGE}x | {amount} {_COIN} @ ~{price:,.2f} "
               f"| {sl_desc} | TP {TP_PCT*100:.0f}% | Regime: {regime}")
        log.info(msg)
        tg_send(f"{'🟢' if direction == 1 else '🔴'} {msg}")
        return amount, price, sl_id, tp_id
    except Exception as e:
        log.error(f"Failed to open position: {e}")
        return 0.0, price, None, None


# ── 最大回撤保護 ─────────────────────────────────────────────────────────────
def check_max_drawdown(balance: float, state: dict) -> bool:
    if state.get('paused'):
        log.warning(f"[{_COIN}] ⛔ Bot 已暫停（回撤保護），跳過本次交易")
        return True
    peak = state.get('peak_balance') or balance
    state['peak_balance'] = max(peak, balance)
    drawdown = (state['peak_balance'] - balance) / state['peak_balance']
    if drawdown >= MAX_DD_PCT:
        msg = (f"[{_COIN}] 🚨 最大回撤保護觸發！\n"
               f"峰值餘額：{state['peak_balance']:,.2f} U\n"
               f"當前餘額：{balance:,.2f} U\n"
               f"回撤：{drawdown*100:.1f}%（閾值 {MAX_DD_PCT*100:.0f}%）\n"
               f"⛔ 暫停交易，請手動在 .json 狀態檔將 paused 改為 false 恢復")
        log.warning(msg)
        tg_send(msg)
        state['paused'] = True
        return True
    return False


# ── 每日績效報告 ──────────────────────────────────────────────────────────────
def handle_daily_report(balance: float, state: dict, now: datetime):
    daily_time = state.get('daily_open_time')
    if daily_time:
        last_day = datetime.fromisoformat(daily_time).date()
        if now.date() > last_day:
            open_bal = state.get('daily_open_balance') or balance
            pnl_usdt = balance - open_bal
            pnl_pct  = pnl_usdt / open_bal * 100 if open_bal else 0
            emoji    = '📈' if pnl_usdt >= 0 else '📉'
            tg_send(
                f"{emoji} <b>[{_COIN}] 每日績效報告</b>\n"
                f"📅 {last_day}\n\n"
                f"開始餘額：{open_bal:,.2f} U\n"
                f"結束餘額：{balance:,.2f} U\n"
                f"日盈虧：{pnl_usdt:+.2f} U（{pnl_pct:+.2f}%）"
            )
            log.info(f"Daily report: {pnl_usdt:+.2f} U ({pnl_pct:+.2f}%)")
    if not daily_time or now.date() > datetime.fromisoformat(daily_time).date():
        state['daily_open_balance'] = balance
        state['daily_open_time']    = now.isoformat()


# ── 健康檢查心跳 ──────────────────────────────────────────────────────────────
def handle_heartbeat(state: dict, now: datetime):
    last = state.get('last_heartbeat')
    if last is None or (now - datetime.fromisoformat(last)).total_seconds() >= 86400:
        tg_send(
            f"💚 <b>[{_COIN}] Bot 健康檢查</b>\n"
            f"⏰ {now.strftime('%Y-%m-%d %H:%M +08')}\n"
            f"✅ 運行正常，每 24h 發送一次"
        )
        state['last_heartbeat'] = now.isoformat()


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

    # ── 選擇性載入 4h 模型 ──
    model_4h = scaler_4h = feature_cols_4h = seq_len_4h = None
    if MULTI_TF and Path(MODEL_4H_PATH).exists() and Path(SCALER_4H_PATH).exists():
        ckpt4  = torch.load(MODEL_4H_PATH, map_location='cpu', weights_only=False)
        cfg4   = ckpt4['config']
        model_4h = TransformerPredictor(
            cfg4['n_features'], cfg4['d_model'], cfg4['nhead'],
            cfg4['num_layers'], cfg4['dropout'], cfg4['seq_len'],
        )
        model_4h.load_state_dict(ckpt4['model_state'])
        model_4h.eval()
        scaler_4h      = joblib.load(SCALER_4H_PATH)
        feature_cols_4h = cfg4['feature_cols']
        seq_len_4h     = cfg4['seq_len']
        log.info(f"4h Model: {MODEL_4H_PATH}  ({cfg4['n_features']} features)")

    mode_str = 'Long/Flat' if LONG_FLAT_ONLY else 'Long/Short'
    mtf_str  = ' + 4h confirm' if model_4h else ''
    log.info(f"Coin   : {_COIN}  ({mode_str}{mtf_str})")
    log.info(f"Model  : {MODEL_PATH}  ({cfg['n_features']} features, seq_len={seq_len})")

    exchange = ccxt.binance({
        'apiKey':  os.getenv('BINANCE_API_KEY', ''),
        'secret':  os.getenv('BINANCE_SECRET_KEY', ''),
        'enableRateLimit': True,
        'options': {'defaultType': 'future'},
    })
    if DEMO_MODE:
        exchange.enable_demo_trading(True)
        log.info("Exchange: Binance Futures (DEMO MODE)")
    else:
        log.warning("Exchange: Binance Futures ⚠️  LIVE MODE — real money!")
        tg_send(f"⚠️ [{_COIN}] Bot 啟動於 LIVE 模式，使用真實資金！")
    exchange_pub = ccxt.binance({'enableRateLimit': True})
    log.info(f"Config  : max_pos={MAX_POS_PCT*100:.0f}%  min_hold={MIN_HOLD_HOURS}h  threshold={THRESHOLD}")

    state = load_state()
    log.info(f"State   : dir={state['direction']}  entry={state.get('entry_time', '—')}")

    while True:
        tick_start = time.time()
        now        = now8()
        log.info(f"\n{'='*55}")
        log.info(f"  [{_COIN}] {now.strftime('%Y-%m-%d %H:%M +08')}")
        log.info(f"{'='*55}")

        try:
            # 每 tick 取一次餘額，供回撤保護和每日報告使用
            balance = get_balance(exchange)
            handle_daily_report(balance, state, now)
            handle_heartbeat(state, now)
            save_state(state)

            if check_max_drawdown(balance, state):
                save_state(state)
                time.sleep(INTERVAL_SECS)
                continue

            log.info("Fetching market data ...")
            df   = fetch_tick_data(exchange_pub, feature_cols, timeframe='1h')
            prob, dir_1h = predict(model, scaler, df, feature_cols, seq_len)
            regime = detect_regime(df)

            # 多時框確認
            if model_4h is not None:
                df_4h = fetch_tick_data(exchange_pub, feature_cols_4h, timeframe='4h')
                prob_4h, dir_4h = predict(model_4h, scaler_4h, df_4h, feature_cols_4h, seq_len_4h)
                direction = dir_1h if dir_1h == dir_4h else 0
                log.info(f"1h Signal : {dir_1h} (prob={prob:.4f})")
                log.info(f"4h Signal : {dir_4h} (prob={prob_4h:.4f})")
                log.info(f"Combined  : {direction}  ({'AGREE ✅' if dir_1h == dir_4h else 'DISAGREE ⚪'})")
            else:
                direction = dir_1h
                log.info(f"Signal  : {direction}  (prob={prob:.4f}  conf={abs(prob-0.5)*200:.1f}%)")

            log.info(f"Regime  : {regime}")
            explain_prediction(model, scaler, df, feature_cols, seq_len)

            # KOL overlay filters
            kol = compute_kol_filters(exchange_pub, df)
            if kol['ema200']:
                log.info(f"EMA200  : {kol['ema200']:,.2f}  ({kol['ma200_ratio']:+.2%})")
            log.info(f"KOL     : squeeze_up={kol['squeeze_fuel_up']}  "
                     f"fake_break={kol['fake_breakout_risk']}  "
                     f"right_side={kol['right_side_long']}  "
                     f"fr_flip_neg={kol['fr_flip_negative']}  "
                     f"near_sup={kol['near_support']}  "
                     f"spyqqq_down={kol['spy_qqq_declining']}")
            _close_now = float(df['close'].iloc[-1])
            _zone_tag  = ('🟢 支撐區' if kol['in_support_zone'] else
                          '🔴 壓力區' if kol['in_resistance_zone'] else '⬜ 中性')
            log.info(f"Zone    : {_zone_tag}  price={_close_now:,.2f}  "
                     f"sup={KEY_SUPPORT_ZONE}  res={KEY_RESISTANCE_ZONE}  "
                     f"squeeze_short_risk={kol['squeeze_short_risk']}")

            # ── KOL direction filters ──────────────────────────────────────────
            # 假突破風險：資費過熱 + 緊貼 EMA200 → 暫停 LONG
            if direction == 1 and kol['fake_breakout_risk']:
                log.info("KOL FILTER: 假突破風險 — 暫停 LONG（資費過熱 + 緊貼 EMA200）")
                tg_send(f"⚠️ [{_COIN}] KOL: 假突破風險，LONG 暫停"
                        f"（fr={kol['fr_raw']:.5f}，ma200={kol['ma200_ratio']:+.2%}）")
                direction = 0

            # 靠近支撐區做空風險：price ≤ EMA200×1.02 且資費非正 → 暫停 SHORT
            if direction == -1 and kol['near_support'] and kol['fr_raw'] <= 0:
                log.info("KOL FILTER: 靠近支撐區且資費非正 — 暫停 SHORT（空在支撐上風險）")
                direction = 0

            # 大幅負費率 + OI 顯著（fr_z 代理）→ 暫停做空，嘎空風險（加密龐克第二輪）
            if direction == -1 and kol['squeeze_short_risk']:
                log.info(f"KOL FILTER: 大幅負費率+OI顯著 — 暫停 SHORT "
                         f"（fr={kol['fr_raw']:.5f}，|fr_z|>1.5σ）")
                tg_send(f"⚠️ [{_COIN}] KOL: 大幅負費率嘎空風險，SHORT 暫停"
                        f"（fr={kol['fr_raw']:.5f}）")
                direction = 0

            # 資費翻負（嘎空動能訊號）→ TG 通知
            if kol['fr_flip_negative']:
                tg_send(f"📡 [{_COIN}] KOL: 資費由正轉負（fr={kol['fr_raw']:.5f}）"
                        f" — 嘎空動能訊號，留意 EMA200 突破機會")

            # 美股回調 → 記錄加密流動性釋放訊號
            if kol['spy_qqq_declining']:
                log.info("KOL NOTE: 美股下跌 > 0.5%，留意加密市場流動性釋放（潛在多頭催化劑）")

            # ── KOL 開倉 regime 調整 ─────────────────────────────────────────
            # entry_regime 用於 open_position 倉位計算，不修改原始 regime（保留 log 正確性）
            entry_regime = regime

            if direction == 1:
                if kol['squeeze_fuel_up']:
                    # 軋空燃料：即使震盪市也維持正常倉位，機率偏向 squeeze
                    if entry_regime == 'ranging':
                        entry_regime = 'neutral'
                        log.info("KOL FILTER: 軋空燃料 — 覆蓋震盪市縮倉，維持正常倉位")
                    else:
                        log.info("KOL FILTER: 軋空燃料偵測 — LONG 確認度高")
                elif kol['fr_raw'] > 0.0005:
                    # 資費過熱（但不在 EMA200 壓力區，fake_breakout 已處理那種情況）→ 縮倉 50%
                    entry_regime = 'ranging'
                    log.info("KOL FILTER: 資費過熱（非壓力區），倉位縮至 50%")

            if direction == 1 and kol['right_side_long']:
                log.info("KOL FILTER: 右側交易確認 — 3 日均收盤站上 EMA200，高確定性 LONG 窗口")

            # 1a. 提前翻倉（震盪偵測）
            if check_preemptive_reversal(exchange, state, model_direction=direction):
                state.update(_DEFAULT_STATE.copy())
                save_state(state)
                if direction != 0:
                    balance = get_balance(exchange)
                    amt, price, sl_id, tp_id = open_position(exchange, direction, balance, regime=entry_regime)
                    state.update({
                        'direction':   direction,
                        'amount_coin': amt,
                        'entry_time':  now.isoformat(),
                        'entry_price': price,
                        'sl_order_id': sl_id,
                        'tp_order_id': tp_id,
                    })
                    save_state(state)

            # 1b. 偵測交易所 SL/TP 是否已觸發
            if check_sltp_triggered(exchange, state, next_direction=direction):
                prev_dir = state['direction']
                state.update(_DEFAULT_STATE.copy())
                save_state(state)
                # 立即根據最新信號開下一單
                if direction != 0:
                    balance = get_balance(exchange)
                    log.info(f"SL/TP triggered — immediately re-entering {dir_label}")
                    amt, price, sl_id, tp_id = open_position(exchange, direction, balance, regime=entry_regime)
                    state.update({
                        'direction':   direction,
                        'amount_coin': amt,
                        'entry_time':  now.isoformat(),
                        'entry_price': price,
                        'sl_order_id': sl_id,
                        'tp_order_id': tp_id,
                    })
                    save_state(state)

            # 2. Min-hold 鎖倉檢查
            locked = False
            if state['direction'] != 0 and state.get('entry_time'):
                held_h = (now - datetime.fromisoformat(state['entry_time'])).total_seconds() / 3600
                remain = max(0, MIN_HOLD_HOURS - held_h)
                log.info(f"Held    : {held_h:.1f}h  |  Lock remaining: {remain:.1f}h")
                if remain > 0 and direction != state['direction']:
                    log.info("Min-hold active — maintaining current position")
                    locked = True

            # 3. 執行信號
            if not locked and direction != state['direction']:
                balance = get_balance(exchange)
                log.info(f"Balance : {balance:,.2f} USDT")

                if state['direction'] != 0:
                    close_position(exchange, state)
                    state.update(_DEFAULT_STATE.copy())
                    save_state(state)
                    time.sleep(1)

                if direction != 0:
                    amt, price, sl_id, tp_id = open_position(exchange, direction, balance, regime=entry_regime)
                    state.update({
                        'direction':   direction,
                        'amount_coin': amt,
                        'entry_time':  now.isoformat(),
                        'entry_price': price,
                        'sl_order_id': sl_id,
                        'tp_order_id': tp_id,
                    })
                    save_state(state)
                else:
                    log.info("Signal is FLAT — staying out of market")
                    save_state(state)
            else:
                cur_label = {1: 'LONG', -1: 'SHORT', 0: 'FLAT'}[state['direction']]
                log.info(f"No action — holding {cur_label}")

            # 寫 status 檔（供 coin-monitor 彙總為整點持倉公告）
            try:
                cur_price = get_price(exchange)
                balance   = get_balance(exchange)
                ep        = state.get('entry_price') or cur_price
                amt       = state.get('amount_coin', 0)
                d         = state['direction']
                price_pct = (cur_price - ep) / ep * d * 100 if d else 0.0
                pnl_pct   = price_pct * LEVERAGE
                held_h    = (
                    (now - datetime.fromisoformat(state['entry_time'])).total_seconds() / 3600
                    if d and state.get('entry_time') else 0.0
                )
                Path(f'{_COIN.lower()}_status.json').write_text(json.dumps({
                    'coin':        _COIN,
                    'direction':   d,
                    'amount':      amt,
                    'entry_price': ep,
                    'cur_price':   cur_price,
                    'price_pct':   round(price_pct, 2),
                    'pnl_pct':     round(pnl_pct, 2),
                    'held_h':      round(held_h, 1),
                    'balance':     round(balance, 2),
                    'updated_at':  now.isoformat(),
                }))
            except Exception as e:
                log.warning(f"Status write error: {e}")

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
