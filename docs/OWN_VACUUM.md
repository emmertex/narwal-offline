# Do this to your own vacuum

Tested on Freo X Ultra / J4, fw v01.09.01.00, app 2.11.90 — other models
may vary. Nothing here modifies firmware; everything is reversible
(`tools/revert_to_stock.sh`).

## 0. What you need

- Your Narwal account email + password (the app's login)
- A LAN box that stays on ("the server"; Python 3, openssl, mosquitto,
  dnsmasq, paho-mqtt)
- The robot on your LAN, and DNS you can point it at
- ~30 minutes

## 1. Your CA + leaves

```bash
cd pki
openssl req -x509 -newkey rsa:2048 -nodes -keyout offline-ca.key \
  -out offline-ca.crt -days 7300 -subj "/CN=MyNarwalOffline" \
  -addext basicConstraints=critical,CA:TRUE -addext keyUsage=critical,keyCertSign,cRLSign
./issue-leaves.sh          # per-host leaves into pki/leaves/
cd ..
```

Leaf gotchas (found the hard way): OpenSSL-3 clients reject leaves
without `subjectKeyIdentifier`/`authorityKeyIdentifier` (issue-leaves.sh
adds them); mosquitto wants its key files chowned + pid_file in /tmp.

## 2. Find your identifiers

```bash
python3 tools/narwal_access.py login --email you@x.com --password '***' --region aus
python3 tools/narwal_access.py devices      # productId + 32-hex deviceId
```

## 3. Server side + robot onboarding

```bash
./emulator/server.sh start                   # :8081 cmd/CA server, :4550 output, :4545 shell
SERVER=10.x.y.z ./tools/owner_setup.sh
```

The setup script: backs up the robot's `rc.local` + `sysctl.conf`
(kept in `backups/<timestamp>/`), installs the command-channel line,
disables IPv6 robot-side, reboots, and prints the exact follow-up
commands. The critical one (installs the CA where the robot's MQTT
client actually looks — an OpenSSL hash-dir entry):

```bash
./emulator/server.sh cmd \
  'cd /etc/ssl/certs && wget -q -O <HASH>.0 http://10.x.y.z:8081/offline-ca.crt && md5sum <HASH>.0'
```

(`<HASH>` = your CA's subject hash — the setup script computes it.)

**Updates off**: once DNS points at the emulator, every OTA check gets
"no update" — that's the whole switch. (Optional hard mode: rename the
upgrader binary via the command channel; revert before any real update.)

## 4. Broker + emulator + DNS

```bash
mkdir -p /etc/mosquitto/narwal-offline && cp pki/leaves/us-01.mqtt* /etc/mosquitto/narwal-offline/
mosquitto -c emulator/mosquitto.conf
python3 emulator/emulator.py --pki pki        # :443 for every Narwal host
```

DNS: merge `emulator/dnsmasq-narwal.conf` (with your IP) into whatever
the robot uses. The `local=/narwaltech.com/` line is load-bearing (and
IPv6 is now off robot-side, so no AAAA leaks). Cycle the robot's Wi-Fi
or reboot it; watch mosquitto's log for `New client connected … as
sweeper_…` — that's your robot on YOUR broker.

## 5. Control — no internet anywhere

```bash
python3 emulator/control.py --device <deviceId> status
python3 emulator/control.py --device <deviceId> wake
python3 emulator/control.py --device <deviceId> start
python3 emulator/control.py --device <deviceId> listen
```

(The robot drops MQTT while docked-asleep and reconnects when it wakes.)

## 6. The app (optional): point it at your emulator

```bash
./apk/make-custom-apk.sh /path/to/app.apkm out/
adb install out/*.apk       # logs in with anything; talks to your emulator
```

## Rollback (full cleanup)

```bash
./tools/revert_to_stock.sh            # CA entry + rc.local + IPv6 + reboot
# then remove the DNS overrides — the robot returns to the real cloud on its own
```
