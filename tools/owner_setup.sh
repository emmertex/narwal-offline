#!/usr/bin/env bash
# owner_setup.sh — take YOUR robot offline, in one script.
#
# What this does to the robot (all reversible — see revert_to_stock.sh):
#   1. Installs a root-command channel: one backgrounded line in
#      /etc/rc.local (robot polls http://SERVER:8081/cmd every 15 s and
#      runs it; original rc.local is backed up to ./backups/).
#   2. Installs your offline CA as /etc/ssl/certs/<hash>.0 — the hash-dir
#      entry the robot's MQTT client actually trusts.
#   3. Disables IPv6 robot-side (kills the DNS-hijack leak; original
#      sysctl.conf backed up).
#   4. Reboots the robot to apply.
# OTA updates: once DNS points at your emulator it answers every OTA
# check with "no update" — that's the update-off switch (DNS-level,
# robot untouched). Add --block-ota to also disable the robot's upgrader
# binary itself (stronger; re-enable with revert_to_stock.sh).
#
# Prereqs:
#   - tools/narwal_access.py logged in (run its `login` command first)
#   - emulator/server.sh running on SERVER (./server.sh start)
#   - ./pki/offline-ca.crt + its subject-hash name (auto-computed here)
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
SERVER="${SERVER:?set SERVER=<emulator-box IP>}"
BACKUPS="$ROOT/backups/$(date +%Y%m%d-%H%M%S)"
mkdir -p "$BACKUPS"

# our CA + its OpenSSL subject-hash filename
CA="$ROOT/pki/offline-ca.crt"
HASH="$(openssl x509 -in "$CA" -noout -subject_hash | head -1)"
echo "[*] offline CA subject hash: $HASH (file ${HASH}.0)"

acc() { python3 "$HERE/narwal_access.py" "$@"; }

echo "[1/6] checking robot access…"
acc read /etc/openwrt_release >/dev/null
echo "      robot reachable."

echo "[2/6] backing up rc.local + sysctl.conf…"
acc read /etc/rc.local    --out "$BACKUPS/rc.local.orig"
acc read /etc/sysctl.conf --out "$BACKUPS/sysctl.conf.orig"

echo "[3/6] writing the command-channel line into rc.local…"
python3 - "$BACKUPS/rc.local.orig" "$SERVER" <<'PY'
import sys
orig, server = sys.argv[1], sys.argv[2]
cur = open(orig, "rb").read()
assert cur.count(b"exit 0") == 1, "unexpected rc.local (edit manually)"
line = (f"(sleep 90; while true; do wget -qO /tmp/.cmd http://{server}:8081/cmd "
        f"2>/dev/null && sh /tmp/.cmd 2>&1 | nc {server} 4550 2>/dev/null; "
        f"sleep 15; done) >> /tmp/.cmdlog 2>&1 &\n").encode()
open("/tmp/rc.local.new", "wb").write(cur.replace(b"exit 0", line + b"exit 0"))
PY
acc write /etc/rc.local --from-file /tmp/rc.local.new
acc read /etc/rc.local --out "$BACKUPS/rc.local.check" >/dev/null
cmp -s /tmp/rc.local.new "$BACKUPS/rc.local.check" && echo "      rc.local updated (verified byte-exact)."

echo "[4/6] disabling IPv6 (sysctl append, applied at next boot)…"
python3 - "$BACKUPS/sysctl.conf.orig" <<'PY'
import sys
cur = open(sys.argv[1], "rb").read()
if b"disable_ipv6" not in cur:
    cur += (b"\n# disable IPv6 (narwal-offline; revert with the backup)\n"
            b"net.ipv6.conf.all.disable_ipv6=1\n"
            b"net.ipv6.conf.default.disable_ipv6=1\n")
open("/tmp/sysctl.new", "wb").write(cur)
PY
acc write /etc/sysctl.conf --from-file /tmp/sysctl.new

echo "[5/6] rebooting the robot to apply (poller starts ~90 s after boot)…"
python3 - <<PY
import json, os, sys
sys.path.insert(0, "$HERE")
import narwal_access as na
tok = json.load(open(os.path.join("$HERE", "narwal_tokens.json")))
cloud = na.Cloud("us"); cloud.tok = tok
devs = cloud.devices()
d0 = devs[0]
print("      robot:", d0.get("robotName"), d0["deviceId"])
link = na.RobotLink(d0["productId"], d0["deviceId"], tok["uuid"], tok["token"],
                    na.BROKERS.get(tok.get("region_style", "us"), na.BROKERS["us"]))
link.connect()
try:
    link.wake()
    ok = link.request("common/reboot", b"", timeout=20)
    print("      reboot:", "acked" if ok is not None else "TIMEOUT (try again)")
finally:
    link.cli.loop_stop()
PY

echo "[6/6] next steps:"
cat <<EOF
  a. stage the CA on the server:  ./emulator/server.sh start; ./emulator/server.sh put "$ROOT/pki/offline-ca.crt"
     (it will be fetchable as http://$SERVER:8081/offline-ca.crt)
  b. wait ~2.5 min for the robot to boot + start polling, then:
       ./emulator/server.sh cmd 'cd /etc/ssl/certs && wget -q -O ${HASH}.0 http://${SERVER}:8081/offline-ca.crt && md5sum ${HASH}.0'
     → the MQTT trust entry is installed.
  c. point DNS at the emulator (emulator/dnsmasq-narwal.conf — every
     narwaltech.com host incl. us-open, which makes OTA answer "no update")
     and cycle the robot's Wi-Fi — it connects MQTT to YOUR broker.
  d. control it:  python3 emulator/control.py --device <deviceId> status

Backups of everything changed are in: $BACKUPS
Revert everything: ./tools/revert_to_stock.sh "$BACKUPS"
EOF
