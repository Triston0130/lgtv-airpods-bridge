#!/bin/sh
# wpa_cli -a callback. A short delay avoids interrupting a normal roam.
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
    sleep 2
    state=$(timeout -t 3 wpa_cli -i wlan0 status 2>/dev/null | sed -n 's/^wpa_state=//p' | head -1)
    case "$state" in
      COMPLETED)
        log "association recovered before reconnect"
        rm -f "$EVENT"
        ;;
      DISCONNECTED|INACTIVE)
        log "event reconnect; state=$state"
        timeout -t 3 wpa_cli -i wlan0 reconnect >> "$DIR/events.log" 2>&1
        ;;
      *) log "event state=${state:-unknown}; watchdog will verify" ;;
    esac
    ;;
esac
