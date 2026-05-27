# TradingBot

ML-powered crypto futures trading bot for BTC, ETH, SOL and altcoins.  
Runs 24/7 on a VPS via Docker, sends all notifications to Telegram.

> Last updated: 2026-05-27 23:30 +08

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
