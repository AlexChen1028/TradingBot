# -*- coding: utf-8 -*-
"""
scripts/kol_fetch.py — 本機（住宅 IP）抓 KOL 新影片逐字稿，給 Claude 自動總結用。

雲端 VPS 抓不到字幕（機房 IP 被 YouTube 封），但本機住宅 IP 可以。
本腳本只做「確定性」的抓取，總結交給 Claude（cowork）。

模式：
  python kol_fetch.py            # 偵測新片→抓逐字稿→寫 notes/.kol_pending.json（不動 seen）
  python kol_fetch.py --mark vid1,vid2   # 成功套用後，把這些 vid 記為已處理（寫入 .kol_seen.json）

seen 檔 notes/.kol_seen.json 為「本機執行期狀態」，不進 git（各機獨立）。
"""
import os
import sys
import json
import re
from pathlib import Path
from datetime import datetime, timezone, timedelta

import requests

# Windows 主控台預設 cp950，印中文標題會 UnicodeEncodeError → 強制 utf-8
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

try:
    import feedparser
except Exception as e:
    print(json.dumps({'error': 'feedparser missing: %s' % e}))
    sys.exit(0)

try:
    from youtube_transcript_api import YouTubeTranscriptApi
except Exception:
    YouTubeTranscriptApi = None

REPO_ROOT    = Path(__file__).resolve().parent.parent
SEEN_FILE    = REPO_ROOT / 'notes' / '.kol_seen.json'
PENDING_FILE = REPO_ROOT / 'notes' / '.kol_pending.json'

# channel_id 寫死（2026-06-19 解析）：避免每次 scrape youtube.com 首頁被限流 →
# 否則解析失敗會誤回 pending=0（假「沒新片」）。RSS 直接用 channel_id 穩定可靠。
KOL_CHANNELS = [
    {'handle': '@crypto_punks', 'name': '加密龐克', 'channel_id': 'UCeeeGbipVKpz23A8_c3I3uA'},
    {'handle': '@BTCfeiyang',   'name': 'BTC飛揚',  'channel_id': 'UCvuvTVzo8W9I6QOyZCXlubg'},
    {'handle': '@BTC-ouyang',   'name': 'BTC歐陽',  'channel_id': 'UCzZ49DculfIZv6W1X81pLlQ'},
]
WANT_LANGS      = ['zh-TW', 'zh-Hant', 'zh', 'zh-Hans', 'en']
MAX_PER_CHANNEL = 6
MAX_TRANSCRIPT  = 14000   # 截斷過長逐字稿


def load_seen():
    if SEEN_FILE.exists():
        try:
            return set(json.loads(SEEN_FILE.read_text(encoding='utf-8')).get('seen', []))
        except Exception:
            pass
    return set()


def save_seen(seen):
    SEEN_FILE.parent.mkdir(exist_ok=True)
    SEEN_FILE.write_text(json.dumps({'seen': sorted(seen)}, indent=2), encoding='utf-8')


def resolve_channel_id(handle):
    try:
        r = requests.get('https://www.youtube.com/' + handle,
                         headers={'User-Agent': 'Mozilla/5.0'}, timeout=15)
        m = re.search(r'"channelId"\s*:\s*"(UC[A-Za-z0-9_-]{22})"', r.text)
        if m:
            return m.group(1)
    except Exception:
        pass
    return None


def get_transcript(vid):
    if YouTubeTranscriptApi is None:
        return None
    try:
        api = YouTubeTranscriptApi()
        tl  = api.list(vid)
        try:
            tr = tl.find_transcript(WANT_LANGS)
        except Exception:
            tr = next(iter(tl))
        data = tr.fetch()
        segs = [getattr(s, 'text', None) or (s.get('text', '') if isinstance(s, dict) else '') for s in data]
        return ' '.join(t for t in segs if t)[:MAX_TRANSCRIPT]
    except AttributeError:
        try:
            data = YouTubeTranscriptApi.get_transcript(vid, languages=WANT_LANGS)
            return ' '.join(d['text'] for d in data)[:MAX_TRANSCRIPT]
        except Exception:
            return None
    except Exception:
        return None


def cmd_mark(ids_csv):
    ids = [i.strip() for i in ids_csv.split(',') if i.strip()]
    seen = load_seen()
    seen.update(ids)
    save_seen(seen)
    print('marked %d seen, total=%d' % (len(ids), len(seen)))


def cmd_detect():
    seen = load_seen()
    pending = []
    for ch in KOL_CHANNELS:
        cid = ch.get('channel_id') or resolve_channel_id(ch['handle'])
        if not cid:
            print('  [%s] 無 channel_id（解析失敗），略過' % ch['name'])
            continue
        feed = feedparser.parse('https://www.youtube.com/feeds/videos.xml?channel_id=' + cid)
        for e in feed.entries[:MAX_PER_CHANNEL]:
            vid = e.get('yt_videoid', '')
            if not vid or vid in seen:
                continue
            txt = get_transcript(vid)
            pending.append({
                'kol':   ch['name'],
                'vid':   vid,
                'title': e.get('title', '(無標題)'),
                'url':   e.get('link', 'https://www.youtube.com/watch?v=' + vid),
                'date':  (e.get('published', '') or '')[:10],
                'transcript_ok': bool(txt),
                'transcript': txt or '',
            })
    PENDING_FILE.write_text(json.dumps(pending, ensure_ascii=False, indent=2), encoding='utf-8')
    ok = sum(1 for p in pending if p['transcript_ok'])
    print('pending=%d (transcript_ok=%d) -> %s' % (len(pending), ok, PENDING_FILE.name))
    for p in pending:
        print('  [%s] %s %s  字幕=%s' % (p['date'], p['kol'], p['title'][:40], '有' if p['transcript_ok'] else '無'))


if __name__ == '__main__':
    if len(sys.argv) > 2 and sys.argv[1] == '--mark':
        cmd_mark(sys.argv[2])
    else:
        cmd_detect()
