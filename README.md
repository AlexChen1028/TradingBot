# TradingBot

ML-powered crypto futures trading bot for BTC, ETH, SOL and altcoins.  
Runs 24/7 on a VPS via Docker, sends all notifications to Telegram.

> Last updated: 2026-06-26 22:03 +08

---

## Current Status

| Service | Status | Note |
|---|---|---|
| `trading-bot` (BTC) | Paused | ML bot 暫置，BTC 改由 coin-monitor 以技術分析交易 |
| `eth-bot` (ETH) | Paused | ML bot 暫置，ETH 改由 coin-monitor 以技術分析交易 |
| `sol-bot` (SOL) | Paused | ML bot 暫置，SOL 改由 coin-monitor 以技術分析交易 |
| `coin-monitor` | **Running** | 交易山寨幣 + BTC/ETH/SOL（技術分析信號），唯一運行中的機器人 |

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

## Strategy Reference / Market-View Inputs

Beyond pure ML, the bot's feature set and risk-management heuristics are informed by external
market-view sources. Most recent reference: KOL analysis from **@crypto_punks (加密龐克)** —
see [`notes/youtube-insights.md`](notes/youtube-insights.md) for the full digest.

### KOL-insight pipeline (fully automated 2026-06-21)

Risk-param updates flow from KOL YouTube videos through a fully-automated hourly pipeline (while Claude Code is open):

1. **Detect + transcribe (local, hourly)** — `scripts/kol_fetch.py` runs on the **local residential IP** (which YouTube does *not* block, unlike the VPS). It reads the channels' RSS, diffs `notes/.kol_seen.json`, and fetches transcripts for new 加密龐克 / BTC飛揚 / BTC歐陽 videos — native captions first, falling back to `scripts/kol_whisper.py` (yt-dlp + faster-whisper audio→text) for caption-disabled channels — into `notes/.kol_pending.json`.
2. **Summarize + apply** — an hourly Claude Code task summarizes each `transcript_ok` entry into a dated `notes/youtube-insights.md` section (**replacing NotebookLM**, which has no API), translates *clear consensus shifts* into `monitor_coins.py` / `main.py` constants, commits/pushes, deploys to the VPS, and Telegram-notifies what changed. A video that only reaffirms current params appends the insight but leaves constants/deploy untouched.

> **The VPS detection-notify cron (`notify_new_kol_videos.py`, `50 0,12 * * *`) was removed 2026-06-21** — it sent an 08:50/20:50 "go run NotebookLM" Telegram alert, which is obsolete now that detection+summarize+apply is automatic. The script is kept (re-add the cron, message reworded, only if a closed-Claude fallback is wanted; note: with it gone there is **no new-video detection while Claude Code is closed**).
>
> `scripts/auto_kol_update.py` (older Gemini auto-summarizer) is **unused** — `youtube_transcript_api` is IP-banned from the VPS and the Gemini free tier is too small. `notes/.kol_seen.json` / `.kol_pending.json` are per-host runtime state (git-ignored).

Key concepts mapped to existing features:

| KOL Concept | Implementation |
|---|---|
| 軋空燃料 / 嘎空動能 (squeeze fuel) | `fr_z`, `fr_ma`, `fr_cumsum` in `data.py` |
| 資費跟著趨勢 vs 背離 (momentum vs contrarian regime) | `fr_trend_corr`, `sent_trend_corr` |
| 收斂盤整 vs 突破 (regime detection) | `detect_regime()` in `main.py` (ATR ratio) |
| 右側交易確認 (higher-timeframe confirmation) | 1h + 4h `MULTI_TF` agreement gate |
| 機構動向 / ETF 流向 | F&G index + news sentiment (proxy; direct ETF flow planned) |

Gaps currently noted (see notes file):
- **200-day MA (牛熊分界線)** — not in feature set (max is EMA50)
- **Short-term holder cost basis** — requires on-chain data
- **Direct ETF flow / whale tx data** — not yet wired

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

**Signal sources (every 15 minutes, 2+ required to enter):**
- Volume spike ≥ 1.5× 24h average
- Price compression (recent range ≤ 50% of average)
- Within 3% of 14-day high (breakout proximity)
- Funding rate surge ≥ 0.02%

**Leaderboard trading (hourly, no TG notification):**
- Binance 24h top gainers / losers → auto-enter on 2+ signals
- Min 24h move ≥ 3% to qualify

**Entry filters (every scan):**
- RSI 14: skip long if RSI ≥ 80 (overbought); skip short if RSI ≤ 20 (oversold)
- EMA 50 (1h): direction must agree with EMA50 trend
- `SHORT_BIAS=True`: altcoins — LONG completely blocked; major coins — LONG needs +1 extra signal (2026-06-03 KOL: 完全放棄山寨幣做多幻想)
- **Short-Squeeze Filter** (`squeeze_no_short`, 2026-06-16 龐克): when BTC funding ≤ `SQUEEZE_FR_EXTREME` (−0.03%) **and** OI at a 14-day high (OI degrades to funding-only if unavailable), **all** new SHORT entries are paused market-wide (主力惡意軋空起手式，避免空在地板被清算)
- `near_support` gate: when BTC ≤ `BTC_SUPPORT_ZONE[1]`×1.01 (2026-06-26: 58K–59K，門檻 ≤59,590；跌破二探創熊市新低 58K、別地板空防報復性軋空), altcoin SHORT entries are skipped (追空禁令；跌破才有暴跌空間)
- ETH-only gate (`ETH_RESISTANCE_ZONE` 1,670–1,720 / `ETH_SUPPORT_ZONE` 1,600–1,640 / `ETH_LONG_ZONE` 1,370–1,390 / `ETH_NO_LONG_ABOVE` 1,700): ETH 突破失敗、跌回 1,604-1,692 區間→operative 壓制下移至 1,670-1,690（飛揚 6/24 連兩日精準承壓、今 1,692 拒絕）。ETH LONG allowed **only** within the 悲觀二探 zone (price ≤ ~1,404), blocked elsewhere; ETH SHORT skipped while price is inside 悲觀二探 (1,356–1,404)、深支撐 (1,600–1,640)、**或突破多頭區 (1,640–1,670，未到高空帶不追空)**；shorts 放行 ≥1,670（接 1,670-1,690 反彈承壓）及破 <1,600 追空 (2026-06-24 飛揚)
- SOL-only gate (`SOL_RESISTANCE_ZONE` 70–72 / `SOL_SUPPORT_ZONE` 66–68, 2026-06-25 飛揚): SOL 已跌至 64–72 區間，high-short 帶下移。SOL is short-biased — LONG skipped unless price ≤ ~68.7 (only buy the 66–68 bounce); SHORT skipped while inside the 66–68 take-profit/support floor (地板追空 R:R 差，飛揚最低打 64.6、跌破 66–68 追空). 70–72 is the preferred high-short entry (飛揚 6/25：SOL 不硬很軟、M 頂續空)
- `COIN_BLACKLIST`: CHZ, ORDI, WLD, LAB, ADA, HYPE, BCH, BEAT, LTC — LONG blocked entirely

**Macro filter (hourly):**
- Fetches BTC 24h change + SPY / QQQ daily return
- Each above threshold casts a bull/bear vote (BTC ±2%, SPY/QQQ ±0.5%)
- 2+ bear votes → skip all longs; 2+ bull votes → skip all shorts; otherwise neutral

**Position parameters:**

| Parameter | Altcoin | Major (BTC/ETH/SOL) |
|---|---|---|
| Leverage | 20x isolated | 50x isolated |
| Margin per trade | 2s: $60 / 3s: $80 / 4s: $100 | same |
| Stop-loss | Exchange `STOP_MARKET` 3.5% | 1% from entry |
| Take-profit | Exchange `TAKE_PROFIT_MARKET` 7% | 2% from entry |
| Break-even SL | Auto-move SL to entry price once gain ≥ 3% | same |
| Software trailing backup | 15% from peak | same |
| Max hold time | 36 hours | same |

### Position Reconciliation (`monitor_coins.py`)
- **On open timeout**: if entry raises `-1007` ("execution status unknown"), the order may have actually filled. `open_pos` queries the exchange's real position and *adopts* it (records locally + places SL/TP) instead of abandoning a potential orphan.
- **On startup**: `_reconcile_orphans` scans all exchange positions; any with no matching local record is adopted (marked `adopted: true`) so it gains SL/TP protection and close logic.
- Adopted positions trigger a 🔧 Telegram alert.

### Breaking News Detector (`monitor_coins.py`)
- Polls CoinDesk, CoinTelegraph, Decrypt RSS every scan cycle
- Filters by keyword (crash, hack, SEC, regulation, rate cut, etc.)
- VADER sentiment threshold: |compound| ≥ 0.25
- Deduplicates via `news_seen.json` (24-hour window)
- Sends to Telegram group with 🟢 bullish / 🔴 bearish / ⚠️ neutral tag

### Telegram Notifications
- **Every hour (整點)**: altcoin positions + today's cumulative P&L
- **On open**: entry price, size, SL/TP confirmation, signal count & margin used
- **On close**: gross profit, fee, net result, close reason
- **SL/TP trigger**: exchange auto-close detected and logged
- **Weekly (every 7 days)**: rolling 7-day performance — net profit, fees, win rate, ROI
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

**Resetting altcoin positions and starting fresh (Demo)**  
If you want to clear all tracked altcoin positions and start from a clean slate:
```bash
# 1. Reset Demo account balance via Binance UI (Futures → Reset Demo Account)
# 2. Clear local position tracking file
echo '{}' > positions_altcoin.json
# 3. Set STATS_FROM in .env so reports only count trades after the reset date
#    echo "STATS_FROM=2026-05-16" >> .env   ← use today's date
# 4. Pull latest code and restart bot
cd ~/TradingBot && git pull
docker compose restart coin-monitor
```
Setting `STATS_FROM` ensures the hourly P&L and weekly reports ignore any trades recorded before the reset — no need to delete `altcoin_trades.jsonl`.

Note: Ghost positions (0 quantity, negative margin) left after Demo liquidation are isolated — they do not affect new trades on other symbols and can be ignored.

---

## Changelog

### 2026-06-21（KOL 共識套用：ETH 高空帶下修 + BTC 支撐上移）
- **首次納入飛揚/歐陽 Whisper 逐字稿**（6 支，6/19~6/21）→ 兩位 KOL 一致強烈看空、反彈無量、高點下移
- **ETH 1,700 突破失效**：`ETH_RESISTANCE_ZONE (1800,1820) → (1700,1740)`（飛揚 1,704-1,706 / 歐陽 1,715-1,748 高空帶）；`ETH_SUPPORT_ZONE (1700,1720) → (1600,1640)`（1,700 由支撐轉回壓力，新支撐＝1,618 景象位/1,608 追空線）→ **解除 1,700 附近禁空**，讓 ETH 在現價 ~1,715 可逢高做空
- **BTC 支撐上移**：`BTC_SUPPORT_ZONE (60000,61100) → (61000,62000)`（62,000 兩度測試守住＝反彈起點/追空分界；61K≈200週均線深支撐）；`near_support` 追空禁令門檻連動上移至 BTC ≤ 62,620
- **BTC 壓力維持** `(64000,65500)`（歐陽 64-65K 開空、飛揚 64K 高空；上方硬壓 66-67K 通道頂/布林上軌）；`BTC_HARD_STOP 69,150`、`ETH_NO_LONG_ABOVE 1,700`、`ETH_LONG_ZONE (1370,1390)`、`SHORT_BIAS`、`COIN_BLACKLIST` 維持。`main.py` KEY zones 同步
- 山寨：SOL 73-74 開空（目標 69）、AVI 順勢追空、ADA/LTC 弱勢無機會（已在黑名單）
- **晚間追加**（飛揚 6/21 ETH，Whisper）：重申 BTC 64.5-65.5K 高空 / ETH 1,704-1,706 承壓、破 1,700 小倉追空；**與上述參數一致，無新變動**（僅 append insight，未改常數、未重啟容器）
- **6/26 晚間（加密龐克 BTC 血崩，原生字幕）**：重申看跌、行情逼近生死線、**參數不變**。BTC 第三次測 6 萬、買單牆消耗、**收在 6 萬之下、逼近收破 59,567 週線生死線**（破則 support→resistance、下看 58K/57K/死寂；守則彈 POC 62.7K）。定點爆破軋空又來（9:30）、短持砸 4.9 萬顆但 6 萬以下有人接、5-6 萬合理買入區。`near_support` 門檻 ≤59,590 幾乎正好＝龐克 59,567 關鍵線（bot 在此線下禁地板空、對齊軋空警告），58K/57K 目標皆在 `BTC_SUPPORT_ZONE (58000,59000)` 內 → **未改常數、未重啟**（★監看 59,567 週線收破與否）
- **6/26 下午（飛揚 BTC「第一幕」，Whisper）**：看跌延續、與歐陽 6/26 一致、**驗證今早下移在軌**。BTC 暴跌「第一幕」、58K 弱反彈沒回 3618（空頭興奮劑）、**6 萬 key resistance 上不去**、高空帶 60-60.5-61K、續跌。所有點位已落在現有 `BTC_SUPPORT_ZONE (58000,59000)`／反彈帶 60.5-62K（bot 58-59K 禁地板空、60-61K 放行高空，對齊飛揚操作）→ **參數不變、不重啟**。SOL 弱跌 64、AAVE 強 88、ZEC 空單小心反彈
- **6/26 上午（歐陽 BTC 創熊市新低，Whisper）★參數變動★**：BTC 連 4 根日線陰線、**跌破 59-60K 二探、創本輪熊市新低 58,000、短期沒支撐**。歐陽：**別地板空**（58-59K 破位後短暫支撐、地板空易報復性軋空）、**別輕易抄底**（6 萬不是底、等完全破位）、反彈 **60.5-61K 高拋**、**62K 已是強壓力**。BTC 區間再下移：`BTC_SUPPORT_ZONE (59000,60000) → (58000,59000)`（near_support ≤59,590）、`BTC_RESISTANCE_ZONE (63000,64000) → (61000,62000)`、`main.py` KEY 同步。SOL M頂走完空單可平、AVAX 等 90 空、ETH 看三位數（跌破 1,000）。**已部署並重啟 coin-monitor**
- **6/25 晚間（加密龐克＋飛揚 ETH/SOL，字幕/Whisper）★SOL 微調★**：BTC 續區間震盪看空——龐克：第三次測 6 萬、跌穿時追空被大鯨魚買牆軋（嘎空）、區間 60-65K、熊底將近（長持 78%）；飛揚：61,900 黃昏星只能做空。**BTC/ETH 維持**（下午的 59-60K/63-64K 在軌）。**SOL 不硬很軟、stale 下移**：`SOL_SUPPORT_ZONE (68,72) → (66,68)`、`SOL_RESISTANCE_ZONE (74,76) → (70,72)`（SOL 跌至 64-72、高空 70-72、最低 64.6，讓 bot 能在實際區間做空、不把高空帶當地板擋掉）。ETH 高空 1,650-1,670/破 1,602 追空、支撐 1,592；AAVE 反彈沒結束別空、120 生死線。**已部署並重啟 coin-monitor**
- **6/25 下午（飛揚＋歐陽 BTC 二探，Whisper）★參數變動★**：方向選擇兌現——BTC 放量**暴跌破 62K → 59K**、現反彈 ~61K（先前監看的「波動率信號＋61,097 破位」＝向下）。兩位共識 **59-60K=二探/最後支撐、千万别地板空、反彈帶 61.5-63K**（歐陽偏 60K 做多博反彈至 63K、飛揚偏 61-61.5K 高空續空）。BTC 區間整體下移：`BTC_SUPPORT_ZONE (61000,62000) → (59000,60000)`（near_support 連動 ≤60,600，護 60K 地板）、`BTC_RESISTANCE_ZONE (64000,65500) → (63000,64000)`、`main.py` KEY zones 同步。山寨別做多、做多首選 BTC。**已部署並重啟 coin-monitor**（監看 60K 守住反彈 63K vs 失守創新低）
- **6/25 凌晨（飛揚 6/24 晚間 ETH，Whisper）★參數變動★**：修 stale 參數——ETH 6/22 衝 1,775 的突破已失敗、跌回 1,604-1,692 區間，operative 壓制下移至 1,670-1,690（飛揚連兩日精準承壓、今 1,692 拒絕）。舊 `(1780,1850)` 高空帶讓 bot 整個 ETH 實際區間都不做空 → `ETH_RESISTANCE_ZONE (1780,1850) → (1670,1720)`，突破多頭區禁空閘門連動收窄 1,640-1,670、**shorts 放行 ≥1,670（接 1,690 反彈承壓）及破 <1,600 追空**。BTC 暴跌至 61,300（逼近支撐、與區間一致不改）。**已部署並重啟 coin-monitor**
- **6/24 晚間（加密龐克 BTC，原生字幕）**：重申 BTC 60-65K 區間（POC 62,700、邊界 65,500/61,097）。兩宏觀信號但**無方向性、不改參數**：⚡波動率信號亮燈＝大波動將至（歷史暴跌前密集亮燈、先誘多再爆破）；🔗長期持有者已實現市值佔比 **78%**（史上第 3 次，宏觀偏多、熊市可能剩 2-3 個月）。皆無可落地方向性常數，**未改常數、未重啟**（監看 65,500 假突破 vs 破 61,097，波動率信號暗示突破將近）
- **6/24 下午（飛揚 BTC/ZEC，Whisper）**：飛揚重申大高空「反彈越猛我越開心」，**把 BTC 天平拉回 2/3 看空**（飛揚+龐克空、歐陽戰術多）。BTC 62K 撐住、頂 64K 未破、今日壓制 63.5-64K、下看 61.2K/60K。ETH 1,604-1,690 窄幅 chop（bot no-trade 避震正確）、ZEC 421-423 空（已由 SHORT_BIAS 偏向）。所有點位落在現有 constants，**未改常數、未重啟**（監看 BTC 62K 是否終破位）
- **6/24 上午（歐陽 BTC/SOL，Whisper）**：歐陽 BTC 砸前低 62K 後反車回 63K、**62K 空轉多持多單博反彈**、今日暫不布局 BTC 空——但**逆飛揚/龐克（仍看 60K）**，單一戰術多單、不構成共識 → **不翻 BTC、不解除禁空、參數不變**（62K 以下早由 `near_support` 禁追空）。SOL 精準兌現：74 開空、**68 目標到並止盈 30%**，驗證上一輪 `SOL_SUPPORT_ZONE 69→68`。**未改常數、未重啟**（監看 BTC 62K 反彈重回通道 vs 破位下 60K）
- **6/23 晚間（加密龐克＋飛揚 ETH，字幕/Whisper）**：反彈失敗、續看跌，**行情正照框架走、參數不變**。BTC 精準打 65,500（區間上緣）受阻摔回、下看 61K→60K→破前低 59,900（鯨魚 5-6 萬掛單等抄底）；飛揚破位追空（63-63.5K）已驗證。ETH 跌一天、扎 1,604 強支撐小反彈仍高位做空——**ETH 跌進現有 `ETH_SUPPORT_ZONE (1600,1640)`，bot 在 1,604 正確擋住追空**。所有點位落在現有區間，**未改常數、未重啟**（監看 BTC 59,900／ETH 1,592 是否破位）
- **6/23 上午（飛揚＋歐陽 BTC，Whisper）★微調★**：反彈轉弱、兩位重申看跌。BTC 昨彈 66K 收長上影回落至 ~64K，飛揚「漲只是過程跌才是結果」、歐陽「跌破下軌看 62.5K→60K 前底」（BTC 區間不改，破位目標在追空分界下方）。微調：`ETH_RESISTANCE_ZONE (1800,1850) → (1780,1850)`（飛揚 6/23 壓制改框 1,780-1,800、ETH 實際 1,779 見頂；突破多頭區禁空閘門連動為 1,640-1,780）、`SOL_SUPPORT_ZONE (69,72) → (68,72)`（歐陽 SOL 止盈目標 69→68）。**已部署並重啟 coin-monitor**
- **6/23 凌晨（飛揚 6/22 晚間 ETH，Whisper）★參數變動★**：飛揚 ETH 空單被打損認虧 1,500 點、空防炮失效，**ETH 再破 1,700-1,750 暴漲至 ~1,775、多頭轉強**。→ `ETH_RESISTANCE_ZONE (1700,1740) → (1800,1850)`（1,800 整數關/3.618 進場、1,850 前高、上看 1,900）＋**新增 ETH scan 閘門：突破多頭區 1,640-1,800 禁追空**（修掉「bot 在 1,775 軋空帶照樣做空」的接線 bug，正是飛揚被打損的位置）。做多維持鎖死（`ETH_NO_LONG_ABOVE 1,700`，結構仍偏空）。**真實接線變動 → 已部署並重啟 coin-monitor**
- **6/22（加密龐克 BTC，原生字幕）**：三方 6/22 收斂——週線偏弱、6 萬 5 之下小區間震盪，64,200 近壓、**需收復 65,500 才轉強**（站上才走大級別雙底）。鏈上大鯨魚＋訂單簿顯示 5-6 萬買盤雄厚、微策略續買 520 BTC，**短期難破 6 萬、更難見 5 萬以下**（強化 `BTC_SUPPORT_ZONE` 62K 守得住、淡化速破 59,800 暴跌風險）。所有點位已落在現有區間內，**參數不變、不重啟**
- **6/22（飛揚 BTC 週線，Whisper）**：週線「勉強空方炮」（下跌動能衰減、空頭仍占優），維持高空劇本——64,500 強壓、65-65.5K 為空單目標、**跌破 59,800 前低才確認延續下跌**、守住則走 W 底上看 80K+。ETH 1,700 未跌破不可追空。與歐陽 6/22 兩位獨立收斂於同一劇本，所有點位已在現有 `BTC_RESISTANCE_ZONE (64000,65500)`／`BTC_SUPPORT_ZONE (61000,62000)`／`ETH_*` 區間內，**參數不變、不重啟**（59,800 列入後續監看的多空生死分界）
- **6/22（歐陽 BTC，Whisper）+ 新增 SOL 閘門**：無量反彈趨勢已結束、波動收窄等多空決戰。改採「即時更動」方針後落地歐陽連兩日的 SOL 點位 → 新增 `SOL_RESISTANCE_ZONE (74,76)`（高空帶）、`SOL_SUPPORT_ZONE (69,72)`（止盈/支撐），及 SOL scan 閘門（仿 ETH：弱勢禁多、69-72 禁地板追空）。BTC/ETH 區間維持（64.5K 阻力/63K 下軌、ETH 1,735 高空均在現有區間內）

### 2026-06-21（Whisper 後備：處理關閉字幕的 KOL 影片）
- BTC飛揚/BTC歐陽 頻道**關閉字幕**（`TranscriptsDisabled`），原生字幕完全抓不到 → 新增 `scripts/kol_whisper.py`：yt-dlp 抓 bestaudio（免系統 ffmpeg，用 faster-whisper 內建 PyAV 解碼）→ faster-whisper（CPU/int8，`small` 模型）轉中文逐字稿
- `kol_fetch.py` 整合：原生字幕抓不到時自動呼叫 Whisper 後備（~5min/支）；轉錄成功即納入總結，不再因「無字幕」漏掉飛揚/歐陽觀點。逾 30h 字幕+Whisper 皆失敗才退休
- 依賴：`pip install yt-dlp faster-whisper`（本機，免費；模型首次自動下載快取）

### 2026-06-20（kol_fetch 無字幕影片自動退休）
- `kol_fetch.py`：無字幕影片逾 `RETIRE_HOURS`（30h）仍抓不到逐字稿 → 自動標記 seen 退休，停止每輪重撈。避免 BTC飛揚/BTC歐陽（通常無字幕）在 `.kol_pending.json` 無限累積（曾長到 9 支）；新片仍保留 30h 重試窗等延遲字幕

### 2026-06-19（KOL 自動總結首次實跑 + 偵測可靠性修復）
- **首次由 Claude 自動鏈完成**：本機抓加密龐克 6/17~6/19 逐字稿 → 自動總結 → 套參數（非 NotebookLM）
- **BTC 三天 67K→62K**：`BTC_RESISTANCE_ZONE (66000,67500) → (64000,65500)`（跌破 65,500 後反彈高空帶下移：64,000 上週雙底頸線 / 65,500 破位轉壓）；`BTC_SUPPORT_ZONE (65200,65500) → (60000,61100)`（200 週均線≈61,097 熊市撿錢區 + 60K 大級別）。`near_support` 連動下移、`main.py` KEY zones 同步
- **嚴禁地板追空**：資費低位 + 太多人做空 → 軋空風險（與 `squeeze_no_short` 同向）；微策略 STRC 脫錨「死亡螺旋」判定為噪音（非系統性風險）
- ETH/COIN_BLACKLIST 維持（本批僅加密龐克有字幕，飛揚/歐陽無字幕未納入）
- **🔧 偵測可靠性修復**：`kol_fetch.py`/`notify_new_kol_videos.py` 改用**寫死的 channel_id**（先前每次 scrape youtube.com 被限流 → `resolve_channel_id` 失敗 → 誤回「沒新片」假陰性）；`kol_fetch.py` 補 stdout utf-8（cp950 印中文標題 `UnicodeEncodeError`）

### 2026-06-18（KOL insight 流程自動化：偵測+總結+套用）
- **目標**：把「偵測新影片 → 總結 → 套參數」的人工流程自動化，使用者只需保持 Claude Code 開著
- **偵測（VPS 常駐）**：`scripts/notify_new_kol_videos.py` + VPS cron `50 0,12 * * *`（8:50/20:50 台灣），純 RSS（不抓字幕/不用 Gemini，避開機房 IP 封鎖），有新片發 Telegram 並記入 `notes/.kol_seen.json`
- **總結+套用（本機，Claude 開著時）**：`scripts/kol_fetch.py` 用**本機住宅 IP** 抓逐字稿（VPS 機房 IP 被 YouTube 封，本機不受影響）→ 每小時 Claude Code 排程把逐字稿總結成 insight 段落 → 翻成 `monitor_coins.py`/`main.py` 常數 → commit/push/部署 → TG 告知變動。取代手動 NotebookLM（Claude 關閉時退回 VPS 通知 + 手動）
- `scripts/auto_kol_update.py`（舊 Gemini 自動摘要）標為**棄用**：YouTube 封 VPS IP 抓不到字幕 + Gemini 免費額度太小。`.kol_seen.json`/`.kol_pending.json` 為各機執行期狀態（git-ignore）

### 2026-06-16（KOL insight 更新：軋空續演 + Short-Squeeze Filter）
- 依 `notes/youtube-insights.md` 2026-06-15~06-16 統整（NotebookLM 127 個來源，4 支新影片：加密龐克 ×1、BTC飛揚 ×2、BTC歐陽 ×1）更新 KOL 共識區間
- **本波定性**：67,200 高空精準命中後軋空續演；極端負費率 + OI 創新高 = 主力惡意軋空起手式；川普放鴿+美伊停戰助推，但消息面利好≠趨勢反轉，大級別仍偏空
- **🔴 新增 Short-Squeeze Filter**（`squeeze_no_short`）：BTC 資金費率 ≤ `SQUEEZE_FR_EXTREME` (−0.03%) **且** OI 達 14 日新高 → 全市場暫停所有新追空（OI 資料不可得時降級為僅看費率）。加進 `get_btc_kol_gate`，`scan()` 對 `d==-1` 全幣種攔截
- **BTC 壓力/支撐再上移**：`BTC_RESISTANCE_ZONE (65500,66500) → (66000,67500)`（保守 66,000-66,500 歐陽佈空；積極 67,000-67,500 軋空終極目標）；`BTC_SUPPORT_ZONE (63500,64500) → (65200,65500)`（關鍵防守線，回踩不破可反手短多）；新增 `BTC_HARD_STOP = 69,150`（飛揚硬止損）。`near_support` 連動、`main.py` KEY zones 同步
- **ETH 做空點大幅上移**：突破 1,700 暴漲百點 → `ETH_RESISTANCE_ZONE (1680,1700) → (1800,1820)`（歐陽長線天花板；飛揚短多止盈 1,780-1,800）；`ETH_SUPPORT_ZONE (1620,1640) → (1700,1720)`（阻力轉支撐帶，此區禁空）。接多 1,370-1,390、`ETH_NO_LONG_ABOVE 1,700` 維持
- 黑名單建議新增 ADA/LAB 皆已在名單內，無需變動
- 暫緩（deferred）：ZEC 回踩 500-520 接多、HYPE 64-66 做空目標 53-55（皆需個幣 limit 掛單模組，SHORT_BIAS 已擋山寨多）

### 2026-06-16（bugfix：SOL SL/TP 補掛 -4130 每輪重刷）
- 症狀：SOL 開倉時 SL/TP 掛失敗（`止損 1.0% ⚠️ 止盈 2% ⚠️`），之後 `_sync_sl_tp` 每 15min 重複補掛並刷 `-4130 An open stop or take profit order with GTE and closePosition in the direction is existing`（log 連刷數十次）
- 根因：前一筆 SOL closePosition SL/TP 在 demo 撤不掉、殘留交易所 → 新倉開倉補掛同向 closePosition 單被 -4130 擋 → `sl_order_id` 留 None → 每輪 `_sync_sl_tp` 判定需補掛又撞 -4130。demo 的 openOrders API 也不列出 closePosition 條件單，故殘單既撈不到也撤不掉，**-4130 本身是唯一可靠的「殘單存在」訊號**
- 影響評估：**無實際損失**。殘留的 closePosition 單仍有效保護倉位（該 SOL 倉最終由交易所 TP 觸發 🎯 +98.42 U 平倉），且 `check_positions` 每 15min 軟體 SL/TP 兜底。純 log 噪音 + 新倉缺自身正確價位的交易所 SL/TP
- 修復：`_sync_sl_tp` 遇 -4130 即標記 `sltp_4130_noted=True` 並停止每輪重刷（與保本止損同設計），改信任軟體 SL/TP；旗標隨倉位生命週期，平倉即清。新增 `scripts/diag_sol_orders.py` / `diag_stale_orders.py` 診斷腳本

### 2026-06-15（KOL insight 更新：逼空後結構上移）
- 依 `notes/youtube-insights.md` 2026-06-13~06-15 統整（NotebookLM 123 個來源，6 支新影片：BTC飛揚 ×4、BTC歐陽 ×2；加密龐克最新停留 6/12）更新 KOL 共識區間
- **本波定性**：空頭清算逼空（BTC 觸 66K），反彈非反轉，空頭趨勢延續；嚴禁地板追空
- **BTC 大幅上移**：`BTC_RESISTANCE_ZONE (62500,64000) → (65500,66500)`（歐陽 65,600+66,500 分批掛空；週線極限 69-70K EMA 缺口）；`BTC_SUPPORT_ZONE (59500,61000) → (63500,64500)`（短線分水嶺，宏觀極限防守 59,000 前低雙底）。`near_support` 追空禁令門檻動態連動此區。`main.py` KEY zones 同步
- **ETH 地板空過濾帶上移**：`ETH_SUPPORT_ZONE (1592,1620) → (1620,1640)`（飛揚：1698 精準承壓，1,620-1,640 已無追空價值，等 1,600 有效跌破才有暴跌空間）。高空 1,680-1,700、接多 1,370-1,390 維持
- 黑名單建議新增 ADA/WLD/LAB 皆已在名單內，無需變動
- 暫緩（deferred，需 on-chain/macro 資料源或 SHORT_BIAS 已涵蓋）：BTC 高空狙擊 Limit Sell 模組（66,000-66,500 掛單+止損 69,500+回落分批）、山寨做多權重 -90%（SHORT_BIAS 已全擋）、HYPE 64-66 接多

### 2026-06-13（KOL insight 更新）
- 依 `notes/youtube-insights.md` 2026-06-10~06-13 統整（NotebookLM 117 個來源，11 支新影片：加密龐克 ×2、BTC飛揚 ×6、BTC歐陽 ×3）更新 KOL 共識區間
- **BTC**：支撐帶加寬 `BTC_SUPPORT_ZONE (59500, 60000) → (59500, 61000)`（新增 60-61K 三位共識短線支撐）；壓力帶 `(62500, 64000)` 維持（牛熊線上修至 65-65.5K）。`main.py` 的 `KEY_SUPPORT_ZONE` 同步
- **ETH 結構翻轉**：上一版「極弱、僅 1,370-1,390 接多、1,500 以上禁多」已過時。ETH 走出 W 底反彈衝頸線 →
  - 新增 `ETH_RESISTANCE_ZONE (1680, 1700)`（三位共識高空進場頸線壓制）
  - 新增 `ETH_SUPPORT_ZONE (1592, 1620)`（生死線大級別支撐，未跌破嚴禁追空，做空閘門新增此區跳過）
  - `ETH_LONG_ZONE (1370, 1390)` 降級為「悲觀二探」（僅 BTC 跌破6萬才看），做多仍僅此區放行
  - `ETH_NO_LONG_ABOVE 1500 → 1700`
- **黑名單**：新增 `LTC`（加密龐克：老牌弱勢流動性枯竭，關閉抄底網格，禁做多）
- 暫緩（deferred，需 on-chain/macro 資料源）：鯨魚單日淨賣出 >1000 BTC 暫停接多、SpaceX IPO/世界盃吸血期山寨多單權重 -80%、ETH 極端負費率+OI 高位軋空保護（暫停 1,650-1,680 做空腳本）

### 2026-06-11（bugfix：保本止損 -4130 無限重試）
- 症狀：倉位獲利 ≥3% 觸發保本止損後，每輪掃描都刷 `-4130 An open stop or take profit order with GTE and closePosition in the direction is existing`（log 中 XLM 連刷數小時）
- 根因：closePosition 改造（`3f52b46`）後的回歸 bug。保本邏輯先 `cancel_order(舊 SL)` 再用 `_place_sltp` 補掛新的 closePosition STOP，但 demo 無法可靠撤銷條件單 → 舊 closePosition 單仍在，同方向只允許一張 → 補掛被拒 `-4130`；且例外在 `breakeven=True` 設定前拋出，導致每輪無限重試
- 修復：保本止損改為**軟體強制**，不再動交易所條件單
  - 觸發時只設 `breakeven=True` + 記錄 `be_price`（進場價 ±0.05%），交易所原 -3.5%/-1% closePosition SL 保留作硬底備援
  - `check_positions` 平倉判斷新增 `be_hit`：價格回落至 `be_price` → 以「保本止損」平倉（每 15min 掃描檢查，符合既有軟體 SL 備援設計）
- 另記錄（未修，需查交易所）：ETH 開倉持續 `-2019 Margin is insufficient`，BTC/SOL 同 $80 保證金可成交 → ETH 專屬（疑 demo 帳戶 ETH 槓桿/isolated 錢包狀態異常），待容器內診斷

### 2026-06-11（feature：ETH 弱勢專屬接多閘門）
- 依 2026-06-10 KOL insight（BTC歐陽 ETH 接多目標重大下修至 1,370–1,390；極度弱勢取消 1,500 以上做多）新增 ETH 專屬進場閘門
- 新增常數 `ETH_LONG_ZONE = (1370, 1390)`、`ETH_NO_LONG_ABOVE = 1500`
- `scan()` 對 `ETH/USDT:USDT` 加兩道閘門（對稱設計，呼應 BTC_SUPPORT_ZONE/near_support）：
  - 做多：價格 > 接多區上緣 ×1.01（≈1,404）一律跳過 → ETH 多單只在極端插針區放行；訊息依 ≥1,500 顯示「極度弱勢」否則「未到極端接多區」
  - 做空：價格落在 1,356–1,404（接多區 ±1%）跳過 → 預期插針反彈，禁地板空
- 現價約 1,600 → ETH 多單實質全關，須等深插至 1,370–1,390 才會接多；ETH 空單則維持（僅在接多區暫停）

### 2026-06-10（KOL insight 更新）
- 依 `notes/youtube-insights.md` 2026-06-10 統整（102~106 個來源，6/09~6/10 共 4 支新影片：加密龐克 ×2、BTC飛揚 ×2、BTC歐陽 ×1）更新 KOL 共識區間
  - 市況定調：議息會議與通脹數據前的雙向收割期；大鯨魚單日砸盤 2,000+ BTC，反彈僅為空頭平倉燃料，非反轉；三方共識嚴禁地板空、逢高做空
  - 宏觀：加密龐克「虧損>盈利」黃金交叉觸發，預期 9~10 月才真正反轉；終極大底 54,000（已實現成本線）
- `monitor_coins.py`：
  - `BTC_RESISTANCE_ZONE`: `(63000, 64000)` → `(62500, 64000)`（寬高空帶：飛揚下調 62.5-63K + 歐陽 63.5-64K 強壓）
  - `BTC_SUPPORT_ZONE`: 維持 `(59500, 60000)`（歐陽二探接多防守區；放量跌破 59,000→清多，下看 54,000）
  - `near_support` 追空禁令門檻維持 BTC ≤ 60,600；`SHORT_BIAS` 維持 `True`
  - `COIN_BLACKLIST` 不變（新點名的 ADA/BCH/WLD/HYPE/CHZ/BEAT 皆已在名單內）
- `main.py`：`KEY_RESISTANCE_ZONE` → `(62500, 64000)`；`KEY_SUPPORT_ZONE` 維持 `(59500, 60000)`
- 暫緩（event-driven，待後續實作）：議息前 ±12h 關閉市價追單、鯨魚淨流出過濾器（ETH 1,370–1,390 極端接多已於 2026-06-11 實作）

### 2026-06-09（bugfix：殘留掛單堆積）
- 症狀：交易所只剩 3 個實際倉位，卻累積 16 張掛單；止盈成交後對側止損沒消失（反之亦然）
- 根因：6/02（`d6aad5a`）把 SL/TP 的 `closePosition: True` 改成 `reduceOnly`，6/03（`620b7a5`）又拿掉 `reduceOnly` → 現在 SL/TP 是「帶數量、無任何旗標」的獨立條件單，平倉時 Binance 不會自動撤銷對側；`_sync_sl_tp` 反覆補掛、demo `cancel` 又靜默失效 → 殘留單越堆越多
- 修復：新增 `_place_sltp()`，所有 SL/TP/保本單一律改用 **`closePosition: True`（不帶數量）**
  - Binance 在倉位平倉時自動撤銷同方向殘留的 closePosition 條件單（OCO 效果：止盈成交→止損自動消失）
  - 每 symbol/方向只允許一張 closePosition STOP + 一張 TP，從根本杜絕堆積，且繞過 demo 失效的 cancel API
  - closePosition 模式不可帶 qty（否則 -1106），故下單數量改為 `None`
  - 涵蓋 5 處下單點：`open_pos` SL/TP、`_sync_sl_tp` 補掛 SL/TP、`check_positions` 保本止損
- 注意：既有的 16 張舊殘留單為非-closePosition 型態，不會自動清除，需一次性手動清掉（見部署說明）

### 2026-06-09（KOL insight 更新）
- 依 `notes/youtube-insights.md` 2026-06-09 統整（101 個來源，6/06~6/09 共 10 支影片）更新 KOL 共識區間（BTC 從 59K 插針反彈至 64K，日線啟明星；三方共識為超跌反彈非反轉，空頭趨勢延續，反彈高空）
- `monitor_coins.py`：
  - `BTC_RESISTANCE_ZONE`: `(61500, 62000)` → `(63000, 64000)`（三方共識反彈高空帶）
  - `BTC_SUPPORT_ZONE`: `(59000, 61000)` → `(59500, 60000)`（6/08-09 二探短多區；失守→57,000 長期趨勢線）
  - `near_support` 追空禁令門檻隨之調整至 BTC ≤ 60,600
  - `COIN_BLACKLIST` 加入 `BEAT`（BTC飛揚 6/09：0.12→2 主力控盤，空中樓閣隨時崩盤如 LAB，嚴禁追高）；CHZ 由 6/07 BTC歐陽復盤再確認（利好出盡 0.045→0.035）
- `main.py`：`KEY_SUPPORT_ZONE` → `(59500, 60000)`、`KEY_RESISTANCE_ZONE` → `(63000, 64000)`（同步 6/09 共識）

### 2026-06-06（bugfix）
- `monitor_coins.py` `open_pos`：修復主流幣開倉全失敗的 bug
  - 症狀：BTC 觸發合法信號 → 開倉，但每次倒在 `set_margin_mode` 拋 `-4047`「Margin type cannot be changed if there exists open orders.」→ 開倉中止
  - 根因：平倉後殘留的條件單（demo `cancel_all` 靜默失敗）擋住改保證金模式；`except` 只容錯 `-4046/-4067`，漏了 Binance 實際回傳的 `-4047`（註解原本就想容錯此情境，但用錯代碼）
  - 修復：`set_margin_mode` 容錯加入 `-4047`。既已是 isolated，改保證金本非必要，殘留條件單也不擋市價開倉 → 直接繼續

### 2026-06-06（KOL insight 更新）
- 依 `notes/youtube-insights.md` 2026-06-06 統整（91 個來源）更新 KOL 共識區間（BTC 已正式跌破 60K，插針 59,000）
- `monitor_coins.py`：
  - `BTC_SUPPORT_ZONE`: `(59800, 61000)` → `(59000, 61000)`（追空禁令帶；生死線 59,800，失守→55K）
  - `BTC_RESISTANCE_ZONE`: `(67000, 67500)` → `(61500, 62000)`（飛揚短線反彈極限；歐陽波段更高 65K–66K）
  - `COIN_BLACKLIST` 加入 `HYPE`（跌破 1.618，思路轉空，禁做多）、`BCH`（老牌山寨無操作價值）
- `main.py`：`KEY_SUPPORT_ZONE` → `(59000, 61000)`、`KEY_RESISTANCE_ZONE` → `(61500, 62000)`（同步 6/06 共識）

### 2026-06-04（KOL insight 更新）
- 依 `notes/youtube-insights.md` 2026-06-04 統整（86 個來源，三方共識）更新 KOL 共識區間
- `monitor_coins.py`：
  - `BTC_SUPPORT_ZONE`: `(65000, 66000)` → `(59800, 61000)`（生死分水嶺；BTC 從 74K 暴跌直逼 60K，前低點/礦機成本區）
  - `BTC_RESISTANCE_ZONE`: `(67000, 67500)` 維持（反彈高空帶，延續 6/03）
  - `near_support` 追空禁令門檻隨之下移至 BTC ≤ 61,610
  - `COIN_BLACKLIST` 加入 `LAB`（BTC飛揚 6/04：24→15 高位崩盤，勿碰）、`ADA`（跌破 1.618 失活，禁合約）
- `main.py`：`KEY_SUPPORT_ZONE` → `(59800, 61000)`、`KEY_RESISTANCE_ZONE` → `(67000, 67500)`（同步 BTC 6/04 共識）

### 2026-06-02（三輪更新）
- `monitor_coins.py`：全面支援 Binance 雙向持倉模式（Hedge Mode）
  - 啟動時自動偵測持倉模式（`_detect_hedge_mode`），印出單向/雙向確認
  - 新增 `_open_params(direction)` / `_close_params(direction)` helper
  - Hedge mode：所有訂單改用 `positionSide: LONG/SHORT`；One-way：維持 `reduceOnly: True`
  - 涵蓋：開倉、SL、TP、保本止損、平倉所有下單路徑

### 2026-06-02（二輪更新）
- `monitor_coins.py`：找到 SL/TP 從未成功掛單的根本原因並修復
  - **根本 bug**：`closePosition: True` 和 `quantity` 同時送出 → Binance 拒絕（"Quantity and closePosition can not be sent together"）
  - 修復：所有 SL/TP/保本止損掛單改用 `reduceOnly: True`（相容 isolated 和 cross margin）
  - `set_margin_mode` 加 `-4046` 容錯：已是 isolated 不再整個 bail，改繼續執行
  - 開倉時 SL/TP 失敗加 Telegram 即時通知（不再只 print）

### 2026-06-02
- `monitor_coins.py`：修復 `close_pos` 平倉失敗後靜默退出的 bug
  - `reduceOnly: True` 失敗時：查詢交易所實際倉位大小，用實際數量重試（不帶 reduceOnly）
  - 若交易所已無此倉位（已手動平/SL/TP/清算）：清除本地 JSON 記錄，發 Telegram 通知
  - 修正 P&L 槓桿倍數：主流幣改用 `MAJOR_LEVERAGE (50x)`，不再誤用 altcoin 的 20x

### 2026-06-01（二輪更新）
- `main.py`：依 2026-06-01 KOL 統整（76 個來源）微調支撐區下沿
  - `KEY_SUPPORT_ZONE`: `(72250, 73000)` → `(72500, 73000)`（73k 確認為箱底，收窄下沿）
  - `KEY_RESISTANCE_ZONE`: `(74000, 75000)` 維持不變
- `monitor_coins.py`：`COIN_BLACKLIST` 加入 `ORDI`（BTC飛揚 6/01：徹底廢了，遠離）

### 2026-06-01
- `monitor_coins.py`：修復 SL/TP 三個 bug
  - Bug 1：軟體備援 SL 對主流幣（BTC/ETH/SOL）錯用 3.5%，改為正確的 `MAJOR_SL_PCT`（1%）
  - Bug 2：新增軟體備援 TP 檢查（`tp_sw`），交易所 TP 訂單失效時由軟體兜底
  - Bug 3：新增 `_sync_sl_tp()` — 每次 `check_positions` 時驗證交易所 SL/TP 訂單存活，失效自動補掛
  - 啟動時若有既有倉位，立刻執行一次 `check_positions` 確認狀態

### 2026-05-31
- `main.py`：依 2026-05-31 KOL 統整（13支影片 5/28~5/31）大幅下移 KOL 共識區間
  - `KEY_RESISTANCE_ZONE`: `(77000, 78000)` → `(74000, 75000)`（三方共識大幅下移，反彈至此高空）
  - `KEY_SUPPORT_ZONE`: `(75000, 75500)` → `(72250, 73000)`（74,000 跌破後下方強支撐）

### 2026-05-27（二輪更新）
- `main.py`：依 2026-05-27 KOL 統整（12支影片）再次收窄區間
  - `KEY_RESISTANCE_ZONE`: `(77000, 78500)` → `(77000, 78000)`（三天連續共識，STH 成本線 77,700）
  - `KEY_SUPPORT_ZONE`: `(75000, 76000)` → `(75000, 75500)`（飛揚/歐陽多頭最後防線）
- `monitor_coins.py`：
  - `analyze_major()` Signal 9 `squeeze_fuel` **停用**：FR 已回歸正常水位（~0.01%），加密龐克要求回歸傳統量價分析
  - 新增 `COIN_BLACKLIST = {'CHZ'}`：世界盃買預期賣事實已兌現，BTC歐陽禁止做多
  - 一般掃描 + 漲跌幅榜：黑名單幣種的 LONG 信號直接跳過

### 2026-05-27
- `main.py`：依 `notes/youtube-insights.md` 2026-05-25 更新（7支影片 5/22~5/24）調整 KOL 共識區間
  - `KEY_RESISTANCE_ZONE`: `(78000, 82000)` → `(77000, 78500)`（壓力區下移至 STH 成本線附近）
  - `KEY_SUPPORT_ZONE`: `(75500, 78500)` → `(75000, 76000)`（收窄至三方一致的多頭最後防線）
- `monitor_coins.py`：新增 `SHORT_BIAS = True`（三方 KOL 共識大級別偏空，弱勢反彈）
  - 一般掃描：LONG 方向需額外 +1 信號（`MIN_SIGNALS + 1 = 4`），SHORT 維持原門檻
  - 漲跌幅榜掃描：同樣對 LONG 方向多要求 1 個確認信號

### 2026-05-26
- `monitor_coins.py` `send_performance_report()`：改版績效報告
  - 報告分為「主流幣（BTC/ETH/SOL）」和「山寨幣」兩區，各含小計
  - 以 `STATS_FROM` 環境變數作為起算日，標題顯示「(起算日 起)」
- `docker-compose.yml`：`coin-monitor` 加入 `STATS_FROM=2026-05-26`（今日錢包重置起算點）

### 2026-05-23
- `main.py`：依 `notes/youtube-insights.md` 2026-05-23（45支影片統整）更新 KOL 共識區間
  - `KEY_SUPPORT_ZONE` 上沿從 76,000 擴大至 **78,500**（含 STH 成本線 78,300，三方支撐共識更新）
- `monitor_coins.py`：依 2026-05-23 KOL 建議調整進場門檻
  - `MIN_SIGNALS`: 2 → **3**（降低假突破入場頻率，震盪行情雜訊多）
  - `LEADERBOARD_MIN_PCT`: 3.0% → **4.0%**（只做強勢標的，篩掉弱訊號）
  - `STOP_LOSS_PCT` 維持 3.5%（已符合 KOL「≥ 3.5%」建議，不變）

### 2026-05-22
- `main.py` `compute_kol_filters()`：依 `notes/youtube-insights.md` 第二輪（2026-05-22）加入靜態 Zone 與嘎空保護
  - 新增常數 `KEY_SUPPORT_ZONE = (75500, 76000)`、`KEY_RESISTANCE_ZONE = (78000, 82000)`（三方 KOL 共識，每輪手動更新）
  - `in_support_zone` / `in_resistance_zone`：偵測現價是否在兩個靜態 Zone 內
  - `squeeze_short_risk`：`fr_raw < -0.0003` 且 `|fr_z| > 1.5σ` → 暫停做空（大幅負費率嘎空風險）
  - main loop 每 tick 新增 Zone log，顯示價格相對支撐/壓力區位置
- `monitor_coins.py` `send_performance_report()`：績效報告時間窗口由 7 天改為 **30 天**
  - 原因：BTC/ETH/SOL 主力幣持倉周期較長（數天），7 天窗口會漏掉大多數已平倉交易
  - 標題改為「月績效報告（過去30天）」
- `monitor_coins.py` `log_altcoin_trade()`：BTC/ETH/SOL 平倉記錄寫入各自獨立檔案
  - 修復：coin-monitor 的主流幣交易全部寫進 `altcoin_trades.jsonl`，績效報告無法分幣種統計
  - BTC → `btc_trades.jsonl`、ETH → `eth_trades.jsonl`、SOL → `sol_trades.jsonl`、其餘 → `altcoin_trades.jsonl`

### 2026-05-19
- `monitor_coins.py` `analyze_major()`：依 `notes/youtube-insights.md` 加入 KOL 參考邏輯
  - **信號 7：bb_mid 支阻互換**（BTC歐陽）：前根收盤低於布林中軌，反抽到中軌 ±0.3% → 空頭訊號
  - **信號 8：日線 EMA200 牛熊分界**（加密龐克）：每次分析獨立抓取 210 根日線，站上/跌破 EMA200
  - **信號 9：嘎空燃料強化**（加密龐克）：接近 EMA200（≥97%）且資費轉負，與 signal 4 不重複
  - **信號 10：多頭過熱過濾**（加密龐克）：資費 > 0.05% = 假突破風險，列入空頭票
  - **移除信號 6 的地板追空**（BTC飛揚）：bb_pct < 0.15 不再加空頭訊號（地板不追空）
  - **週末量能門檻**（BTC飛揚）：scan loop 週末時主流幣 MIN_SIGNALS +1，降低過度交易

### 2026-05-17
- `monitor_coins.py` 主流幣（BTC/ETH/SOL）獨立止盈止損與槓桿：
  - 槓桿：20x → **50x** isolated（山寨幣維持 20x）
  - 止損：3.5% → **1%**（主流幣波動較小，需更緊的止損）
  - 止盈：7% → **2%**（對應 50x 下 2% = 保證金 100% 獲利）
  - `open_pos()` 自動依 `WATCH_ALWAYS` 判斷主流/山寨，套用對應參數

### 2026-05-16
- `main.py` `compute_kol_filters()`：BTC/ETH/SOL 機器人全面落地加密龐克 KOL 觀點
  - **假突破風險封鎖**（方向過濾）：資費 > 0.05% 且緊貼 EMA200 → 暫停 LONG，TG 通知
  - **靠近支撐區暫停做空**（方向過濾）：price ≤ EMA200×1.02 且資費非正 → 暫停 SHORT（空在支撐上）
  - **資費翻負 TG 通知**：FR 由正轉負 → 推播嘎空動能訊號
  - **軋空燃料覆蓋縮倉**（倉位調整）：squeeze_fuel_up 時即使震盪市也維持正常倉位
  - **資費過熱縮倉**（倉位調整）：fr > 0.05%（非壓力區）→ 倉位縮至 50%
  - **右側交易確認**：3 日均收盤站上 EMA200 → log 高確定性 LONG 窗口
  - **美股回調流動性**：SPY/QQQ 下跌 > 0.5% → log 加密多頭催化劑提示
- `monitor_coins.py` `get_btc_kol_gate()`：山寨幣機器人新增 BTC 結構門檻
  - BTC 假突破風險時，`scan()` 和 `scan_leaderboard()` 均跳過所有 LONG 進場
  - `analyze()` 信號 4 新增「資費轉負（嘎空燃料）」觸發條件（除了原有劇變外）
- `monitor_coins.py` `WATCH_ALWAYS`：BTC/ETH/SOL 加入常駐掃描清單，三個 ML bot 暫置，改由 coin-monitor 以技術分析信號交易
- `monitor_coins.py` `analyze_major()`：主流幣（BTC/ETH/SOL）獨立分析邏輯，與山寨幣分開
  - 信號：EMA9/21/50 排列、MACD histogram 連續方向、成交量 ≥1.3x 均量、資費翻轉
  - 方向由多數決（bull 信號數 vs bear 信號數），非預設做多
  - `analyze_dispatch()` 自動路由：主流幣用 `analyze_major`，其他用 `analyze`
  - 信號擴充至 6 個：新增 RSI 動能（RSI 方向 × 50 線）、布林帶突破（bb_pct > 0.85 / < 0.15）
- `scripts/auto_kol_update.py` KOL 指標追蹤與擴充：
  - 新增 KOL：**BTC飛揚**（@BTCfeiyang）、**BTC歐陽**（@BTC-ouyang），共 3 個頻道
  - 分析引擎改為 **Gemini 2.0 Flash**（免費），直接分析影片 URL，繞過 VPS IP 被 YouTube 封鎖的問題
  - 字幕 API → 失敗自動 fallback 到 Gemini 直接看影片（不再因 IP 封鎖卡住）
  - `--historical` 模式：`python3 scripts/auto_kol_update.py --historical` 掃描所有 RSS 歷史影片（最多 15 支）
  - `ta_indicators` 輸出欄位 + `update_kol_indicator_profile()` 累計各 KOL 指標次數 → `notes/kol_indicators.json`
- `STATS_FROM` env var：Demo 重置後設定此日期，整點 P&L 和週報只計算之後的交易，無需刪除 `altcoin_trades.jsonl`

### 2026-05-15
- `scripts/auto_kol_update.py`：每日自動抓取 KOL YouTube 影片字幕 → Claude 分析 → 更新 `notes/youtube-insights.md` → 高信心參數自動套用 → git push → TG 通知
- `notes/youtube-insights.md`：加密龐克頻道初始洞察（3 支影片）+ 可實作映射表
- README 新增 Strategy Reference 段落，對應 KOL 概念與現有特徵

### 2026-05-11
- 一般掃描信號門檻 3→2，漲跌幅榜移除 TG 通知（保留交易邏輯）
- RSI 過濾門檻 70/30→80/20（放寬，避免漲幅榜幣種被全部擋掉）
- 修復 yfinance `FutureWarning`（`close.squeeze()` 取代直接 `float()`）
- 止盈 9%→7%

### 2026-05-10
- 保本止損：獲利 ≥ 3% 後自動把止損移至進場價附近
- RSI-14 進場過濾（做多 RSI < 80 / 做空 RSI > 20）
- EMA-50 趨勢過濾（方向必須與 EMA50 一致）
- 大環境投票過濾：BTC ±2%、SPY ±0.5%、QQQ ±0.5% 各投票；2 票偏空→跳過做多，2 票偏多→跳過做空
- 整點報告加入今日累積 P&L
- 週報加入勝率

### 2026-05-07（revert）
- 追蹤止損改回固定止損：SL 3.5% + TP 9% + 軟體備援 15%（追蹤止損期間績效轉負，還原）

### 2026-05-06
- 動態保證金：2 信號 $60 / 3 信號 $80 / 4 信號 $100
- 限價單進場：掛偏 0.02% 的限價單，15 秒未成交改市價
- 修復啟動時 `STOP_LOSS_PCT` NameError crash

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
