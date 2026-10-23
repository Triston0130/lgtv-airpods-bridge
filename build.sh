#!/bin/sh
# Builds the TV helpers (32-bit ARM, glibc 2.24, via Zig) and the macOS player (Clang).
set -eu
cd "$(dirname "$0")"
"${ZIG:-zig}" cc -target arm-linux-gnueabi.2.24 -mcpu=cortex_a9 -O1 -g -rdynamic capture.c -o capture -ldl -lpthread -lrt
"${ZIG:-zig}" cc -target arm-linux-gnueabi.2.24 -mcpu=cortex_a9 -O2 wlpriv.c -o wlpriv
clang -O2 -Wall -Wextra player.c -o player -framework AudioToolbox -framework CoreAudio -framework CoreFoundation -lpthread
