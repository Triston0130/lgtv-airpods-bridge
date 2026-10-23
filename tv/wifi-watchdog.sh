#!/bin/sh
# TV Wi-Fi watchdog: when the link dies, save evidence persistently, then recover without a power cycle.
# Levels: 1) wlan0 down/up  2) re-authorize the USB Wi-Fi device (same as replugging it).
DIR=/var/lib/webosbrew/wifi-watchdog
WLPRIV=$DIR/wlpriv
mkdir -p $DIR
log() { echo "$(date '+%F %T') $*" >> $DIR/events.log; }
gateway() { ip route | awk '/^default/{print $3; exit}'; }
alive() { G=$(gateway); [ -n "$G" ] && ping -c 1 -W 2 "$G" >/dev/null 2>&1; }
usbdev() { for d in /sys/bus/usb/devices/*; do [ "$(cat $d/idVendor 2>/dev/null):$(cat $d/idProduct 2>/dev/null)" = "043e:3107" ] && echo $d; done; }
evidence() {
  F=$DIR/drop-$(date +%Y%m%d-%H%M%S).txt
  { echo "== $1"; uptime; free; ip addr show wlan0; ip route; [ -x $WLPRIV ] && $WLPRIV wlan0 stat; cat /proc/net/wireless; dmesg | tail -300; } > $F 2>&1
  ls -1t $DIR/drop-*.txt 2>/dev/null | tail -n +11 | xargs rm -f 2>/dev/null
}
sleep 180
seen_up=0; down_since=0; last_reset=0
log "started"
while :; do
  now=$(date +%s)
  if alive; then
    [ $down_since -ne 0 ] && log "link back after $((now-down_since))s"
    seen_up=1; down_since=0
  elif [ $seen_up -eq 1 ]; then
    [ $down_since -eq 0 ] && { down_since=$now; log "gateway unreachable"; }
    if [ $((now-down_since)) -ge 30 ] && [ $((now-last_reset)) -ge 600 ]; then
      evidence "link down $((now-down_since))s"
      log "level 1: wlan0 down/up"; ifconfig wlan0 down; sleep 2; ifconfig wlan0 up
      for i in 1 2 3 4 5 6 7 8 9 10 11 12; do sleep 5; alive && break; done
      if ! alive; then
        D=$(usbdev)
        log "level 2: re-authorize USB ${D:-not-found}"
        [ -n "$D" ] && { echo 0 > $D/authorized; sleep 3; echo 1 > $D/authorized; }
      fi
      last_reset=$(date +%s)
    fi
  fi
  sleep 5
done
