#!/usr/bin/env bash
# revert_to_stock.sh — put the robot back the way you found it.
#
# Undoes everything owner_setup.sh (and the CA install) did:
#   1. removes the CA hash entry  /etc/ssl/certs/<hash>.0   (via the
#      command channel while it is still alive)
#   2. restores /etc/sysctl.conf from the backup              (MQTT write)
#   3. restores /etc/rc.local from the backup (kills the poller
#      channel at the next boot)                              (MQTT write)
#   4. reboots the robot
#   5. reminds you to revert DNS/DHCP (the robot self-recovers on the
#      real cloud as soon as DNS is normal — verified repeatedly)
#
# Usage: ./revert_to_stock.sh [backup-dir]     (default: newest in backups/)
set -euo pipefail
HERE="$(cd ${0%/*} && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
BK="${1:-}"
if [ -z "$BK" ]; then BK="$(ls -1dt "$ROOT"/backups/*/ 2>/dev/null | head -1)"; fi
[ -n "${BK:-}" ] && [ -f "$BK/rc.local.orig" ] || {
  echo "no backup dir with rc.local.orig found — pass the path from owner_setup.sh"; exit 1; }
echo "[*] using backup: $BK"

HASH="$(openssl x509 -in "$ROOT/pki/offline-ca.crt" -noout -subject_hash | head -1)"
echo "[1/4] removing the CA entry via the command channel (if the robot is polling)…"
"$ROOT/emulator/server.sh" cmd \
  "rm -f /etc/ssl/certs/${HASH}.0 && echo CA-removed; ls /etc/ssl/certs/ | grep -c ${HASH} || true" \
  || echo "      (channel not up — remove /etc/ssl/certs/${HASH}.0 manually later)"

acc() { python3 "$HERE/narwal_access.py" "$@"; }

echo "[2/4] restoring sysctl.conf…"
acc write /etc/sysctl.conf --from-file "$BK/sysctl.conf.orig"

echo "[3/4] restoring rc.local…"
acc write /etc/rc.local --from-file "$BK/rc.local.orig"

echo "[4/4] rebooting…"
python3 - <<PY
import json, os, sys
sys.path.insert(0, "$HERE")
import narwal_access as na
tok = json.load(open(os.path.join("$HERE", "narwal_tokens.json")))
cloud = na.Cloud("us"); cloud.tok = tok
d0 = cloud.devices()[0]
link = na.RobotLink(d0["productId"], d0["deviceId"], tok["uuid"], tok["token"],
                    na.BROKERS.get(tok.get("region_style", "us"), na.BROKERS["us"]))
link.connect()
try:
    link.wake()
    print("      reboot:", "acked" if link.request("common/reboot", b"", timeout=20) is not None else "timeout")
finally:
    link.cli.loop_stop()
PY

cat <<EOF
Done. Robot-side changes are reverted (verified at next boot).
Remaining (your LAN):
  - DNS: remove the narwaltech.com overrides from your dnsmasq/router —
    the robot returns to the real cloud on its own.
  - Optionally stop the emulator stack: ./emulator/server.sh stop
EOF
