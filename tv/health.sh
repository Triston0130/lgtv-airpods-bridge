#!/bin/sh
# Per-second TV Wi-Fi / TCP / memory telemetry for correlating audio stalls. Runs until killed or its parent session ends.
W=/tmp/airpods-bridge/wlpriv
OUT=${1:-/tmp/airpods-bridge/health.log}
P=$PPID
while [ "$(cat /proc/$P/stat 2>/dev/null | cut -d' ' -f1)" = "$P" ]; do
  T=$(date +%s.%N | cut -c1-14)
  S=$($W wlan0 stat | awk -F= '/Tx success/{ts=$2+0} /Tx retry count/{split($2,a,","); tr=a[1]+0} /Tx fail/{split($2,a,","); tf=a[1]+0} /Rx success/{rs=$2+0} /Rx with CRC/{split($2,a,","); rc=a[1]+0} /out of resource/{ro=$2+0} /RSSI-0/{r0=$2+0} END{printf "txok=%d txretry=%d txfail=%d rxok=%d rxcrc=%d rxnores=%d rssi=%d", ts,tr,tf,rs,rc,ro,r0}')
  R=$($W wlan0 show connStatus 2>/dev/null | grep -c "connected AP")
  K=$(awk 'NR%2==1{for(i=1;i<=NF;i++) n[i]=$i; next} {for(i=1;i<=NF;i++) if (n[i] ~ /^(RetransSegs|TCPTimeouts|TCPLossProbes)$/) printf "%s=%s ", n[i], $i}' /proc/net/snmp /proc/net/netstat)
  M=$(awk '/^MemFree/{f=$2} /^SwapFree/{s=$2} END{printf "memfree=%d swapfree=%d", f, s}' /proc/meminfo)
  V=$(awk '/^pswpin/{i=$2} /^pswpout/{o=$2} END{printf "pswpin=%d pswpout=%d", i, o}' /proc/vmstat)
  echo "$T assoc=$R $S $K$M $V" >> "$OUT"
  sleep 1
done
