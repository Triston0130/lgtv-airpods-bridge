#!/bin/sh
# TV Wi-Fi watchdog: record the first failure, then recover without a power cycle.
# A lost association gets a quick wpa_supplicant reconnect. A still-broken link gets wlan0 down/up,
# then USB re-authorization (same as replugging it). Gateway-only failures get more time before reset.
DIR=/var/lib/webosbrew/wifi-watchdog
WLPRIV=$DIR/wlpriv
EVENT=/tmp/wifi-watchdog-disconnected-at
SEEN=/tmp/wifi-watchdog-seen-up
LISTENER_PID=/tmp/wifi-watchdog-wpa-cli.pid
mkdir -p "$DIR"
exec 9>"$DIR/watchdog.lock"
flock -n 9 || exit 0
log() { echo "$(date '+%F %T') $*" >> "$DIR/events.log"; }
default_interface() { ip route | awk '$1=="default" {print $5; exit}'; }
gateway() { ip route | awk '$1=="default" && $5=="wlan0" {print $3; exit}'; }
alive() { G=$(gateway); [ -n "$G" ] && ping -c 1 -W 1 "$G" >/dev/null 2>&1; }
wpa_state() { timeout -t 3 wpa_cli -i wlan0 status 2>/dev/null | sed -n 's/^wpa_state=//p' | head -1; }
admin_down() {
  [ -r /sys/class/net/wlan0/flags ] || return 1
  flags=$(cat /sys/class/net/wlan0/flags)
  [ $((flags & 1)) -eq 0 ]
}
ensure_listener() {
  pid=$(cat "$LISTENER_PID" 2>/dev/null)
  if [ -n "$pid" ] && ps -p "$pid" -o args= 2>/dev/null | grep -qF "$DIR/wifi-event.sh"; then return; fi
  listener_now=$(date +%s)
  [ $((listener_now-last_listener_try)) -lt 10 ] && return
  last_listener_try=$listener_now
  wpa_cli -i wlan0 -a "$DIR/wifi-event.sh" -B -P "$LISTENER_PID" >> "$DIR/event-listener.log" 2>&1 9>&-
}
usbdev() { for d in /sys/bus/usb/devices/*; do [ "$(cat "$d/idVendor" 2>/dev/null):$(cat "$d/idProduct" 2>/dev/null)" = "043e:3107" ] && echo "$d"; done; }
evidence() {
  F=$1
  { uptime; free; ip addr show wlan0; ip route; timeout -t 3 wpa_cli -i wlan0 status; [ -x "$WLPRIV" ] && timeout -t 3 "$WLPRIV" wlan0 stat; cat /proc/net/wireless; dmesg | tail -300; } >> "$F" 2>&1
  # Generated filenames contain only digits and hyphens.
  # shellcheck disable=SC2012
  ls -1t "$DIR"/drop-*.txt 2>/dev/null | tail -n +11 | xargs rm -f 2>/dev/null
}
sample_health() {
  [ -x "$WLPRIV" ] || return
  stats=$(timeout -t 3 "$WLPRIV" wlan0 stat 2>/dev/null) || return
  case "$stats" in *'Tx success'*'Rx with CRC'*) ;; *) return ;; esac
  values=$(printf '%s\n' "$stats" | awk '
    /^Tx success/ {v=$0; sub(/^[^=]*= */, "", v); tx=v+0}
    /^Tx retry count/ {v=$0; sub(/^[^=]*= */, "", v); retry=v+0}
    /^Rx success/ {v=$0; sub(/^[^=]*= */, "", v); rx=v+0}
    /^Rx with CRC/ {v=$0; sub(/^[^=]*= */, "", v); crc=v+0}
    /^RSSI-0/ {v=$0; sub(/^[^=]*= */, "", v); rssi0=v+0}
    /^RSSI-1/ {v=$0; sub(/^[^=]*= */, "", v); rssi1=v+0}
    /^CHannel:/ {channel=$2+0}
    END {printf "%d,%d,%d,%d,%d,%d,%d", channel,tx,retry,rx,crc,rssi0,rssi1}
  ')
  bssid=$(timeout -t 3 wpa_cli -i wlan0 status 2>/dev/null | sed -n 's/^bssid=//p' | head -1)
  file=$DIR/health.csv
  [ -e "$file" ] || printf 'epoch,bssid,channel,tx_success,tx_retry,rx_success,rx_crc,rssi0,rssi1\n' > "$file"
  printf '%s,%s,%s\n' "$(date +%s)" "${bssid:-unknown}" "$values" >> "$file"
  if [ "$(wc -c < "$file")" -gt 1048576 ]; then mv "$file" "$file.1"; fi
}
seen_up=0; down_since=0; last_reset=0; stage=0; action_at=0; last_listener_try=0
reported_ip_issue=0
next_listener_check=0; next_interface_check=0; next_gateway_probe=0; next_health_sample=0; iface=
log "started"
while :; do
  now=$(date +%s)
  if [ "$now" -ge "$next_listener_check" ]; then
    ensure_listener
    next_listener_check=$((now+10))
  fi
  if [ "$now" -ge "$next_interface_check" ]; then
    iface=$(default_interface)
    next_interface_check=$((now+5))
  fi
  if [ -n "$iface" ] && [ "$iface" != wlan0 ]; then
    # Ethernet or another preferred route is active; leave Wi-Fi management to ConnMan.
    seen_up=0; down_since=0; stage=0
    rm -f "$SEEN" "$EVENT"
    sleep 5
    continue
  fi
  event_down=0; event_at=0
  if [ -f "$EVENT" ]; then
    event_at=$(cat "$EVENT" 2>/dev/null)
    case "$event_at" in
      *[!0-9]*|'') event_at=0 ;;
    esac
    if [ "$event_at" -gt 0 ] && [ $((now-event_at)) -ge 0 ] && [ $((now-event_at)) -le 60 ]; then
      state=$(wpa_state)
      if [ "$state" = COMPLETED ]; then rm -f "$EVENT"; elif [ -n "$state" ]; then event_down=1; fi
    else
      rm -f "$EVENT"
    fi
  fi
  carrier=$(cat /sys/class/net/wlan0/carrier 2>/dev/null)
  if [ "$event_down" -eq 0 ] && [ "$seen_up" -eq 1 ] && [ "$carrier" = 0 ]; then
    event_down=1
    if [ "$down_since" -ne 0 ]; then event_at=$down_since; else event_at=$now; fi
    state=$(wpa_state)
  fi
  if [ "$event_down" -eq 0 ] && [ "$down_since" -eq 0 ] && [ "$now" -lt "$next_gateway_probe" ]; then
    sleep 1
    continue
  fi
  next_gateway_probe=$((now+5))
  if [ "$event_down" -eq 0 ] && alive; then
    [ "$down_since" -ne 0 ] && log "link back after $((now-down_since))s"
    seen_up=1; down_since=0; stage=0; reported_ip_issue=0
    touch "$SEEN"
    if [ "$now" -ge "$next_health_sample" ]; then
      sample_health 9>&- &
      next_health_sample=$((now+30))
    fi
  elif [ $seen_up -eq 1 ]; then
    [ "$event_down" -eq 1 ] || state=$(wpa_state)
    if [ "$down_since" -eq 0 ]; then
      if [ "$event_down" -eq 1 ]; then down_since=$event_at; else down_since=$now; fi
      log "link down; event=$event_down; carrier=${carrier:-unknown}; wpa_state=${state:-unknown}"
      snapshot=$DIR/drop-$(date +%Y%m%d-%H%M%S)-$$.txt
      printf '== first link failure; event=%s; carrier=%s; wpa_state=%s\n' "$event_down" "${carrier:-unknown}" "${state:-unknown}" > "$snapshot"
      evidence "$snapshot" 9>&- &
    fi
    if [ $stage -ne 2 ] && admin_down; then
      log "wlan0 administratively down; leaving it disabled"
      seen_up=0; down_since=0; stage=0
      rm -f "$SEEN" "$EVENT"
      sleep 5
      continue
    fi
    if [ "$state" = COMPLETED ] && [ "$carrier" = 1 ] && { [ -z "$(gateway)" ] || ! ip -4 addr show dev wlan0 | grep -q 'inet '; }; then
      if [ "$reported_ip_issue" -eq 0 ]; then log "association healthy; IP address or default route missing; no radio reset"; reported_ip_issue=1; fi
      sleep 2
      continue
    fi
    if [ $stage -eq 3 ] && [ $((now-last_reset)) -ge 600 ]; then
      log "still down after cooldown; retrying recovery"
      stage=0; down_since=$((now-8))
    fi
    elapsed=$((now-down_since))
    reconnect_after=8
    [ "$event_down" -eq 1 ] && reconnect_after=2
    if [ $stage -eq 0 ] && { [ "$state" = DISCONNECTED ] || [ "$state" = INACTIVE ]; } && [ "$elapsed" -ge "$reconnect_after" ]; then
      log "level 0: wpa_cli reconnect after ${elapsed}s; state=$state"
      timeout -t 3 wpa_cli -i wlan0 reconnect >> "$DIR/events.log" 2>&1
      stage=1; action_at=$now
    fi
    if [ $stage -le 1 ] && [ $((now-last_reset)) -ge 600 ]; then
      if [ "$event_down" -eq 1 ] && [ $elapsed -ge 8 ] || [ $elapsed -ge 30 ] || { [ -n "$state" ] && [ "$state" != COMPLETED ] && [ $elapsed -ge 18 ]; }; then
        log "level 1: wlan0 down/up after ${elapsed}s; state=${state:-unknown}"
        ifconfig wlan0 down; sleep 1; ifconfig wlan0 up
        stage=2; action_at=$(date +%s)
      fi
    fi
    if [ $stage -eq 2 ] && [ $((now-action_at)) -ge 15 ]; then
      if ! alive; then
        D=$(usbdev)
        log "level 2: re-authorize USB ${D:-not-found}"
        [ -n "$D" ] && { echo 0 > "$D/authorized"; sleep 3; echo 1 > "$D/authorized"; }
      fi
      stage=3; last_reset=$(date +%s)
    fi
    if [ $stage -eq 3 ]; then sleep 5; else sleep 1; fi
    continue
  fi
  sleep 1
done
