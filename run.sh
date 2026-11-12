#!/bin/bash
# Сторож бота: держит ровно один экземпляр и перезапускает его, если процесс завершился.
# Запускается Планировщиком Windows при входе; второй экземпляр сторожа сразу выходит (flock).
cd "$(dirname "$0")"
log() { echo "$(date '+%F %T') run.sh: $*" >> bot.log; }

exec 9<>.run.lock
if ! flock -n 9; then
    log "сторож уже запущен (pid $(cat .run.lock)), второй не нужен"
    exit 0
fi
echo $$ > .run.lock   # pid сторожа — для restart.sh и stop.sh

delay=10              # пауза перед перезапуском: 10 с → … → 5 мин, если бот падает сразу после старта
while true; do
    rm -f .restart
    started=$(date +%s)
    .venv/bin/python bot.py >> bot.log 2>&1 9>&-   # 9>&- — бот не держит блокировку сторожа
    code=$?
    if [ -f .restart ]; then   # ./restart.sh попросил перезапуск — без паузы
        log "перезапуск по restart.sh"
        delay=10
        continue
    fi
    [ $(( $(date +%s) - started )) -ge 60 ] && delay=10   # поработал нормально — пауза снова короткая
    log "бот завершился (код $code), перезапуск через $delay с"
    sleep $delay
    delay=$(( delay * 2 > 300 ? 300 : delay * 2 ))
done
