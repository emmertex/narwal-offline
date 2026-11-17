# Do this to your own vacuum

A checklist for repeating this on another Narwal robot (tested on Freo
X Ultra / J4, fw v01.09.01.00, app 2.11.90 — other models may vary).

## 0. What you need

- Your Narwal **account email + password** (the app's login).
- A LAN box that stays on (the "emulator box") with Python 3, openssl,
  mosquitto, dnsmasq.
- The robot on your LAN with DHCP you control (or a DNS server you can
  point it at).
- 30–60 minutes. Nothing here modifies firmware; everything is
  reversible (keep the backups the tools make).

## 1. Find your identifiers

```bash
python3 tools/narwal_access.py login --email you@x.com --password '***' --region aus
python3 tools/narwal_access.py devices
# → note productId (EHf6cRNRGT) and deviceId (32-hex)
```

Region is one of us/eu/il/cn/aus/de (pick where your account lives;
`aus` works for AU).

## 2. Verify filesystem access

```bash
python3 tools/narwal_access.py read /etc/openwrt_release   # OS identity
python3 tools/narwal_access.py list-files | head           # config registry
python3 tools/narwal_access.py read /etc/passwd
```

If `read` returns data — you're in. Explore freely (robot is
`Tina Linux`, board `mr813-ip1221*`). Useful targets:
`/etc/ssl/certs/ca-certificates.crt`, `/robotdata/static/robot_info/*`
(the device RSA key), `/tmp/.lastlog` (system log).

## 3. Create + install your CA

```bash
cd pki
openssl req -x509 -newkey rsa:2048 -nodes -keyout offline-ca.key -out offline-ca.crt \
  -days 7300 -subj "/CN=MyNarwalOffline" \
  -addext basicConstraints=critical,CA:TRUE -addext keyUsage=critical,keyCertSign,cRLSign
./issue-leaves.sh          # per-host leaves into pki/leaves/
cd ..
python3 tools/narwal_access.py install-ca pki/offline-ca.crt
```

`install-ca` reads the bundle, appends your CA, writes it back, and
keeps `ca-certificates.crt.bak` locally. Rollback = write the .bak back
with the `write` command.

## 4. Run the emulator + broker

On the emulator box (say 10.x.y.z — edit the dnsmasq snippet to match):

```bash
python3 emulator/emulator.py --pki pki        # :443 for every Narwal host
mkdir -p /etc/mosquitto/narwal-offline && cp pki/leaves/us-01.mqtt* \
  pki/leaves/eu-01.mqtt* /etc/mosquitto/narwal-offline/
# edit emulator/mosquitto.conf paths if needed, then:
mosquitto -c emulator/mosquitto.conf
```

Smoke-test: `openssl s_client -connect 10.x.y.z:443 -servername
us-open.narwaltech.com -CAfile pki/offline-ca.crt` should verify.

## 5. Point the robot at it (DNS)

Merge `emulator/dnsmasq-narwal.conf` (with your IP) into the dnsmasq
your robot uses for DNS, then make the robot re-lease (toggle its
Wi-Fi via the app's network settings, or power-cycle it).

What to expect today (see docs/STATUS.md): OTA-style HTTPS calls land
on the emulator and validate fine. **The MQTT channel will reject the
broker's cert until the itls-store blocker is solved** — during
experiments the robot will be cloud-offline while DNS points at you;
revert DNS to restore it (it recovers by itself).

## 6. Control (once the broker is trusted)

```bash
python3 emulator/control.py --broker 10.x.y.z --device <deviceId> status
python3 emulator/control.py ... wake      # if docked-asleep
python3 emulator/control.py ... start
python3 emulator/control.py ... listen    # stream pushes
```

## 7. The custom app (optional, for testing)

`apk/make-custom-apk.sh app.apk` → `app-offline.apk` with all hosts
pointed at `emulator.local` (your DNS resolves it to the emulator).
Install on a spare phone/tablet, log in with anything — the emulator
accepts all credentials.

## Rollback (full cleanup)

1. DNS: remove the narwaltech overrides.
2. Robot CA: `write` the `.bak` bundle back (`narwal_access.py write
   /etc/ssl/certs/ca-certificates.crt --from-file ca-certificates.crt.bak`).
3. Nothing else persists.
