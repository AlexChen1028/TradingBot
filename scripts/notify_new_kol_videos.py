#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scripts/notify_new_kol_videos.py

每日 KOL 新影片自動偵測 + Telegram 通知（VPS cron 用）。
只讀 YouTube RSS（不抓字幕、不呼叫 Gemini）→ 雲端機房 IP 也能跑。

流程：
  1. 讀 notes/.kol_seen.json（已通知過的 video_id）
  2. 抓各 KOL 頻道 RSS，找出未通知的新影片
  3. 有新片 → 發 Telegram 通知（標題+連結+發布日），並把它們記為已通知
  4. 首次執行（seen 為空）→ 只把目前全部記為已通知 + 發一則「監控已啟動」，不洗版歷史片

通知後 NotebookLM 總結仍為手動步驟；貼進 insight 後由 code 套用流程接手。

Cron（VPS，每天 8:50 / 20:50 台灣 = 00:50 / 12:50 UTC）：
  50 0,12 * * * cd ~/TradingBot && set -a && . ./.env && set +a && /usr/bin/python3 scripts/notify_new_kol_videos.py >> logs/kol_notify.log 2>&1
"""

import os
import sys
import json
import re
from pathlib import Path
from datetime import datetime, timezone, timedelta

import feedparser
import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
SEEN_FILE = REPO_ROOT / 'notes' / '.kol_seen.json'

MONITOR_TOKEN    = os.getenv('MONITOR_TOKEN', '')
MONITOR_CHAT_IDS = [i.strip() for i in os.getenv('MONITOR_CHAT_ID', '').split(',') if i.strip()]

# channel_id 寫死（2026-06-19 解析）：避免每次 scrape youtube.com 被限流導致誤判「無新片」
KOL_CHANNELS = [
    {'handle': '@crypto_punks', 'channel_id': 'UCeeeGbipVKpz23A8_c3I3uA', 'name': '加密龐克'},
    {'handle': '@BTCfeiyang',   'channel_id': 'UCvuvTVzo8W9I6QOyZCXlubg', 'name': 'BTC飛揚'},
    {'handle': '@BTC-ouyang',   'channel_id': 'UCzZ49DculfIZv6W1X81pLlQ', 'name': 'BTC歐陽'},
]

# 每個頻道 RSS 只取最新幾支比對（避免把整個歷史清單都當候選）
MAX_PER_CHANNEL = 8


def now8():
    return datetime.now(timezone(timedelta(hours=8)))


def out(text=''):
    try:
        sys.stdout.write(str(text) + '\n')
        sys.stdout.flush()
    except Exception:
        pass


def tg(text: str):
    """發送 Telegram 通知到 monitor 群（與 monitor_coins.py 同一個 bot/群）。"""
    if not MONITOR_TOKEN or not MONITOR_CHAT_IDS:
        out('  ⚠️ 未設定 MONITOR_TOKEN / MONITOR_CHAT_ID，略過 TG 通知')
        return
    for chat_id in MONITOR_CHAT_IDS:
        try:
            requests.post(
                f'https://api.telegram.org/bot{MONITOR_TOKEN}/sendMessage',
                data={'chat_id': chat_id, 'text': text,
                      'parse_mode': 'HTML', 'disable_web_page_preview': 'true'},
                timeout=15,
            )
        except Exception as e:
            out(f'  ⚠️ TG 發送失敗（{chat_id}）：{e}')


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
        out(f'  resolve_channel_id({handle}) 失敗：{e}')
    return None


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


def fetch_new():
    """回傳 (new_videos, all_ids)。new_videos=[(kol, vid, title, url, pub)]。"""
    seen = load_seen()
    new_videos = []
    all_ids = set()

    for ch in KOL_CHANNELS:
        cid = ch.get('channel_id') or resolve_channel_id(ch['handle']) or ''
        if not cid:
            out(f'  [{ch["name"]}] 找不到 channel_id，略過')
            continue
        rss_url = 'https://www.youtube.com/feeds/videos.xml?channel_id=' + cid
        try:
            feed = feedparser.parse(rss_url)
        except Exception as e:
            out(f'  [{ch["name"]}] RSS 錯誤：{e}')
            continue
        for entry in feed.entries[:MAX_PER_CHANNEL]:
            vid = entry.get('yt_videoid', '')
            if not vid:
                continue
            all_ids.add(vid)
            if vid not in seen:
                new_videos.append((
                    ch['name'], vid,
                    entry.get('title', '(無標題)'),
                    entry.get('link', 'https://www.youtube.com/watch?v=' + vid),
                    (entry.get('published', '') or '')[:10],
                ))
    return seen, new_videos, all_ids


def main():
    out(f'[{now8().strftime("%Y-%m-%d %H:%M +08")}] KOL 新影片偵測開始')
    seen, new_videos, all_ids = fetch_new()

    # 首次執行（seen 為空）→ 種子化，不洗版歷史影片
    if not seen:
        save_seen(all_ids)
        out(f'  首次執行：已記錄 {len(all_ids)} 支現有影片為基準，日後有新片才通知')
        tg(f'✅ <b>KOL 影片監控已啟動</b>\n目前已記錄 {len(all_ids)} 支現有影片為基準。'
           f'\n日後三位 KOL（加密龐克／BTC飛揚／BTC歐陽）有新片，會自動在此通知你。')
        return

    if not new_videos:
        out('  無新影片')
        return

    # 有新片 → 通知 + 記為已通知
    new_videos.sort(key=lambda v: v[4], reverse=True)
    lines = [f'🆕 <b>發現 {len(new_videos)} 支 KOL 新影片</b>（{now8().strftime("%m-%d %H:%M")} 台灣）',
             '去 NotebookLM 總結後貼進 insight，再叫我（或等 9:03 排程）套用到 code：', '']
    for kol, vid, title, url, pub in new_videos:
        lines.append(f'• [{kol}] {title}')
        lines.append(f'  {url}  （{pub}）')
    tg('\n'.join(lines))
    out(f'  已通知 {len(new_videos)} 支新影片')

    seen.update(v[1] for v in new_videos)
    save_seen(seen)


if __name__ == '__main__':
    main()
