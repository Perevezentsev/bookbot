#!/bin/bash
pkill -f "python bot.py"
sleep 2
schtasks.exe /end /tn BookBot >/dev/null 2>&1
schtasks.exe /run /tn BookBot
sleep 4
pgrep -af "python bot.py"
