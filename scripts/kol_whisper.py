# -*- coding: utf-8 -*-
"""
scripts/kol_whisper.py — 對「關閉字幕」的 KOL 影片：下載音訊 → faster-whisper 轉文字。

供 kol_fetch.py 在 youtube_transcript_api 抓不到字幕時後備呼叫（飛揚/歐陽 關了字幕）。
不需系統 ffmpeg：yt-dlp 抓 bestaudio 原始檔，faster-whisper 用內建 PyAV (av) 解碼。

CLI（測試用）：python kol_whisper.py <video_id 或 url>
"""
import os
import sys
import tempfile
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

MODEL_SIZE   = os.getenv('KOL_WHISPER_MODEL', 'small')   # base/small/medium
MAX_CHARS    = 14000
_model = None


def _get_model():
    global _model
    if _model is None:
        from faster_whisper import WhisperModel
        # CPU + int8：免 GPU，速度可接受
        _model = WhisperModel(MODEL_SIZE, device='cpu', compute_type='int8')
    return _model


def _download_audio(video_id, dest_dir):
    import yt_dlp
    url = video_id if video_id.startswith('http') else 'https://www.youtube.com/watch?v=' + video_id
    outtmpl = str(Path(dest_dir) / '%(id)s.%(ext)s')
    opts = {
        'format': 'bestaudio/best',
        'outtmpl': outtmpl,
        'quiet': True,
        'no_warnings': True,
        'noplaylist': True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        return ydl.prepare_filename(info)


def transcribe(video_id):
    """回傳逐字稿字串；失敗回 None。"""
    try:
        with tempfile.TemporaryDirectory() as td:
            audio = _download_audio(video_id, td)
            if not audio or not os.path.exists(audio):
                return None
            model = _get_model()
            segments, info = model.transcribe(audio, language='zh', vad_filter=True)
            text = ' '.join(s.text.strip() for s in segments if s.text)
            return text[:MAX_CHARS] or None
    except Exception as e:
        sys.stderr.write('whisper transcribe(%s) 失敗: %s\n' % (video_id, e))
        return None


if __name__ == '__main__':
    vid = sys.argv[1] if len(sys.argv) > 1 else ''
    t = transcribe(vid)
    if t:
        print('LEN=%d' % len(t))
        print(t[:600])
    else:
        print('FAIL')
