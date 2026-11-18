# narwal-offline

Take YOUR Narwal Freo X Ultra off the cloud: run the cloud on your LAN.
No firmware modification, no disassembly — everything is configuration,
all of it reversible with one script.

**Status (2026-11-18): WORKING END-TO-END on a real robot** — the robot
connects MQTT (TLS) to the local broker, streams telemetry, and takes
commands via `control.py`. All research artifacts live in the
companion repo **narwal-decompile** (exfiltrated binaries, decrypted
firmware, keys, reversing write-ups).

## What's here (owner kit only)

| Path | What |
|---|---|
| `tools/owner_setup.sh` | one script → command channel + CA + IPv6-off + reboot |
| `tools/revert_to_stock.sh` | one script → put the robot back exactly as found |
| `tools/narwal_access.py` | MQTT login/read/write driver (the primitive) |
| `emulator/emulator.py` | the local "cloud" (HTTPS, SNI-routed; OTA answers "no update" = updates off) |
| `emulator/server.sh` | start/stop the server side + run shell commands on the robot |
| `emulator/mosquitto.conf` | the local MQTT broker (MQTT5 + your CA leaf) |
| `emulator/dnsmasq-narwal.conf` | DNS overrides (edit to your server IP) |
| `emulator/control.py` | drive the robot offline: status / wake / start / dock / listen |
| `apk/make-custom-apk.sh` | patch the Android app to talk to your emulator instead of the cloud |
| `pki/` | your offline CA + per-host leaves (`issue-leaves.sh`) |
| `docs/OWN_VACUUM.md` | the step-by-step owner guide |
| `docs/HOW_IT_WORKS.md` | mechanisms, briefly |

## Quickstart

```bash
# 0. your CA
cd pki && openssl req -x509 -newkey rsa:2048 -nodes -keyout offline-ca.key \
  -out offline-ca.crt -days 7300 -subj "/CN=MyNarwalOffline" \
  -addext basicConstraints=critical,CA:TRUE -addext keyUsage=critical,keyCertSign,cRLSign
./issue-leaves.sh && cd ..

# 1. cloud login once (needs internet + your robot on the real cloud)
python3 tools/narwal_access.py login --email you@x.com --password '***' --region aus

# 2. server side (on the always-on box; set its IP)
./emulator/server.sh start
SERVER=10.x.y.z ./tools/owner_setup.sh          # → follows its own instructions

# 3. broker + emulator + DNS
mosquitto -c emulator/mosquitto.conf            # after copying leaves
python3 emulator/emulator.py --pki pki
# merge emulator/dnsmasq-narwal.conf (with your IP) into the robot's DNS

# 4. control — no internet anywhere
python3 emulator/control.py --device <deviceId> status

# back to stock anytime
./tools/revert_to_stock.sh
```

## Ethics / legality

For **your own hardware**. Standard interoperability research — no crypto
broken, no firmware modified, everything reversible. Don't point this at
vacuums you don't own.
