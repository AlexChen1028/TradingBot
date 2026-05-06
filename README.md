# TradingBot

ML-powered crypto futures trading bot for BTC, ETH, SOL and altcoins.  
Runs 24/7 on a VPS via Docker, sends all notifications to Telegram.

> Last updated: 2026-05-06 00:30 +08

---

## Current Status

| Service | Status | Note |
|---|---|---|
| `trading-bot` (BTC) | Paused | ML model underperforming, retraining in progress |
| `eth-bot` (ETH) | Paused | ML model underperforming, retraining in progress |
| `sol-bot` (SOL) | Paused | ML model underperforming, retraining in progress |
| `coin-monitor` | **Running** | Altcoin strategy profitable, running solo |

---

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                  Docker Compose (VPS)               │
│                                                     │
│  trading-bot  ── BTC  1h + 4h Transformer model    │  ← currently paused
│  eth-bot      ── ETH  1h + 4h Transformer model    │  ← currently paused
│  sol-bot      ── SOL  1h     Transformer model      │  ← currently paused
│  coin-monitor ── Altcoin scanner + auto-trader      │  ← active
└─────────────────────────────────────────────────────┘
           │  Telegram notifications → Telegram group
           ▼
   Trading Bot  &  Alert Bot  (both in same group)
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
| Stop-loss | `TRAILING_STOP_MARKET` (3% callback); fallback to `STOP_MARKET` on Demo |
| Take-profit | Exchange-level `TAKE_PROFIT_MARKET` (5%) |
| Min hold | 6-hour lock after entry |
| Preemptive reversal | Flip early if model reverses + unrealised loss > 1.5% |
| Max drawdown guard | Pause all trading if account DD ≥ 20% |
| Correlation guard | Halve position size if BTC and ETH are in the same direction |
| Isolated-margin guard | Detects cross-margin positions, force-closes, retries isolated |

### Altcoin Monitor (`monitor_coins.py`)

**Signal sources (every 15 minutes):**
- Volume spike ≥ 1.5× 24h average
- Price compression (recent range ≤ 50% of average)
- Within 3% of 14-day high (breakout proximity)
- Funding rate surge ≥ 0.02%

**Leaderboard trading (hourly):**
- Binance 24h top gainers / losers → auto-enter on 2+ signals
- Min 24h move ≥ 3% to qualify

**Position parameters:**

| Parameter | Value |
|---|---|
| Margin per trade | $60 USDT × 20x isolated |
| Max open positions | Unlimited |
| Trailing stop | Exchange `TRAILING_STOP_MARKET` 3.5% callback |
| TP ceiling | Exchange `TAKE_PROFIT_MARKET` 15% (hard cap) |
| Software trailing backup | 3.5% from peak (activates if exchange order fails) |
| Max hold time | 36 hours |

### Breaking News Detector (`monitor_coins.py`)
- Polls CoinDesk, CoinTelegraph, Decrypt RSS every scan cycle
- Filters by keyword (crash, hack, SEC, regulation, rate cut, etc.)
- VADER sentiment threshold: |compound| ≥ 0.25
- Deduplicates via `news_seen.json` (24-hour window)
- Sends to Telegram group with 🟢 bullish / 🔴 bearish / ⚠️ neutral tag

### Telegram Notifications
- **Every hour (整點)**: consolidated position report — all BTC/ETH/SOL + altcoin positions in one message
- **On open**: entry price, size, trailing SL confirmation, TP ceiling
- **On close**: gross profit, fee, net result, close reason
- **SL/TP trigger**: exchange auto-close detected and logged
- **Weekly (every 7 days)**: rolling 7-day performance — net profit, fees, ROI per coin
- **Daily**: opening vs closing balance report
- **Heartbeat**: once every 24h to confirm bot is alive
- **Breaking news**: real-time market-moving headlines with sentiment tag
- All notifications → shared **Telegram group**

---

## Backtest

```bash
# ML model backtest (BTC / ETH / SOL)
python backtest.py --coin BTC
python backtest.py --coin ETH
python backtest.py --coin SOL

# Optional flags
python backtest.py --coin BTC --since 2022-01-01 --fee 0.0004 --min_hold 24

# Altcoin volatile strategy backtest (top 20 coins, 180-day)
python backtest_volatile.py
```

**Output metrics:**

| Metric | Description |
|---|---|
| Total Return | Cumulative return over the test period |
| Ann. Return | Annualised return |
| **Sharpe** | (Ann. return − 4%) / annualised vol |
| **Max DD** | Maximum peak-to-trough drawdown |
| **Calmar** | Ann. return / abs(Max DD) — return per unit of drawdown risk |
| **Sortino** | Like Sharpe but only penalises downside volatility |
| **Profit Factor** | Total gains / total losses — >1.5 is good |
| **Win/Loss Ratio** | Avg winning return / avg losing return |

**Model quality guide:**

| Metric | Acceptable | Good |
|---|---|---|
| Sharpe | > 0.5 | > 1.0 |
| Calmar | > 0.3 | > 0.5 |
| Sortino | > 0.8 | > 1.5 |
| Profit Factor | > 1.2 | > 1.5 |

---

## File Structure

```
├── main.py              # Main ML bot (BTC / ETH / SOL)
├── monitor_coins.py     # Altcoin scanner + auto-trader
├── data.py              # Feature engineering and data fetching
├── train_wf.py          # Walk-forward training script
├── backtest.py          # ML model backtesting (supports --coin BTC/ETH/SOL)
├── backtest_volatile.py # Altcoin volatile strategy backtest
├── social_sentiment.py  # Reddit + CoinGecko sentiment (optional)
├── docker-compose.yml   # Multi-service deployment
├── Dockerfile           # Container image
└── fix_sltp.py          # Helper: add missing SL/TP to open positions
```

---

## Environment Variables (`.env`)

```env
BINANCE_API_KEY=...
BINANCE_SECRET_KEY=...

# Main bots (BTC / ETH / SOL) — Trading Bot
TELEGRAM_TOKEN=...
TELEGRAM_CHAT_ID=-100xxxxxxxxxx        # Telegram group chat_id (negative number)

# Altcoin monitor — Alert Bot
MONITOR_TOKEN=...
MONITOR_CHAT_ID=-100xxxxxxxxxx         # Same group recommended
```

> Both `TELEGRAM_CHAT_ID` and `MONITOR_CHAT_ID` accept comma-separated IDs:
> - **Group chat**: a single negative number (e.g. `-5279333490`) — recommended
> - **Multiple individual users**: `id1,id2,id3` (positive numbers)

Additional variables configurable in `.env` or `docker-compose.yml`:

| Variable | Default | Description |
|---|---|---|
| `SYMBOL` | `BTC` | Coin to trade (`BTC` / `ETH` / `SOL`) |
| `LONG_FLAT_ONLY` | `false` | Disable short positions |
| `MULTI_TF` | `true` | Enable 1h + 4h confirmation |
| `LEVERAGE` | `20` | Futures leverage |
| `SL_PCT` | `0.03` | Trailing stop callback rate |
| `TP_PCT` | `0.05` | Take-profit distance from entry |
| `MAX_DD_PCT` | `0.20` | Max drawdown before pausing |
| `DEMO_MODE` | `true` | Use Binance Demo Trading |

---

## Telegram Group Setup

1. **Create a Telegram group** and add both bots as members.
2. **Send any message** in the group.
3. **Get the group `chat_id`**:
   ```
   https://api.telegram.org/bot<TOKEN>/getUpdates
   ```
   Look for `"chat":{"id":-100xxxxxxxxxx,...,"type":"supergroup"}` — the ID is negative.
4. **Update `.env`**:
   ```env
   TELEGRAM_CHAT_ID=-100xxxxxxxxxx
   MONITOR_CHAT_ID=-100xxxxxxxxxx
   ```
5. **Apply on VPS**:
   ```bash
   docker compose down && docker compose up -d
   ```

---

## Training a Model

```bash
# BTC 1h model
python train_wf.py --symbol BTC/USDT

# ETH 1h model (with BTC cross-asset features)
python train_wf.py --symbol ETH/USDT

# SOL 1h model
python train_wf.py --symbol SOL/USDT

# ETH 4h model
python train_wf.py --symbol ETH/USDT --timeframe 4h --target_ahead 6

# Key flags
#   --train_months 18   rolling training window size (default 18)
#   --epochs 60         training epochs per window
#   --balance_classes   oversample minority class
#   --target_ahead N    predict N bars ahead (default from data.py)
#   --min_move 0.005    only label moves > 0.5%
```

Output: `{coin}_model_wf.pt` + `{coin}_scaler_wf.pkl`  
Generates `{coin}_backtest_wf.png` with equity curve + drawdown chart.

### Retraining on VPS (tmux/screen recommended)

```bash
screen -S btc-train
docker compose exec trading-bot python train_wf.py --symbol BTC/USDT
# Ctrl+A D to detach, screen -r btc-train to reattach
```

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
touch btc_status.json eth_status.json sol_status.json
touch news_seen.json

# 3. Start services (coin-monitor only, main bots paused)
docker compose up -d coin-monitor

# 4. Start all services (when main bots are ready)
docker compose up -d --build

# 5. Monitor logs
docker compose logs -f coin-monitor
```

### Updating after code changes

```bash
ssh root@<VPS_IP>
cd ~/TradingBot && git pull
# For volume-mounted files (monitor_coins.py, main.py):
docker compose restart coin-monitor
# For Dockerfile changes:
docker compose up -d --build
```

State files (`*_state.json`, `*_trades.jsonl`, `positions_altcoin.json`) are mounted as host volumes — rebuilds **never lose** trade history or open positions.

---

## Fee & P&L Accounting

Every closed trade is appended to `{coin}_trades.jsonl`:

| Field | Description |
|---|---|
| `pnl_usdt` | Gross P&L in USDT |
| `fee_usdt` | Taker fee × 2 sides (0.05% open + 0.05% close) |
| `net_pnl_usdt` | `pnl_usdt − fee_usdt` |
| `margin_usdt` | Margin deployed for the trade |

The weekly Telegram report aggregates all `*_trades.jsonl` files:
- **Net profit** = Σ `net_pnl_usdt` over past 7 days
- **ROI** = net profit ÷ total margin deployed × 100%

---

## Troubleshooting

**`Tick error: Not enough data: <N> rows (need <seq_len>)`**  
Fixed in current code. Pull latest and rebuild — `git pull && docker compose up -d --build`.

**Existing position is on cross margin (全倉) instead of isolated (逐倉)**  
The `ensure_isolated_margin` guard auto-handles this: detects cross position, force-closes it via `reduceOnly`, switches to isolated, opens new trade. You'll see `⚠️ 偵測到全倉持倉` in Telegram.

**Weekly P&L report shows "尚無已完成交易記錄"**  
No trades have closed yet. Records are written on close (signal flip / SL / TP). Wait for the next position to close.

**`*_trades.jsonl` is a directory instead of a file**  
Docker creates a directory if the file doesn't exist before `docker compose up`. Fix:
```bash
docker compose down
rm -rf <bad_file> && touch <bad_file>
docker compose up -d --build
```

**Telegram bot not posting to group**  
1. Verify both bots are members of the group
2. Group `chat_id` starts with `-` (negative number)
3. After updating `.env`: `docker compose down && docker compose up -d` (`restart` won't reload env)

**Hourly position report not sending**  
Ensure `btc_status.json`, `eth_status.json`, `sol_status.json` exist as files (not directories) before starting coin-monitor. Run `touch btc_status.json eth_status.json sol_status.json` on VPS first.

---

## Resuming Claude Code Sessions

```bash
cd "D:\User Files\Desktop\working\crypto-bot"

# Continue most recent session
claude -c

# Pick from past sessions
claude -r
```

Session history: `C:\Users\ASUS\.claude\projects\D--User-Files-Desktop-working-crypto-bot\`
