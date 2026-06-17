#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scripts/check_new_kol_videos.py

Lightweight checker: fetch RSS for each KOL channel,
compare against .kol_seen.json, and print any NEW videos.
Exit code 0 = new videos found; exit code 1 = nothing new.

Uses the same flat format as auto_kol_update.py: {"seen": [...video_ids...]}
"""

import sys, json, re, os
from pathlib import Path
from datetime import datetime, timezone, timedelta

import feedparser
import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
SEEN_FILE = REPO_ROOT / 'notes' / '.kol_seen.json'

KOL_CHANNELS = [
    {'handle': '@crypto_punks', 'channel_id': '', 'name': '加密龐克'},
    {'handle': '@BTCfeiyang',   'channel_id': '', 'name': 'BTC飛揚'},
    {'handle': '@BTC-ouyang',   'channel_id': '', 'name': 'BTC歐陽'},
]

def resolve_channel_id(handle):
    url = 'https://www.youtube.com/' + handle
    try:
        r = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=15)
        m = re.search(r'"channelId"\s*:\s*"(UC[A-Za-z0-9_-]{22})"', r.text)
        if m:
            return m.group(1)
        m2 = re.search(r'canonical.*?channel/(UC[A-Za-z0-9_-]{22})', r.text)
        if m2:
            return m2.group(1)
    except Exception as e:
        sys.stderr.write('  resolve_channel_id(%s) failed: %s\n' % (handle, e))
        sys.stderr.flush()
    return None

def load_seen():
    """Load seen video IDs - flat format: {"seen": [...]} same as auto_kol_update.py"""
    if SEEN_FILE.exists():
        try:
            return set(json.loads(SEEN_FILE.read_text(encoding='utf-8')).get('seen', []))
        except Exception:
            pass
    return set()

def now8():
    return datetime.now(timezone(timedelta(hours=8)))

def out(text=''):
    try:
        sys.stdout.write(str(text) + '\n')
        sys.stdout.flush()
    except Exception:
        pass

def main():
    seen = load_seen()
    new_videos = []

    for ch in KOL_CHANNELS:
        cid = ch.get('channel_id') or ''
        if not cid:
            cid = resolve_channel_id(ch['handle']) or ''
            if cid:
                ch['channel_id'] = cid

        if not cid:
            sys.stderr.write('  [%s] channel_id not found, skip\n' % ch['name'])
            sys.stderr.flush()
            continue

        rss_url = 'https://www.youtube.com/feeds/videos.xml?channel_id=' + cid
        try:
            feed = feedparser.parse(rss_url)
        except Exception as e:
            sys.stderr.write('  [%s] RSS error: %s\n' % (ch['name'], e))
            sys.stderr.flush()
            continue

        for entry in feed.entries[:15]:
            vid_id = entry.get('yt_videoid', '')
            if not vid_id:
                continue
            if vid_id not in seen:
                title = entry.get('title', '(no title)')
                url   = entry.get('link', 'https://www.youtube.com/watch?v=' + vid_id)
                pub   = entry.get('published', '')
                new_videos.append((ch['name'], vid_id, title, url, pub))

    if not new_videos:
        out('✅ 沒有新影片')
        sys.exit(1)

    ts = now8().strftime('%Y-%m-%d %H:%M')
    out('\U0001f195 發現 %d 支新影片（%s 台灣時間）:\n' % (len(new_videos), ts))
    for kol, vid_id, title, url, pub in new_videos:
        pub_str = pub[:10] if pub else 'unknown'
        out('  [%s] %s' % (kol, title))
        out('         %s  (%s)' % (url, pub_str))
    out()
    out('-> run: python scripts/auto_kol_update.py')
    sys.exit(0)

if __name__ == '__main__':
    main()
