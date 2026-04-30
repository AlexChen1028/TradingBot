"""
社群情緒分析模組
來源：Reddit (r/CryptoCurrency, r/CryptoMoonShots, r/altcoin) + CoinGecko Trending

用法：
  from social_sentiment import get_coin_sentiment, get_trending_coins

  score = get_coin_sentiment('SOL')   # -1.0 ~ +1.0
  trending = get_trending_coins()     # ['BTC', 'ETH', 'SOL', ...]
"""

import re
import time
import requests
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

_analyzer = SentimentIntensityAnalyzer()

REDDIT_HEADERS = {
    'User-Agent': 'TradingBot/2.0 (crypto sentiment scraper)',
    'Accept': 'application/json',
}

SUBREDDITS = [
    'CryptoCurrency',
    'CryptoMoonShots',
    'altcoin',
    'binance',
]

# 快取：避免短時間重複抓取
_cache: dict = {}
_CACHE_TTL = 900  # 15 分鐘


def _fetch_reddit(subreddit: str, limit: int = 25) -> list[dict]:
    url = f'https://www.reddit.com/r/{subreddit}/hot.json?limit={limit}'
    try:
        r = requests.get(url, headers=REDDIT_HEADERS, timeout=10)
        if r.status_code == 200:
            posts = r.json()['data']['children']
            return [
                {
                    'title': p['data'].get('title', ''),
                    'text':  p['data'].get('selftext', '')[:500],
                    'score': p['data'].get('score', 0),
                    'comments': p['data'].get('num_comments', 0),
                }
                for p in posts
            ]
    except Exception:
        pass
    return []


def _get_all_posts(max_age_secs: int = _CACHE_TTL) -> list[dict]:
    now = time.time()
    if 'posts' in _cache and now - _cache.get('posts_ts', 0) < max_age_secs:
        return _cache['posts']

    posts = []
    for sub in SUBREDDITS:
        posts.extend(_fetch_reddit(sub, limit=25))
        time.sleep(0.5)

    _cache['posts'] = posts
    _cache['posts_ts'] = now
    return posts


def get_trending_coins(max_age_secs: int = _CACHE_TTL) -> list[str]:
    """
    從 CoinGecko 取得熱搜幣種（symbol 列表，大寫）。
    """
    now = time.time()
    if 'trending' in _cache and now - _cache.get('trending_ts', 0) < max_age_secs:
        return _cache['trending']

    try:
        r = requests.get(
            'https://api.coingecko.com/api/v3/search/trending',
            timeout=10,
        )
        if r.status_code == 200:
            coins = [
                c['item']['symbol'].upper()
                for c in r.json().get('coins', [])
            ]
            _cache['trending'] = coins
            _cache['trending_ts'] = now
            return coins
    except Exception:
        pass
    return []


def get_coin_sentiment(symbol: str, max_age_secs: int = _CACHE_TTL) -> float:
    """
    計算指定幣種在 Reddit 上的情緒分數。

    Returns:
        float: -1.0（極度負面）~ +1.0（極度正面），0.0 = 無提及或中性
    """
    symbol = symbol.upper()
    cache_key = f'sent_{symbol}'
    now = time.time()
    if cache_key in _cache and now - _cache.get(f'{cache_key}_ts', 0) < max_age_secs:
        return _cache[cache_key]

    posts = _get_all_posts()
    scores = []

    # 用正則找提及：$SOL、SOL/USDT、#SOL、"SOL" 等
    patterns = [
        rf'\${re.escape(symbol)}\b',
        rf'\b{re.escape(symbol)}/USDT\b',
        rf'#{re.escape(symbol)}\b',
        rf'\b{re.escape(symbol)}\b',
    ]

    for post in posts:
        text = f"{post['title']} {post['text']}"
        mentioned = any(re.search(p, text, re.IGNORECASE) for p in patterns)
        if not mentioned:
            continue

        # 找提及上下文（前後 100 字）
        context = ''
        for p in patterns:
            m = re.search(p, text, re.IGNORECASE)
            if m:
                start = max(0, m.start() - 100)
                end   = min(len(text), m.end() + 100)
                context += text[start:end] + ' '

        vader = _analyzer.polarity_scores(context)['compound']
        weight = 1 + min(post['score'] / 100, 3)  # 高讚文章權重更高
        scores.append(vader * weight)

    score = sum(scores) / len(scores) if scores else 0.0
    _cache[cache_key] = score
    _cache[f'{cache_key}_ts'] = now
    return score


def get_social_signal(symbol: str) -> dict:
    """
    綜合社群信號：情緒分數 + 是否在 CoinGecko 趨勢榜。

    Returns dict:
        sentiment  : float  -1~1
        trending   : bool
        signal     : int    1=偏多, -1=偏空, 0=中性
        detail     : str
    """
    symbol  = symbol.split('/')[0].upper()
    score   = get_coin_sentiment(symbol)
    trending_list = get_trending_coins()
    is_trending = symbol in trending_list

    # 信號邏輯
    if score > 0.3 or is_trending:
        signal = 1
    elif score < -0.3:
        signal = -1
    else:
        signal = 0

    detail = f"sentiment={score:+.2f}  trending={'Yes' if is_trending else 'No'}"
    return {
        'sentiment':   score,
        'trending':    is_trending,
        'signal':      signal,
        'detail':      detail,
    }


if __name__ == '__main__':
    print("測試中...")
    trending = get_trending_coins()
    print(f"CoinGecko 熱搜：{trending}")

    for sym in ['BTC', 'ETH', 'SOL', 'PEPE', 'DOGE']:
        result = get_social_signal(sym)
        arrow = '🟢' if result['signal'] == 1 else ('🔴' if result['signal'] == -1 else '⚪')
        print(f"{arrow} {sym:6s} | {result['detail']}")
