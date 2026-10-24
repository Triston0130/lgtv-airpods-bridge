# lgtv-airpods-bridge

Play an LG webOS TV's audio on AirPods, even when the TV has no Bluetooth. A helper on the rooted TV captures the audio straight from the TV's DSP and streams it over Wi-Fi to a Mac. The Mac plays it on AirPods connected to it:

```
TV DSP ──(native capture)──► UDP over Wi-Fi ──► Mac ──► Bluetooth ──► AirPods
```

- **Lossless 48 kHz 16-bit stereo PCM.** No compression on the TV-to-Mac hop.
- **About 60–70 ms of network buffering.** Lost packets are re-requested and resent instead of buffered around. The AirPods themselves add their usual Bluetooth delay (macOS reports 160 ms).
- **TV speakers on or muted.** By default both the TV and the AirPods play. `--mute-tv` silences the TV while the AirPods keep playing; its previous mute state returns when the bridge stops.
- **Recovers a degrading TV Wi-Fi link** by refreshing the association while streaming.

Verified on an **LG 49UK6200PUA** (2018, MediaTek/MStar SoC, webOS 4.x) with AirPods Pro 2 on an Apple-silicon Mac. Other MStar-based LG TVs may work, but their capture channel or source number may differ. See [Porting](#porting).

## Requirements

- **A rooted LG TV with SSH as root**, for example via [RootMyTV](https://rootmy.tv/) or the webOS Homebrew Channel. Root is what lets the helper reach the audio DSP.
- **A Mac on the same network**, staying awake while you listen.
- **[Zig](https://ziglang.org/)** to cross-compile the TV helpers, and the Xcode Command Line Tools for `clang`.
- **Optional:** full Xcode, for `gamepolicyctl`. Without it the bridge cannot switch Game Mode on and runs unchanged.

## Setup

```sh
git clone https://github.com/Triston0130/lgtv-airpods-bridge
cd lgtv-airpods-bridge
sh build.sh                      # ZIG=/path/to/zig sh build.sh if zig is not on PATH
cp config.example.json config.json
```

Edit `config.json` with your TV's address, SSH user and private key. Host keys are checked strictly, so connect once with plain `ssh` first to record the TV's key.

## Use

Connect the AirPods to the Mac and select them as the sound output, then run:

```sh
./control start                  # TV and AirPods both play
./control start --mute-tv        # AirPods only; the TV unmutes when you stop
./control status
./control stop
```

The first time, macOS may ask whether Python can accept incoming network connections. Allow it, or no audio arrives.

| Option | Default | Meaning |
|---|---|---|
| `--volume N` | last remote level, initially 35 | Player volume, 0–100 |
| `--buffer-ms N` | 70 | Jitter-buffer target, 20–200. Lower is closer to lip sync; too low drops out during Wi-Fi stalls. |
| `--mute-tv` | off | Mute the TV's own output while streaming |
| `--no-game-mode` | off | Leave macOS Game Mode alone. By default it is forced on while streaming, because Apple says it lowers AirPods latency, and set back to automatic on stop. |
| `--recover-ms N` | 50 | How long a missing packet is re-requested before it is concealed |
| `--transport ssh` | `udp` | The older PCM-inside-SSH transport. It stalls up to about 300 ms behind TCP. |

`relay-state.json` reports these:

| Field | Meaning |
|---|---|
| `recovered` | Missing packets that were re-requested and arrived |
| `concealed` | Missing packets given up on and filled in |
| `rtt_p50_ms` | Median Mac-to-TV round trip |
| `reassociations` | TV Wi-Fi refreshes the bridge triggered |
| `peak_last_second` | Loudest sample in the last second; 0 means silence |

## How it works

### Capture (`capture.c`, runs on the TV)

LG's audio DSP sits behind MStar's "Utopia" driver layer, exposed by `/usr/lib/libkadaptor.so`.
- The helper `dlopen`s that library and initializes `MsOS`, `Utopia`, MMIO and the audio register mapping. A plain `MApi_AUDIO_PCMCapture_*` call faults without that setup.
- It then captures PCM channel 1 from source 8. On the 49UK6200 that is the TV's final mixed output: HDMI, apps and tuner alike.
- The capture taps the audio before the TV's volume and mute, so muting the TV does not silence the AirPods.
- The helper is uploaded to TV RAM (`/tmp`) on each start. Nothing is installed on the TV.

### Transport (`capture.c` and `relay.py`)

Each 10 ms chunk is one UDP datagram: sequence number, the TV's send time, then PCM.
- The TV keeps the last 1.28 s of packets.
- The Mac reorders by sequence number and `NACK`s gaps immediately, repeating every 20 ms. It conceals a chunk only once its deadline passes: repeat for short gaps, silence for long ones.
- SSH only launches the helper and owns its lifetime. The helper exits when its session goes away.

### TV menus and remote volume

The optional Mac agent lets the TV's full Settings and Quick Settings menus start the bridge. Connect and select the AirPods as the Mac's output, then run:

```sh
python3 agent.py install
python3 tv/install_settings.py --check   # before the menus have been installed
python3 tv/install_settings.py
```

The menu installer copies the stock Settings and Quick Settings files, patches the copies, and bind-mounts them at boot. It checks the stock file hashes before mounting, so a changed firmware leaves the stock menu in place. Quick Settings may need a TV restart to load the patched QML. Run `/var/lib/webosbrew/airpods-bridge/uninstall-settings.sh` on the TV to remove the menu changes.

Selecting AirPods or AirPods + TV starts streaming; choosing a TV output stops it. The agent remembers the last AirPods choice and reconnects when they become the Mac's output again. The TV must be able to reach the Mac on TCP port 47801.

On this TV, remote volume keys are handled by an existing Python 2 remote remapper (`/var/lib/webosbrew/remote-remap.py`). `python3 tv/install_remote_keys.py` installs the tested AirPods extension only when that remapper's fingerprint matches. The original is saved as `remote-remap.py.before-airpods`. While AirPods-only is active, volume and mute control the AirPods without unmuting the TV. In AirPods + TV mode the keys also keep their normal TV action. The Mac player's volume changes live in 5-point steps.

### What survives a restart

- The Settings and Quick Settings copies, remapper, and their startup hooks live under `/var/lib/webosbrew` on the TV. They survive a normal reboot. This repo also keeps the remapper's virtual-keyboard helper, startup hooks, and a snapshot of the current button map in `tv/` so that setup is recoverable.
- The Mac agent runs from this checkout through a `KeepAlive` LaunchAgent. Its private `config.json`, `agent-token`, and saved choice stay local and are ignored by Git. Keep the checkout at the same path while the agent is installed. Re-run `python3 agent.py install` if you move it.
- The capture binary and active volume-key route live under `/tmp/airpods-bridge` on the TV. They are created again when streaming starts; losing them on reboot is expected. One-off diagnostics in `/tmp` are also temporary.

For a full machine or TV replacement, restore the private SSH key and `config.json` separately, build the binaries, install the Mac agent, then reinstall the TV menus. The committed files preserve the code; private credentials are intentionally not in Git.

### Link health

The relay pings the helper 10 times a second over the same socket. If the median round trip exceeds 80 ms, or a third of pings are lost, for 5 s, the TV runs `wpa_cli reassociate`, at most once a minute.
- On the test TV the downlink decayed over hours of uptime until the router disassociated it (reason 2), and the TV's connection manager then took minutes to reconnect.
- A fresh association restores the link. Forcing one mid-stream costs one dropout of about 100 ms.

### Playback (`player.c`, runs on the Mac)

A native AudioQueue output bound to the AirPods' device ID, with a jitter buffer that has a latency target.
- Standing delay is removed by playing up to 2% fast with interpolation. Nothing is skipped, so there are no clicks.
- It stops if the AirPods disconnect.

## Measurements (49UK6200, Wi-Fi 6/7 router, 5 GHz)

| What | Result |
|---|---|
| TV capture to send | ≤ 5 ms |
| UDP network delay, best effort | 0.8 ms median, 13 ms p99, 61 ms p99.9, 0 of 4,002 lost |
| PCM over SSH/TCP, same link | stalls up to 285 ms |
| UDP marked DSCP EF (Wi-Fi voice) | up to **5 s** delay and 10% loss: the TV driver's voice queue is broken, so the default is best effort |
| 5% simulated loss | 343 of 343 recovered, 0 concealed |

The remaining 40–120 ms bursts were on the router-to-Mac leg. Some coincided with macOS `locationd` Wi-Fi scans and `sharingd` Bluetooth "Nearby" scans.

## Optional TV tools (`tv/`)

- **`health.sh`:** per-second radio, TCP and memory telemetry. Copy it and `wlpriv` to `/tmp/airpods-bridge/` on the TV and run it over SSH.
- **`wlpriv`:** a minimal `iwpriv` for the TV's `mt7662u_sta` Wi-Fi driver (`stat`, `show NAME`, `set NAME=VALUE`).
- **`wifi-watchdog.sh` + `wifi-event.sh` + `68-wifi-watchdog`:** Homebrew Channel startup hook for the test TV. The `wpa_cli` callback records association changes and returns immediately; the watchdog confirms a disconnect for 2 s before requesting a reconnect. A one-second carrier check catches a missed callback, while gateway pings remain five seconds apart when healthy. The watchdog restarts `wlan0` after about 8 s of confirmed association loss, then re-authorizes the USB Wi-Fi device 15 s later if the link is still dead. A gateway failure with the radio still associated gets the slower 18–30 s path. It does not reset an administratively disabled radio or a still-associated radio whose IPv4 address or default route is missing. Only one watchdog can hold its lock. The first failure is logged immediately, while detailed evidence is saved in the background under `/var/lib/webosbrew/wifi-watchdog/`; full reset attempts have a 10-minute cooldown. The USB ID is specific to the 49UK6200's module (043e:3107).
  - `health.csv` records driver counters, signal, channel, and BSSID every 30 s while the gateway is reachable. It and its rotated copy are capped at roughly 2 MiB total. Compare counter *deltas* before a spontaneous dropout with the first-failure snapshot; do not treat a high cumulative CRC percentage alone as a reset trigger.
  - The radio's USB `power/control` is already `on`, so autosuspend is not the observed cause. The driver reports high receive CRC counts even while the link works; this is recorded for diagnosis, not used alone to force a reset.
  - The observed connection is 5 GHz channel 36 (80 MHz centered at 42), WPA2-PSK with CCMP. That is already outside the DFS channel range; a DFS-channel change is not indicated by the TV's current state.
  - On an eero network, compare a spontaneous failure's timestamp in `events.log` with the [eero Event Stream](https://eero.com/support/articles/what-are-eero-events), available on Secure, Plus, and Business accounts. The [device details page](https://eero.com/support/articles/what-is-the-devices-tab-in-the-eero-app) identifies the eero node and band the TV used. A reason code on the TV alone does not establish why the access point disconnected it.
- **`tools/udp-probe.py`:** splits packet delay into TV-side and network-side parts. Run it only while the bridge is stopped.

## Porting

- **Capture channel and source.** `capture --stream CHANNEL SOURCE` takes both. To test a pair, record two seconds while the TV plays something and check the PCM is not silent. With the bridge stopped, after copying `capture` to `/tmp/airpods-bridge/` on the TV:
  ```sh
  ssh root@TV '/tmp/airpods-bridge/capture --capture 1 8 2' > test.pcm
  ```
- **Never run two capture helpers at once.** Each re-initializes the vendor hardware layer. On the test TV, running two at once coincided with its Wi-Fi dying.
- **Bluetooth TVs.** If your TV does have Bluetooth, use the TV's own Bluetooth instead. This bridge is for sets without it. The 49UK6200 firmware ships LG's whole Bluetooth stack, but the fitted Wi-Fi module (043e:3107) has no Bluetooth radio.

## License

MIT. This repository contains only original code; it calls LG's libraries on the TV at runtime and ships none of them.
