# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A crypto futures trading bot suite that runs 24/7 on a VPS via Docker Compose, trading on **Binance USDⓈ-M perpetuals** (demo/testnet by default) and sending all notifications to a shared Telegram group. There are two distinct trading systems sharing one codebase:

1. **ML bots** (`main.py`) — one container per coin (BTC/ETH/SOL), each running a walk-forward Transformer model. **Currently paused** (see README "Current Status").
2. **Altcoin monitor** (`monitor_coins.py`) — the only bot currently running. Technical-analysis scanner + auto-trader covering BTC/ETH/SOL ("major") *and* altcoins. This is where almost all active development happens.

When the user reports a live trading bug, they almost always mean `monitor_coins.py` / the `coin-monitor` container.

## Commands

```bash
# Deploy / update on VPS (most common — coin-monitor is volume-mounted, so restart picks up code)
cd ~/TradingBot && git pull && docker compose restart coin-monitor
docker compose logs -f coin-monitor          # follow logs

# Dockerfile/requirements change → full rebuild
docker compose up -d --build

# Emergency close exchange positions (does not need positions_altcoin.json to be in sync)
docker compose exec coin-monitor python close_major.py            # close all
docker compose exec coin-monitor python close_major.py --dry      # list only
docker compose exec coin-monitor python close_major.py --symbols ETH ZEC

# Backtest ML models
python backtest.py --coin BTC                # also ETH / SOL
python backtest_volatile.py                  # altcoin volatile-strategy backtest (top 20, 180d)

# Train an ML model → produces {coin}_model_wf.pt + {coin}_scaler_wf.pkl + {coin}_backtest_wf.png
python train_wf.py --symbol BTC/USDT
python train_wf.py --symbol ETH/USDT --timeframe 4h --target_ahead 6

# Scan for volatile/washed-out altcoins
python scan_coins.py
```

There is **no test suite, linter, or build step** — `_test_api.py` / `_test_model.py` are ad-hoc scripts, not a framework. Verification is done by running the bot and watching logs/Telegram.

## Architecture notes that span multiple files

**Public vs private exchange split.** Both bots keep two ccxt clients: a public one for OHLCV/funding/ticker (no auth, avoids rate-limit/key issues) and a private one for orders/positions. In `monitor_coins.py` these are `exchange_pub` and `exchange_priv`; functions take whichever they need. `enable_demo_trading(True)` is what routes orders to Binance's demo environment (`DEMO_MODE`).

**Binance demo API silently fails to cancel conditional orders.** This is the single most important quirk. `cancel_all_orders`, `fetch_order`, and `fetch_open_orders` are all unreliable for STOP_MARKET / TAKE_PROFIT_MARKET orders in demo mode. Stale SL/TP orders accumulate across open/close cycles. The codebase works around this with `pending_cancels.json` (`_pc_add` / `_pc_flush` in `monitor_coins.py`): known order IDs are persisted on close and retried via individual `cancel_order(oid)` before the next open/close/sync, because per-ID cancel is more reliable than `cancel_all`. **Any new code path that closes a position or replaces SL/TP must call `_pc_add` then `_pc_flush`** or it will reintroduce the order-accumulation bug.

**State is host-mounted JSON, not a database.** `positions_altcoin.json` (open altcoin positions), `{coin}_state.json` (ML bot state), `{coin}_trades.jsonl` / `altcoin_trades.jsonl` (closed-trade ledger), `news_seen.json`, `*_status.json`. These are bind-mounted in `docker-compose.yml`, so rebuilds never lose trade history or open positions. **They must exist as files before `docker compose up`** — Docker creates a *directory* in their place otherwise (see README troubleshooting). `save_positions()` rewrites the whole file each time; there is no locking, so the single monitor loop is the only writer.

**"Major" vs "altcoin" branching is everywhere.** `WATCH_ALWAYS = ['BTC/USDT:USDT','ETH/USDT:USDT','SOL/USDT:USDT']` is the dividing line. Majors get 50x leverage / 1% SL / 2% TP; altcoins get 20x / 3.5% SL / 7% TP. `analyze_dispatch()` routes to `analyze_major()` or `analyze()`. When changing position logic, check whether it should apply to both — most parameters have a `MAJOR_*` twin.

**The monitor's main loop** (`monitor_coins.py:main`) runs every `SCAN_INTERVAL` (15 min) and multiplexes several cadences off `time.time()` deltas: signal scan every cycle, leaderboard + market-bias hourly, hourly position report on the integer hour, daily performance report on date change. `check_positions()` runs first to detect exchange auto-closes (TP/SL hit) and sync/replenish SL/TP via `_sync_sl_tp()`. The ML bot loop (`main.py:main`) is a simpler hourly tick (`INTERVAL_SECS=3600`).

**Entry is gated by stacked filters, not a single signal.** A scan produces a signal count (volume spike, compression, breakout proximity, funding surge); 2+ required. On top of that: RSI extremes, EMA50 trend agreement, `SHORT_BIAS` (currently blocks all altcoin longs, requires +1 signal for major longs), `near_support` gate, `COIN_BLACKLIST`, and an hourly macro bias vote (`get_market_bias`). Most of these constants live at the top of `monitor_coins.py` and are tuned by hand from KOL insights — see below.

**ML feature engineering is centralized in `data.py`.** `add_features()` builds the ~40-feature matrix (`FEATURE_COLS`) from OHLCV + US-market context (SPY/QQQ/VIX/GLD via yfinance) + Fear&Greed + funding-rate stats + VADER news sentiment. ETH adds 4 BTC cross-asset features. `train_wf.py`, `backtest.py`, and `main.py`'s live `predict()` all consume the same `FEATURE_COLS` and the per-coin `{coin}_scaler_wf.pkl` — if you change features, all three plus the saved scaler must stay in sync, which in practice means retraining.

## KOL-insight workflow (project-specific)

This project's risk parameters are driven by a recurring manual+automated pipeline, not just backtests:

- `notes/youtube-insights.md` is the canonical digest of crypto-KOL YouTube analysis. New sections are appended (dated).
- `scripts/auto_kol_update.py` is a daily cron job (8am Taipei) that fetches KOL video transcripts, summarizes them with the Gemini API, appends to the insights file, applies high-confidence parameter changes to `monitor_coins.py`, and commits/pushes.
- When the user says "我更新了 insight，幫我更新 code", they mean: read the newest dated section of `notes/youtube-insights.md` and translate the consensus (support/resistance zones, long/short bias, coin blacklist changes) into the constants at the top of `monitor_coins.py` (e.g. `BTC_SUPPORT_ZONE`, `BTC_RESISTANCE_ZONE`, `SHORT_BIAS`, `COIN_BLACKLIST`, the `near_support` gate). `main.py` has its own `KEY_SUPPORT_ZONE` / `KEY_RESISTANCE_ZONE`.

## Conventions

- **Timezone is UTC+8 (Taipei).** `now8()` (defined in both bots) is `utcnow() + 8h`. README timestamps and report scheduling are all +08. The dev machine's system clock is unreliable — when a timestamp is needed, ask the user for the actual time rather than trusting `date`.
- **Comments and Telegram messages are in Traditional Chinese**; match that style in `monitor_coins.py` / `main.py`.
- **Per the user's standing instruction: update `README.md` (both the content and the `> Last updated:` timestamp) in every commit that changes bot code.**
- Commit-message convention is Conventional Commits with a scope, e.g. `fix(check_positions): ...`, `feat(monitor): ...`.
- Config is read from environment (`.env`, surfaced via `docker-compose.yml`). Key vars: `SYMBOL`, `DEMO_MODE`, `SHORT_BIAS` behavior, `STATS_FROM` (performance-report start date), `MONITOR_TOKEN`/`MONITOR_CHAT_ID` (alert bot) vs `TELEGRAM_TOKEN`/`TELEGRAM_CHAT_ID` (trading bot) — the monitor posts to both.
