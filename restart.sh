#!/bin/bash
# Перезапуск бота, чтобы применить изменения. Второй экземпляр не создаёт.
cd "$(dirname "$0")"
mine() { for p in $(pgrep -f "$1"); do [ "$(readlink /proc/$p/cwd)" = "$PWD" ] && echo "$p"; done; }

if flock -n .run.lock true; then
    # сторожа нет — запускаем как при входе в Windows, через Планировщик
    kill $(mine "python bot\.py$") 2>/dev/null   # осиротевший бот без сторожа, если вдруг остался
    sleep 2
    schtasks.exe /end /tn BookBot >/dev/null 2>&1
    schtasks.exe /run /tn BookBot
else
    # сторож работает — просим перезапуск без паузы и гасим его дочерний процесс (бота или паузу)
    touch .restart
    pkill -P "$(cat .run.lock)"
fi
sleep 5
for p in $(mine "run\.sh$|python bot\.py$"); do ps -o pid=,args= -p "$p"; done
