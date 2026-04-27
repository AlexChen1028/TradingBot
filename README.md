# TradingBot

ML-powered crypto futures trading bot for BTC, ETH, SOL and altcoins.  
Runs 24/7 on a VPS via Docker, sends all notifications to Telegram.

---

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                  Docker Compose (VPS)               │
│                                                     │
│  trading-bot  ── BTC  1h + 4h Transformer model    │
│  eth-bot      ── ETH  1h + 4h Transformer model    │
│  sol-bot      ── SOL  1h     Transformer model      │
│  coin-monitor ── Altcoin scanner + auto-trader      │
└─────────────────────────────────────────────────────┘
           │  Telegram notifications (2 users)
           ▼
   Trading Bot 1  &  Alert Bot
```

---

## Features

### ML Models (`main.py`)
- **Walk-Forward Transformer** (Pre-LN Encoder) trained with `train_wf.py`
- **44 features** for ETH (40 base + 4 BTC cross-asset); 40 for BTC / SOL
  - Technical: EMA, RSI, MACD, Bollinger Bands, ATR, OBV, realized volatility
  - Market context: SPY / QQQ / VIX / GLD daily returns
  - Fear & Greed Index, funding rate (z-score, MA, 28-day cumsum)
  - News sentiment (RSS feeds + VADER)
  - ETH/BTC ratio momentum (ETH model only)
- **Multi-timeframe confirmation**: 1h + 4h models must agree, else FLAT
- **Regime detection**: ATR ratio → trending / ranging / neutral

### Risk Management (`main.py`)

| Feature | Detail |
|---|---|
| Leverage | 20x isolated margin |
| Position size | 5% of account balance per trade |
| Stop-loss | Exchange-level `STOP_MARKET` (3%) |
| Take-profit | Exchange-level `TAKE_PROFIT_MARKET` (5%) |
| Trailing SL | `TRAILING_STOP_MARKET` (3% callback) |
| Min hold | 6-hour lock after entry |
| Preemptive reversal | Flip early if model reverses + unrealised loss > 1.5% |
| Max drawdown guard | Pause all trading if account DD ≥ 20% |
| Correlation guard | Halve position size if BTC and ETH are in the same direction |
| Isolated-margin guard | Detects existing cross-margin positions, force-closes them, then opens isolated |

### Altcoin Monitor (`monitor_coins.py`)
- Scans top 20 high-volatility USDT perpetual futures every **15 minutes**
- Four signals: volume spike ×1.5, price compression, 14-day breakout proximity, funding rate surge
- **Leaderboard trading**: Binance 24h top gainers / losers → auto-enter on 2+ signals
- **$50 USDT × 20x isolated margin** per trade (max 3 open positions)
- Exchange-level SL (3%) + TP (6%) placed immediately on entry
- Detects exchange auto-close via SL/TP and logs net result
- Hourly Binance spot leaderboard (top 10 gainers + losers) sent to Telegram

### Telegram Notifications
- **Every tick**: hourly position report — direction, price change, margin P&L, balance
- **On open**: entry price, size, leverage, SL/TP confirmation
- **On close**: gross profit, fee deduction, net result, next signal
- **SL/TP trigger**: notified instantly with P&L breakdown
- **Daily 00:00 +08**: rolling 7-day performance report — net profit, fees, ROI per coin
- **Daily balance report**: opening vs closing balance each day
- **Heartbeat**: once every 24h to confirm the bot is alive
- Broadcasts to **2 Telegram users** simultaneously

---

## File Structure

```
├── main.py              # Main ML bot (BTC / ETH / SOL)
├── monitor_coins.py     # Altcoin scanner + auto-trader
├── data.py              # Feature engineering and data fetching
├── train_wf.py          # Walk-forward training script
├── backtest.py          # Backtesting framework
├── social_sentiment.py  # Reddit + CoinGecko sentiment (optional)
├── dashboard.py         # HTML trade dashboard generator
├── docker-compose.yml   # Multi-service deployment
├── Dockerfile           # Container image
└── retrain.sh           # Monthly model retraining cron script
```

---

## Environment Variables (`.env`)

```env
BINANCE_API_KEY=...
BINANCE_SECRET_KEY=...

# Main bots (BTC / ETH / SOL) — Trading Bot 1
TELEGRAM_TOKEN=...
TELEGRAM_CHAT_ID=user1_id,user2_id

# Altcoin monitor — Alert Bot
MONITOR_TOKEN=...
MONITOR_CHAT_ID=user1_id,user2_id
```

Additional variables configurable in `.env` or `docker-compose.yml`:

| Variable | Default | Description |
|---|---|---|
| `SYMBOL` | `BTC` | Coin to trade (`BTC` / `ETH` / `SOL`) |
| `LONG_FLAT_ONLY` | `false` | Disable short positions |
| `MULTI_TF` | `true` | Enable 1h + 4h confirmation |
| `LEVERAGE` | `20` | Futures leverage |
| `SL_PCT` | `0.03` | Stop-loss distance from entry |
| `TP_PCT` | `0.05` | Take-profit distance from entry |
| `MAX_DD_PCT` | `0.20` | Max drawdown before pausing |
| `DEMO_MODE` | `true` | Use Binance Demo Trading |

---

## Training a Model

```bash
# BTC 1h model
python train_wf.py --coin BTC --epochs 30 --balance_classes

# ETH 4h model
python train_wf.py --coin ETH --timeframe 4h --epochs 30 --balance_classes \
  --target_ahead 6 --min_move 0.005

# Key flags
#   --balance_classes   oversample minority class (fixes long/short imbalance)
#   --target_ahead N    predict N bars ahead (default 6)
#   --min_move 0.005    only label moves > 0.5% (filters noise)
#   --d_model 64        Transformer embedding dimension
#   --seq_len 60        input sequence length in bars
```

Output: `{coin}_model_wf.pt` + `{coin}_scaler_wf.pkl`  
For 4h timeframe: `{coin}_4h_model_wf.pt` + `{coin}_4h_scaler_wf.pkl`

---

## Deployment (VPS)

```bash
# 1. Clone and configure
git clone https://github.com/AlexChen1028/TradingBot.git
cd TradingBot
cp .env.example .env        # fill in API keys and Telegram tokens

# 2. Create required state files (must be FILES, not directories)
printf '{}' > btc_state.json
printf '{}' > eth_state.json
printf '{}' > sol_state.json
printf '{}' > positions_altcoin.json
touch btc_bot.log eth_bot.log sol_bot.log
touch btc_trades.jsonl eth_trades.jsonl sol_trades.jsonl altcoin_trades.jsonl

# 3. Upload trained models from local machine
scp *_model_wf.pt *_scaler_wf.pkl root@<VPS_IP>:~/TradingBot/

# 4. Start all services
docker compose up -d --build

# 5. Monitor logs
docker compose logs -f trading-bot
docker compose logs -f coin-monitor
```

### Updating after code changes

```bash
ssh root@<VPS_IP>
cd ~/TradingBot && git pull && docker compose up -d --build
```

State files (`*_state.json`, `*_trades.jsonl`, `positions_altcoin.json`) are mounted as host volumes, so rebuilds **never lose** trade history or open positions.

---

## Fee & P&L Accounting

Every closed trade is appended to `{coin}_trades.jsonl`:

| Field | Description |
|---|---|
| `pnl_usdt` | Gross P&L in USDT |
| `fee_usdt` | Taker fee × 2 sides (0.05% open + 0.05% close) |
| `net_pnl_usdt` | `pnl_usdt − fee_usdt` |
| `margin_usdt` | Margin deployed for the trade |

The daily 7-day Telegram report aggregates all `*_trades.jsonl` files:
- **Net profit** = Σ `net_pnl_usdt` over past 7 days
- **ROI** = net profit ÷ total margin deployed × 100%

---

## Notes

- All bots run in **Binance Demo mode** by default (`DEMO_MODE=true`). Set to `false` for live trading.
- DigitalOcean blocks `fapi.binance.com` → public market data uses the spot API (`api.binance.com`).
- Models are **not** committed to this repo (too large). Train locally and upload to VPS via `scp`.
- OHLCV fetch uses paginated `since`-based requests to bypass Binance's silent limit cap on recent-only fetches (avoids `Not enough data: <N> rows` errors).

---

## Troubleshooting

**`Tick error: Not enough data: <N> rows (need <seq_len>)`**  
Fixed in current code. Pull latest and rebuild — `git pull && docker compose up -d --build`.

**Existing position is on cross margin (全倉) instead of isolated (逐倉)**  
The `ensure_isolated_margin` guard auto-handles this on the next signal: detects the cross position, force-closes it via `reduceOnly`, switches to isolated, then opens the new trade. You'll see `⚠️ 偵測到全倉持倉，強制平倉以切換逐倉` in Telegram.

**Weekly P&L report shows "尚無已完成交易記錄"**  
Means no trades have closed since `*_trades.jsonl` files were mounted. Trade records are only written on close (signal flip / SL / TP). Wait for the next position to close.

**`*_trades.jsonl` is a directory instead of a file**  
Docker creates a directory if the file doesn't exist before `docker compose up`. Fix:
```bash
docker compose down
rm -rf <bad_file>
touch <bad_file>
docker compose up -d --build
```

**Telegram bot not posting to group**  
1. Verify both bots are members of the group
2. Group `chat_id` starts with `-` (negative number)
3. After updating `.env`, run `docker compose down && docker compose up -d` (`restart` won't reload env)
