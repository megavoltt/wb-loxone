#!/bin/sh
# Установка и обновление моста WB → Loxone на контроллере Wiren Board.
#
#   wget -qO- https://raw.githubusercontent.com/megavoltt/wb-loxone/main/install.sh | sh
#
# Код и настройки — в /mnt/data: переживают обновление прошивки. В корневой ФС
# только файл службы; после перепрошивки FIT запустить установку ещё раз.
# Повторный запуск = обновление: настройки и выбор каналов сохраняются.
set -e
REPO=${WBLOX_REPO:-megavoltt/wb-loxone}
BRANCH=${WBLOX_BRANCH:-main}
DIR=/mnt/data/wb-loxone
FILES="wbloxone.py index.html install.sh"

[ "$(id -u)" = 0 ] || { echo "Запустите от root"; exit 1; }
command -v python3 >/dev/null || { echo "Нет python3 — это точно контроллер Wiren Board?"; exit 1; }
mkdir -p "$DIR" /mnt/data/etc

# Запуск из распакованного архива — копируем рядом лежащие файлы, иначе качаем с GitHub
SRC=$(cd "$(dirname "$0")" 2>/dev/null && pwd || true)
if [ -n "$SRC" ] && [ -f "$SRC/wbloxone.py" ] && [ -f "$SRC/index.html" ]; then
    if [ "$SRC" != "$DIR" ]; then
        for f in $FILES; do cp "$SRC/$f" "$DIR/"; done
    fi
    echo "Файлы взяты из $SRC"
else
    URL=https://raw.githubusercontent.com/$REPO/$BRANCH
    for f in $FILES; do
        wget -qO "$DIR/$f.new" "$URL/$f" || { echo "Не скачался $URL/$f"; rm -f "$DIR"/*.new; exit 1; }
    done
    for f in $FILES; do mv "$DIR/$f.new" "$DIR/$f"; done
    echo "Файлы скачаны с github.com/$REPO ($BRANCH)"
fi
chmod +x "$DIR/wbloxone.py" "$DIR/install.sh"

cat > /etc/systemd/system/wb-loxone.service <<EOF
[Unit]
Description=WB to Loxone bridge
After=mosquitto.service network-online.target
Wants=mosquitto.service

[Service]
ExecStart=/usr/bin/python3 $DIR/wbloxone.py
Environment=WBLOX_CONFIG=/mnt/data/etc/wb-loxone.json
Restart=always
RestartSec=5
Nice=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable wb-loxone >/dev/null 2>&1
systemctl restart wb-loxone
sleep 2
if systemctl is-active --quiet wb-loxone; then
    PORT=$(python3 -c "import json;print(json.load(open('/mnt/data/etc/wb-loxone.json')).get('http_port',8099))" 2>/dev/null || echo 8099)
    IP=$(hostname -I | awk '{print $1}')
    echo "Готово: $(python3 "$DIR/wbloxone.py" --version 2>/dev/null) — откройте http://$IP:$PORT"
else
    echo "Служба не запустилась:"; journalctl -u wb-loxone -n 20 --no-pager; exit 1
fi
