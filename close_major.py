"""
緊急平倉腳本：強制平掉交易所上的 BTC / ETH / SOL 倉位。
positions_altcoin.json 無需有記錄，直接從交易所查詢並市價平倉。

VPS 上跑法：
  docker compose exec coin-monitor python close_major.py
  docker compose exec coin-monitor python close_major.py --dry    # 只列出，不下單
  docker compose exec coin-monitor python close_major.py --symbols BTC ETH SOL
"""

import os, sys, json, time, argparse
import ccxt
from dotenv import load_dotenv

load_dotenv()

WATCH_ALWAYS  = ['BTC/USDT:USDT', 'ETH/USDT:USDT', 'SOL/USDT:USDT']
TAKER_FEE     = 0.0005
MAJOR_LEVERAGE = 50
POSITIONS_FILE = 'positions_altcoin.json'

TG_TOKEN   = os.getenv('TELEGRAM_TOKEN', '')
TG_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '')

def tg(msg):
    if not TG_TOKEN or not TG_CHAT_ID:
        return
    import requests
    try:
        requests.post(
            f'https://api.telegram.org/bot{TG_TOKEN}/sendMessage',
            json={'chat_id': TG_CHAT_ID, 'text': msg, 'parse_mode': 'HTML'},
            timeout=10,
        )
    except Exception:
        pass


def detect_hedge_mode(ex):
    try:
        resp = ex.fapiPrivateGetPositionSideDual()
        hedge = bool(resp.get('dualSidePosition', False))
    except Exception as e:
        print(f'  ⚠️  無法偵測持倉模式 ({e})，預設單向')
        hedge = False
    print(f'  持倉模式：{"雙向 Hedge ⚠️" if hedge else "單向 One-way ✅"}')
    return hedge


def close_params(direction, hedge):
    if hedge:
        return {'positionSide': 'LONG' if direction == 1 else 'SHORT'}
    return {'reduceOnly': True}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry', action='store_true', help='試跑：只列出，不下單')
    ap.add_argument('--symbols', nargs='+', default=['BTC', 'ETH', 'SOL'],
                    help='要平倉的幣（預設 BTC ETH SOL）')
    args = ap.parse_args()

    target_coins = set(args.symbols)
    target_symbols = [s for s in WATCH_ALWAYS if s.split('/')[0] in target_coins]

    ex = ccxt.binance({
        'apiKey':          os.getenv('BINANCE_API_KEY', ''),
        'secret':          os.getenv('BINANCE_SECRET_KEY', ''),
        'enableRateLimit': True,
        'options':         {'defaultType': 'future'},
    })
    if os.getenv('DEMO_MODE', 'true').lower() == 'true':
        ex.enable_demo_trading(True)
        print('[DEMO MODE]')
    else:
        print('[LIVE MODE]')

    print(f'目標幣種：{", ".join(target_coins)}')
    print('=' * 60)

    hedge = detect_hedge_mode(ex)

    # ── 從交易所查詢所有持倉 ──────────────────────────────────────
    try:
        all_positions = ex.fetch_positions()
    except Exception as e:
        print(f'❌ 無法取得倉位：{e}')
        sys.exit(1)

    active = [
        p for p in all_positions
        if p.get('symbol') in target_symbols and abs(p.get('contracts') or 0) > 0
    ]

    if not active:
        print('交易所上沒有找到目標倉位，已結束。')
        return

    # ── 逐一平倉 ──────────────────────────────────────────────────
    for p in active:
        symbol = p['symbol']
        coin   = symbol.split('/')[0]
        side   = p['side']           # 'long' or 'short'
        d      = 1 if side == 'long' else -1
        amt    = abs(float(p.get('contracts') or 0))
        ep     = float(p.get('entryPrice') or 0)
        side_str = 'LONG' if d == 1 else 'SHORT'

        print(f'\n{coin} {side_str}  qty={amt}  entry={ep:.4f}')

        # 取消此交易對的所有掛單（SL/TP）
        try:
            open_orders = ex.fetch_open_orders(symbol)
            for o in open_orders:
                try:
                    ex.cancel_order(o['id'], symbol)
                    print(f'  取消掛單 {o["id"]}')
                except Exception:
                    pass
        except Exception as e:
            print(f'  ⚠️  取消掛單失敗：{e}')

        if args.dry:
            print(f'  [DRY] 不下單，跳過')
            continue

        close_fn = ex.create_market_sell_order if d == 1 else ex.create_market_buy_order

        # 第一次嘗試：帶 reduceOnly / positionSide
        try:
            close_fn(symbol, amt, params=close_params(d, hedge))
            print(f'  ✅ 平倉成功（reduceOnly / positionSide）')
        except Exception as e1:
            print(f'  ⚠️  第一次失敗 ({e1})，改用純市價單…')
            # 第二次嘗試：不帶任何 params
            try:
                close_fn(symbol, amt)
                print(f'  ✅ 平倉成功（純市價）')
            except Exception as e2:
                print(f'  ❌ 平倉失敗：{e2}')
                tg(f'❌ <b>{side_str} {coin} 平倉失敗</b>\n{e2}')
                continue

        # ── 計算損益 & 發 TG ─────────────────────────────────────
        try:
            price_now = float(ex.fetch_ticker(symbol)['last'])
        except Exception:
            price_now = ep

        pnl_pct  = (price_now - ep) / ep * d * 100 * MAJOR_LEVERAGE if ep else 0
        pnl_usdt = amt * (price_now - ep) * d
        fee_usdt = amt * (ep + price_now) * TAKER_FEE
        net_usdt = pnl_usdt - fee_usdt

        print(f'  進場 {ep:.4f} → 現價 {price_now:.4f}')
        print(f'  保證金損益 {pnl_pct:+.2f}%  淨利 {net_usdt:+.2f} U')

        tg(
            f'🔒 <b>強制平倉 {side_str} {coin}</b>\n'
            f'進場：{ep:.4f} → 現價：{price_now:.4f}\n'
            f'保證金盈虧：{pnl_pct:+.2f}%\n'
            f'毛利：{pnl_usdt:+.2f} U  手續費：-{fee_usdt:.2f} U\n'
            f'<b>淨利：{net_usdt:+.2f} U</b>  原因：緊急手動平倉'
        )

        time.sleep(0.5)

    # ── 清理本地 JSON（如果有記錄的話）──────────────────────────
    try:
        with open(POSITIONS_FILE, 'r', encoding='utf-8') as f:
            local_pos = json.load(f)
        changed = False
        for sym in target_symbols:
            if sym in local_pos:
                del local_pos[sym]
                changed = True
                print(f'\n  🗑️  從本地 JSON 移除 {sym}')
        if changed:
            with open(POSITIONS_FILE, 'w', encoding='utf-8') as f:
                json.dump(local_pos, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

    print('\n' + '=' * 60)
    print('完成。')


if __name__ == '__main__':
    main()
