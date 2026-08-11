# TradingBot

ML-powered crypto futures trading bot for BTC, ETH, SOL and altcoins.  
Runs 24/7 on a VPS via Docker, sends all notifications to Telegram.

> Last updated: 2026-08-11 14:21 +08

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
- `SHORT_BIAS=False`（2026-07-22 更新，此前自 2026-05-25 起長期 True）: **龐克明確稱「昨日突破了 65,500 這一線平行高點」（首次用「突破」非「摸到」），現貨查證連續 4h 收盤站穩、回踩 65,505 守住支撐＝SHORT_BIAS 鬆動門檻（龐克創 65,500 新高）正式觸發**；altcoins LONG 完全解禁、major coins LONG 不再需要額外 +1 訊號
- **Short-Squeeze Filter** (`squeeze_no_short`, 2026-06-16 龐克): when BTC funding ≤ `SQUEEZE_FR_EXTREME` (−0.03%) **and** OI at a 14-day high (OI degrades to funding-only if unavailable), **all** new SHORT entries are paused market-wide (主力惡意軋空起手式，避免空在地板被清算)
- `near_support` gate: when BTC ≤ `BTC_SUPPORT_ZONE[1]`×1.01（**2026-07-28 更新**：`BTC_SUPPORT_ZONE (62,000-63,500)`，門檻 ≤64,135；歐陽+飛揚同日互相印證跌破 64,000 後的新支撐帶）, altcoin SHORT entries are skipped (追空禁令；跌破才有暴跌空間)。Cosmetic `BTC_RESISTANCE_ZONE` 維持 68,000–71,000（龐克：短期持有者成本線／200 日均線，本輪熊市最後最關鍵位）
- ETH-only gate（**2026-08-01 更新**：`ETH_RESISTANCE_ZONE` 1,948–2,030 / `ETH_SUPPORT_ZONE` 1,810–1,850）：歐陽公開策略實單於 1,903 開空一路拿到 1,846 分批指盈、飛揚獨立確認最低探至 1,847 收線收回 1,850 之上——兩者互相印證同一次已發生跌破，`ETH_SUPPORT_ZONE (1,900-1,930) → (1,810,1,850)`，舊帶翻轉為中繼壓力區。SHORT 在深支撐帶 1,810-1,850 內暫緩（未跌破嚴禁追空）、在 1,850-1,948 突破多頭區同樣暫緩，放行僅限 ≥1,948（高空帶）或深破支撐帶追空；`ETH_LONG_ZONE` 1,800–1,820／`ETH_NO_LONG_ABOVE` 1,860 本輪未變動（LONG 僅在此區 ±1% 放行，仍需 SHORT_BIAS major +1 訊號）
- SOL-only gate (`SOL_RESISTANCE_ZONE` 100–120 / `SOL_SUPPORT_ZONE` 60–75, 2026-07-02 飛揚): SOL 月線 TD9 反轉、自 60 反彈至 80（日線分水嶺），支撐下移周線 886/2618 = 60–75、守 60；生死線/高空區上移 100–120。SOL is short-biased — LONG skipped unless price ≤ ~75.75 (逢低接多至 75、80 以上勿追涨); SHORT skipped while inside the 60–75 support floor (地板追空 R:R 差、守 60). 100–120 是真正分水嶺高空區（飛揚 7/2：不值得空 SOL、等 120 再空）— 記於 cosmetic `SOL_RESISTANCE_ZONE`（未接線）
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
- **8/11（飛揚＋歐陽）★純重申、參數不變、未重啟、SHORT_BIAS 維持 False★**：飛揚 8/10 晚間頭肩頂預警本輪兌現，BTC 隔夜下跌直落 63,007-63,008(恰觸現行支撐帶上緣)，今日考慮反彈至 64,300-64,800 後再高空，非追多。歐陽 65,500-66,000 空單框架(連續多輪)完整兌現獲利，公開策略/ETH 空單(1,902 進場現 1,872，浮盈約 300 點)皆處獲利狀態，規劃對高點空單平倉指引並轉向 62,500-63,000(支撐帶中軌)接多，判斷 8 月大機率維持寬幅震盪非趨勢性下跌。→ **不改任何常數**：BTC 下跌至支撐帶上緣是既有框架驗證而非破位，兩人的反彈壓制/接多規劃皆完全落在現行 `BTC_SUPPORT_ZONE (62,000-63,500)` 內；未宣告趨勢反轉，SHORT_BIAS 重新武裝條件依舊未觸及。**git 同步未重啟、發 TG**（★下一輪最高優先監看:BTC 能否守住支撐帶並反彈至 64,300-64,800;反彈受阻後高空/平多轉空劇本是否兌現;62,500-63,000 接多規劃是否觸發;能否延續測試支撐帶下緣 62,000★）
- **8/10（第二輪，加密龐克＋飛揚）★純重申、參數不變、未重啟、SHORT_BIAS 維持 False★**：龐克發現 CME 期貨機構淨部位近幾週轉為異常增多做多，明確強調屬前瞻觀察數據、方向尚未確定（歷史上僅出現過兩次類似訊號，分別發生在 74,000→126,000 大漲前、3 月底近 30% 軋空行情前），若能維持 64,000 以上則看向 68,000-71,000(cosmetic 壓力帶內)。飛揚：BTC/ETH 15 分鐘級別同時出現頭肩頂結構轉入回彩，BTC 看向 64,800-64,900 支撐、ETH 看向 1,800 附近支撐，考慮回彩低多，會員 BTC 空單已於成本價出場。→ **不改任何常數**：龐克 CME 訊號為單一 KOL 前瞻觀察非已實現事件，目標區間落在現行 cosmetic `BTC_RESISTANCE_ZONE` 內；飛揚回彩支撐位完全落在現行 `BTC_SUPPORT_ZONE`/`ETH_SUPPORT_ZONE` 延伸範圍；未宣告趨勢反轉，SHORT_BIAS 重新武裝條件依舊未觸及。**git 同步未重啟、發 TG**（★下一輪最高優先監看:CME 機構淨部位發展及是否伴隨價格方向確認；BTC 能否守住 64,800-64,900；ETH 能否守住 1,800 附近；68,000-71,000 目標後續發展★）
- **8/10（飛揚＋歐陽）★純重申、參數不變、未重啟、SHORT_BIAS 維持 False★**：BTC 持續在 64,800-65,500 壓制區間拉鋸未突破。飛揚：週線多頭指標發力，65,000 持續受測，若突破布林中軌看向 69,000(cosmetic 壓力帶內)，短線建議等回彩 64,000-64,500 再低多。歐陽：重申 65,500-66,000 做空框架(連續多輪)，新增穩定幣流動性收縮數據佐證謹慎立場，提醒熊市橫久必跌魔咒，建議空倉者管住手、現貨長線持有者於 65,000 附近適度減倉，重申 6 萬附近週線終極底部推演(延續 8/9 已報告 48,000 推演非新事件)。→ **不改任何常數**:雙方框架完全落在現行 `BTC_SUPPORT_ZONE (62,000-63,500)`/cosmetic `BTC_RESISTANCE_ZONE (68,000-71,000)` 延伸範圍內,屬持續驗證;未宣告趨勢反轉,SHORT_BIAS 重新武裝條件依舊未觸及。**git 同步未重啟、發 TG**（★下一輪最高優先監看:BTC 能否有效突破 65,000-65,500;週線 69,000 目標是否兌現;歐陽現貨減倉建議是否獲其他 KOL 佐證;穩定幣供應量收縮趨勢後續發展★）
- **8/9（第二輪，飛揚單片 BTC+ETH，Whisper）★純重申、參數不變、未重啟、SHORT_BIAS 維持 False★**：BTC 反彈提前開啟錯過 64,500 低多入場點，現持續在 4,800-5,300(64,800-65,300) 壓制區間磨。飛揚宣布 ETH 現貨已開始分批建倉(防踏空)，但本人仍判斷大方向未解除「最後一跌」風險(月線軍線空頭排列、若本月冲高後乘壓恐形成第三個死叉)；短線合約關注日線 1950 大分水嶺與 4 小時 2000 整數關卡突破結果，1900-1902 支撐持續守住。→ **不改任何常數**:BTC 測試區間在現行 `BTC_SUPPORT_ZONE`/cosmetic `BTC_RESISTANCE_ZONE` 合理延伸範圍內;ETH 現貨建倉是防踏空紀律性布局非結構反轉宣告,測試區間逼近但未觸及 `ETH_RESISTANCE_ZONE (1,948-2,030)` 下緣;未宣告趨勢反轉,SHORT_BIAS 重新武裝條件依舊未觸及。**git 同步未重啟、發 TG**（★下一輪最高優先監看:ETH 能否站穩 2000 並延續挑戰 `ETH_RESISTANCE_ZONE` 下緣;ETH 月線是否形成第三個死叉;BTC 能否延續突破 64,800-65,300 壓制區間★）
- **8/9（飛揚＋歐陽）★純重申、參數不變、未重啟、SHORT_BIAS 維持 False★**：BTC 持續在 64,700-65,500 窄幅盤整未能有效突破，雙方皆定性此區間為關鍵分水嶺。飛揚：現貨可於 6 萬-6 萬 4 進場一半至三分之二倉位、合約不建議追多，月線角度上方目標 66,000-67,000，需突破死亡三角才確立反轉。歐陽：警惕多頭陷阱，延續 65,500 埋伏空單止損 66,000 策略，提及約 2 億美元空單清算強度可能引發軋空，但預估接下來會有約 2000 點雙向劇烈震盪，並首度提出週線終極底部約 **48,000** 推演(估跌幅 60%，因 ETF 機構墊底)。→ **不改任何常數**:飛揚/歐陽觀察帶皆在現行 `BTC_SUPPORT_ZONE (62,000-63,500)`/cosmetic `BTC_RESISTANCE_ZONE (68,000-71,000)` 合理延伸範圍內;歐陽 48,000 推演屬單一 KOL 條件式長線判讀、現價遠未觸及不構成共識門檻;未宣告趨勢反轉,SHORT_BIAS 重新武裝條件依舊未觸及。**git 同步未重啟、發 TG**（★下一輪最高優先監看:BTC 能否有效突破 65,000-65,500;若突破是否引發軋空;2000 點雙向震盪是否出現;48,000 週線底部推演是否獲佐證;月線死亡三角能否被打掉★）
- **8/8（第二輪，飛揚單片 BTC+ETH，Whisper）★純重申、參數不變、未重啟、SHORT_BIAS 維持 False★**：公開頻道 BTC 多單(8/6 63,900-64,200 進場)今日全部止盈完畢，總獲利約 900 點，公開頻道達成 8 連勝(近 3 個多月 18 單 15 勝 3 敗)。ETH 持續在 1900 關卡窄幅震盪，1,902 支撐區間一天內尚未重新站回 1,920 以上，週末波動不強，明確表態現階段無進場價值、只能等待突破 1,900 追漲或回測後低多，日線上方目標 1,950。→ **不改任何常數**:BTC 多單為對已完成交易的績效彙報，區間在現行 `BTC_SUPPORT_ZONE`/cosmetic `BTC_RESISTANCE_ZONE` 合理延伸範圍內;ETH 測試範圍逼近但未觸及 `ETH_RESISTANCE_ZONE (1,948-2,030)` 下緣,與前兩輪內容一致;未宣告趨勢反轉,SHORT_BIAS 重新武裝條件依舊未觸及。**git 同步未重啟、發 TG**（★下一輪最高優先監看:ETH 能否站穩 1,900 之上並延續挑戰 `ETH_RESISTANCE_ZONE` 下緣;ETH 打回 1,900 低多劇本是否兌現;BTC 後續新一輪公開策略進場★）
- **8/8（飛揚＋歐陽）★純重申、參數不變、未重啟、SHORT_BIAS 維持 False★**：飛揚昨晚 ETH 1,900-1,902 低多提醒本輪兌現(最高 1,943 回撤至 1,903 觸底反彈，尚未重新站回 1,902)，BTC 公開策略 63,900-64,000 多單於 65,000 附近止盈一半獲利 1,000 點、剩餘部位止損移至成本價，現於 64,500-64,800 區間週末盤整，4 小時 64,800-65,300 持續構成關鍵壓制。歐陽標題轉為明確看空「絕佳做空機會即將上演」，實際延續 7 月底已設定的 65,000-65,500 區間開空/64,000 分水嶺框架，公開策略空單已有浮盈；另評 SOL(75 美元壓力可空)、AVI(隨 ETH 聯動)。→ **不改任何常數**:飛揚為對已發生行情的即時驗證;歐陽標題轉強硬但框架非本輪新提出,與飛揚多單觀點屬同一區間內正常雙向戰術非結構反轉共識;未宣告趨勢反轉,SHORT_BIAS 重新武裝條件依舊未觸及。**git 同步未重啟、發 TG**（★下一輪最高優先監看:BTC 能否守住 64,000-64,500 或跌破轉向測試前低;BTC 能否突破 65,000-65,500;ETH 能否重新站回 1,902 之上;歐陽做空浮盈能否延續兌現★）
- **8/7（第二輪，飛揚單片 BTC+ETH，Whisper）★純重申、參數不變、未重啟、SHORT_BIAS 維持 False★**：公開策略 BTC 63,900-64,200 進場多單一路上漲至 65,000-65,180，已止盈 50%、剩餘部位放在成本價保護、「立於不敗之地」。ETH 突破 1,900 整數關卡上衝至 **1,943**，1,895 支撐再度驗證未破，日線收小十字星守住 1,900，4 小時分水嶺 1,904 附近回調測試 1,901-1,902 視為低多機會，上方看向 1,950。→ **不改任何常數**:單一 KOL 延續追蹤已發生行情，BTC 測試區間在現行 `BTC_SUPPORT_ZONE`/cosmetic `BTC_RESISTANCE_ZONE` 合理延伸範圍內;ETH 1,943 逼近但未觸及 `ETH_RESISTANCE_ZONE (1,948-2,030)` 下緣,飛揚明確表態階梯式上漲不宜過分看漲;未宣告趨勢反轉,SHORT_BIAS 重新武裝條件依舊未觸及。**git 同步未重啟、發 TG**（★下一輪最高優先監看:ETH 能否觸及測試 `ETH_RESISTANCE_ZONE` 下緣;ETH 1,901-1,902 低多劇本是否兌現;BTC 公開策略剩餘部位出場時機;BTC 能否延續挑戰 65,500-68,000★）
- **8/7（龐克＋飛揚＋歐陽）★純重申、參數不變、未重啟、SHORT_BIAS 維持 False★**：龐克重申 BTC 守住 64,000(重合大級別趨勢線)則有機會挑戰 65,500 甚至 68,000-71,000，近期利空(微策略賣幣/冷錢包盜竊/清晰法案推遲至 9 月三週窗口)對 59,000-65,000 區間紋絲不動，視為賣方力量衰竭的熊末特徵。飛揚：公開策略 BTC 63,900-64,200 多單未破 65,000 回到成本價整理，補倉價 63,004；ETH 1,895 支撐守住同樣回落整理(另一支公開策略影片轉錄失敗，未列入判讀)。歐陽：標題「橫久必跌」語氣轉謹慎、提醒防守，但操作框架仍是 62,000 接多/65,000 做空，8 月轉輕倉試單。→ **不改任何常數**:三位 KOL 皆為對現行 `BTC_SUPPORT_ZONE (62,000-63,500)`/cosmetic `BTC_RESISTANCE_ZONE (68,000-71,000)`/`ETH_SUPPORT_ZONE (1,810-1,850)` 框架的延續驗證，歐陽語氣轉謹慎但操作邏輯未變;未宣告趨勢反轉，SHORT_BIAS 重新武裝條件依舊未觸及。**git 同步未重啟、發 TG**（★下一輪最高優先監看:BTC 能否守住 64,000 並延續挑戰 65,500-68,000;歐陽「橫久必跌」風險提醒是否獲其他 KOL 佐證;ETH 能否延續 1,895-1,900 支撐或觸及 `ETH_RESISTANCE_ZONE`;今晚非農數據影響;清晰法案 9 月表決窗口進度★）
- **8/8 每日健檢 bug fix（重啟誤發績效報告 + 漲跌幅榜選幣未過濾 demo 不支援幣種）**：依新增的每日盈虧報告健檢流程巡查過去 24h VPS log，發現兩個問題。(1) `last_report_date` 初始值為 `None`，導致**每次重啟容器都會立即誤發一次績效報告**（不只在每天 00:00 +08 該發的那次）——已於 8/7 23:30 觸發一次非預期報告，緊接著 8/8 00:01 才是正常班次；修法：初始化為 `now8().date()`，僅在真正日期切換時才發送。(2) `send_leaderboard()`（漲跌幅榜選幣）完全基於公網 ticker 挑選候選，未比照 `get_top_coins()` 過濾 demo 交易所不支援的幣種，導致 `ALLO/USDT:USDT` 這類主網有行情、demo 無市場的幣種每次觸發強訊號（+17.7%/+26.5%/+25.1%，皆 2 信號）都以 `-1121 Invalid symbol` 開倉失敗，過去 24h 內錯過 3 次；修法：比照 `get_top_coins()` 邏輯，在取得漲跌幅榜候選後與 `exchange_priv.markets` 交集過濾，排除 demo 不支援的幣種。兩項修復皆為明確根因、可重現模式，已 `ast.parse` 驗證通過。
- **8/7 bug fix（`open_pos` 山寨幣槓桿降級重試）**：巡查 VPS log 發現 `BICO`/`ESP`/`MEGA`/`ZAMA` 等山寨幣每次觸發訊號開倉都以 `-4028 Leverage 20 is not valid` 失敗（該幣種在 demo 允許的最大槓桿低於程式碼寫死的 20x 山寨幣預設值），累計已噴 49 次、當中 `BICO` +38.5%/+34.5% 兩筆強訊號（2 個信號觸發）皆因此直接錯過，且失敗發生在 `set_leverage()` 這一步、尚未下單，故無部位風險純屬機會流失。修法：遇到 `-4028` 時改為在 `(10,5,3,2,1)` 中逐步降低槓桿重試直到交易所接受為止，成功後沿用該實際槓桿值計算部位大小（`amount = margin×lev/price`），與既有邏輯完全相容。已 `ast.parse` 驗證通過。
- **8/6（第三輪，飛揚單片 BTC+ETH，Whisper）★純重申、參數不變、未重啟、SHORT_BIAS 維持 False★**：BTC 昨日突破後衝高至 65,000，今日在 64,000-65,000 附近走出雙頂，回踩測試 64,000 支撐未破(連續兩次插針)，等待重回 64,500 以上。ETH 突破 1,900 後衝高至 1,927 反抽，回落測試 **1,895** 三重底支撐守住，視為低多機會，1,800-1,900 整段支撐持續未破，4 小時結出金三角等待發力。→ **不改任何常數**:單一 KOL 短線戰術追蹤,BTC 測試區間與現行 `BTC_SUPPORT_ZONE (62,000-63,500)`/cosmetic `BTC_RESISTANCE_ZONE (68,000-71,000)` 一致、ETH 測試區間與現行 `ETH_SUPPORT_ZONE (1,810-1,850)` 之上緣一致,皆屬既有框架延伸監看;未宣告趨勢反轉,SHORT_BIAS 重新武裝條件依舊未觸及。**git 同步未重啟、發 TG**（★下一輪最高優先監看:BTC 能否重回 64,500 以上並延續挑戰 65,500-68,000;ETH 能否延續蓄力發力挑戰 `ETH_RESISTANCE_ZONE`;BTC 雙頂結構是否被有效突破或再度受阻★）
- **8/6（第二輪，加密龐克單片，字幕）★純重申、參數不變、未重啟、SHORT_BIAS 維持 False★**：BTC 逼近 65,500 關鍵位；週線大級別下降趨勢線(126,000-98,000-83,000 連線)「昨日剛突破」，類比 22 年熊市末期同類突破後未再破位、走出最後一段盤整的先例；日線下降趨勢線(83,000 高點連線)同步突破，解釋近兩周機械式下跌的「空氣牆」阻礙已解除；並援引黃金昨日同步突破類似趨勢線後爆拉上攻作為類比。判斷若創出高於 65,500 的更高高點，下一步大機率拉向 **68,000-71,000**(短期持有者成本線)，若受阻回落才觸發真正終極震倉、但深度可能不會太深(估 56,000 或前低附近)；總結表態「只要不低於 64,000 就都有機會向上挑戰」。60 天流動性降至近 4 年最低、180 天長期流動性降至 0.32；現貨 ETF 今日流入 244M(逾 3,000 顆 BTC)。→ **不改任何常數**:本輪僅龐克單一 KOL,趨勢線突破雖為已發生技術事件,但後續能否創高、能否挑戰 68,000-71,000 皆屬條件式推演;**68,000-71,000 目標與現行 cosmetic `BTC_RESISTANCE_ZONE (68,000-71,000)` 完全吻合、64,000 分水嶺與現行 `BTC_SUPPORT_ZONE (62,000-63,500)` 監看位一致**,屬既有框架強化非新結構表態;未宣告趨勢反轉,SHORT_BIAS 重新武裝條件依舊未觸及。**git 同步未重啟、發 TG**（★下一輪最高優先監看:BTC 能否守住 64,000 並創出高於 65,500 的更高高點;能否延續挑戰 68,000-71,000;若受阻回落終極震倉幅度是否落在 56,000 或前低附近;ETH 能否隨 BTC 挑戰 68,000 同步摸向 2,000;現貨 ETF 資金流入是否延續恢復★）
- **8/6（飛揚＋歐陽）★純重申、參數不變、未重啟、SHORT_BIAS 維持 False★**：飛揚昨晚設下的 ETH 1,900 強勢壓制位突破確認提醒本輪兌現(最高衝至 1,927，收在 1,925 附近，現回撤測試 1,900)；BTC 64,500 壓制也小幅突破後回落再測，日線 TD9 反轉+啟明星結構持續發展，月線角度續看 66,000-67,000。歐陽:BTC 反彈延續嘗試但 4 小時連續上引線顯示走勢不穩,62,000 接多框架持續有效(已於 64,000 分批止盈),續看 65,000 做空;ETH 仍以強勢做多為主。→ **不改任何常數**:ETH/BTC 突破提醒皆為短線戰術驗證的兌現,仍在現行常數合理延伸範圍內、非結構性突破確認,兩位皆未宣告趨勢反轉,SHORT_BIAS 重新武裝條件依舊未觸及。**git 同步未重啟、發 TG**（★下一輪最高優先監看:BTC 能否站穩 64,500 並挑戰 65,000-65,500;ETH 能否延續站穩 1,900 之上或回測 `ETH_SUPPORT_ZONE`;歐陽上引線疑慮是否兌現;BTC/ETH 是否觸及 cosmetic 壓力帶★）
- **8/5（龐克＋飛揚×2＋歐陽）★純重申、參數不變、未重啟、SHORT_BIAS 維持 False★**：龐克 BTC 收回 64,000 密集成交區(POC)之上,需突破 65,500 終結短線下降趨勢,68,000 短期持有者成本線仍是觀察位;**首支比特幣現貨 ETF 正式清退**(24 年 1 月首發以來首見),定性為散戶→交易所→機構全面投降跡象;**冷錢包盜竊事件第五天,24 小時內超 70 萬個地址轉移資產創新高**;180 天流動性指標降至 0.33。飛揚:64,500/1,880 不破不追提醒本輪雙雙兌現;另一支確認 BTC 強於 ETH、大方向看向 66,000-70,000。歐陽:**62,000 接多框架完整驗證**(最低正探 62,000-62,005 反彈),64,000 分批止盈,續看 65,000-65,500 做空。→ **不改任何常數**:龐克 ETF 清退+冷錢包規模擴大屬熊市尾聲敘事延續非新結構;飛揚戰術驗證/歐陽框架驗證皆完全落在現行 `BTC_SUPPORT_ZONE (62,000-63,500)` 內;未宣告趨勢反轉,SHORT_BIAS 重新武裝條件依舊未觸及。**git 同步未重啟、發 TG**（★下一輪最高優先監看:BTC 能否有效突破 64,500-65,500;ETH 能否突破 1,880-1,900;冷錢包駭客受害規模與 ETF 清退潮後續發展;歐陽 65,000-65,500 做空布局是否兌現★）
- **8/4（龐克＋飛揚×2＋歐陽）★純重申、參數不變、未重啟、SHORT_BIAS 維持 False★**：龐克重申賣方衰竭指數達熊市末端水平(較 22 年同水平提早約 40 天)，**50,000-60,000 終極底部推演與 61,097 關鍵位監看**維持，冷錢包駭客受害人數升至近 8,000 人/累計被盜逾 1.3 億美元——皆屬前瞻條件式判讀,現價仍在現行 `BTC_SUPPORT_ZONE (62,000-63,500)` 內、未觸及更未跌破,與 8/3 第二輪判讀一致,屬第二次重申。飛揚 ETH:1,800-1,900 區間持續高位震盪、1,880 關鍵壓制未破,與 `ETH_SUPPORT_ZONE (1,810-1,850)` 重疊,屬持續驗證。飛揚 BTC:4 小時三重底驗證 62,400-62,500 支撐、**64,000-64,500 分水嶺連續未破**,另評 SOL 相對抗跌、LDO/OP 判定「垃圾」不核心監控。歐陽重申 62,000 接多/65,000 附近部分止盈框架,本輪操作成功驗證(62,000 觸及反彈至 64,000 整數關卡)。→ **不改任何常數**:四位 KOL 均為對現行常數的持續驗證非新資訊;未宣告趨勢反轉,SHORT_BIAS 重新武裝條件依舊未觸及。**git 同步未重啟、發 TG**（★下一輪最高優先監看:BTC 能否有效突破 64,000-64,500 分水嶺;BTC 是否轉向測試 61,097 或跌破現行 `BTC_SUPPORT_ZONE` 下緣(龐克連續兩輪監看位,若獲多方佐證將是常數下修觸發點);ETH 能否有效突破 1,880-1,900 或跌破 1,800 轉向直接看空;冷錢包駭客受害規模是否持續擴大;賣方衰竭指數後續變化★）
- **8/3（第三輪，飛揚單片 ETH+ZEC+HYPE，Whisper）★純重申、參數不變、未重啟、SHORT_BIAS 維持 False★**：會員頻道早盤 1,869-1,873 進場 ETH 空單、45 左右了結,最低摸到 1,827,獲利可觀;現反彈至 **1,860** 明顯水平支撐/壓力位,日線大分水嶺鎖定 **1,800**(守住則維持逢高做空邏輯,跌破才轉向直接看空)。**現價 1,860 測試區間仍在 `ETH_SUPPORT_ZONE (1,810-1,850)` 上緣附近,未跌破現行支撐帶**。ZEC(週線相對安全,200 雙頂頸線為絕對防守位、反彈目標 500-530)、HYPE(反彈目標 53-58)皆屬個人合約交易評論,非核心監控幣種不涉及 `COIN_BLACKLIST`。→ **不改任何常數**:本輪僅飛揚單一 KOL,屬延續性戰術觀察;1,800 分水嶺屬前瞻條件式判讀;未宣告趨勢反轉,SHORT_BIAS 重新武裝條件依舊未觸及。**git 同步未重啟、發 TG**（★下一輪最高優先監看:ETH 日線收線是否跌破 1,800;ETH 能否延續 1,800-1,900 區間震盪或有效突破;1,860-1,880 乘壓做空劇本是否兌現★）
- **8/5 bug fix（`open_pos` SL/TP 掛單失敗處理）**：用戶回報 SOL/USDT:USDT 開倉後 SL、TP 皆掛單失敗(`-4130`：交易所已有同方向殘留 closePosition 條件單,demo 撤不掉也撈不到)；同批 24h log 另發現 HOME/USDT:USDT 開倉瞬間 SL 掛單失敗(`-4509`：TIF GTE 要求已有倉位,交易所尚未同步完成的競態)但已於下個掃描週期被 `_sync_sl_tp()` 自動補掛痊癒。排查確認 `check_positions()` 每 15 分鐘的軟體止損/止盈判斷（monitor_coins.py:1240）本就與交易所訂單是否存在無關,故此類失敗最壞情況只是延後到下個掃描週期才被軟體強制平倉,並非真正裸奔,但仍優化 `open_pos()`：SL/TP 掛單遇 `-4509` 時等待 2 秒重試一次(直接處理競態根因)；遇 `-4130` 時立即標記 `sltp_4130_noted`,不必等到下一輪 `_sync_sl_tp()` 才切換到軟體止損兜底、減少無謂重試與 log 噪音。已 `ast.parse` 驗證通過。
- **8/3（第二輪，加密龐克單片，字幕）★純重申、參數不變、未重啟、SHORT_BIAS 維持 False★**：BTC 上周反彈至 65,500 後跌破 64,000 密集成交區,昨日反抽再度受阻續跌,高低點持續下移仍處短線下降趨勢;下一關鍵支撐區看向 **59,567-61,097**,但**現價仍在現行 `BTC_SUPPORT_ZONE (62,000-63,500)` 範圍內、尚未觸及更未跌破**,屬前瞻條件式判讀。鏈上驚見規模比擬 22 年交易所破產的異動——某老牌冷錢包公司遭駭、逾 4,500 用戶錢包被掏空,引發散戶恐慌將幣轉回交易所(以幣安為主),龐克明確定性為「保管行為轉移、非賣壓」。川普旗下公司週末再度轉出 2,600+ 顆 BTC 進交易所,與 5/22 同規模轉移後隨即發生史上最大短期持有者投降式拋售(74,000→59,000)的先例雷同,需持續留意但尚未確認重演;清晰法案本周投票議程仍未排入,僅剩約 4 天。→ **不改任何常數**:本輪僅龐克單一 KOL,59,567-61,097 支撐區及「終極震倉」推演皆屬前瞻條件式表態、現價未觸及現行支撐帶,不構成共識轉變門檻;冷錢包事件不涉及具體交易訊號或幣種;未宣告趨勢反轉,SHORT_BIAS 重新武裝條件依舊未觸及。**git 同步未重啟、發 TG**（★下一輪最高優先監看:BTC 是否跌破現行 `BTC_SUPPORT_ZONE` 下緣、測試 59,567-61,097(若跌破且獲飛揚/歐陽佐證將是常數下修觸發點);61,000 附近假跌破快速收回情況;川普旗下公司 BTC 轉出是否重演 5/22 崩盤先例;清晰法案剩餘投票窗口進度;冷錢包駭客事件信任危機後續★）
- **8/3（飛揚＋歐陽，Whisper）★純重申、參數不變、未重啟、SHORT_BIAS 維持 False★**：週線／月線終於更新。飛揚檢討昨晚「1,850-1,803 支撐區不追空」提醒本輪完全兌現——ETH 反彈比預期猛,一路衝到接近 1,900、摸到 1,618 費波位才受壓回落,**全程未跌破支撐區**,是 `ETH_SUPPORT_ZONE (1,810-1,850)` 又一輪正面驗證。BTC 月線延續上月 TD9 反轉信號、上方 MA55 缺口約 66,000-67,000,判斷「先看反彈、反彈後高空」;週線收在 63,618 附近,估計最終支撐 62,500-63,000、上方 63,500-64,000 為新一輪高空觀察位——**完全落在現行 `BTC_SUPPORT_ZONE (62,000-63,500)` 的合理延伸範圍內**。歐陽 BTC 維持 62,000 接多／65,000 附近看空的中軌區間框架不變;首度提醒山寨幣近期呈「無效震盪」(深跌後未醞釀方向),建議避免操作但未點名具體幣種,不觸及 `COIN_BLACKLIST` 門檻。→ **不改任何常數**:兩人皆未宣告趨勢反轉,ETH/BTC 觀察完全落在現行框架內,純屬又一輪正面驗證。**git 同步未重啟、發 TG**(★下一輪最高優先監看:ETH 能否延續守住 1,803-1,850 或測試上緣壓力;BTC 62,500-64,000 觀察帶後續發展(飛揚高空劇本是否兌現);BTC 月線死亡三角/週線 63,618 支撐後續;歐陽山寨「無效震盪」判讀是否獲龐克佐證;8 月雙頂是否成形★)
- **8/2 bug fix（`get_top_coins`/`open_pos`）**：根查 VPS 48h log 發現 `TLM/USDT:USDT` 連續 4 次「開倉失敗／對帳失敗：binance does not have market symbol」——根因是選幣清單用**公開/主網行情** (`exchange_pub`) 算分,但下單走的是 **demo 交易所** (`exchange_priv`),demo 可交易幣種是主網子集,選到 demo 沒有的幣就會**每輪掃描持續失敗**直到清單刷新。修法：`get_top_coins()` 新增 `exchange_priv` 參數,選幣時與 demo `load_markets()` 交集過濾,不合格幣種直接排除。同時發現 `PUMP/USDT:USDT` 開倉時 `fetch_ticker(...)['last']` 回傳 `None` 導致 `float(None)` 例外(同一批 demo 資料不全的幣種)，`open_pos()` 加上 `None` 檢查、明確拋出可讀錯誤訊息改善日誌可讀性。已 `ast.parse` 驗證通過。
- **8/2（第二輪，飛揚單片 ETH，Whisper）★純重申、參數不變、未重啟、SHORT_BIAS 維持 False★**：昨晚設下的「ETH 先反彈再乘壓、別太樂觀,上方 1,800-1,900」劇本本輪精準兌現——暴跌至 **1,802**,反彈僅摸到 **1,885** 便再度受壓收在 1,808 附近。日線關鍵位鎖定在 **1,850**(連兩日皆冲高回落收在此線之上),若明日收線仍在 1,850 下方對空頭是一大利多;下方觀察目標 **1,803**(886 費波),明確表態希望跌破以開啟追空,若守住反彈則需等站上 1,850 才考慮低多。BTC 反彈受制 63,500,僅約 500-600 點空間,週末行情「就是這個樣子」。**★1,803-1,850 整個觀察區間與 8/1 剛下修的 `ETH_SUPPORT_ZONE (1,810-1,850)` 幾乎完全對齊(1,850=常數上緣、1,803 與常數下緣 1,810 僅差 7 點),是常數變更後第二輪的獨立驗證★**。→ **不改任何常數**:本輪僅飛揚單一 KOL,屬延續性戰術驗證非新結構表態;BTC 同樣純屬正常波動範圍;未宣告趨勢反轉,SHORT_BIAS 重新武裝條件依舊未觸及。**git 同步未重啟、發 TG**（★下一輪最高優先監看:明日(週一)週線+月線更新後方向確認;ETH 能否守住 1,803-1,850 或跌破 1,803 開啟追空;ETH 站上 1,850 後能否延續看多;BTC 64,000-64,500 分水嶺與 62,000 支撐後續發展;8 月雙頂是否成形★）
- **8/2（飛揚＋歐陽，Whisper）★純重申、參數不變、未重啟、SHORT_BIAS 維持 False★**：BTC 日線出現 TD9 十字星、卡在 618 費波位,若明日收陽可能構成「啟明星」結構延續月線反彈需求,但飛揚反覆強調上漲是為了修復、極可能伴隨更大下跌,關鍵分水嶺 **64,000-64,500**,站上才是真突破。**★歐陽公開策略 ETH 1,903(7/31)/1,870(8/1)兩筆空單皆已兌現拿到 1,847,並標注 1,810 為下一支撐——與 8/1 剛下修的 `ETH_SUPPORT_ZONE (1,810-1,850)` 完全吻合,是常數變更後第一輪即時的實單驗證★**;另有 1,868、1,905 附近多單皆兌現獲利,7 月近 30 天盈利 13,700U、近 90 天約 4 萬 U。歐陽 BTC:4 小時高低點持續下移,**62,000 關口本輪獲驗證企穩**,63,000 尚不構成短期高點,上方 **65,000-65,500** 插針區視為有效空點,續看 8 月初雙頂。→ **不改任何常數**:ETH 實單為對新常數的正面驗證非需再調整;BTC 兩人觀察皆完全落在現行 `BTC_SUPPORT_ZONE (62,000-63,500)`/cosmetic `BTC_RESISTANCE_ZONE (68,000-71,000)` 框架內、方向相容非新結構共識;飛揚啟明星訊號同時警告修復性上漲極可能伴隨更大下跌,延續謹慎立場未宣告反轉,SHORT_BIAS 重新武裝條件依舊未觸及。**git 同步未重啟、發 TG**（★下一輪最高優先監看:BTC 能否站上 64,000-64,500(啟明星確認關鍵);ETH 1,810-1,850 支撐延續驗證;BTC 62,000 企穩延續性;65,000-65,500 是否插針拒絕;8 月雙頂是否成形★）
- **8/1（第二輪，飛揚單片 ETH，Whisper）★純重申、參數不變、未重啟、SHORT_BIAS 維持 False★**：週末無明顯行情,等週一週線+月線更新後力量齊發。月線 TD9 反轉信號出現、衝高至 1,900 多高位後被 236/113 費波壓制打回收黑,明確警告**「別過分看漲,這波反彈可能又是陷阱」**——布林軌三軌向下開口、月線死亡三角未解除,本月劇本延續「先看漲、後看空」(與上月同款劇本)。日線續 **1,800-1,904** 寬幅區間震盪,**1,800-1,808 為非常強支撐、跌不破**,上方中軌壓制同步而來,明確表態「這行情沒法做」。**★1,804-1,808 支撐與同日稍早剛下修的 `ETH_SUPPORT_ZONE (1,810-1,850)` 高度吻合、1,900-1,950 反彈上緣與舊支撐轉壓力框架一致,屬本輪常數變更的正面驗證★**。→ **不改任何常數**:本輪僅飛揚單一 KOL,純戰術/月線觀望,未宣告新趨勢反轉,SHORT_BIAS 重新武裝條件依舊未觸及。**git 同步未重啟、發 TG**（★下一輪最高優先監看:週一週線+月線更新後方向確認;ETH 能否守住 1,810-1,850 或測試 1,900-1,950 上緣壓力;BTC 63,000 能否延續企穩或飛揚「高空劇本」兌現;BTC 月線死亡三角後續發展;清晰法案下週參議院投票結果★）
- **8/1（飛揚＋歐陽，Whisper）★★`ETH_SUPPORT_ZONE (1,900-1,930) → (1,810,1,850)`；已 `ast.parse` 驗證、重啟★★**：歐陽公開策略實單於 **1,903** 開空,一路拿到 **1,846** 分批指盈(獲「超額盈益」),標注下一觀察支撐第一 1,845、第二 **1,810**;飛揚獨立確認 ETH 最低探至 **1,847**、收線收回 1,850 之上,此後未再跌破,反彈受制 1,900(886 費波)非常弱勢——**兩者從不同角度確認了同一次已發生的跌破**,符合 pipeline 多 KOL 共識門檻。→ **`ETH_SUPPORT_ZONE` 下修為 (1,810, 1,850)**(下緣採歐陽第二觀察支撐、上緣採兩人皆確認的實際低點/收線位),舊帶 1,900-1,930 由支撐翻轉為中繼壓力區;`ETH_RESISTANCE_ZONE (1,948-2,030)` 維持不動。**SHORT_BIAS 維持 False**——兩位皆未宣告趨勢反轉,僅支撐帶隨價格下移需更新,歐陽甚至認為企穩後續多需求猶存。BTC 部分:飛揚警告 8 月月線收陽但死亡三角危險未解除(月線首次收在 MA55 下方、下軌下修至 51,000)、今日以高空思路為主看 63,500 附近承壓;歐陽則認為 4 小時大陰線破位後已圍繞 63,000 企穩、62,000-62,500 有短多需求——**兩者方向不完全一致且皆非結構性宣告**,`BTC_SUPPORT_ZONE`／`BTC_RESISTANCE_ZONE` 維持不動。**已 `ast.parse` 驗證通過、commit＋部署＋重啟＋TG**（★★下一輪最高優先監看:新設 `ETH_SUPPORT_ZONE (1,810-1,850)` 是否守住;ETH 反彈能否突破 1,900 舊支撐轉壓力區;BTC 63,000 能否延續企穩或飛揚「高空劇本」兌現;BTC 月線死亡三角後續發展;清晰法案下週參議院投票結果★★）
- **7/31（第三輪，龐克＋飛揚，字幕/Whisper）★純重申、參數不變、未重啟、SHORT_BIAS 維持 False★**：龐克首度記錄罕見鯨魚異象——原掛 61,300 的巨量現貨買單移至 **64,000** 完全成交(約 1,300 顆),幾分鐘內另一鯨魚追加約 400 多顆,合計約 **1,700 顆(逾一億美元)在交易所完成**(通常應走場外交易);即時鏈上數據顯示鯨魚增持已擴大至逾 **5,000 顆**,且來源是 1 顆/10 顆以下**散戶賣出**而非鯨魚錢包整理,推測可能與熊市尾聲大周期買入或下週**清晰法案**參議院投票消息面有關(市場預估通過機率不到三成)。BTC 64,000 密集成交區彈起最高摸 65,500,但高低點仍逐步降低、回踩尚未終結,若守住 64,000 且創高於 65,500 才算終結。飛揚確認 **ETH 跌破 1904 陷阱兌現**,日線跌至中軌以下(**自 7/2 以來近一個月首次**),測試 **1845-1870**(EMA21/55)關鍵支撐,明日日線收線至關重要,建議等支撐跌破再進場、沒破不追空。→ **不改任何常數**:龐克鯨魚異象/清晰法案屬背景資訊,不涉及短中期常數,BTC 64,000/65,500/68,000 與現行框架一致;**`ETH_SUPPORT_ZONE (1,900-1,930)` 本輪再度被實際跌破,但僅飛揚單一 KOL 表態、龐克未提及 ETH,不構成多 KOL 共識轉變門檻,飛揚也未宣告結構反轉,需等下一輪確認**;兩位皆未宣告 BTC 趨勢反轉,SHORT_BIAS 重新武裝條件依舊未觸及。**git 同步未重啟、發 TG**（★★下一輪最高優先監看:ETH 明日日線收線結果;ETH 1845-1870 支撐守住或跌破;BTC 64,000 是否守住、能否創高於 65,500 終結回踩;清晰法案下週參議院投票結果;歐陽「8 月下跌中繼/40,000」推演是否獲進一步佐證★★）
- **7/31（第二輪，飛揚單片，Whisper）★純重申、參數不變、未重啟、SHORT_BIAS 維持 False★**：昨晚 ETH **1902-1940** 壓制持續未突破 **1904**,續判定為「騙炮」假突破,下方 1800-1860 接多目標也未觸及,整體行情「沒法做」。今日 BTC 一根大陽線衝高至 **65,000** 隨即回落至 **64,500**(近千點跌幅),上方走出黃昏星結構、**886 費波位壓制強勢未破**;今日為 7 月最後一天+週末,預期波動不大,下週月線/週線更新後方向會更明朗。**65,000-66,000(2618 費波位)為非常關鍵的大壓制**,明確表態未突破前不追漲、也不建議追空,最感興趣區域是 **64,000-63,005(618 費波位)**,計畫觀察支撐情況後再接多。→ **不改任何常數**:本輪僅飛揚單一 KOL、純戰術觀望;ETH/BTC 觀察皆與現行 `ETH_SUPPORT_ZONE (1,900-1,930)`/`BTC_SUPPORT_ZONE (62,000-63,500)`/cosmetic `BTC_RESISTANCE_ZONE (68,000-71,000)` 框架一致,與同期歐陽(65,000 中軌)、龐克(64,700/65,500)觀察吻合,非新論述;未宣告趨勢反轉,SHORT_BIAS 重新武裝條件依舊未觸及。**git 同步未重啟、發 TG**（★下一輪最高優先監看:下週月線/週線更新後方向確認;BTC 能否突破 65,000-66,000 或回踩 63,005-64,000;ETH 能否突破 1904 或探至 1800-1860;歐陽「8 月下跌中繼/40,000」推演是否獲進一步佐證;本月線收盤結果★）
- **7/31（歐陽單片，Whisper）★純重申、參數不變、未重啟、SHORT_BIAS 維持 False★**：BTC 收陽線來到中軌 **65,000** 附近震盪,能否有效企穩支撐是本輪關鍵——企穩則延續向上並出現持續預告的「雙頂」,未能守住則再度回踩上行通道下軌(底部持續上移)。回顧 63,000 接多預期已兌現,現階段不建議直接追多,若插針至 65,500 形成看跌吞沒會嘗試短空。**★★首度提出 8 月展望:本輪(7 月)超跌反彈完成後,判斷此區間可能成為週線級別大級別下跌中繼結構,若後續破位價格恐深探至約 **40,000 美元**,屆時才是真正的底部;明確表態現階段仍是搏反彈階段、雙頂出現後才開始以做空為主——單一 KOL 條件式長線推演("如果...破位"),非已實現事件★★**。ETH 匯率隊持續強勢、未跌破 0.0285,暫不開空。→ **不改任何常數**:65,000 中軌/63,000 接多完全落在現行 `BTC_SUPPORT_ZONE (62,000-63,500)`/cosmetic `BTC_RESISTANCE_ZONE (68,000-71,000)` 框架內,與同期龐克/飛揚觀察一致;40,000 推演屬單一 KOL 條件式假設,不構成共識轉變門檻,待後續真正破位且獲其他 KOL 佐證才是調整時機。**git 同步未重啟、發 TG**（★★下一輪最高優先監看:BTC 能否於 65,000 中軌形成有效企穩;65,500 附近是否出現插針看跌吞沒;歐陽 8 月下跌中繼/40,000 推演是否獲其他 KOL 佐證或價格驗證;本周/本月線收盤結果(今日為 7 月最後一天)★★）
- **7/30（第三輪，龐克＋飛揚，字幕/Whisper）★純重申、參數不變、未重啟、SHORT_BIAS 維持 False★**：龐克 BTC 日線再收十字線,64,000 極致盤整持續數天,波動率信號連續亮燈 2-3 週,續看 65,500 甚至 **68,000** 短期持有者成本線。**★首度提出「全網已實現盈虧死亡交叉」框架:歷史僅四次,三次(19年/15年/幾乎22年)精準對應熊市最低部,目前已實現盈利快速下降,預估最快 **2-3 週內**正式死叉,距離本輪熊市大底價格與時間上都已非常接近★**;鏈上連續三天鯨魚小幅買入,ETF 今日轉流入 32M/500+ 顆、中斷連續四天淨流出。飛揚 ETH 續困於 **1902-1903** 多重頂部持續多日,月線 M5(約 1908)修復需求已達成,T13 反轉信號出現;今日失業金數據小利空未使行情下跌,顯示多頭情緒尚可,但**明確表態這不是現在追多的理由**,需等突破 **1908-1920** 才追漲,否則看 **1800-1860** 接多機會。→ **不改任何常數**:龐克死亡交叉框架屬長線背景資訊,強化既有熊市尾聲敘事,不涉及短中期 wired 常數,64,000/65,500/68,000 與現行框架一致;飛揚 1902-1903 與 `ETH_SUPPORT_ZONE (1,900-1,930)` 上緣對齊、1800-1860 現行常數已涵蓋,屬個人前瞻猜測非結構性表態;兩者皆未宣告趨勢反轉,SHORT_BIAS 重新武裝條件依舊未觸及。**git 同步未重啟、發 TG**（★下一輪最高優先監看:龐克死亡交叉是否於 2-3 週內正式發生;BTC 能否守住 64,000/POC 之上並挑戰 65,500;ETH 能否突破 1908-1920 或回落至 1800-1860;本周/本月線收盤結果(明日 7/31 月線更新)★）
- **7/30（第二輪，歐陽單片，上輪 403 重試成功）★純重申、參數不變、未重啟、SHORT_BIAS 維持 False★**：BTC 觸及上行通道下軌後嘗試低吸反彈至中軌下翻回落,現價圍繞下軌區間獲支撐。歐陽 **7/28 於 63,100、7/29 於 63,900** 兩筆多單皆已在 **64,500** 附近分批指盈兌現,與同期飛揚「64,000 未破/64,500 中軌」是對同一行情的一致觀察。**★路線圖:7 月底進入「弱勢震盪框架」而非回到下跌趨勢,操作以低吸高拋短頻快為主;需有效站穩中軌 65,000 附近後才會迎來 4 小時雙頂/二次高點,屆時才是布局 8 月做空的時機,此高點是給大家準備做空、不是用來追漲★**。→ **不改任何常數**:63,100/63,900/64,500 完全落在現行 `BTC_SUPPORT_ZONE (62,000-63,500)` 內,與飛揚觀察一致;65,000 站穩→雙頂→8 月做空的路線圖屬前瞻推演尚未實現,非已實現共識轉變,待雙頂真正出現且獲其他 KOL 佐證才是調整時機;歐陽明確定性弱勢震盪非趨勢反轉,SHORT_BIAS 重新武裝條件依舊未觸及。**git 同步未重啟、發 TG**（★★下一輪最高優先監看:BTC 能否有效站穩中軌 65,000 附近;站穩後是否出現 4 小時雙頂/二次高點(歐陽 8 月做空布局觸發點);飛揚 65,000-65,500 突破追漲劇本是否兌現;ETH 1900-1902/1800-1805 區間後續走勢;本周/本月線收盤結果★★）
- **7/30（飛揚單片，Whisper；歐陽 Whisper 403 失敗待重試）★純重申、參數不變、未重啟、SHORT_BIAS 維持 False★**：昨晚 FOMC 利率決議公布維持不變,「雷聲大雨點小」,波動遠不如預期,BTC 消息前後皆迅速回到 **64,000** 附近。日線收十字星,**64,000 支撐持續未破**,RSI/KDJ 出現初步反攻跡象,今日思路轉為「突破追漲為主」,上看 **65,000-65,500** 分水嶺,明確重申「前提是突破,未突破前沒有進場意義」。歐陽本輪 Whisper 因 YouTube 403 失敗,標題暗示「雙頂預期」,內容待下輪重試確認。→ **不改任何常數**:本輪僅飛揚單一 KOL,不構成共識轉變門檻;64,000/65,000-65,500 完全落在現行 `BTC_SUPPORT_ZONE (62,000-63,500)`/cosmetic `BTC_RESISTANCE_ZONE (68,000-71,000)` 框架內,與歷史觀察位一致;未宣告趨勢反轉,SHORT_BIAS 重新武裝條件依舊未觸及。**git 同步未重啟、發 TG**（★下一輪最高優先監看:歐陽本輪失敗影片下輪重試結果(雙頂預期需確認);BTC 能否突破 65,000-65,500 分水嶺;ETH 1900-1902/1800-1805 區間後續走勢;9 月升息預期發酵情況;本周/本月線收盤結果★）
- **7/29（第四輪，飛揚單片 ETH，Whisper 重試成功）★純重申、參數不變、未重啟、SHORT_BIAS 維持 False★**：整日無明顯行情,ETH 困於 **1800-2000** 區間中上軌,**1900-1902**(4 小時級別)為壓在頭上的巨石,「未突破之前沒有任何做多的追漲餘地」;**1800-1805 為日線雙底頸線關鍵分水嶺**,多次測試皆未跌破;2000 整數關口持續壓制未曾觸及。月線關鍵:EMA7 死叉缺口仍在 2100,死亡三角第三次死叉尚未出現,需徹底站上 EMA7 才能解除風險;一小時級別可能正形成 M 頭/雙頂,頸線位落在 1807-1809 但尚未跌至該位置。飛揚坦言「這種行情不管做哪邊都在虧錢」,建議忍住不躁進、等月線收線後方向會更明朗,今晚兩點聯準會利率決議(沃什首秀)公布前先觀望。→ **不改任何常數**:本輪僅飛揚單一 KOL、純戰術觀望;**1900-1902 與現行 `ETH_SUPPORT_ZONE (1,900-1,930)` 下緣完全對齊**、2000 整數關口與 `ETH_RESISTANCE_ZONE (1,948-2,030)` 框架精神一致,皆無需調整;未宣告趨勢反轉,SHORT_BIAS 重新武裝條件依舊未觸及。**git 同步未重啟、發 TG**（★下一輪最高優先監看:聯準會利率決議(沃什首秀)結果與市場反應;ETH 能否突破 1900-1902 或跌破 1800-1805;月線收線結果;BTC POC 63,700 收復狀況;本周/本月線收盤結果★）
- **7/29（第三輪，龐克單片，字幕）★純重申、參數不變、未重啟、SHORT_BIAS 維持 False★**：昨日回踩創更低低點,終結短線上升趨勢,但仍在既有區間震盪內;**POC 最密集成交區恰在 63,700**(與前次平行低點吻合),判斷可能只是順應美股連動下跌,預估最多半天到一天內須收回 POC 之上並在回踩時獲得支撐,若如此則有機會回到原上升趨勢,短時間內挑戰 **65,500**、甚至 **68,000-71,000** 短期持有者成本線。**★POC 63,700 與同日稍早(第一輪飛揚、第二輪歐陽)描述的 BTC 63,000-64,000 止穩反彈是第三方獨立驗證,完全落在現行 `BTC_SUPPORT_ZONE (62,000-63,500)` 內★**。背景:韓國股市單月近腰斬、六月初從加密轉戰股市的韓國散戶重傷,重演「市場最多人瘋狂時結束、最冷清時醞釀」循環;長期流動性指標續降至 **0.38**、幣安流動性四月中至今流出近 60 億美元;鏈上鯨魚買入、中型持有人賣出規模不大;龐克明確表態「目前沒有太好交易機會,只是震盪,沒必要多操作」。→ **不改任何常數**:65,500/68,000-71,000 與 cosmetic `BTC_RESISTANCE_ZONE` 一致非新論述;未宣告結構反轉,SHORT_BIAS 重新武裝條件依舊未觸及;韓國崩盤/流動性 0.38 屬背景資訊。**git 同步未重啟、發 TG**（★下一輪最高優先監看:BTC 能否於半天至一天內收復 POC 63,700 之上延續反彈至 65,500/68,000-71,000;聯準會利率決議(沃什首秀)結果與市場反應(凌晨 2:30 公布);65,000 附近 POC 支撐能否守住;本周/本月線收盤結果★）
- **7/29（第二輪，歐陽單片，Whisper 重試成功）★純重申、參數不變、未重啟、SHORT_BIAS 維持 False★**：上輪 Whisper 因 YouTube 403 失敗的影片本輪重試成功。歐陽 **63,000 附近進場的多單現已浮盈近 100%(漲幅近千點)**、反彈至 **64,000** 附近,續抱多單分批止盈,第一站 **65,000**(中軌),站穩則看向 **67,000** 雙頂機會,判定「這波行情不會快速跌破,至少還有一輪上漲」。ETH/BTC 匯率 **0.03** 仍高於 **0.0285** 對沖切換門檻,續做多 ETH 空 BTC;ETH 測試 1,980 附近高點,回落幅度不如 BTC 深。**★本輪與同日稍早飛揚描述的「ETH 1,855 止穩反彈至 1,901」是同一波市場企穩的兩個獨立觀察角度、構成多 KOL 交叉驗證,但兩者價位皆完全落在現行 `BTC_SUPPORT_ZONE (62,000-63,500)`/`ETH_SUPPORT_ZONE (1,900-1,930)` 之上,屬正面驗證非需調整★**。→ **不改任何常數**:歐陽 65,000/67,000 與 cosmetic `BTC_RESISTANCE_ZONE (68,000-71,000)` 及其歷史 66,000-67,000 框架一致,非新論述;未宣告趨勢反轉,SHORT_BIAS 重新武裝條件依舊未觸及。**git 同步未重啟、發 TG**（★下一輪最高優先監看:BTC 能否站穩中軌延續反彈至 65,000/67,000 雙頂;ETH/BTC 匯率 0.0285 是否跌破;聯準會利率決議(沃什首秀)結果與市場反應;本周/本月線收盤結果★）
- **7/29（飛揚單片，字幕；歐陽 Whisper 403 失敗待重試）★純重申、參數不變、未重啟、SHORT_BIAS 維持 False★**：昨晚設下的 **ETH 1,902 回彩接多預測本輪精準命中**——最低僅探至 **1,855** 便強力反彈至 1,920 附近、現於 1,901 震盪,**上輪標記已跌破的 `ETH_SUPPORT_ZONE (1,900-1,930)` 本輪已收復、未進一步破位**。BTC 反彈仍受制於 **64,000-65,000** 壓制帶,日線收長下影線但未突破,續偏低多思路,今日續看回彩 **62,000-62,500**;因聯準會利率決議(沃什接替鮑威爾首秀,凌晨 2 點公布)預期白天波動不大,不建議重倉/高槓桿操作。→ **不改任何常數**:本輪僅飛揚單一 KOL(歐陽影片 Whisper 因 YouTube 403 轉錄失敗,未標記,待下輪重試);ETH 收復支撐帶是現行 `ETH_SUPPORT_ZONE` 的正面驗證,非需調整的新資訊;BTC 完全落在現行 `BTC_SUPPORT_ZONE (62,000-63,500)`/`BTC_RESISTANCE_ZONE (68,000-71,000)` 框架內。**git 同步未重啟、發 TG**（★下一輪最高優先監看:聯準會利率決議(沃什首秀)結果與市場反應;ETH 能否延續站穩 1,900 之上;BTC 62,000-62,500 回彩/64,000-65,000 壓制突破;歐陽本輪失敗影片下輪重試結果;本周/本月線收盤結果★）
- **7/28 晚間第二輪（飛揚單片 ETH，字幕）★純重申、參數不變、未重啟、SHORT_BIAS 維持 False★**：昨日設下的 ETH 2,000 整數關口衝擊本輪確認失敗(僅拉到 1,982、未觸及 2,000),隨即暴跌,**現測試 1,800-1,850 支撐——剛在 7/27 設定的 `ETH_SUPPORT_ZONE (1,900-1,930)` 本輪已被實際跌破**;若進一步跌破 1,800,飛揚坦言「很嚇人,甚至該想月線反彈是否到此為止」,但目前明確表態「沒有太大追空必要,除非等 1,850 跌破」。BTC 同步在早盤支撐區 62,500-63,000 反彈疲弱、乘壓 1414 位再打回,現價 62,600 附近**仍完全落在剛下修的 `BTC_SUPPORT_ZONE (62,000-63,500)` 範圍內、未破**。→ **不改任何常數**:本輪僅飛揚單一 KOL,不構成共識轉變門檻;ETH 支撐帶雖已被跌破,但僅單一 KOL 表態且他本人未確認跌破 1,800,暫不下修。**git 同步未重啟、發 TG**(★★下一輪最高優先監看:ETH 1,800-1,850 是否守住或跌破(`ETH_SUPPORT_ZONE` 才剛設定一天即被測試);`BTC_SUPPORT_ZONE` 是否延續疲弱反彈或跌破;歐陽/龐克是否佐證或不同判讀;本周/本月線收盤結果★★)
- **7/28 深夜（加密龐克回歸，字幕）★純重申、參數不變、未重啟、SHORT_BIAS 維持 False★**：明確定性短線急跌破 64,000、創更低低點為「短線市場框架轉變」（一個多月上升趨勢短線終結），但**週線級別仍在 60,000-65,000 既有震盪區間內、非結構性反轉**。技術細節：POC（最密集成交區）恰在 **63,700**、最差情況下看 **61,300**（大鯨魚密集掛單/前次插針最低點）——**兩者皆與稍早同日已根據飛揚＋歐陽下修的 `BTC_SUPPORT_ZONE (62,000-63,500)` 高度吻合**，等於第三位 KOL 對同一次跌破事件的獨立驗證。首度提出「三個大週期買點全指南」（死寂抄底區現貨／週K帶量上穿建立槓桿倉／牛市起漲點）作為長線佈局框架分享。→ **不改任何常數**：本輪僅龐克單一 KOL，其技術描述屬獨立驗證非新增依據；SHORT_BIAS 重新武裝條件（68,000-71,000 遭拒/終極震倉確認）本輪依舊未觸及。**git 同步未重啟、發 TG**（★下一輪最高優先監看：`BTC_SUPPORT_ZONE (62,000-63,500)` 是否守住或考驗 61,300 最差支撐；64,000 平行低點 1-2 日內能否收復；68,000-71,000 觸及反應；ETH 1,860 止跌情況；本周/本月線收盤結果★）
- **7/28 晚間（飛揚單片，字幕）★★`BTC_SUPPORT_ZONE (64,000-65,500) → (62,000-63,500)`；`near_support` 門檻降至 ≤64,135；已 `ast.parse` 驗證、重啟★★**：飛揚獨立確認日線大陰線跌破 64,000、VIP 空單過程中最低觸及 63,000、續看 **62,000-63,000** 下一支撐帶——與同日早盤歐陽所述「63,000-63,500 接多試探帶」互相印證,**兩位 KOL 從不同時間點確認了同一次已發生的跌破事件**,符合 pipeline 多 KOL 共識門檻。ETH 同步暴跌至 1,860 附近(飛揚原先看好的 1,940/2618 突破未能兌現)。→ **`BTC_SUPPORT_ZONE` 下修為 (62,000-63,500)**(下緣採飛揚目標區間、上緣採歐陽接多帶);**`near_support` 連動降至 ≤64,135**;**SHORT_BIAS 維持 False**——兩位 KOL 皆未宣告趨勢反轉或終結,僅支撐帶隨價格下移需更新,與 SHORT_BIAS 重新武裝條件是兩件獨立的事;`BTC_RESISTANCE_ZONE (68,000-71,000)` 維持不動。**已 `ast.parse` 驗證通過、commit＋部署＋重啟＋TG**(★★下一輪最高優先監看:新支撐 62,000-63,500 是否守住(若再跌破需重新評估歐陽 63,000 觸發線/SHORT_BIAS);ETH 是否於 1,860 止跌;66,000-67,000/68,000-71,000 上方壓力帶;本周/本月線收盤結果;龐克是否回歸表態★★)
- **7/28（歐陽單片，字幕）★純重申、參數不變、未重啟、SHORT_BIAS 維持 False，現價已跌破現行 `BTC_SUPPORT_ZONE` 下緣列最高優先監看★**：BTC 昨日兩次上插針後今早大陰線跌破中軌,現價 **63,600**,逼近但尚未確認跌破歐陽 7/25 設定的 **63,000 無條件轉空觸發線**。歐陽回顧驗證了自己 7/27 的判斷(65,500 壓力區指引、66,800-67,000 高點預判皆準確兌現),並將 **63,000-63,500 定性為下一個接多試探區**(而非立即轉空)——與既有觸發線邏輯一致,只是價格尚未真正到達確認。ETH 續強勢領漲,ETH/BTC 匯率 0.0285 為對沖策略切換觀察位,目前仍处強勢階段。→ **不改任何常數**:單一 KOL 不構成共識轉變門檻;現價雖已跌破現行 `BTC_SUPPORT_ZONE (64,000-65,500)` 下緣,但歐陽本人未宣告確認跌破或轉空。**git 同步未重啟、發 TG**(★★下一輪最高優先監看:63,000-63,500 是否觸及並守住/跌破(歐陽無條件轉空觸發線,現價已非常接近);`BTC_SUPPORT_ZONE` 是否需在確認跌破後下修;ETH/BTC 匯率 0.0285 是否跌破;66,000-67,000/66,500 上方壓力區觸及反應;本周/本月線收盤結果★★)
- **7/27 晚間第二輪（飛揚單片 ETH，字幕）★純重申、參數不變、未重啟、SHORT_BIAS 維持 False★**：今早設下的 BTC 條件式劇本（65,500-65,800 突破追漲／64,800 回彩接多）本輪雙雙兌現，純戰術驗證。**ETH 周線四連陽逼近 2,000 整數關口**，「本週一定會在這個位置見分曉」；日線偏多但明確表態「沒有必要去追」，前提是突破 2,000 或在此承壓回彩，理想接多位置 1,900-1,940、最後防線 1,900。現價測試 1,950-1,980，**恰好落在昨日剛設定的 `ETH_RESISTANCE_ZONE (1,948-2,030)` 下緣**，與常數設定精神完全一致。→ **不改任何常數**：單一 KOL 不構成共識轉變門檻；ETH 現價落在新設常數預期觀察窗內，無需調整。**git 同步未重啟、發 TG**（★下一輪最高優先監看：ETH 2,000 整數關口本週突破或承壓回彩；`ETH_SUPPORT_ZONE (1,900-1,930)` 是否守住；BTC 本周/本月線收盤結果；66,000-67,000 週線壓力或雙頂第二高點；63,000-63,400 歐陽 BTC 觸發線★）
- **7/27 晚間（加密龐克回歸，字幕）★純重申、參數不變、未重啟、SHORT_BIAS 維持 False★**：64,000 回踩守住、彈起測試 65,500，維持「正常回踩範圍」判讀，續看 **68,000-71,000**（短期持有者成本線／200 日均線，本輪熊市終極震倉最可能時間點，類比 22 年熊市模式）。鏈上：一週內兩間二三線交易所倒閉、180 天長期流動性指標降至 0.4，視為熊市死寂區信號；CLARITY Act 新版草案已發布，下週一參議院全體表決。**★ETH 獨立提及 1,950 下跌關口，與同日稍早歐陽命名的 1,948 幾乎完全吻合，進一步印證剛上修的 `ETH_RESISTANCE_ZONE (1,948-2,030)` 精準對齊★**——三位 KOL（飛揚／歐陽／龐克）本輪首次交叉印證同一價位帶。→ **不改任何常數**：68,000-71,000 尚未觸及更未遭拒；ETH 新設常數已獲獨立驗證非需再調整。**git 同步未重啟、發 TG**（★下一輪最高優先監看：BTC 65,500 能否站穩收回進而挑戰 68,000-71,000；ETH 新壓力 1,948-2,030 觸及反應；CLARITY Act 下週一參議院表決結果；63,000-63,400 歐陽 BTC 觸發線；本月線收盤結果★）
- **7/27（飛揚＋歐陽，字幕）★★ETH_RESISTANCE_ZONE (1,840-1,860) → (1,948-2,030)；ETH_SUPPORT_ZONE (1,800-1,820) → (1,900-1,930)；已 `ast.parse` 驗證、重啟★★**：飛揚 7/26 晚間設下「站穩 1,900 才追漲」的條件本輪已被市場兌現（現貨衝高至 1,966）；歐陽獨立命名 **1,948／2,000／2,030** 為本輪反彈三次高點強壓區、建議區間做空不追多——**兩位 KOL 從不同角度各自確認 ETH 已明確站上舊高空帶**，符合 pipeline 多 KOL 共識轉變門檻。沿用「壓力翻轉為支撐」邏輯：新 `ETH_SUPPORT_ZONE (1,900-1,930)`（飛揚多輪重申的關鍵支撐）、新 `ETH_RESISTANCE_ZONE (1,948-2,030)`（歐陽命名的三次高點強壓區）。BTC：周線四連陽站上 3618，本周面臨 66,000 週線壓力，但**飛揚、歐陽皆仍定性為「反彈階段」、未宣告反轉**（飛揚要求月線確認；歐陽延續雙頂/逼空框架，67,000 為做空點、65,000 為多單籌碼密集區）→ **`BTC_SUPPORT_ZONE`／`BTC_RESISTANCE_ZONE`／`SHORT_BIAS` 維持不動**。**已 `ast.parse` 驗證通過、commit＋部署＋重啟＋TG**（★★下一輪最高優先監看：BTC 本周月線更新結果；66,000-67,000 週線壓力突破或雙頂第二高點；ETH 新支撐 1,900-1,930 是否守住；ETH 新壓力 1,948-2,030 觸及反應；63,000-63,400 歐陽 BTC 觸發線★★）
- **7/26 晚間（飛揚單片，龐克/歐陽無新片）★純重申、參數不變、未重啟、SHORT_BIAS 維持 False★**：BTC 延續早盤 64,500-65,000 區間震盪無結果、佛系觀望。**ETH 逼近 1,900 關鍵位**，MACD/EMA 等多項指標轉多，飛揚判斷「突破機率偏大」，但明確表態「不用做自己方向的判斷，只需要讓時間給答案」，非結構性反轉宣告。→ **不改任何常數**：單一 KOL 更新不構成 pipeline 要求的多 KOL 共識轉變門檻；**現行 `ETH_RESISTANCE_ZONE (1,840-1,860)` 事實上已被價格逼近甚至可能超越其當初註記的 1,900 cosmetic 參考關口**，但因僅單一 KOL 且尚未收盤確認，暫不調整，留待下輪確認後處理。**git 同步未重啟、發 TG**（★下一輪最高優先監看：ETH 是否站穩 1,900（若站穩，`ETH_RESISTANCE_ZONE`/`ETH_SUPPORT_ZONE` 是否需整體上修）；BTC 64,500-65,000 方向性突破；63,000-63,400 歐陽觸發線持續監看★）
- **7/26（飛揚×2＋歐陽，龐克連續兩輪無新片）★純重申、參數不變、未重啟、SHORT_BIAS 維持 False，63,000 觸發線本輪未觸發★**：上輪監看的歐陽 63,000 無條件轉空觸發線本輪並未被觸及——BTC 週末低點僅探至約 63,800-63,964 即反彈站穩，歐陽本人重申「63,500 附近才考慮佈局多頭」。飛揚兩支純戰術（ETH 1,800-1,900 壓制未破偏空；BTC 63,964 起明星低點守住反彈至 64,400，**明確表態「65,000（886 位）未站穩前反彈都是扯淡」**），與現行 `BTC_SUPPORT_ZONE (64,000-65,500)` 完全相容。歐陽標題「雙頂結構即將兌現」但內容仍是條件式：需 66,000-67,000 出現二次高點才算頂部確認，與既有「逼空→魚尾頂部」框架一致，非新論述；新增關注 ETH/BTC 匯率 0.0285（跌破則 ETH 優先做空）。→ **不改任何常數**：63,000 觸發線暫緩、龐克連續兩輪缺席判讀依據無進展。**git 同步未重啟、發 TG**（★下一輪最高優先監看（延續）：63,000-63,400 是否再觸及並守住/跌破；66,000-67,000 二次高點雙頂結構；65,000/886 位能否站穩收復；龐克是否回歸表態；ETH/BTC 匯率 0.0285 是否跌破★）
- **7/25（飛揚＋歐陽，龐克本輪無新片）★純重申、參數不變、未重啟、SHORT_BIAS 維持 False，但列下一輪最高優先監看★**：飛揚純戰術（VIP 65,750 空單 63,810 平倉獲利 2,000 點；ZEC 公開策略 496 平倉；公開頻道 17 單 14 勝 3 敗 87% 勝率連 7 勝），BTC 跌破前高支撐轉壓力來到 64,000 分水嶺，**明確表態「64,000 沒跌破之前不可以追空」**、跌破則後續有更多跌幅空間，無結構表態。**歐陽本輪關鍵轉變：66,000-65,500 前高支撐互換已被他自己判定失效、確認向下破位，將關鍵支撐下修至 63,000-63,400，並明確表態若跌破即代表「整波上行通道徹底結束」、屆時「毫不猶豫」順勢轉空**；首度將本輪下跌部分歸因於**美股大盤同步崩盤共振**（外部宏觀衝擊）。→ **不改任何常數**：龐克本輪缺席、SHORT_BIAS 判讀依據無新進展；歐陽 63,000 觸發線目前仍是單一 KOL 前瞻條件式表態、尚未被觸及更未跌破，且僅小幅低於現行 `BTC_SUPPORT_ZONE (64,000-65,500)` 下緣，屬同精神內技術性下修、非結構性證據，暫不提前反應。**git 同步未重啟、發 TG**（★★下一輪最高優先監看：63,000-63,400 是否觸及並守住/跌破（歐陽本人設定的無條件轉空觸發線）；龐克後續是否對 68,000-71,000/64,000 表態；美股聯動是否重複出現、強化為系統性風險敘事；飛揚是否轉向確認 64,000 破位★★）
- **7/24（龐克＋飛揚×2＋歐陽，字幕）★純重申、參數不變、未重啟、SHORT_BIAS 維持 False★**：龐克回踩仍在正常回踩範圍（無大陰線破壞結構）、**首次給出量化機率：68,000-71,000 帶量上穿約 30%／再度回落約 70%**；鏈上重磅——**長期持有者籌碼佔比達 84%、創有史以來最高**，配合 180 天抄底區指標降至 0.41，估最佳死寂抄底區約 4 週後（9 月初）浮現。飛揚兩支純戰術（BTC 跌向 64,000-64,223、ETH 跌破 1,806 續看 1,804），無結構表態。歐陽標題「反彈頂部已確認」但內容仍是條件式框架：跌破 64,004 才算頂、守住則還有最後一輪逼空衝 67,000 才是真正做空點——與 7/21-7/23 框架一致，非新宣告。→ **不改任何常數**：68,000-71,000 尚未觸及、更未遭拒，SHORT_BIAS 重新武裝條件不成立；籌碼佔比創新高反而強化多頭敘事。**git 同步未重啟、發 TG**（★監看：68,000-71,000 觸及反應；歐陽 64,004 支撐是否失守；長期持有者籌碼佔比/抄底區指標後續；CLARITY Act 立法進度★）
- **7/23（龐克＋飛揚×2＋歐陽，字幕）★純重申、參數不變、未重啟、SHORT_BIAS 維持 False★**：龐克確認 65,500 突破後回踩仍在正常範圍、資金費率初現負值但**空頭堆積量遠低於上輪連續嘎空前夕**；續看 68,000-71,000。飛揚兩支純戰術（66,200 支撐轉壓力續看 65,000-65,500；ETH 1,905 月線 MA5 持續受壓）。歐陽確認站穩 66,000-66,500、預期先拉 67,000 完成「逼空」換手，但**重申 58,000 僅是定頭位非抄底位、後續仍有更低點**——延續既有偏空終局框架。→ **不改任何常數**：本輪未觸及 68,000-71,000、更未在該帶遭拒，SHORT_BIAS 重新武裝條件不成立。**git 同步未重啟、發 TG**（★監看：68,000-71,000 觸及反應（尚未到達）；空頭堆積量變化；67,000 籌碼密集區★）
- **7/22 晚間（加密龐克＋飛揚 ETH，字幕）★★SHORT_BIAS: True → False；cosmetic BTC_RESISTANCE_ZONE 上修 68,000-71,000；重啟★★**：**龐克連續四輪（7/16／7/20／7/21／7/22）追蹤後，本輪首次明確稱「昨日突破了 65,500 這一線平行高點」（非「摸到」）**——SHORT_BIAS 鬆動門檻（龐克創 65,500 新高）正式滿足。現貨查證吻合：連續 4h 收盤站穩、高探 66,924、回踩 65,505 精準守住支撐（非插針假突破）。→ `SHORT_BIAS = True → False`（解除山寨 LONG 完全停止 + 主流幣 LONG 額外 +1 訊號要求）；cosmetic **`BTC_RESISTANCE_ZONE (66,000,67,000) → (68,000,71,000)`**（龐克：68,000＝STH 平均成本線、71,000＝200 日均線，定性為「本輪熊市最後最關鍵位置」，能否帶量上穿繫於 CLARITY Act 8/7 前立法進度）；`BTC_SUPPORT_ZONE (64,000,65,500)` 不變（65,500 由壓力翻轉為確認支撐）、`near_support` 門檻 ≤66,155 不變。飛揚本輪僅 ETH 戰術更新（1,900-1,902 支撐再驗證），未提供結構性表態。**已 `ast.parse` 驗證通過、commit＋部署＋重啟＋TG**（★★下一輪最高優先監看：SHORT_BIAS 解禁後實盤 LONG 訊號觸發狀況；68,000-71,000 觸及反應（龐克：若未帶量上破恐為終極震倉，之後看 5-6 萬大底）；CLARITY Act 8/7 前立法進度；龐克提及「68,000 以上重新跌回＝本輪熊市最後一次波段空單機會（CP 值已不如前）」，屆時需評估是否恢復部分 SHORT 邏輯★★）
- **7/22（飛揚 BTC＋歐陽，Whisper/字幕）★純重申、參數不變、未重啟★**：飛揚**連續第三輪以幾乎相同措辭明確否定結構翻多**——「現在只是一個反彈，大方向仍在下跌，並沒有做出很明顯的反轉的迹象」；今日續低多支撐 65,500-66,000（涵蓋現行帶內）。歐陽確認 **BTC 有效突破站穩 66,000-66,500**（與 cosmetic `BTC_RESISTANCE_ZONE (66,000-67,000)` 精準對齊）、定調「逼空行情」預期先回彩 65,000-65,500 續多再插針清算空頭，但**收尾框架仍是「魚尾行情→階段性頂部」**——延續其一貫偏空終局定調，非新的看多結構論述。→ **不改任何常數**：兩支影片皆未觸發 SHORT_BIAS 鬆動門檻（飛揚結構翻多本人再度否定；龐克本輪無新片）。**git 同步未重啟、發 TG**（★監看：67,000 籌碼密集區觸及反應；68,000-69,138 STH 成本線（龐克待表態）；飛揚是否終有一輪改口承認反轉★）
- **首次納入飛揚/歐陽 Whisper 逐字稿**（6 支，6/19~6/21）→ 兩位 KOL 一致強烈看空、反彈無量、高點下移
- **ETH 1,700 突破失效**：`ETH_RESISTANCE_ZONE (1800,1820) → (1700,1740)`（飛揚 1,704-1,706 / 歐陽 1,715-1,748 高空帶）；`ETH_SUPPORT_ZONE (1700,1720) → (1600,1640)`（1,700 由支撐轉回壓力，新支撐＝1,618 景象位/1,608 追空線）→ **解除 1,700 附近禁空**，讓 ETH 在現價 ~1,715 可逢高做空
- **BTC 支撐上移**：`BTC_SUPPORT_ZONE (60000,61100) → (61000,62000)`（62,000 兩度測試守住＝反彈起點/追空分界；61K≈200週均線深支撐）；`near_support` 追空禁令門檻連動上移至 BTC ≤ 62,620
- **BTC 壓力維持** `(64000,65500)`（歐陽 64-65K 開空、飛揚 64K 高空；上方硬壓 66-67K 通道頂/布林上軌）；`BTC_HARD_STOP 69,150`、`ETH_NO_LONG_ABOVE 1,700`、`ETH_LONG_ZONE (1370,1390)`、`SHORT_BIAS`、`COIN_BLACKLIST` 維持。`main.py` KEY zones 同步
- 山寨：SOL 73-74 開空（目標 69）、AVI 順勢追空、ADA/LTC 弱勢無機會（已在黑名單）
- **晚間追加**（飛揚 6/21 ETH，Whisper）：重申 BTC 64.5-65.5K 高空 / ETH 1,704-1,706 承壓、破 1,700 小倉追空；**與上述參數一致，無新變動**（僅 append insight，未改常數、未重啟容器）
- **7/21 晚間（加密龐克＋飛揚 ETH，字幕/Whisper）★純重申、參數不變、未重啟——龐克首度針對本輪突破表態，門檻仍未觸發★**：**龐克明確定性：仍稱「摸到 65,500 平行高點」，刻意不用「創新高」／「突破」措辭**，延續既有平行高點框架；下一關鍵觀察區前移至 **68,000-69,138（STH 平均成本線／200 日均線）**——若觸及但未帶量上破（同 83,000 前例）＝很可能是本輪熊市「終極震倉」，屆時看 5-6 萬盤大底；**唯一可能免二次回踩、甚至直接爆量上穿的催化劑：CLARITY Act 若能趕在 8/7 美國國會休會前完成三讀＋簽字**（川普已與民主黨在道德規範達成共識，增加一些希望但仍高難度）。鏈上：63,000-50,000 鯨魚密集買單牆、買賣比例失衡創三輪下跌以來最誇張。飛揚 ETH：冲高 1,950 回落，明確重申**「還是反彈、不是反轉」**（月線 M5 未站上、死亡三角未除）、等回彩 1,900-1,902 或站穩 1,965 才進場。→ **SHORT_BIAS 嚴格未觸發**：龐克刻意不用「創新高」措辭、決勝點前移至更高的 68,000-69,138（該區已由既有 `BTC_HARD_STOP 69,150` 涵蓋，無需新增變更）；飛揚結構門檻同樣未觸發。ETH 飛揚今日新接多位（1,900-1,902）僅單方觀點，未經其他 KOL 本輪跟進，暫記候選觀察點、不動現行帶。**git 同步未重啟、發 TG**（★★監看：68,000-69,138＝下一 SHORT_BIAS 觀察窗（帶量上破或龐克明確改口「創新高」＝觸發／二次回踩＝終極震倉）；CLARITY Act 8/7 前立法進度★★）
- **7/21（飛揚 BTC ＋ 歐陽，Whisper）★★BTC 明確突破站穩 65,500、接多/壓力帶上修＋重啟；SHORT_BIAS 維持（鬆動門檻未達成）★★**：**盤面實測（Binance 4h）BTC 連續四根收盤 65,539→66,167→66,318→66,497，真正站穩 65,500（非插針）、現貨 66,000-66,500**；ETH 同步站上 1,900（現貨 1,941）。飛揚：日線終於收在 65,000 上（近一個月首次）、續持回彩接多框架、理想低多位 **64,500-65,000**、未提出結構翻多論述。歐陽：**66,000-66,500 分批止盈、67,000＝反彈最終目標（僅剩約 2,000 點），觸及後即是「多轉空的重要轉折點」**、明確重申 **58,000 僅是定頭位非抄底位、後續仍有更低點＝維持大級別偏空框架**。→ **上修 wired**：**`BTC_SUPPORT_ZONE (62,000,64,000)→(64,000,65,500)`**（near_support ≤64,640→**≤66,155**）、cosmetic **`BTC_RESISTANCE_ZONE (65,500,67,000)→(66,000,67,000)`**（歐陽明確重申 66,000-66,500/67,000）。**★★SHORT_BIAS 維持 True——嚴格門檻（飛揚結構翻多／龐克創 65,500 新高）技術面已逼近但未達成★★**：飛揚僅視為回彩戰術延續、歐陽反而明確重申看空框架、**龐克本人尚未對此次突破發表任何評論**（上次 7/20 仍稱 65,500 為平行高點）——技術突破不等於 KOL 論述轉向，不因價格數據片面反轉。ETH 本輪無新 KOL 具體點位，區間暫不動。**已重啟、發 TG**（★★下一輪最高優先監看：龐克是否針對此次突破表態確認「創 65,500 新高」＝SHORT_BIAS 鬆動直接觸發點★★；66,000-67,000 觸頂反應）
- **7/20 晚間二（飛揚 ETH，Whisper）★純重申、參數不變、未重啟★**：**早盤 1,806-1,804 接多戰術兌現、反抽 1,896**，但 1,900（周線 113 費波位）未做出像樣突破、隨即回落再測支撐。周線持續被 113 卡住未破（RSI/KDG/MACD 仍偏多、量能巨大）；日線"**只是正常回彩、暴跌沒門**"（布林中下軌向上托舉）；**續盯 1,800-1,806 埋伏低多（EMA55 所在）、維持 1,800 以上多頭不算彻底失敗**。→ **不改任何常數**：本片完全落在現行框架內正面驗證——1,800-1,806 接多＝`ETH_SUPPORT/LONG_ZONE (1,800-1,820)` 精準對齊、1,900 未破＝`ETH_RESISTANCE (1,840-1,860)` 帶上方自然延伸（SHORT 已放行 ≥1,840）；SHORT_BIAS 維持。**git 同步未重啟、發 TG**（★監看：ETH 1,800-1,806 接多反應持續驗證、1,900 突破確認★）
- **7/20 晚間（加密龐克，字幕）★純重申、參數不變、未重啟★**：**65,500 續定性為平行高點**（「差幾十美金沒創出更高高點」，與 7/16 定性一致，SHORT_BIAS 門檻續未觸發）；波動率信號再亮燈（無方向性，僅預告大波動將至）；**主題：英文區知名大空頭（22 年 16,000 見底喊多）全面轉多，平所有空單並在 64,000 開始買現貨，策略 54,000-64,000 內每天投 5% 資金、20 天買滿**（理由：200 週均線、估最低下修至 54-64K、看空者過多、十月見底共識太擁擠、結構性轉變）；龐克回應**與自己論述雷同、維持既有 5-6 萬買入立場不變**（未因此調整價位）。**鏈上：現貨買單牆從 61,000 上調至 63,000（1,300 顆，為上週撞牆彈起量的 6 倍）**——完全落在現行 `BTC_SUPPORT (62,000-64,000)` 帶內，強化該帶下緣防守合理性。→ **不改任何常數**：門檻未觸發、鯨魚買單牆印證現有帶設定精準、68,000-69,138 轉折框架與 `BTC_HARD_STOP`/cosmetic `BTC_RESISTANCE` 一致；大空頭轉多屬宏觀資訊非三位 KOL 共識訊號。**git 同步未重啟、發 TG**（★監看：65,500 能否創新高＝龐克門檻；63,000 買單牆撞牆反應；68,000-69,138 轉折區★）
- **7/20（飛揚 BTC ＋ 歐陽，Whisper）★純重申、參數不變、未重啟★**：飛揚開場證實**昨夜 ETH 1,850-1,860 低多戰術驗證成功**（7/19 記錄的候選觀察點兌現，但屬同一人短線戰術非其他 KOL 跟進共識，暫不上修）。**BTC：周線連三週站穩 3618、65,000＝周線關鍵分水嶺連兩週未破**（月線缺口在 68,000、死亡三角未解）、大方向偏回彩接多但不建議追漲、理想低多位 **63,000-63,500**（周線 EMA/日線 618）、深破看 62,000-62,500。**歐陽：64,000 多單已半倉止盈、剩倉看 66,000（可能插針至 67,000 才轉跌，像 6/22 一樣）、67,000 依舊是絕佳做空區間不變**（第四批影片維持）、魚尾行情降倉。→ **不改任何常數**：63,000-63,500/62,500-62,700 低多位＝`BTC_SUPPORT (62-64K)` 帶內、65,000/66,000/67,000 全對齊 cosmetic `BTC_RESISTANCE`；SHORT_BIAS 維持（65,000/65,500 連三週未破、龐克門檻未觸發）。ETH 現行帶（1,800-1,820/1,840-1,860）仍涵蓋昨夜波動範圍，暫不上修。**git 同步未重啟、發 TG**（★監看：65,000-65,500 站穩 vs 回彩 63,000-63,500；66,000-67,000 插針佈空區★）
- **7/19 晚間（飛揚 ETH，Whisper）★純重申、參數不變、未重啟★**：BTC 追漲被套警示（886 未破回彩至費波 618）。**ETH 罕見全天未回彩、短線支撐 1,860-1,865 一直未破**（現貨查證 ~1,871-1,880，站在該支撐之上）；飛揚兩手策略：**回彩 1,840-1,860 即可接多**（現行 `ETH_RESISTANCE` 帶）或**站穩 1,900+ 才追漲**，本人明確表態不確定（"讓時間給答案"）、今晚僅打算輕倉試單。→ **不改任何常數**：僅單一支不確定表態、非結構共識轉向；現行 wiring 已相容（ETH 現貨 >1,838/>1,860，LONG 被擋、SHORT 於 ≥1,840 放行，與飛揚「現在追漲有點瘋」態度一致）。**候選觀察點記錄**：回彩 1,840-1,860 接多若獲其他 KOL 跟進、或站穩 1,900+ 真突破，屆時才是 `ETH_LONG_ZONE`/`ETH_NO_LONG_ABOVE` 上修觸發點。**git 同步未重啟、發 TG**
- **7/19（飛揚 BTC ＋ 歐陽，Whisper）★純重申、參數不變、未重啟★**：**BTC 週末強勢反彈上探 64,878（886 承壓）**、日線雙底頸線約 **66,000 尚未彻底站穩突破**（飛揚：64,000 守住是良好訊號、6,600-6,400 分水嶺含金量高、日內先等回彩撐住 64,000-64,500 才低多、千萬別追漲、站上 66,000 前一切塵埃未定）。**歐陽：63,500 多單延續 7/17-18 劇本完全兌現，現浮盈 700+U（+180%）、已指引半倉防 65,500 回測、剩半倉等站穩冲高；65,500＝籌碼密集強壓已測兩次、67,000 仍為本輪最後強壓/最佳做空區不變**（觸及後插針看跌吞沒→擬 67,000-67,500 連續掛空）。→ **不改任何常數**：64,000＝`BTC_SUPPORT` 上緣持續有效（near_support 邊緣觸發）、64,878/66,000/65,500/67,000 全對齊 cosmetic `BTC_RESISTANCE (65,500-67,000)`；SHORT_BIAS 維持（65,500 仍測試中未創新高、龐克門檻未觸發、飛揚戰術謹慎非結構翻多）。兩支均未提 ETH，ETH 四區維持 7/18 設定不動。**git 同步未重啟、發 TG**（★監看：66,000 頸線突破/65,500 站穩＝下一觀察窗；67,000＝歐陽佈空觸發位★）
- **7/18 晚間（飛揚 ETH，Whisper）★純重申、參數不變、未重啟——四區下修全數獲印證★**：週末極限窄幅（一天 1,835-1,850、15 點「巧婦難為無米之炊」）。**月線邏輯：M5 已摸到走平、EMA 缺口上方仍有一部分漲幅——突破站上＝「行情彻底解放、彻底奔騰」／月線 EMA 第三死叉＝反彈告一段落**（＝飛揚版結構翻多條件、與龐克「爆量上穿 STH」呼應、列 SHORT_BIAS 前瞻觸發器）；日內「**昨天採到 1,800 沒跌破＝千叮嚀萬囑咐不破 1,800 不能追空**」（禁空帶驗證）、1,820-1,840 小回彩不破接小多（放行 ≤1,838 ✓）、1,840 直接做多冒進（RES 帶 ✓）、**跌破 1,800 吃 3-40 點到 1,780＝886 分水嶺（深破 <1,782 放行追空精準對齊）**。參數不變、SHORT_BIAS 維持。**git 同步未重啟、發 TG**
- **7/18（3 支：飛揚 7/17 晚 ETH＋7/18 BTC、歐陽 7/18，Whisper）★★ETH 四區下修（支撐/壓力翻轉）、重啟；BTC 全 reaffirm★★**：**BTC 昨插針 62,500（帶內獲撐、歐陽生死線未收破）＋底部巨量反彈 63,800-64,000＝歐陽判定探底企穩**（62,000 多單持有中、週末窄幅→重上 65,000→67,000 才值得開空）；飛揚 BTC 空單 62,800 出局**賺 1,400 點**、週末縮量不追漲、唯一機會＝跌破 63,500 順勢追空。**ETH 1,905 摸月線 M5 見頂→跌破 1,850 生死線（龐克預告兌現）→支撐/壓力翻轉**：飛揚「**1,800＝要了命的分水嶺**（跌破→追空→1,750）、1,840-1,860 承壓可高空、站上 1,860 才反彈開啟、此位不空不多管住手」→ **`ETH_RESISTANCE (1900,2000)→(1840,1860)`、`ETH_SUPPORT`/`ETH_LONG_ZONE` (1850,1900)→(1800,1820)**（放行 ≤1,838 不追漲、1,782-1,840 禁空＝防插針掃損、深破 <1,782 才追空）、`ETH_NO_LONG_ABOVE 1900→1860`。BTC 全 reaffirm（62,500 插針＝62-64K 帶實戰驗證、near_support 觸發中與「山寨等 BTC 漲完才動」一致）。SHORT_BIAS 維持（飛揚空單獲利＋大方向看空、門檻無觸發）。**已重啟、發 TG**（★監看：ETH 1,800 分水嶺「堅決的答案」vs 站上 1,860；BTC 跌破 63,500 追空 vs 65,000→67,000 佈空；龐克 7/18 待發★）
- **7/17（4 支，7/18 晚間補跑：龐克字幕＋飛揚×2 歐陽 Whisper）★全 reaffirm、參數不變、未重啟★**：機器休眠約一天後補跑。**BTC：65,500 打出第二個平行高點受阻、收破 64,000 回落**（跌幅有點變大＝關鍵位反轉衰竭）。飛揚 ETH 1,900-1,902 低多**止損 -40~50 點**（月線死亡三角第四度警告）、轉戰 BTC 空單 64,100-64,300 浮盈、下看 62,700、64,500 倒車接人再空；歐陽**接多帶下移 63,500-63,000（等一小時收針介入）、止損上移 62,500——跌破＝整波反彈趨勢彻底結束重回弱勢**、67,000 仍為最後強壓/最佳空區、魚尾縮倉、山寨等 BTC 到高位才補漲；龐克**61,000 鯨魚買單牆 1,300 顆＝下方主防線**（彈起→一兩周再試 68,000 STH 順掃 65,500 流動性＝最完整走法）、**180 天流動性 0.47 近垂直下降＝死寂抄底區將至、68,000 回踩＝終極震倉、5-6 萬買入區「請做好買入準備」**；**ETH 懸在 1,850 生死線——龐克：維持之上一切正常、收破確認→下看 1,592 甚至 1,500、屆時空單盈虧比好**（未收線確認）。→ **不改任何常數**：63,000-63,500/62,500/62,700/61,000 全貼合 `BTC_SUPPORT (62-64K)`、near_support ≤64,640 觸發中（禁山寨追空＝與「山寨主力不敢動」一致）、65,500/67,000＝cosmetic 對齊；ETH `1,850-1,900` 帶以下緣為判定線（飛揚 1,840-1,860 接在 ±1% 放行覆蓋內；**候選下修：日線收破 1,850→龐克 1,592/1,500 為新錨**、破位 <1,831 現行 wiring 自動放行追空）；SHORT_BIAS 維持（65,500 兩度未創新高、飛揚持空、終極震倉框架）。**git 同步未重啟、發 TG**（★監看：ETH 1,850 收破＝下一個帶調整觸發器；BTC 62,500＝歐陽趨勢生死線（跌破→BTC 帶候選下修）；61,000 鯨魚買單牆；補查 7/18 新片★）
- **7/16 晚間（加密龐克，字幕）★純重申、參數不變、未重啟——等待中的定性影片★**：**龐克親口定性 65,500：「最高點差了幾十美金、還沒創出更高的高點…嚴格來說沒有創高、沒有假突破」＝平行高點**→ SHORT_BIAS 龐克門檻**明確未觸發（正面確認）**。平行高點伏筆：64,000 撐住（＝正常回踩範圍）→可能再上一筆**掃 65,500 空頭止損流動性**（觀察資金費率轉負/空頭湧入＝續攻燃料）；大級別：**68,000 STH 成本線＝熊市最後一次壓制、不會爆量上穿、6 萬 8 千多回落→5-6 萬盤大底**（下跌衰竭 -40%→-30%→第三次更小、類比 22 年熊市）；上邊緣不開多、想交易只有「6 萬 8 假突破跌回短空」＝最後波段空機會、否則等現貨買入（「四年一次」）。**ETH：近 2,000 差一些些受阻回踩＝不算非常強勢；1,850 生死線——撐住挑戰 2,000／跌回 1,850 以下下看 1,592**＝昨日剛上移的 `ETH_SUPPORT/LONG (1,850-1,900)` 帶下緣獲龐克驗證（新警訊：跌回 1,850→1,592＝帶失效屆時下修）。BTC reaffirm：64,000＝`BTC_SUPPORT (62-64K)` 上緣精準、65,500＝cosmetic `BTC_RESISTANCE` 下緣、68,000/69,138 外框架＝`BTC_HARD_STOP 69,150` 一致。**前瞻：若走「掃 65,500→上攻 68,000」，SHORT_BIAS 觸發點順延至「爆量上穿 STH 成本線」（龐克：熊轉牛必要條件）**。鯨魚鏈上連兩天大動靜（熊市以來罕見）＝關鍵時點前兆。**僅 append insight、git 同步未重啟、發 TG**
- **7/16（飛揚 BTC ＋ 歐陽，Whisper）★ETH wired ×2 變更（接多帶上移 1,850-1,900）、重啟；BTC 全 reaffirm★**：BTC 昨夜上摸 65,500 後**一晚跌回區間**（彈花一線疑慮、未站穩＝龐克門檻仍未確認）、現測 64,400-64,700。飛揚「**64,000＝分水嶺**（MA5/M30/MA7 缺口全指向）、日線沒被打回 64,000 以下都安全、回彩皆多、等打到 64,000 接低多、**此位做空意義不大**、跌破 64,000-64,400 閘門才有小追空」；歐陽「**67,000（6/15 高點）＝很可能本輪階段性頂部**、萬刀反彈已走 80%＝**魚尾行情吃多肉少、縮小倉位**、支撐放 63,500（64,000 下沿接多贏虧比差易插針）、65,500 部分止盈→殘倉等 67,000 最後指引＝**非常好的空點**；仍看多低多為主、剩最後一兩波拉涨」。**★ETH 接多帶候選條件觸發★**：歐陽 ETH 回落 **1,860-1,870 有接多預期**（昨 1,870 多單×2 兌現）＝跟進飛揚 7/15 深夜的 1,900-1,902，三方接多位全在 1,850 上（龐克 1,850 站上續攻）→ **`ETH_SUPPORT_ZONE`/`ETH_LONG_ZONE` (1800,1850)→(1850,1900)**（放行 ≤1,919 不追漲、1,831-1,919 禁空；`ETH_RESISTANCE 1,900-2,000`/`ETH_NO_LONG_ABOVE 1,900` 不動）。BTC 全 reaffirm：63,500-64,000 接多＝`BTC_SUPPORT (62-64K)` 帶內、near_support ≤64,640 觸發中（禁山寨追空＝與雙方「此位別空」一致）、65,500/67,000＝cosmetic `BTC_RESISTANCE` 精準對齊。**SHORT_BIAS 維持**（65,500 未站穩、歐陽 67,000 見頂佈空＝偏空框架未變）。**已重啟、發 TG**（★監看：64,000 分水嶺攻防／65,500 二次突破站穩＝龐克門檻／67,000 階段頂佈空；ETH 1,850-1,900 接多帶、2,000 關口；山寨補漲窗口＝BTC 觸 67,000 後 1-2 天★）
- **7/15 深夜（飛揚 ETH，Whisper）★純重申、參數不變、未重啟★**：BTC 窄幅震盪「以震代跌」蓄力後**上摸 65,500 打白板**（沒給回彩機會）；**ETH 突破暴涨直衝 1,946**、飛揚不追漲（「拼死拼活追上去吃魚尾不划算」）、**回彩接多位上移 1,900-1,902**（4h 目標 1.13、1h 缺口 1,900）；**月線第三度警告：月線沒站穩、死亡三角還在——「哪怕扎到 2,000 多一點都沒事、收線收下來就穩」＝2,000+ 仍是月線高空劇本**。**★盤面實測（Binance 4h）：BTC 高 65,589／收 65,391／現 65,023＝插針突破 65,500 後跌回、未站穩＝歐陽「突破恐插針」劇本兌現★** → **不改任何常數**：SHORT_BIAS 龐克門檻（突破並**站穩** 65,500 創新高）觸線但未確認、判定權在龐克下支影片；飛揚結構門檻第三度親口否定；ETH 現價 1,914 正在幾小時前才上移的高空帶 1,900-2,000 內、飛揚接多位 1,900-1,902 屬單方未成共識（候選：歐陽/龐克跟進 → `ETH_SUPPORT/LONG_ZONE → 1,850-1,900`）。**僅 append insight、git 同步 VPS 未重啟、發 TG**（★★監看一觸即發：龐克下支影片對 65,589 插針的定性＝SHORT_BIAS 鬆動直接判定點；BTC 4h 收穩 65,500 上／再破 65,589＝站穩證據；ETH 觸 2,000 反應、跌破 1,900 回看 1,830-1,850 接多帶★★）
- **7/14→7/15（8 支補跑：龐克×2 字幕＋飛揚×3 歐陽×3 Whisper）★★參數變更 ×5 wired＋1 cosmetic、重啟★★**：**CPI 大幅降溫（前值 4.2/預期 3.8/實印 3.5）＋沃什國會首秀 → BTC 自 7/14 早盤 61,008 一口氣拉 3,000+ 觸 65,000-65,100、ETH 千軍萬馬突破 1,800 觸 1,882 現 1,870**。三方共識：接多帶一致上移 **63,500-64,500**（歐陽「64,000 化壓為撐、接多 63,500-63,800、65,000 別摸頂空、突破 65,500 恐插針 67,000＝階段頂佈空位」；飛揚「回踩 64,000-64,500 低多、月線還沒涨完但別急追」；龐克「守 64,000 之上短線強勢、回踩 65,500 後挑戰 69,138、鯨魚買單上移 61,500」）；ETH 三方齊多（飛揚「日線收上 1,800＝多頭穩、回踩 1,830-1,850 接多看 1,900」；歐陽「1,780 多單+2,000U 兌現、1,870-1,900 短期高點摸頂空困難」；龐克「MVRV 低估信號第二次亮燈、站上 1,850 首目標 2,000」）＝與舊區間完全脫鉤 → **`BTC_SUPPORT_ZONE (60000,62000)→(62000,64000)`**（`near_support` 追空禁令連動 62,620→**≤64,640**）、**`ETH_RESISTANCE_ZONE (1780,1800)→(1900,2000)`**（價已在舊高空帶上方、剛突破放行追空＝軋空風險，飛揚 ZEC 空單被軋平是活教材）、**`ETH_SUPPORT_ZONE (1700,1720)→(1800,1850)`**（跌破 1,800＝假突破才再議空）、**`ETH_LONG_ZONE (1370,1390)→(1800,1850)`＝ETH 低多解禁**（回踩 ≤1,868 放行、>1,868 不追漲、仍需主流 +1 信號）、**`ETH_NO_LONG_ABOVE 1700→1900`**、cosmetic **`BTC_RESISTANCE (63500,65500)→(65500,67000)`**。**SHORT_BIAS 維持 True**：兩門檻皆未觸發——龐克門檻（突破站穩 65,500 創新高）**差 400-500 點**、龐克親口「差一些些還沒突破」；飛揚門檻（月線結構翻多）7/14+7/15 **連兩天親口否定**（「月線死亡三角還要發力、不能認為彻底反轉轉牛」）。**已重啟 coin-monitor、發 TG**（★★下一觸發點：BTC 突破並站穩 65,500 創新高＝龐克門檻＝SHORT_BIAS 鬆動、下支影片確認即動；監看 65,500→插針 67,000 佈空→69,138／跌破 64,000→63,500 接多→61,500 鯨魚買單；ETH 回踩 1,830-1,850 接多、1,900-2,000 首次到帶先看回踩、跌破 1,800＝假突破劇本★★）。另註：7/14-7/15 RSS 間歇回空造成兩天假「沒新片」、7/15 深夜以 feedparser 直查+seen 差集發現補跑
- **7/13 晚間（飛揚 ETH，Whisper）★純重申、參數不變★**：**ETH 本周開盤回落扎 1,761、下看 1,700-1,720 接多**。飛揚早盤喊的 BTC 下跌兌現（回彩 62,500-63,000 但反彈沒站上 63,200 再跌、飛揚跑 BTC 改做 ZEC 518-520 空現 502）；ETH 逃頂 1,835 後一路下行，**日線 1,800 沒突破、雙底變多重頂→1,800 含金量/壓制越來越強、可靈活短空、突破才修復月線**；今晚盯 **1,700-1,720**（理想接多位/深支撐）、金三角腰折、跌破 1,760 續下 1,720；「先漲後跌」邏輯。→ **不改任何常數、不重啟**：ETH 完全印證 `ETH_SUPPORT (1700,1720)`＝飛揚盯的 1,700-1,720 接多/深支撐、`ETH_RESISTANCE (1780,1800)`＝1,800 雙底變多重頂含金量增強可短空。**SHORT_BIAS 維持（更偏空、正面確認）**：飛揚 BTC 跑去做 ZEC 空＋ETH 下看 1,700＋「你還認為熊市見底嗎」＝明確非結構翻多、門檻未觸發。**僅 append insight、不改 wired、不重啟、發 TG**（★監看：ETH 回彩 1,700-1,720 接多今晚重點/站上 1,800 才追/跌破 1,760 續下 1,720；BTC 回彩 62,500-63,000 撐死 62,500、摸 65,500 突破創新高＝SHORT_BIAS 鬆動關鍵）
- **7/13 傍晚（加密龐克 BTC，字幕）★純資訊補充、參數不變★**：龐克「熊末劇本」聚焦**礦工關機價**——**紅線（全礦工正常運作最低限度）＝45,500**（18 年後 7 年沒碰過）、**藍線（低於此 20% 礦工投降）＝61,000 多，BTC 已進入礦工開始關機範圍**（上輪 16,000 見底時關機價 18,000）；論點「礦工投降＝熊市最好買入機會、不該此時過分看空 BTC」、維持大底 5-6萬/9 月、不上移 5 萬掛單。短線 BTC 收斂三角（64,000 上邊緣、末端 1-2 天選方向）、期待摸 65,500 上邊緣、走壞才下看 61,097。→ **不改任何常數、不重啟**：框架完全一致（65,500 上邊緣＝cosmetic `BTC_RESISTANCE` 對齊、61,097＝`BTC_SUPPORT (60-62K)` 帶內、大底 5-6萬9月）；**★礦工關機價藍線 61,000≈61,097≈現有 `BTC_SUPPORT` 帶內，反而從基本面強化此帶結構合理性（20% 礦工投降價）★**、紅線 45,500 為 cosmetic 資訊。**SHORT_BIAS 維持**（龐克熊底未到、BTC 未創 65,500 新高、門檻未觸發；「不過分看空」是長線抄底非 altcoin 追多）。**僅 append insight、不改 wired、不重啟、發 TG**（★監看：BTC 收斂三角末端 1-2 天選方向、摸 65,500 上邊緣突破創新高＝SHORT_BIAS 鬆動關鍵/走壞跌 61,097→回測 5-6萬礦工投降大底區）
- **7/13（飛揚 BTC ＋ 歐陽 BTC，Whisper）★參數不變、reaffirm（突破 65,500＝SHORT_BIAS 鬆動關鍵觀察）★**：**周線量能巨大（很久沒見）、雙方一致「本周看突破 64,500-65,000→67-68K、回彩皆多」**。飛揚「7/10 ETH 多單今早 1,835 止盈**賺 199.5%**；周線十字守 3618 但 65,000 分水嶺沒守住、要站上才突飛猛進；**本周劇本回彩皆多、先打掉 64,500-65,000→涨 67-68K**、回彩接多 62,500-63,000 撐死 62,500、突破就追、激進可短空非首選」＋歐陽「暴涨预警、7 月反彈趨勢做多擴大倉位、今早插針後看跌吞沒現 63,000、**64,500 不再視為壓力、回彩 63,000 接多、目標 65,500/67,000、止損 62,500**、反彈沒結束、ADA 0.16 接多」。→ **不改任何常數、不重啟**：接多區上移 62,500-63,000，但**歐陽止損 62,500／飛揚撐死 62,500＝仍貼合現有 `BTC_SUPPORT (60-62K)` 上緣**、near_support ≤62,620 回踩觸發、61,097 在帶內＝偏多震盪上移非結構轉移；`BTC_RESISTANCE` cosmetic 65,500 對齊歐陽第一指引/飛揚分水嶺。**SHORT_BIAS 維持**（門檻：飛揚結構翻多昨已親口否定、龐克創 65,500 新高未觸發今無片、BTC 未創新高；山寨仍被擋）。**發 TG、未重啟**（★★本周關鍵監看：BTC 回彩 62,500-63,000 接多→打掉 64,500-65,000→65,500→67,000→68,000→69,138；**突破並站穩 65,500 創新高＝同時觸發龐克門檻＝SHORT_BIAS 鬆動關鍵觀察點、本周量能巨大須緊盯**；跌破 62,500 止損→回測 6 萬★★）
- **7/12 晚間（飛揚 ETH，Whisper）★純重申、參數不變★**：ETH 冲擊 **1,800「生死局」**（日線雙底頸線位＝整數關口＋支撐轉阻力，含金量強）、冲高回彻站稳 1,800 再上、回彩沒到 1,780-1,760、現 1,800 上震盪多頭不錯；**站上 1,800 才追（奔 1,900/2,000）、1,820 站不住走 M 頭、回踩 1,706-1,708 接多**。**★月線關鍵：飛揚明確「ETH 只能確定是反彈、斷言反轉為時過早、月線死亡三角未發力、姑且當反彈」＝親口否定結構翻多★** → 我設的 SHORT_BIAS 鬆動門檻「飛揚結構翻多」被本人直接否定、**SHORT_BIAS 續留正面確認**。ETH 完全印證 `ETH_RESISTANCE (1780,1800)`＝雙底頸線、`ETH_SUPPORT (1700,1720)`＝低多 1,706-1,708。**僅 append insight、不改 wired、不重啟、發 TG**（★監看：ETH 站上 1,800 頸線才追/回踩 1,706-1,708 接多；SHORT_BIAS 待飛揚月線確認反轉或龐克創 65,500 新高）
- **7/12（飛揚 BTC ＋ 歐陽 BTC，Whisper）★參數不變、reaffirm★**：**BTC 高位窄幅震盪 63,600-64,000、雙方一致別追高、回彩接多 63,000-63,500**。飛揚「64,000 壓制沒破、TD 翻紅反彈但日線沒站上 64K、KDG 高檔鈍化、4h MACD 空頭放量死差＝追涨冒險；**站上 64,500 才追**、64,000-64,500 壓制頂在頭上、別追涨管住雙手空倉等待；ETH 冲 1,820 走 **M 頭、頸線 1,820 跌破**後暴跌回 1,706-1,708＝再證壓制」＋歐陽「周線看漲吞沒、**下週多頭延續**、缝低做多、上壓 65,500 插針位；掛單回踩 63,600/63,300 插針、**63,000-63,500 接多、指引 65,000（6-22 4h 壓力）/67,000（6-15 高點）、止損 62,009**；ETH 昨開多賺 20 點快速止盈」。→ **不改任何常數、不重啟**：BTC 回彩 63,000-63,500 屬**戰術接多區、非結構轉移**，**歐陽止損 62,009＝現有 `BTC_SUPPORT (60-62K)` 上緣仍有效深防線**、near_support ≤62,620 涵蓋、60-62K＋61,097 不動；`BTC_RESISTANCE` cosmetic 上緣 65,500 對齊插針位；`ETH_RESISTANCE (1780,1800)`＋1,820 追涨門檻＋`ETH_SUPPORT (1700,1720)`＝回踩 1,706-1,708 全再驗證。**SHORT_BIAS 維持**（區間戰術多、歐陽止損明確、飛揚看長做低多、龐克熊底 8-9 月、門檻未觸發、山寨仍弱等 BTC 突破才補漲）。**發 TG、未重啟**（★監看：BTC 回彩 63,000-63,500 接多→站上 64,500→65,000→65,500→67,000→69,138 STH 成本線/跌破 62,009→回測 6 萬；ETH 回落 1,706-1,708 接多/需站穩 1,820 才追涨；山寨待 BTC 突破 64,500 補漲）
- **7/11 晚間（飛揚 ETH 補充，字幕）★純重申、參數不變★**：同日飛揚 ETH 晚間片，**完全重申早盤判斷、無新變化**：週末量能極小、BTC 整天 63,900-64,300 極限震盪「沒法做」；ETH 現彈上 1,800 以上但**日線未穩住 1,800、上引線**、1,800-1,820 仍壓制區間（EMA55 1,810）、**重申 1,820 突破才有勝算、不建議追涨**、回踩 1,706-1,708 接多、做空太危險、佛系等周一。→ `ETH_RESISTANCE (1780,1800)` 對齊「1800-1820 壓制帶」下沿、`ETH_SUPPORT (1700,1720)`＝回踩 1,706-1,708、1,820 追涨門檻昨已記監看。**僅 append insight、不改 wired、不重啟、發 TG**
- **7/11（飛揚 BTC ＋ 歐陽 BTC，Whisper/字幕）★參數不變、reaffirm（ETH 1,800 觸發點解除）★**：**昨日 ETH 1,800 觸發點今日揭曉＝失敗**——飛揚「怕什麼來什麼、ETH 昨日線收在 **1,800 以下**、1,800 又回阻力、**1,820 突破才能追涨**、回落接低多 1,706-1,708」＋歐陽「ETH 昨 1,770 延續多單到 1,806、**現 1,800 附近全平**（＝平倉當壓力）」；雙方確認 1,800 仍是壓力、無人追涨、ETH 收 1,800 下回落 → **昨日設的 ETH_RESISTANCE 上移觸發點明確不成立、正式解除、驗證保守判斷正確**。BTC 高位盤整：飛揚「64,608 受阻、日線 64K 沒破、今回彩接多 63,000-63,500、4h TD9 出但跌幅不強、別追涨也別追空、佛系等回踩」＋歐陽「高位醞釀突破前高、64,000 暫穩還會上、62,000-63,000 接多、64,500 做空、7/9 的 61-62K 回測接多已命中、山寨 ADA/XRP 弱勢等 BTC 破 64K 站穩才補漲」。→ **不改任何常數、不重啟**：`ETH_RESISTANCE (1780,1800)` 高空帶維持（觸發點失敗）、`ETH_SUPPORT (1700,1720)`＝回落接多 1,706-1,708 對齊；`BTC_SUPPORT (60-62K)`＋near_support ≤62,620 涵蓋雙方 62-63K 接多；`BTC_RESISTANCE` cosmetic 不動。**SHORT_BIAS 維持**（兩位偏多但區間戰術多、歐陽 64,500 就空、飛揚看空下輪、龐克熊底 8-9 月、門檻未觸發）。**發 TG、未重啟**（★監看：BTC 回彩 62-63.5K 接多→突破 64K 前高→65,500→69,138 STH 成本線/破 61,097→回測 6 萬；ETH 回落 1,706-1,708 接多/破 1,700 追空/**需 1,820 突破才追涨（觸發點改站穩 1,820 更高門檻）**；山寨 ADA/XRP 待 BTC 破 64K 補漲）
- **7/10 傍晚（加密龐克 BTC ＋ 飛揚 ETH，Whisper/字幕）★參數不變、reaffirm（ETH 1,800 站穩＝待確認觸發點）★**：**龐克給出「熊市大底 8-9 月」時間表**——BTC 摸 64,000、60-65K 區間、周末周線邊緣關鍵；**65,500 上緣壓力（創此更高高點才終結 83K 下跌）→突破挑 69,138＝短期持有者 STH 平均成本線**（每輪熊市最關鍵線、周線爆量上穿＝右側絕佳上車）；⭐大底時間框架：投降階段自今年 2 月 6 萬急殺起算、歷史 18/22 年皆 6 個月見底→**本輪大底最可能 8-9 月、區間 5-6萬**（預期先摸 69K→回落→22 年式和緩回踩→9 月盤底、61K 大買牆無黑天鵝難破 4萬/3萬）。**飛揚 7/10 ETH 由 7/9 空轉偏多**——1,708 回踩接多、**ETH 日線對 1,800 發起衝刺（1,800 扼殺太多次反彈）、若明早守 1,800 以上＝月線級別反彈確認**、回踩 1,706-1,708 低多/站上 1,800 就追/不建議做空。→ **不改任何常數、不重啟**：BTC 框架一致（65,500/69,138 為 cosmetic、61,097 support、大底 8-9 月＝時間資訊無 wired 對應）；**ETH 飛揚轉多但不構成明確共識轉向**（條件未確認「要明早站穩 1,800」＋歐陽 7/10 把 1,800 當平倉壓力＋龐克未講 ETH）→ 維持 `ETH_RESISTANCE (1780,1800)` 高空帶、`ETH_SUPPORT (1700,1720)`（飛揚 1,706-1,708 回踩對齊）。**★高優先待確認觸發點：ETH 明早站穩 1,800 且第 2 位 KOL 跟進轉多→下批上移 ETH_RESISTANCE、解除 1780-1800 放空、避免逆勢空進月線反彈★**。**SHORT_BIAS 維持**（龐克熊底未到 8-9 月才見、飛揚看空下輪、鬆動門檻未觸發）。**發 TG、未重啟**（★監看：BTC 守 61,097/突破 65,500→69,138 STH 成本線大概率先回落、大底 8-9 月 5-6萬；ETH 明早站穩 1,800＝潛在 ETH_RES 上移觸發點、否則回踩 1,706-1,708 仍在 support；7 月底歐陽轉空窗）
- **7/9-7/10（加密龐克 BTC ＋ 飛揚 BTC/ETH ＋ 歐陽 BTC×2，Whisper/字幕）★參數不變、reaffirm★**：BTC **60-65K 區間震盪、現價逼近 63-64K**。**歐陽轉堅定「反攻號角、明牌多頭」**——昨 61,500-62,000 接多**獲利 1,800 點**、寬幅震盪上 64,000/下 61,500、**階段頂上調 67,000-68,000**（65,000 才觀察布空、過了 7 月才走下跌中繼跌破、中長線全出）＋飛揚 7/10 BTC 轉偏多（月線發力、日線金三角、MA5 撐、上看 64-64.5K→65K 布林上軌、「已失去追涨勇气」但缝低接多 63-63.5K、大方向仍看空下輪）＋飛揚 7/9 ETH 偏空（支阻內無聊震盪 1,700-1,702 撐/1,706-1,708 壓、持空看 1,700、破 1,700-1,702 才做空）＋加密龐克 7/9 中性（60-65K 中線震盪、現貨買單 60,500→61,000 上移、大鯨魚吸籌、⚠️**超級巨鯨持倉近 22 年水平但反彈 40% 未獲利了結＝可能非真反轉**、ETF 轉流出 84M）。→ **不改任何常數、不重啟**：現有帶精準對齊——`BTC_SUPPORT (60-62K)`+near_support ≤62,620＝歐陽 61,500-62,000/6萬+龐克 61,097、`ETH_SUPPORT (1700-1720)`＝飛揚 1,700-1,702+歐陽 1,725 多、`ETH_RESISTANCE (1780-1800)`＝上緣高空帶（現價 1,770 未到、bot 1720-1780 禁追空合「無聊震盪別追空」）。**★SHORT_BIAS 維持（關鍵）★**：短線 2/3 偏多（歐陽明牌多+飛揚 BTC 月線發力）但**大框架仍熊末反彈**——歐陽自言「7 月只吸對手盤、過了 7 月跌破」、飛揚「失去追涨勇气+看空下輪」、龐克「巨鯨未平倉非真反轉」；鬆動門檻（飛揚結構翻多/龐克創 65,500 新高）**均未觸發**。歐陽 67-68K 階段頂屬 cosmetic、`BTC_RESISTANCE` 上緣 65,500 對齊不動。**發 TG、未重啟**（★監看：BTC 破 64-64.5K→65-65.5K→歐陽 67-68K 頂/破 61,097→回測 6 萬；ETH 站 1,700-1,702 觀望/破 1,700 追空/上 1,780-1,800 放空；**7 月底歐陽轉空窗逼近＝SHORT_BIAS 續留關鍵**；山寨補漲做多 DOGE/ADA/XRP 被 SHORT_BIAS 擋、保守續留）
- **7/8（加密龐克 BTC ＋ 飛揚 BTC/ETH ＋ 歐陽 BTC，Whisper/字幕）★參數不變、reaffirm★**：BTC **64,000 再受阻、連兩天回踩**、62-63K 密集成交區（POC）為短線關鍵防線。BTC 三方偏多/震盪：飛揚昨 62,700 多單獲利逃頂、今收陰破 MA5 但「**跌破 62-62.5K 也仍不做空、佛系等低多、該把握多單**」＋歐陽「**底部不斷抬高、插針洗盤、反彈沒走完、62-62.5K 接多、64,500 才空止損、别摸頂**」＋加密龐克（震盪 6萬-6.5萬、關鍵 **65,500 創此才終結下跌/61,097 最低限度**、⚠️**持倉連 6 天降+資費高位＝空頭平倉離場、上漲推力已耗盡、主力逼空後恐再下跌**、鯨魚 60,500 買單牆+散戶賣鯨魚買 2,500 顆）。**唯飛揚 ETH 轉偏空**：1,800 強壓站不上、破 MA5、4h 死亡三角破位、**下看 1,700-1,702**、追空不如等 1,700 跌破/反抽 1,704-1,706 高空；歐陽 ETH 仍 1,780 空/**1,740 延多**、ETH 生態幣（AV1/ONDO）強、等 ETH 走弱優先空龍頭。→ **不改任何常數、不重啟**：現有帶精準對齊——`BTC_SUPPORT (60-62K)`+near_support ≤62,620 涵蓋三方 62-63K/61,097、`ETH_SUPPORT (1700-1720)`＝飛揚 1,700-1,702+歐陽 1,740、`ETH_RESISTANCE (1780-1800)`＝歐陽 1,780 空/飛揚 1,800 強壓。**SHORT_BIAS 維持**：BTC 三方偏多但飛揚 ETH 偏空＋龐克「逼空後模式」謹慎、非翻空亦非 2/3 翻多共識；65,500 對齊 cosmetic `BTC_RESISTANCE` 上緣不動。**發 TG、未重啟**（★監看：BTC 62-63K POC 撐→64K→65,500（歐陽 64,500 空止損）/破 61,097→回測 6 萬/破 59,000 主跌浪；ETH 站 1,700-1,702 觀望/破 1,700 追空/上 1,800 追涨；持倉續降+逼空後模式＝短線隨時再洗）
- **7/7（加密龐克 BTC ＋ 飛揚 BTC/ETH ＋ 歐陽 BTC，Whisper/字幕）★參數不變、reaffirm★**：BTC 昨插針近 **61,097** 後 **V 反收 63,800**、ETH 彈 1,833。短線三方偏多：飛揚逢低做多 63,000-63,500（做空很危險、順勢而為）＋飛揚 ETH 低多 1,704-1,706（跌破才追空、反彈 1,800/1,900 就跑）＋**歐陽單日由「摩頂空」翻回「别急摸顶空、本轮上涨还有空间、頂 66,000-68,000、7 月底才向下找熊底」**＋加密龐克（微策略賣幣時大鯨魚逆勢掃貨 2,500 顆+60,500 巨量買單牆吸籌，但**未創 65,500 新高＝下跌未終結**、5-6萬買入區、ETF 恢復流入）。→ **不改任何常數、不重啟**：昨天（7/6）剛上移的參數今天仍精準對齊——`ETH_SUPPORT (1700-1720)`＝飛揚低多 1,704-1,706、`ETH_RESISTANCE (1780-1800)`＝歐陽 1,785 做空/飛揚 1,800 就跑、`BTC_SUPPORT (60-62K)`+near_support ≤62,620 涵蓋歐陽 62-63K 接多/龐克 61,097 最低限度。**SHORT_BIAS 維持**：歐陽雖翻回偏多（顶 66-68K），但飛揚短多長空＋龐克「未創新高非反轉」仍 2/3 偏空/中性、無翻多共識；歐陽 66-68K 頂屬 cosmetic 層級不動。**發 TG、未重啟**（★監看：BTC 撐 62-63K→65,500→歐陽 66-68K 頂（收復 65,500 才終結下跌）/跌破 61,097→回測 6 萬；ETH 站 1,706 低多/破 1,700-1,702 追空/上 1,800 追涨；歐陽方向反覆＝短線情緒、7 月底轉空窗仍關鍵）
- **7/6（加密龐克 BTC ＋ 飛揚 BTC/ETH ＋ 歐陽 BTC ＋ 飛揚 7/5 補回，Whisper/字幕）★三方共識上移、BTC/ETH support wired 更新★**：BTC 從 57,700 連漲觸 **64,000** 後回落、區間確認上移 **60,000-65,500 震盪數週**。**歐陽由「等回測」正式轉「摩頂空」**（完成逼空、涨的不健康、當強壓；**63,500-64,000 做空、回落 62,000 指引**、大區間 62,000 接多/65,000 做空；定調「**7 月上半月反彈完結、下半月主跌浪、熊底布局**」）。飛揚 7/6 BTC（五連陽 63,999、錒低接多 63,000-63,500 目標 65,000-65,500、月線高空防瀑布）＋飛揚 7/6 ETH（雙頂回落、低多帶上移 **1,704-1,720 站上 1,706 確認**、跌破 1,700-1,702 才追空、1,800 分水嶺）＋加密龐克（連彈站上 63K POC、區間 60-65K 未創新高＝非反轉、需創 65,500 才終結下跌、**微策略首次真賣 3,588 顆進入「買賣都做」時代持 4% 供給**、6 萬以下大鯨魚補買單吸籌、右側抄底/低倍多單未到成本線 68,800）。→ **wired 落地**：`BTC_SUPPORT_ZONE (58.5-60K) → (60-62K)`（三方接多/支撐上移 61,097-62,000、舊帶落價格後；`near_support` 門檻 60,600→**62,620**、BTC 回落 6萬2 接多區禁山寨追空合補漲）、`ETH_SUPPORT_ZONE (1,600-1,640) → (1,700-1,720)`（飛揚 floor 由 1,640 上移 1,700、舊帶 stale＝ETH 回落低點 1,706 仍在其上；止住 bot 在 1,706-1,780 逆勢追空）、`BTC_RESISTANCE_ZONE (62-63K) → (63.5-65.5K)`（cosmetic）。**SHORT_BIAS 維持 True、SOL 維持**：三方無翻多（歐陽轉摩頂空/飛揚長空/龐克非反轉），偏空結構反被強化。**已部署並重啟 coin-monitor**（★監看：BTC 撐 62-63K→挑戰 65,500（收復才終結下跌）/跌破 61,097→回測 6 萬/破 59,000 主跌浪、歐陽 63.5-64K 摩頂空；ETH 站 1,706 確認低多/破 1,700-1,702 追空/上 1,800-1,850 下半場；**歐陽下半月主跌浪＝7 月中下旬留意轉空、SHORT_BIAS 續留關鍵窗**）
- **7/5（飛揚 ETH ＋ 歐陽 BTC，Whisper/字幕；飛揚 7/5 BTC 待重試）★參數不變、reaffirm★**：BTC 凌晨放量突破上探 **63,000**（我監看目標進行中）、ETH 逼近 1,780-1,800 高空帶緣。**歐陽 7/5 從全多轉「到頂減多＋62-63K 佈空＋等回測」**：BTC 已到強壓（4H 下跌 70 點）、多單減倉、**62,000-63,000 區間做空保留底倉**、等回測 **61,500/60,000** 再接多；但定調「**7 月上半月延續反彈、至少 2-3 週做多窗口、上半月完成後才走下跌**」＝仍「漲勢中回測」非翻空。飛揚 7/4 ETH（1,778 低多沒觸發、日線雙底頸線 **1,800-1,850 分水嶺、突破才下半場**、月線反抽修復/死亡三角未成、不敢追漲續等回踩 1,700-1,760、4H TD13 出）；飛揚 7/5 BTC 標題「承压回撤别追空、真正机会马上要来」（續判、Whisper 403 失敗下輪重試）。→ **不改任何常數、不重啟**：行情落在框架緣上、bot 行為正確（BTC 62-63K 空放行合歐陽主動高位空、near_support ≤60,600 護 60K 回測接多區、ETH 1,778<1,780 禁追空合「別追」、≥1,780 放空合弥端壓制/歐陽開空）；歐陽仍多（上半月續反彈）＋飛揚短多長空＝無翻多、SHORT_BIAS 維持。**發 TG、未重啟**（★監看：BTC 站穩 63,000→65,500；回測破 61,500→60,000→破 59,000 主跌浪 57K/52-53K；ETH 上 1,780-1,800 高空/突破 1,800-1,850 下半場/回踩 1,700-1,760 接多；歐陽「上半月完成後才下跌」2-3 週後留意轉空）
- **7/4（加密龐克 BTC ＋ 飛揚 BTC/ETH ＋ 歐陽 BTC，Whisper/字幕）★參數不變、reaffirm★**：四片全面重申，行情觸及我 7/3 監看的 **62-63K 壓力**、三方定調「到壓力→回測→再漲」。飛揚 7/4 BTC（站穩 3618、續看漲但**月線反抽非反轉**、4H TD13＋光腳陽線乘壓需回撤、等回踩 61,000-61,500 接多、**千萬別盲目做空**）＋飛揚 7/3 ETH（1,748 雙底健康、空倉別追漲、等回踩 1,700-1,720、沒必要做空）＋歐陽 7/4（BTC 已達關鍵壓力 62.5-63K **要回測**、回測 61,000-61,500 仍多、之後 **6 萬-6 萬5 宽幅震荡高拋低吸**下沿 60-60.5K 接多/上沿 62-63K 空；ETH 1,770 觸強壓暫不追多；DOGE 補漲 0.08）＋加密龐克 7/3（收上 **61,097 短線轉強**→63K POC→**65,500**、6 萬以下巨量買盤/微策略投降拋售只砸 57K 被接、大鯨魚偷跑，但**熊市最痛苦未過、週期底 9-10 月才到**）。→ **不改任何常數、不重啟**：行情落在 7/3 剛設框架內、bot 行為正確（BTC 62-63K 空放行合歐陽高拋、ETH 1,640-1,780 禁追空合「別空」、上 1,780 才放空合歐陽開空、near_support ≤60,600 護 6 萬回踩區）；飛揚長空＋龐克痛苦未過 vs 歐陽全多＝無 2/3 翻多、SHORT_BIAS 維持。**發 TG、未重啟**（★監看：BTC 站穩 63,000→65,500；跌破 61,000-61,500+4H 886→回測 6 萬（破 59,000 主跌浪 57K/52-53K）；ETH 上 1,780-1,800 高空/守 1,600-1,640/回踩 1,700-1,720；6 萬-6 萬5 高拋低吸）
- **7/3（飛揚 BTC ＋ 歐陽 BTC×2/ETH，Whisper）★反彈延續、BTC support＋ETH zones 上移★**：反彈延續、BTC 站上 61,100（我先前監看的「站穩 61,097/60,500」觸發）、ETH 匯率隊領漲至 1,715、**6 萬關口由壓力轉有效支撐**。飛揚（短多長空：日線指標多頭 OK 但「不追漲 61K 上方、等回踩 60,400-60,800 接多」、突破 62,000 才追、**大方向仍看空下一輪暴跌**）＋歐陽（全多：58,000 見底站 61,100、59,500-60,000 回踩接多止損 59,000、目標 62,500-63,000、破位 70 點才空；ETH 1,715 但 **1,800 頂/1,780 開空/最多再 16 點**；ADA+17%、DOGE 補漲）。→ **wired 落地**：`BTC_SUPPORT_ZONE (58,000-59,000) → (58,500-60,000)`（歐陽兩度重申 6 萬轉支撐、雙方回踩接多區上移；`near_support` 門檻連動 59,590→**60,600**、BTC 回落 6 萬回踩區時禁山寨地板追空防軋空）、`ETH_SUPPORT_ZONE (1,500-1,520) → (1,600-1,640)`＋`ETH_RESISTANCE_ZONE (1,600-1,620) → (1,780-1,800)`（現價 1,715 已在舊高空帶之上＝bot 會逆勢追空，上移後 1,640-1,780 禁追空、1,780+ 才放行＝止住逆勢空）、`BTC_RESISTANCE_ZONE (60.5-61.5K) → (62-63K)`（cosmetic）。**SHORT_BIAS 維持 True、SOL 維持**：無 2/3 翻多共識（飛揚反彈中繼＋龐克熊末 vs 歐陽全多）、反彈延續已在預期內非翻轉；飛揚 7/3 未細講 SOL。**已部署並重啟 coin-monitor**（★監看：BTC 破 62,000 站穩→62.5-63K；跌破 61,100+4H 886→回測 6 萬（破 59,000 則主跌浪 57K/52-53K）；ETH 上 1,780-1,800 高空、守 1,600-1,640；SOL 100-120 高空）
- **7/2（飛揚 BTC/ETH/SOL ＋ 加密龐克 BTC，Whisper/字幕）★反彈確認、SOL 落地變動★**：**反彈由「預期」轉「確認」**——BTC 一天 57,700→62,000、ETH 守 1,500 生死線彈 1,700、SOL 自 60 反彈至 80。但飛揚（養豬陷阱、仍高空、反弹不反转、短線分水嶺 60,000-60,500 突破追涨/乘壓高空）＋加密龐克（月線實體陰 K、55 萬顆史上最大投降拋售、大鯨魚連 5 天砸盤卻掛巨量買單 5-6 萬、僅 46% 供給獲利=近六年最低近底部、正測 61,097 收上則挑戰 POC 63,000、買區 5萬-6萬）**2/3 仍定調結構偏空**、歐陽唯一多方（Whisper 待補）。**SOL 結構明確上移 → wired 落地**：`SOL_SUPPORT_ZONE (66,68) → (60,75)`（周線 886/2618 支撐、守 60、逢低接多至 75、80 勿追涨）、`SOL_RESISTANCE_ZONE (70,72) → (100,120)`（生死線/高空區、不值得空等 120 再空；cosmetic 未接線）——舊帶已失準（SOL 現 80 在兩帶之上）。**BTC/ETH/SHORT_BIAS 全留**：反彈已在 7/1 預期內、非方向翻轉；near_support ≤59,590 續擋 58-59.5K 地板追空（大鯨魚買區）、價現 62K 已可於 60.5K+ 空反彈（合飛揚高空）；ETH 守 1,500、破 1,800 更大漲幅。**已部署並重啟 coin-monitor**（★監看：BTC 站穩 61,097/60,500→63K POC；破前低 57,000→主跌浪下移 52-53K；ETH 守 1,500/破 1,800；SOL 100-120 高空）
- **6/28（飛揚 BTC/ETH ＋ 歐陽 BTC/ETH/SOL，Whisper）★BTC 高拋帶下移★**：兩方一致「**58K 二探止跌但空頭主導未變、6 萬弱勢無量震盪、6 萬＝多空生死線**」。飛揚：BTC 熊旗已成、墓碑線、MA5 壓制 61K、1 小時死亡三角、**今日高空帶下移 60,500-60,600**（昨 60,700-61,200）、跌破 59,800/59,500 才追空、別地板空。歐陽：58K 二探（58,003 低點）僅技術修復、空頭主導未變、**入場開空 611-615**（多頭陷阱密集套牢區）、目標回 58K。反彈高拋帶同向下移 `BTC_RESISTANCE_ZONE (61000,62000) → (60500,61500)`、`main.py KEY_RESISTANCE_ZONE` 同步。`BTC_SUPPORT_ZONE (58000,59000)`（near_support ≤59,590）不變。ETH（飛揚 1,600-1,601 成壓打下 1,560、歐陽守 1,500/高空 1,650-1,700）→ 現有 `(1600,1620)`/`(1500,1520)` 一致**不改**；SOL 反彈、空單已止盈、無新明確點位**維持**。**已部署並重啟 coin-monitor**
- **6/27 晚間（飛揚 BTC/ETH ＋ 歐陽 BTC，Whisper）★ETH 微調★**：三方一致「**6 萬＝多空生死線、弱勢無量震盪**」。飛揚：BTC 58K 反彈逼近 6 萬「這不是上漲是陷阱」；歐陽：深破後不再猛跌、58K 低吸/60.5-61K 指引/61.5-62K 開空。**BTC 不改**（6 萬生死線＝near_support 邊界、58K 低吸/60.5-62K 高空在軌）。ETH 雙底震盪 1,500-1,620、高空帶同向上移 `ETH_RESISTANCE_ZONE (1580,1600) → (1600,1620)`（飛揚 MA5 1600/M310 1620；突破多頭區禁空連動 1,520-1,600、shorts 放行 ≥1,600）。SOL/AVAX 逼空預期、SOL 空單考慮平倉。**已部署並重啟 coin-monitor**
- **6/26 深夜（飛揚 ETH 續跌，Whisper）★ETH 參數變動★**：ETH「純減錢」隨 BTC 跌至 1,500-1,600、stale 下移。`ETH_RESISTANCE_ZONE (1670,1720) → (1580,1600)`（飛揚：高空帶 1,580-1,600、最好 1,600、MA5 缺口 1,603）、`ETH_SUPPORT_ZONE (1600,1640) → (1500,1520)`（1,500-1,510＝2618 關鍵支撐、空單跑路；突破多頭區禁空連動 1,520-1,580、shorts 放行 ≥1,580 及破 <1,500 追空）。BTC 重申高空（58.3K 反彈做空、續高空、參數不變）。**已部署並重啟 coin-monitor**
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
