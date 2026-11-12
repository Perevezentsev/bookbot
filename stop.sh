#!/bin/bash
# Полная остановка: сторож (чтобы не перезапустил) и бот. Запуск снова — ./restart.sh.
cd "$(dirname "$0")"
mine() { for p in $(pgrep -f "$1"); do [ "$(readlink /proc/$p/cwd)" = "$PWD" ] && echo "$p"; done; }

if ! flock -n .run.lock true; then
    pid=$(cat .run.lock)
    kill "$pid"            # сначала сторож — чтобы не перезапустил бота
fi
kill $(mine "python bot\.py$") 2>/dev/null
sleep 3
schtasks.exe /end /tn BookBot >/dev/null 2>&1   # чтобы Планировщик не считал задачу запущенной
left=$(mine "run\.sh$|python bot\.py$")
if [ -n "$left" ]; then echo "Ещё работает:"; ps -o pid=,args= -p $left; else echo "Бот остановлен"; fi
