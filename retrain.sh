#!/bin/bash
# 月度自動重訓腳本
# VPS 使用 cron 每月 1 日凌晨 2:00 執行：
#   0 2 1 * * /root/TradingBot/retrain.sh >> /root/TradingBot/retrain.log 2>&1

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "=========================================="
echo "月度重訓開始：$(date -u '+%Y-%m-%d %H:%M UTC')"
echo "=========================================="

PYTHON="python3"

# BTC 重訓
echo "[BTC] 開始訓練..."
$PYTHON train_wf.py \
    --symbol BTC/USDT \
    --since 2019-10-01 \
    --train_months 18 \
    --threshold 0.53 \
    --min_hold 24 \
    --sizing half_kelly

if [ $? -eq 0 ]; then
    echo "[BTC] 訓練完成"
else
    echo "[BTC] 訓練失敗！"
fi

# ETH 重訓
echo "[ETH] 開始訓練..."
$PYTHON train_wf.py \
    --symbol ETH/USDT \
    --since 2019-10-01 \
    --train_months 15 \
    --balance_classes \
    --threshold 0.53 \
    --min_hold 24 \
    --sizing half_kelly

if [ $? -eq 0 ]; then
    echo "[ETH] 訓練完成"
else
    echo "[ETH] 訓練失敗！"
fi

# 重啟 Bot 使用新模型
echo "重啟 Docker 容器..."
docker compose up -d --build

echo "=========================================="
echo "月度重訓完成：$(date -u '+%Y-%m-%d %H:%M UTC')"
echo "=========================================="
