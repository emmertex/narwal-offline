# narwal-offline

Take a Narwal Freo X Ultra (and likely other Narwal robots) off the
cloud: an on-LAN server emulator + the access tooling to install it.

**Status (2026-11-17):** the file read/write primitives over MQTT work
end-to-end; the offline CA is installed in the robot's system trust
store; the HTTPS side of the emulator is live and the robot talks to it
where DNS points it. **One blocker remains:** the robot's MQTT and
device-API channels use Alibaba ID²/itls with their OWN trust store
(they reject the system bundle), so the offline MQTT broker isn't
accepted yet — see docs/STATUS.md. Everything else in this repo is
ready and tested.

## Layout

| Path | What |
|---|---|
| `docs/HOW_IT_WORKS.md` | Full explanation of every mechanism used here |
| `docs/OWN_VACUUM.md` | Step-by-step guide to do this to your own vacuum |
| `docs/STATUS.md` | What works, what's blocked, what's next |
| `tools/narwal_access.py` | Standalone read/write access (email+password → MQTT → filesystem) |
| `emulator/emulator.py` | The offline "cloud": HTTPS SNI server for all Narwal hosts |
| `emulator/mosquitto.conf` | Offline MQTT broker config (MQTT5 + your CA) |
| `emulator/dnsmasq-narwal.conf` | DNS overrides (with the critical IPv6-leak fix) |
| `emulator/control.py` | Drive the robot over MQTT (start/stop/dock/status/listen) |
| `apk/make-custom-apk.sh` | Build a test APK whose app talks to the emulator |
| `pki/` | Your offline CA + per-host leaf certs (`issue-leaves.sh`) |

## Quickstart (once the MQTT trust blocker is solved)

```bash
# 1. your CA
cd pki && openssl req -x509 -newkey rsa:2048 -nodes -keyout offline-ca.key \
  -out offline-ca.crt -days 7300 -subj "/CN=MyNarwalOffline" \
  -addext basicConstraints=critical,CA:TRUE -addext keyUsage=critical,keyCertSign,cRLSign
./issue-leaves.sh

# 2. install the CA on the robot (needs cloud login once)
python3 tools/narwal_access.py login --email you@x.com --password '***' --region aus
python3 tools/narwal_access.py install-ca pki/offline-ca.crt

# 3. run the emulator + broker on some LAN box (edit IPs first)
python3 emulator/emulator.py --pki ../pki
mosquitto -c emulator/mosquitto.conf   # after copying leaves

# 4. point DNS at it (dnsmasq snippet), cycle the robot's Wi-Fi

# 5. control — no internet anywhere
python3 emulator/control.py status
python3 emulator/control.py start
```

## Ethics / legality

This is for **your own hardware**. The techniques are standard
interoperability research (no crypto was broken; the robot's firmware
signature chain was never attacked and modified firmware was never
installed). Don't point any of this at vacuums you don't own.
