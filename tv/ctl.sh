#!/bin/sh
# TV side of the Settings integration: asks the Mac agent to start, stop or report the bridge.
# usage: ctl.sh status | start only | start both | stop      (prints the agent's JSON reply)
DIR=/var/lib/webosbrew/airpods-bridge
E=$DIR/endpoint.json
if [ ! -f "$E" ]; then echo '{"reachable":false,"error":"The Mac has not set up the bridge yet."}'; exit 0; fi
URL=$(sed -n 's/.*"url": *"\([^"]*\)".*/\1/p' "$E")
TOKEN=$(sed -n 's/.*"token": *"\([^"]*\)".*/\1/p' "$E")
case "$1" in
  status) METHOD=GET;  PATH_Q=/status ;;
  start)  METHOD=POST; PATH_Q="/start?mode=${2:-both}" ;;
  stop)   METHOD=POST; PATH_Q=/stop ;;
  *) echo '{"error":"usage"}'; exit 2 ;;
esac
OUT=$(curl -s -m 20 -X $METHOD -H "X-Token: $TOKEN" "$URL$PATH_Q")
if [ -z "$OUT" ]; then echo '{"reachable":false,"error":"The Mac is not reachable. Is it awake and on Wi-Fi?"}'; else echo "$OUT"; fi
