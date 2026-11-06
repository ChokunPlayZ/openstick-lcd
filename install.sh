#!/bin/sh
# Explicit on-device installation; never changes the DTB or network settings.
set -eu
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if [ "$(id -u)" -ne 0 ]; then
    echo "Run on the MF800H as root: sudo sh install.sh" >&2
    exit 1
fi
test -r /proc/device-tree/model
MODEL=$(tr -d '\000' < /proc/device-tree/model)
case "$MODEL" in
    *MF800H*) ;;
    *) echo "Refusing non-MF800H hardware: $MODEL" >&2; exit 1 ;;
esac
test -e /dev/fb0
test -e /sys/class/leds/mf800h:lcd/brightness
test "$(cat /sys/class/graphics/fb0/virtual_size)" = "128,128"
test "$(cat /sys/class/graphics/fb0/bits_per_pixel)" = "32"
command -v systemctl >/dev/null
command -v systemd-inhibit >/dev/null
python3 -c 'from PIL import Image, ImageFont; import qrcode'
test -f /usr/share/fonts/truetype/dejavu/DejaVuSans.ttf
test -f /usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf
for NAME in openstick_device.py lcd_display.py lcd_telemetry.py openstick-device.service README.md; do
    test -f "$HERE/$NAME"
done
BACKUP=$(mktemp -d /var/backups/openstick-device-XXXXXXXX)
for PATHNAME in /usr/local/lib/openstick-device /etc/systemd/system/openstick-device.service; do
    if [ -e "$PATHNAME" ]; then
        cp -pR "$PATHNAME" "$BACKUP/"
    fi
done
echo "Previous runtime/unit saved in $BACKUP (configuration is left untouched)."
if systemctl cat openstick-device.service >/dev/null 2>&1; then
    systemctl stop openstick-device.service
fi
if systemctl cat mf800h-display.service >/dev/null 2>&1; then
    systemctl disable --now mf800h-display.service
fi
install -d -m 700 /etc/openstick-device /etc/openstick-device/macros
install -d -m 755 /usr/local/lib/openstick-device /usr/local/share/openstick-device
for NAME in openstick_device.py lcd_display.py lcd_telemetry.py; do
    install -m 644 "$HERE/$NAME" "/usr/local/lib/openstick-device/$NAME"
done
install -m 644 "$HERE/README.md" /usr/local/share/openstick-device/README.md
install -m 644 "$HERE/openstick-device.service" /etc/systemd/system/openstick-device.service
systemctl daemon-reload
systemctl enable openstick-device.service
systemctl restart openstick-device.service
systemctl is-active openstick-device.service
