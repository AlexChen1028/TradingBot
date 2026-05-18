#!/usr/bin/env python3
"""
scripts/auto_kol_update.py

Daily KOL analysis pipeline:
  1. Fetch latest YouTube videos (RSS) from configured channels
  2. Extract transcripts (youtube_transcript_api)
  3. Analyze with Gemini API (free tier) → insights + parameter change suggestions
  4. Append to notes/youtube-insights.md
  5. Apply high-confidence parameter changes to monitor_coins.py
  6. git commit + push
  7. TG notification with summary

Cron (VPS, runs daily at 8am Taipei):
  0 0 * * * cd ~/TradingBot && python3 scripts/auto_kol_update.py >> logs/kol_update.log 2>&1
"""

import os, sys, io, json, re, time, subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Fix Windows console encoding (cp950 can't handle some Chinese chars)
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ('utf-8', 'utf8'):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

import feedparser
import requests

try:
    from youtube_transcript_api import YouTubeTranscriptApi, TranscriptsDisabled, NoTranscriptFound
except ImportError:
    YouTubeTranscriptApi = None

try:
    from google import genai as _genai
except ImportError:
    _genai = None

# ── KOL Channel Config ────────────────────────────────────────────────────────
# Add more channels here. channel_id can be found:
#   1. Open channel page in browser
#   2. View page source → search for "channelId" or "UC"
#   3. Or use: https://www.tunetheweb.com/tools/find-youtube-channel-id/
KOL_CHANNELS = [
    {
        'handle':     '@crypto_punks',
        'channel_id': '',
        'name':       '加密龐克',
        'langs':      ['zh-TW', 'zh-Hant', 'zh', 'zh-Hans', 'en'],
    },
    {
        'handle':     '@BTCfeiyang',
        'channel_id': '',
        'name':       'BTC飛揚',
        'langs':      ['zh-TW', 'zh-Hant', 'zh', 'zh-Hans', 'en'],
    },
    {
        'handle':     '@BTC-ouyang',
        'channel_id': '',
        'name':       'BTC歐陽',
        'langs':      ['zh-TW', 'zh-Hant', 'zh', 'zh-Hans', 'en'],
    },
]

# --historical flag sets this to 8760 (1 year) to process all RSS videos
LOOKBACK_HOURS       = 30           # look for videos published in last N hours
HISTORICAL_MAX_PER_KOL = 5         # --historical mode: max videos per KOL
MAX_TRANSCRIPT_CHARS = 10000        # truncate transcripts before sending to Claude
AUTO_APPLY_MIN_CONF  = 'high'       # only auto-apply parameter changes at this confidence

# ── Paths ─────────────────────────────────────────────────────────────────────
REPO_ROOT    = Path(__file__).resolve().parent.parent
NOTES_FILE   = REPO_ROOT / 'notes' / 'youtube-insights.md'
MONITOR_FILE = REPO_ROOT / 'monitor_coins.py'
SEEN_FILE    = REPO_ROOT / 'notes' / '.kol_seen.json'
LOG_DIR      = REPO_ROOT / 'logs'

# ── Env ───────────────────────────────────────────────────────────────────────
GEMINI_KEY       = os.getenv('GEMINI_API_KEY', '')
MONITOR_TOKEN    = os.getenv('MONITOR_TOKEN', '')
MONITOR_CHAT_IDS = [i.strip() for i in os.getenv('MONITOR_CHAT_ID', '').split(',') if i.strip()]

def now8():
    return datetime.now(timezone(timedelta(hours=8)))

def tg(text):
    if not MONITOR_TOKEN or not MONITOR_CHAT_IDS:
        return
    for chat_id in MONITOR_CHAT_IDS:
        try:
            requests.post(
                f'https://api.telegram.org/bot{MONITOR_TOKEN}/sendMessage',
                json={'chat_id': chat_id, 'text': text, 'parse_mode': 'HTML'},
                timeout=10,
            )
        except Exception:
            pass

# ── Channel ID resolution ─────────────────────────────────────────────────────

def resolve_channel_id(handle: str) -> str | None:
    """Scrape YouTube channel page to extract channel ID from embedded JSON."""
    url = f'https://www.youtube.com/{handle}'
    try:
        r = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=15)
        # channel ID appears as "channelId":"UCxxxx" in page source
        m = re.search(r'"channelId"\s*:\s*"(UC[A-Za-z0-9_-]{22})"', r.text)
        if m:
            return m.group(1)
        # fallback: look in canonical URL
        m2 = re.search(r'canonical.*?channel/(UC[A-Za-z0-9_-]{22})', r.text)
        if m2:
            return m2.group(1)
    except Exception as e:
        print(f'  resolve_channel_id({handle}) failed: {e}')
    return None

# ── Seen videos ───────────────────────────────────────────────────────────────

def load_seen() -> set:
    if SEEN_FILE.exists():
        try:
            return set(json.loads(SEEN_FILE.read_text())['seen'])
        except Exception:
            pass
    return set()

def save_seen(seen: set):
    SEEN_FILE.parent.mkdir(exist_ok=True)
    SEEN_FILE.write_text(json.dumps({'seen': sorted(seen)}, indent=2))

# ── YouTube data ──────────────────────────────────────────────────────────────

def get_latest_videos(channel_id: str, since: datetime) -> list[dict]:
    """Fetch recent videos via YouTube RSS. Returns list of {id, title, url, published, description}."""
    url = f'https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}'
    feed = feedparser.parse(url)
    results = []
    for e in (feed.entries or []):
        vid_id = e.get('yt_videoid', '')
        if not vid_id:
            continue
        pub = e.get('published_parsed')
        if pub:
            pub_dt = datetime(*pub[:6], tzinfo=timezone.utc)
            if pub_dt < since:
                continue
        # description from RSS (usually first ~500 chars of video description)
        desc = e.get('summary', '') or e.get('description', '')
        if isinstance(desc, str):
            desc = re.sub(r'<[^>]+>', '', desc).strip()[:500]
        results.append({
            'id':          vid_id,
            'title':       e.get('title', ''),
            'url':         e.get('link', f'https://youtu.be/{vid_id}'),
            'published':   pub,
            'description': desc,
        })
    return results

def get_transcript(video_id: str, langs: list) -> str | None:
    if YouTubeTranscriptApi is None:
        print('  youtube_transcript_api not installed, skipping transcript')
        return None
    try:
        # v1.x: instantiate first, then call fetch()
        fetched = YouTubeTranscriptApi().fetch(video_id, languages=langs)
        raw = ' '.join(s.text for s in fetched)
        return raw[:MAX_TRANSCRIPT_CHARS]
    except (TranscriptsDisabled, NoTranscriptFound):
        return None
    except Exception as e:
        print(f'  transcript error ({video_id}): {e}')
        return None

# ── Claude analysis ───────────────────────────────────────────────────────────

ANALYSIS_PROMPT = """你是加密貨幣交易機器人（crypto-bot）的策略分析師。
你正在分析 KOL 的市場觀點影片（或逐字稿），目的是提取能應用到 monitor_coins.py 的具體建議。

monitor_coins.py 的關鍵參數（供你參考）：
- MIN_SIGNALS = 2           # 觸發進場的最低信號數
- STOP_LOSS_PCT = 0.035     # 固定止損 3.5%
- TP_PCT = 0.07             # 固定止盈 7%
- LEVERAGE = 20             # 槓桿
- MARGIN_BY_SIGNALS = {{2:60, 3:80, 4:100}}  # 動態保證金
- LEADERBOARD_MIN_PCT = 3.0 # 漲跌幅榜最低 24h 幅度

KOL 頻道：{channel_name}
影片標題：{title}

逐字稿（節選）：
{transcript}

請輸出嚴格的 JSON（不要多餘文字）：
{{
  "market_bias": "bullish" | "bearish" | "neutral",
  "key_insights": ["最多 5 條，具體影響交易的觀點"],
  "ta_indicators": [
    {{
      "name": "EMA200",
      "usage": "KOL 如何使用這個指標的一句話說明",
      "signal_type": "trend" | "momentum" | "volatility" | "volume" | "sentiment"
    }}
  ],
  "parameter_changes": [
    {{
      "variable": "STOP_LOSS_PCT",
      "suggested_value": 0.04,
      "reason": "KOL提到波動加劇需要更寬止損",
      "confidence": "high" | "medium" | "low"
    }}
  ],
  "logic_suggestions": [
    {{
      "description": "需要修改邏輯的建議（非參數），供人工審閱",
      "priority": "high" | "medium" | "low"
    }}
  ],
  "tg_summary": "一句話摘要（繁體中文，50字以內，用於 TG 通知）"
}}

規則：
- ta_indicators：列出影片中 KOL 有明確提到或用來判斷行情的所有技術指標（EMA、RSI、MACD、布林帶、資費、成交量等）
- parameter_changes 只填有充分影片依據的建議
- confidence=high 意味著你確信這個改動在當前市況合理
- 沒有建議時用空陣列
- market_bias 根據 KOL 對未來 1-7 天的整體看法判斷"""


def _parse_gemini_json(text: str) -> dict | None:
    """Strip markdown fences and parse JSON from Gemini response."""
    text = re.sub(r'^```(?:json)?\s*', '', text.strip())
    text = re.sub(r'\s*```$', '', text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r'\{.*\}', text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group())
            except Exception:
                pass
    return None

_ANALYSIS_DEFAULT = {
    'market_bias': 'neutral', 'key_insights': [], 'ta_indicators': [],
    'parameter_changes': [], 'logic_suggestions': [],
    'tg_summary': '分析失敗，請查看 notes/youtube-insights.md',
}


_quota_exhausted = False  # set True when daily quota confirmed gone

def _call_gemini_with_retry(client, prompt: str, max_retries: int = 3) -> str | None:
    """Call Gemini text API with automatic retry on 429 rate limit."""
    global _quota_exhausted
    if _quota_exhausted:
        return None
    for attempt in range(max_retries):
        try:
            resp = client.models.generate_content(model='gemini-2.0-flash-lite', contents=prompt)
            return resp.text
        except Exception as e:
            err = str(e)
            if '429' in err or 'RESOURCE_EXHAUSTED' in err:
                # PerDay quota or limit:0 → daily allowance gone, retrying is pointless
                if 'PerDay' in err or "'limit': 0" in err or '"limit": 0' in err:
                    print('  ❌ Gemini daily quota exhausted — aborting run.')
                    _quota_exhausted = True
                    return None
                m = re.search(r'retryDelay.*?(\d+)s', err)
                if not m:
                    # No retryDelay = daily quota exhausted, no point retrying
                    print('  ❌ Gemini daily quota exhausted — aborting run.')
                    _quota_exhausted = True
                    return None
                wait = int(m.group(1)) + 5
                if wait > 120:
                    # Very long delay also signals daily quota
                    print(f'  ❌ Gemini quota exhausted (retryDelay={wait}s) — aborting run.')
                    _quota_exhausted = True
                    return None
                print(f'  Rate limited — waiting {wait}s (attempt {attempt+1}/{max_retries})...')
                time.sleep(wait)
            else:
                print(f'  Gemini error: {e}')
                return None
    # All retries failed — treat as quota exhausted to avoid wasting time on remaining videos
    print('  ❌ All retries failed — assuming quota exhausted, aborting run.')
    _quota_exhausted = True
    return None


def analyze_video_direct(video: dict, channel_name: str, client) -> dict:
    """
    Analyze video using title + RSS description (text only).
    Avoids direct video URL which exhausts free-tier token quota.
    """
    context = f"標題：{video['title']}\n描述：{video.get('description', '（無描述）')}"
    prompt = ANALYSIS_PROMPT.format(
        channel_name=channel_name,
        title=video['title'],
        transcript=context,
    )
    text = _call_gemini_with_retry(client, prompt)
    if text:
        result = _parse_gemini_json(text)
        if result:
            return result
    return dict(_ANALYSIS_DEFAULT)


def analyze_with_gemini(title: str, channel_name: str, transcript: str, client) -> dict:
    prompt = ANALYSIS_PROMPT.format(
        channel_name=channel_name, title=title, transcript=transcript,
    )
    text = _call_gemini_with_retry(client, prompt)
    if text:
        result = _parse_gemini_json(text)
        if result:
            return result
    return dict(_ANALYSIS_DEFAULT)

# ── Notes update ──────────────────────────────────────────────────────────────

def append_to_notes(video: dict, channel_name: str, analysis: dict):
    ts = now8().strftime('%Y-%m-%d %H:%M +08')
    bias_e = {'bullish': '🟢', 'bearish': '🔴', 'neutral': '⚪'}.get(
        analysis.get('market_bias', 'neutral'), '⚪')

    lines = [
        f'\n---\n',
        f'### [{ts}] {video["title"]}',
        f'**來源**：{channel_name} — {video["url"]}  ',
        f'**市場偏向**：{bias_e} {analysis.get("market_bias", "neutral")}\n',
        '**關鍵洞察**：',
    ]
    for ins in analysis.get('key_insights', []):
        lines.append(f'- {ins}')

    param_changes = analysis.get('parameter_changes', [])
    if param_changes:
        lines.append('\n**參數建議**（`high` 信心自動套用）：')
        for p in param_changes:
            lines.append(
                f'- `{p["variable"]}` → `{p["suggested_value"]}` '
                f'[{p["confidence"]}] — {p["reason"]}'
            )

    logic_sug = analysis.get('logic_suggestions', [])
    if logic_sug:
        lines.append('\n**邏輯建議**（待人工審閱）：')
        for l in logic_sug:
            lines.append(f'- [{l["priority"]}] {l["description"]}')

    lines.append('')

    NOTES_FILE.parent.mkdir(exist_ok=True)
    with open(NOTES_FILE, 'a', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')


# ── KOL indicator profile ─────────────────────────────────────────────────────
INDICATORS_FILE = REPO_ROOT / 'notes' / 'kol_indicators.json'

def update_kol_indicator_profile(channel_name: str, ta_indicators: list):
    """
    累計每個 KOL 提到的技術指標次數，存到 notes/kol_indicators.json。
    格式：{ "加密龐克": { "EMA200": 3, "RSI": 2, ... }, ... }
    """
    if not ta_indicators:
        return

    profile = {}
    if INDICATORS_FILE.exists():
        try:
            profile = json.loads(INDICATORS_FILE.read_text(encoding='utf-8'))
        except Exception:
            pass

    kol_counts = profile.setdefault(channel_name, {})
    for ind in ta_indicators:
        name = ind.get('name', '').strip()
        if name:
            kol_counts[name] = kol_counts.get(name, 0) + 1

    # Sort each KOL's indicators by frequency
    profile[channel_name] = dict(
        sorted(kol_counts.items(), key=lambda x: x[1], reverse=True)
    )

    INDICATORS_FILE.parent.mkdir(exist_ok=True)
    INDICATORS_FILE.write_text(
        json.dumps(profile, ensure_ascii=False, indent=2), encoding='utf-8'
    )
    top = list(kol_counts.keys())[:5]
    print(f'  📊 KOL 指標統計更新：{channel_name} → 前五名：{top}')


def notes_indicator_summary() -> str:
    """從 kol_indicators.json 產生 Markdown 摘要（追加到 notes 用）。"""
    if not INDICATORS_FILE.exists():
        return ''
    try:
        profile = json.loads(INDICATORS_FILE.read_text(encoding='utf-8'))
    except Exception:
        return ''
    lines = ['\n## KOL 技術指標統計（累計）\n']
    for kol, counts in profile.items():
        lines.append(f'### {kol}')
        for ind, n in list(counts.items())[:10]:
            lines.append(f'- {ind}: {n} 次')
        lines.append('')
    return '\n'.join(lines)


# ── Parameter change application ──────────────────────────────────────────────

def apply_parameter_changes(changes: list) -> list:
    """Apply high-confidence changes to monitor_coins.py. Returns list of applied changes."""
    if not changes:
        return []

    code = MONITOR_FILE.read_text(encoding='utf-8')
    applied = []

    for ch in changes:
        if ch.get('confidence') != AUTO_APPLY_MIN_CONF:
            continue

        var  = ch['variable']
        val  = ch['suggested_value']
        note = ch.get('reason', '')[:60]

        # Match lines like: VARIABLE = 0.035  # some comment
        pattern = rf'^({re.escape(var)}\s*=\s*)[\d.]+([^\n]*)'
        repl    = rf'\g<1>{val}  # KOL auto-update: {note}'
        new_code, n = re.subn(pattern, repl, code, flags=re.MULTILINE)
        if n > 0:
            code = new_code
            applied.append(f'{var}={val}')
            print(f'  ✅ Applied: {var} → {val}')
        else:
            print(f'  ⚠️ Could not find {var} in monitor_coins.py')

    if applied:
        MONITOR_FILE.write_text(code, encoding='utf-8')

    return applied


# ── Git ───────────────────────────────────────────────────────────────────────

def git_commit_push(summary: str) -> bool:
    try:
        subprocess.run(
            ['git', 'add', 'notes/', 'monitor_coins.py'],
            cwd=REPO_ROOT, check=True, capture_output=True,
        )
        result = subprocess.run(
            ['git', 'diff', '--cached', '--quiet'],
            cwd=REPO_ROOT,
        )
        if result.returncode == 0:
            print('  Nothing to commit.')
            return True  # no changes, that's fine

        msg = (
            f'feat(kol): daily KOL analysis — {summary}\n\n'
            f'Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>'
        )
        subprocess.run(['git', 'commit', '-m', msg,
                        '--author', 'KOL Bot <kol-bot@tradingbot>'],
                       cwd=REPO_ROOT, check=True, capture_output=True)
        subprocess.run(['git', 'push'], cwd=REPO_ROOT, check=True, capture_output=True)
        return True
    except subprocess.CalledProcessError as e:
        print(f'  Git error: {e.stderr.decode() if e.stderr else e}')
        return False


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    historical = '--historical' in sys.argv   # scan all RSS videos regardless of age
    lookback   = 8760 if historical else LOOKBACK_HOURS
    print(f'[{now8().strftime("%Y-%m-%d %H:%M +08")}] KOL auto-update started'
          + (' [HISTORICAL MODE]' if historical else ''))

    if not GEMINI_KEY:
        print('ERROR: GEMINI_API_KEY not set in environment')
        tg('⚠️ KOL 自動分析失敗：缺少 GEMINI_API_KEY')
        return

    if _genai is None:
        print('ERROR: google-genai not installed. Run: pip3 install google-genai --break-system-packages')
        return

    client = _genai.Client(api_key=GEMINI_KEY)
    seen   = load_seen()
    if historical:
        seen = set()   # re-process everything in historical mode
    since  = datetime.now(timezone.utc) - timedelta(hours=lookback)

    all_processed     = []
    all_applied_changes = []

    for ch_cfg in KOL_CHANNELS:
        if _quota_exhausted:
            break
        handle  = ch_cfg['handle']
        name    = ch_cfg['name']
        cid     = ch_cfg.get('channel_id', '')
        langs   = ch_cfg.get('langs', ['zh-TW', 'zh', 'en'])

        # Auto-resolve channel ID if not set
        if not cid:
            print(f'Resolving channel ID for {handle}...')
            cid = resolve_channel_id(handle)
            if not cid:
                print(f'  Could not resolve {handle}, skipping')
                continue
            print(f'  Resolved: {cid}')
            # Update config in memory (not persisted; add to KOL_CHANNELS manually)

        print(f'\n[{name}] Fetching RSS...')
        videos = get_latest_videos(cid, since)
        if historical:
            videos = videos[:HISTORICAL_MAX_PER_KOL]
        print(f'  {len(videos)} video(s) to process')

        for video in videos:
            if _quota_exhausted:
                break
            vid_id = video['id']
            if vid_id in seen:
                continue

            print(f'  Processing: {video["title"][:60]}')

            transcript = get_transcript(vid_id, langs)   # None if blocked/unavailable
            if transcript:
                print(f'    Transcript: {len(transcript)} chars → text analysis...')
                analysis = analyze_with_gemini(video['title'], name, transcript, client)
            else:
                print(f'    No transcript — title+description analysis...')
                analysis = analyze_video_direct(video, name, client)

            # skip empty results (Gemini failed)
            if not analysis.get('key_insights') and not analysis.get('ta_indicators'):
                print('    Analysis empty, skipping notes update')
                if _quota_exhausted:
                    break   # stop processing remaining videos
                seen.add(vid_id)
                time.sleep(4)
                continue

            append_to_notes(video, name, analysis)
            update_kol_indicator_profile(name, analysis.get('ta_indicators', []))
            time.sleep(4)   # stay within free-tier RPM limit
            applied = apply_parameter_changes(analysis.get('parameter_changes', []))
            all_applied_changes.extend(applied)

            all_processed.append({
                'title':   video['title'],
                'url':     video['url'],
                'bias':    analysis.get('market_bias', 'neutral'),
                'summary': analysis.get('tg_summary', ''),
                'applied': applied,
            })
            seen.add(vid_id)
            time.sleep(1)

    save_seen(seen)

    if not all_processed:
        print('No new videos processed.')
        return

    # Git commit + push
    change_str = ', '.join(all_applied_changes) if all_applied_changes else '筆記更新'
    pushed = git_commit_push(change_str)

    # TG notification
    bias_map = {'bullish': '🟢', 'bearish': '🔴', 'neutral': '⚪'}
    msg_lines = ['📺 <b>KOL 每日分析</b>']
    for v in all_processed:
        be = bias_map.get(v['bias'], '⚪')
        msg_lines.append(
            f"\n{be} <b>{v['title'][:45]}</b>\n"
            f"{v['summary']}"
            + (f"\n⚙️ 自動套用：{', '.join(v['applied'])}" if v['applied'] else '')
        )
    msg_lines.append(f"\n\n{'✅ 已推送到 GitHub' if pushed else '⚠️ Push 失敗，請手動處理'}")
    tg('\n'.join(msg_lines))

    print(f'\nDone. {len(all_processed)} video(s) processed.')
    if all_applied_changes:
        print(f'Parameter changes applied: {all_applied_changes}')


if __name__ == '__main__':
    LOG_DIR.mkdir(exist_ok=True)
    main()
