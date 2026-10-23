#!/bin/sh
# TV Wi-Fi watchdog: record the first failure, then recover without a power cycle.
# A lost association gets a quick wpa_supplicant reconnect. A still-broken link gets wlan0 down/up,
# then USB re-authorization (same as replugging it). Gateway-only failures get more time before reset.
DIR=/var/lib/webosbrew/wifi-watchdog
WLPRIV=$DIR/wlpriv
mkdir -p "$DIR"
log() { echo "$(date '+%F %T') $*" >> "$DIR/events.log"; }
default_interface() { ip route | awk '$1=="default" {print $5; exit}'; }
gateway() { ip route | awk '$1=="default" && $5=="wlan0" {print $3; exit}'; }
alive() { G=$(gateway); [ -n "$G" ] && ping -c 1 -W 1 "$G" >/dev/null 2>&1; }
wpa_state() { timeout -t 3 wpa_cli -i wlan0 status 2>/dev/null | sed -n 's/^wpa_state=//p' | head -1; }
usbdev() { for d in /sys/bus/usb/devices/*; do [ "$(cat "$d/idVendor" 2>/dev/null):$(cat "$d/idProduct" 2>/dev/null)" = "043e:3107" ] && echo "$d"; done; }
evidence() {
  F=$DIR/drop-$(date +%Y%m%d-%H%M%S).txt
  { echo "== $1"; uptime; free; ip addr show wlan0; ip route; timeout -t 3 wpa_cli -i wlan0 status; [ -x "$WLPRIV" ] && timeout -t 3 "$WLPRIV" wlan0 stat; cat /proc/net/wireless; dmesg | tail -300; } > "$F" 2>&1
  # Generated filenames contain only digits and hyphens.
  # shellcheck disable=SC2012
  ls -1t "$DIR"/drop-*.txt 2>/dev/null | tail -n +11 | xargs rm -f 2>/dev/null
}
seen_up=0; down_since=0; last_reset=0; stage=0; action_at=0
log "started"
while :; do
  iface=$(default_interface)
  if [ -n "$iface" ] && [ "$iface" != wlan0 ]; then
    # Ethernet or another preferred route is active; leave Wi-Fi management to ConnMan.
    seen_up=0; down_since=0; stage=0
    sleep 5
    continue
  fi
  now=$(date +%s)
  if alive; then
    [ "$down_since" -ne 0 ] && log "link back after $((now-down_since))s"
    seen_up=1; down_since=0; stage=0
  elif [ $seen_up -eq 1 ]; then
    state=$(wpa_state)
    if [ "$down_since" -eq 0 ]; then
      down_since=$now
      log "gateway unreachable; wpa_state=${state:-unknown}"
      evidence "first failed gateway probe; wpa_state=${state:-unknown}"
    fi
    if [ $stage -eq 3 ] && [ $((now-last_reset)) -ge 600 ]; then
      log "still down after cooldown; retrying recovery"
      stage=0; down_since=$((now-8))
    fi
    elapsed=$((now-down_since))
    if [ $stage -eq 0 ] && [ -n "$state" ] && [ "$state" != COMPLETED ] && [ $elapsed -ge 8 ]; then
      log "level 0: wpa_cli reconnect after ${elapsed}s; state=$state"
      timeout -t 3 wpa_cli -i wlan0 reconnect >> "$DIR/events.log" 2>&1
      stage=1; action_at=$now
    fi
    if [ $stage -le 1 ] && [ $((now-last_reset)) -ge 600 ]; then
      if [ $elapsed -ge 30 ] || { [ -n "$state" ] && [ "$state" != COMPLETED ] && [ $elapsed -ge 18 ]; }; then
        log "level 1: wlan0 down/up after ${elapsed}s; state=${state:-unknown}"
        ifconfig wlan0 down; sleep 1; ifconfig wlan0 up
        stage=2; action_at=$(date +%s)
      fi
    fi
    if [ $stage -eq 2 ] && [ $((now-action_at)) -ge 20 ]; then
      if ! alive; then
        D=$(usbdev)
        log "level 2: re-authorize USB ${D:-not-found}"
        [ -n "$D" ] && { echo 0 > "$D/authorized"; sleep 3; echo 1 > "$D/authorized"; }
      fi
      stage=3; last_reset=$(date +%s)
    fi
    if [ $stage -eq 3 ]; then sleep 5; else sleep 2; fi
    continue
  fi
  sleep 5
done
