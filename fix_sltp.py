"""
一次掃描所有開倉，幫沒有 SL / TP 的倉位補上交易所層級的止損止盈。
適用於：
  - 從 Binance UI 手動開的倉
  - bot 開倉時 SL 訂單失敗的倉
  - 從舊版 bot 留下來沒掛 SL 的倉

用法：
  docker compose exec trading-bot python fix_sltp.py          # 預設 3% SL / 5% TP
  docker compose exec trading-bot python fix_sltp.py --sl 0.02 --tp 0.04
  docker compose exec trading-bot python fix_sltp.py --dry    # 只列出，不下單
"""

import os
import sys
import time
import argparse
import ccxt


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--sl', type=float, default=0.03, help='止損 % (預設 0.03 = 3%)')
    ap.add_argument('--tp', type=float, default=0.05, help='止盈 % (預設 0.05 = 5%)')
    ap.add_argument('--dry', action='store_true', help='試跑模式，不實際下單')
    args = ap.parse_args()

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

    print(f'SL: {args.sl*100:.1f}%  TP: {args.tp*100:.1f}%')
    print('=' * 60)

    positions = ex.fetch_positions()
    fixed = skipped = 0

    for p in positions:
        contracts = abs(p.get('contracts') or 0)
        if contracts <= 0:
            continue

        symbol = p['symbol']
        side   = p['side']
        entry  = float(p['entryPrice'])
        direction = 1 if side == 'long' else -1
        sl_side   = 'sell' if direction == 1 else 'buy'

        # 檢查現有掛單
        try:
            open_orders = ex.fetch_open_orders(symbol)
        except Exception as e:
            print(f'⚠️  {symbol}: fetch_open_orders 失敗: {e}')
            continue

        has_sl = any(
            o.get('type', '').lower() in ('stop_market', 'stop', 'trailing_stop_market')
            for o in open_orders
        )
        has_tp = any(
            o.get('type', '').lower() in ('take_profit_market', 'take_profit')
            for o in open_orders
        )

        coin = symbol.split('/')[0]
        print(f'\n{coin:<8s} {side.upper():5s} {contracts:>10g} @ {entry:.6g}'
              f'   SL={"✅" if has_sl else "❌"}  TP={"✅" if has_tp else "❌"}')

        if has_sl and has_tp:
            print('  → 已齊全，跳過')
            skipped += 1
            continue

        if not has_sl:
            sl_price = entry * (1 - args.sl) if direction == 1 else entry * (1 + args.sl)
            sl_price = round(sl_price, 6)
            if args.dry:
                print(f'  [DRY] 會掛 SL @ {sl_price}')
            else:
                try:
                    ex.create_order(symbol, 'stop_market', sl_side, contracts, None, {
                        'stopPrice':     sl_price,
                        'closePosition': True,
                        'workingType':   'MARK_PRICE',
                    })
                    print(f'  ✅ SL 掛上 @ {sl_price}')
                except Exception as e:
                    print(f'  ❌ SL 失敗: {e}')

        if not has_tp:
            tp_price = entry * (1 + args.tp) if direction == 1 else entry * (1 - args.tp)
            tp_price = round(tp_price, 6)
            if args.dry:
                print(f'  [DRY] 會掛 TP @ {tp_price}')
            else:
                try:
                    ex.create_order(symbol, 'take_profit_market', sl_side, contracts, None, {
                        'stopPrice':     tp_price,
                        'closePosition': True,
                        'workingType':   'MARK_PRICE',
                    })
                    print(f'  ✅ TP 掛上 @ {tp_price}')
                except Exception as e:
                    print(f'  ❌ TP 失敗: {e}')

        fixed += 1
        time.sleep(0.3)

    print('\n' + '=' * 60)
    print(f'處理：{fixed} 個倉位  跳過：{skipped} 個（已齊全）')


if __name__ == '__main__':
    main()
