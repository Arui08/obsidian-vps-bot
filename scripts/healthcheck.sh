#!/bin/bash
# 每小时跑一次：bot.py 进程不在就 systemctl restart obsidian-bot
LOG=/opt/obsidian-bot/logs/healthcheck.log
ts() { date '+%Y-%m-%d %H:%M:%S'; }
if ! pgrep -f '/opt/obsidian-bot/bot.py' > /dev/null; then
    echo "[$(ts)] bot.py 进程不在，systemctl restart obsidian-bot" >> $LOG
    systemctl restart obsidian-bot
    sleep 5
    if pgrep -f '/opt/obsidian-bot/bot.py' > /dev/null; then
        echo "[$(ts)] 重启成功" >> $LOG
    else
        echo "[$(ts)] 重启失败！" >> $LOG
    fi
fi
