#!/bin/sh
# wpa_cli -a callback. Return immediately so later association events are not held up.
DIR=/var/lib/webosbrew/wifi-watchdog
EVENT=/tmp/wifi-watchdog-disconnected-at
SEEN=/tmp/wifi-watchdog-seen-up
log() { echo "$(date '+%F %T') $*" >> "$DIR/events.log"; }

[ "$1" = wlan0 ] || exit 0
case "$2" in
  CONNECTED)
    if [ -f "$EVENT" ]; then
      log "association restored"
      rm -f "$EVENT"
    fi
    ;;
  DISCONNECTED)
    [ -f "$SEEN" ] || exit 0
    date +%s > "$EVENT"
    log "association lost (event)"
    ;;
esac
