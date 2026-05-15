#!/usr/bin/env python3
"""
scripts/auto_kol_update.py

Daily KOL analysis pipeline:
  1. Fetch latest YouTube videos (RSS) from configured channels
  2. Extract transcripts (youtube_transcript_api)
  3. Analyze with Claude API → insights + parameter change suggestions
  4. Append to notes/youtube-insights.md
  5. Apply high-confidence parameter changes to monitor_coins.py
  6. git commit + push
  7. TG notification with summary

Cron (VPS, runs daily at 8am Taipei):
  0 0 * * * cd ~/TradingBot && python3 scripts/auto_kol_update.py >> logs/kol_update.log 2>&1
"""

import os, json, re, time, subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import feedparser
import requests

try:
    from youtube_transcript_api import YouTubeTranscriptApi, TranscriptsDisabled, NoTranscriptFound
except ImportError:
    YouTubeTranscriptApi = None

try:
    import anthropic as _anthropic
except ImportError:
    _anthropic = None

# ── KOL Channel Config ────────────────────────────────────────────────────────
# Add more channels here. channel_id can be found:
#   1. Open channel page in browser
#   2. View page source → search for "channelId" or "UC"
#   3. Or use: https://www.tunetheweb.com/tools/find-youtube-channel-id/
KOL_CHANNELS = [
    {
        'handle':      '@crypto_punks',
        'channel_id':  '',           # fill in once (see instructions above)
        'name':        '加密龐克',
        'langs':       ['zh-TW', 'zh-Hant', 'zh', 'zh-Hans', 'en'],
    },
    # Add more KOLs here:
    # {
    #     'handle':     '@another_kol',
    #     'channel_id': 'UCxxxxxxxxxxxxxxxxxxxxxxxx',
    #     'name':       'KOL Name',
    #     'langs':      ['zh-TW', 'en'],
    # },
]

LOOKBACK_HOURS       = 30           # look for videos published in last N hours
MAX_TRANSCRIPT_CHARS = 10000        # truncate transcripts before sending to Claude
AUTO_APPLY_MIN_CONF  = 'high'       # only auto-apply parameter changes at this confidence

# ── Paths ─────────────────────────────────────────────────────────────────────
REPO_ROOT    = Path(__file__).resolve().parent.parent
NOTES_FILE   = REPO_ROOT / 'notes' / 'youtube-insights.md'
MONITOR_FILE = REPO_ROOT / 'monitor_coins.py'
SEEN_FILE    = REPO_ROOT / 'notes' / '.kol_seen.json'
LOG_DIR      = REPO_ROOT / 'logs'

# ── Env ───────────────────────────────────────────────────────────────────────
ANTHROPIC_KEY    = os.getenv('ANTHROPIC_API_KEY', '')
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
    """Fetch recent videos via YouTube RSS. Returns list of {id, title, url, published}."""
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
        results.append({
            'id':        vid_id,
            'title':     e.get('title', ''),
            'url':       e.get('link', f'https://youtu.be/{vid_id}'),
            'published': pub,
        })
    return results

def get_transcript(video_id: str, langs: list) -> str | None:
    if YouTubeTranscriptApi is None:
        print('  youtube_transcript_api not installed, skipping transcript')
        return None
    try:
        parts = YouTubeTranscriptApi.get_transcript(video_id, languages=langs)
        raw = ' '.join(p['text'] for p in parts)
        return raw[:MAX_TRANSCRIPT_CHARS]
    except (TranscriptsDisabled, NoTranscriptFound):
        return None
    except Exception as e:
        print(f'  transcript error ({video_id}): {e}')
        return None

# ── Claude analysis ───────────────────────────────────────────────────────────

ANALYSIS_PROMPT = """你是加密貨幣交易機器人（crypto-bot）的策略分析師。
你正在分析 KOL 的市場觀點影片逐字稿，目的是提取能應用到 monitor_coins.py 的具體建議。

monitor_coins.py 的關鍵參數（供你參考）：
- MIN_SIGNALS = 2           # 觸發進場的最低信號數
- STOP_LOSS_PCT = 0.035     # 固定止損 3.5%
- TP_PCT = 0.07             # 固定止盈 7%
- LEVERAGE = 20             # 槓桿
- MARGIN_BY_SIGNALS = {2:60, 3:80, 4:100}  # 動態保證金
- LEADERBOARD_MIN_PCT = 3.0 # 漲跌幅榜最低 24h 幅度

KOL 頻道：{channel_name}
影片標題：{title}

逐字稿（節選）：
{transcript}

請輸出嚴格的 JSON（不要多餘文字）：
{{
  "market_bias": "bullish" | "bearish" | "neutral",
  "key_insights": ["最多 5 條，具體影響交易的觀點"],
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
- parameter_changes 只填有充分影片依據的建議
- confidence=high 意味著你確信這個改動在當前市況合理
- 沒有建議時用空陣列
- market_bias 根據 KOL 對未來 1-7 天的整體看法判斷"""


def analyze_with_claude(title: str, channel_name: str, transcript: str, client) -> dict:
    prompt = ANALYSIS_PROMPT.format(
        channel_name=channel_name,
        title=title,
        transcript=transcript,
    )
    try:
        resp = client.messages.create(
            model='claude-sonnet-4-6',
            max_tokens=1200,
            messages=[{'role': 'user', 'content': prompt}],
        )
        text = resp.content[0].text.strip()
        # strip markdown code fences if present
        text = re.sub(r'^```(?:json)?\s*', '', text)
        text = re.sub(r'\s*```$', '', text)
        return json.loads(text)
    except json.JSONDecodeError:
        # try to extract JSON object
        m = re.search(r'\{.*\}', text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group())
            except Exception:
                pass
    except Exception as e:
        print(f'  Claude API error: {e}')

    return {
        'market_bias': 'neutral',
        'key_insights': [],
        'parameter_changes': [],
        'logic_suggestions': [],
        'tg_summary': '分析失敗，請查看 notes/youtube-insights.md',
    }

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
        subprocess.run(['git', 'commit', '-m', msg], cwd=REPO_ROOT, check=True, capture_output=True)
        subprocess.run(['git', 'push'], cwd=REPO_ROOT, check=True, capture_output=True)
        return True
    except subprocess.CalledProcessError as e:
        print(f'  Git error: {e.stderr.decode() if e.stderr else e}')
        return False


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print(f'[{now8().strftime("%Y-%m-%d %H:%M +08")}] KOL auto-update started')

    if not ANTHROPIC_KEY:
        print('ERROR: ANTHROPIC_API_KEY not set in environment')
        tg('⚠️ KOL 自動分析失敗：缺少 ANTHROPIC_API_KEY')
        return

    if _anthropic is None:
        print('ERROR: anthropic package not installed. Run: pip install anthropic')
        return

    client = _anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    seen   = load_seen()
    since  = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)

    all_processed     = []
    all_applied_changes = []

    for ch_cfg in KOL_CHANNELS:
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
        print(f'  {len(videos)} new video(s) in last {LOOKBACK_HOURS}h')

        for video in videos:
            vid_id = video['id']
            if vid_id in seen:
                continue

            print(f'  Processing: {video["title"][:60]}')

            transcript = get_transcript(vid_id, langs)
            if not transcript:
                print('    No transcript, skipping')
                seen.add(vid_id)
                continue

            print(f'    Transcript: {len(transcript)} chars → analyzing...')
            analysis = analyze_with_claude(video['title'], name, transcript, client)

            append_to_notes(video, name, analysis)
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
