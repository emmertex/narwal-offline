#!/usr/bin/env bash
# make-custom-apk.sh — build an offline-testing APK.
#
# Patches the app's Dart AOT image (libapp.so) so every
# "narwaltech.com" (14 chars) becomes "emulator.local" (14 chars) —
# same length, so no offsets shift — then rebuilds and signs.
# The patched app resolves *.emulator.local via YOUR DNS to the
# emulator; the emulator's login accepts any credentials.
#
# That alone is NOT enough to log in: the app's network_security_config
# has no trust-anchors override, so (per the stock Android default) its
# REST client trusts system CAs only — our offline CA's leaf on
# *.emulator.local gets rejected and login never completes (the app's
# MQTT client is the one that skips cert validation; REST does not —
# see docs/HOW_IT_WORKS.md §1/§7). So this script also bundles the
# offline CA as a raw resource inside base.apk and rewrites
# res/xml/network_security_config.xml to add it (plus "system") as a
# trust-anchor, via an apktool decode/edit/build pass with --no-src
# (keeps the original dex untouched — full smali disassembly of this
# app OOMs apktool's default heap and we don't need it).
#
# Requirements: apktool, zipalign + apksigner (Android build-tools)
# or jarsigner, and the app bundle. Works with the split APKM from
# APKMirror (base.apk + split_config.arm64_v8a.apk).
#
# Usage: ./make-custom-apk.sh /path/to/APKM-or-dir outdir [ca-cert]
set -euo pipefail

SRC="${1:?usage: make-custom-apk.sh <apkm-or-dir> <outdir> [ca-cert]}"
OUT="${2:?outdir}"
HERE="$(cd "$(dirname "$0")" && pwd)"
CA_CERT="${3:-$HERE/../pki/offline-ca.crt}"
[ -f "$CA_CERT" ] || { echo "CA cert not found: $CA_CERT (generate pki/offline-ca.crt first, or pass it as arg 3)"; exit 1; }

mkdir -p "$OUT/work"
if [ -f "$SRC" ]; then
  unzip -o -q "$SRC" -d "$OUT/work"
fi
BASE="$OUT/work/base.apk"
ARM="$OUT/work/split_config.arm64_v8a.apk"
[ -f "$BASE" ] || BASE="$SRC/base.apk"
[ -f "$ARM" ] || ARM="$SRC/split_config.arm64_v8a.apk"
[ -f "$BASE" ] && [ -f "$ARM" ] || { echo "base + arm64 split required"; exit 1; }
# the rest of the script works on copies named base.apk /
# split_config.arm64_v8a.apk inside $OUT/work, regardless of whether
# SRC was a .apkm file (already unzipped there above) or a directory.
[ "$BASE" = "$OUT/work/base.apk" ] || cp "$BASE" "$OUT/work/base.apk"
[ "$ARM" = "$OUT/work/split_config.arm64_v8a.apk" ] || cp "$ARM" "$OUT/work/split_config.arm64_v8a.apk"

# 1. patch libapp.so inside the base split
cd "$OUT/work"
unzip -o -q "split_config.arm64_v8a.apk" lib/arm64-v8a/libapp.so
python3 - <<'EOF'
data = bytearray(open("lib/arm64-v8a/libapp.so","rb").read())
needle = b"narwaltech.com"
repl   = b"emulator.local"
assert len(needle) == len(repl)
n = data.count(needle)
if n == 0:
    # already patched?
    print("no occurrences (already patched?)")
else:
    data = data.replace(needle, repl)
    open("lib/arm64-v8a/libapp.so","wb").write(data)
    print(f"patched {n} occurrences of narwaltech.com -> emulator.local")
EOF
python3 - <<'PYEOF'
import zipfile, shutil
src = "split_config.arm64_v8a.apk"; tmp = "arm64-new.apk"
with zipfile.ZipFile(src) as zin, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
    for item in zin.infolist():
        if item.filename == "lib/arm64-v8a/libapp.so":
            zout.write("lib/arm64-v8a/libapp.so", item.filename)
        else:
            zout.writestr(item, zin.read(item.filename))
shutil.move(tmp, src)
print("libapp.so replaced inside split")
PYEOF

# 2. patch base.apk's network_security_config to trust the offline CA
#    (bundled as a raw resource — no manual "install CA cert" step on
#    the phone needed).
rm -rf base_decoded
apktool d -f -s -o base_decoded base.apk >/dev/null
mkdir -p base_decoded/res/raw
cp "$CA_CERT" base_decoded/res/raw/narwal_offline_ca.pem
cat > base_decoded/res/xml/network_security_config.xml <<'EOF'
<?xml version="1.0" encoding="utf-8"?>
<network-security-config>
    <base-config cleartextTrafficPermitted="false">
        <trust-anchors>
            <certificate src="@raw/narwal_offline_ca" />
            <certificate src="system" />
        </trust-anchors>
    </base-config>
    <domain-config cleartextTrafficPermitted="true">
        <domain includeSubdomains="true">localhost</domain>
    </domain-config>
</network-security-config>
EOF
apktool b base_decoded -o base.apk.new >/dev/null
mv base.apk.new base.apk
echo "network_security_config patched: trusts offline CA + system"

# 3. sign all splits with one key
KEY="$OUT/work/test.keystore"
[ -f "$KEY" ] || keytool -genkeypair -keystore "$KEY" -alias narwaloffline \
  -keyalg RSA -keysize 2048 -validity 10000 -storepass narwaloffline -keypass narwaloffline \
  -dname "CN=narwal-offline" 2>/dev/null

if command -v apksigner >/dev/null; then
  for apk in base.apk split_config.arm64_v8a.apk; do
    zipalign -f 4 "$apk" "$apk.aligned" && mv "$apk.aligned" "$apk"
    apksigner sign --ks "$KEY" --ks-pass pass:narwaloffline --key-pass pass:narwaloffline "$apk"
  done
else
  echo "apksigner not found — falling back to jarsigner (v1 only; may not install on Android 11+)"
  for apk in base.apk split_config.arm64_v8a.apk; do
    jarsigner -keystore "$KEY" -storepass narwaloffline "$apk" narwaloffline >/dev/null
  done
fi

cp base.apk "$OUT/narwal-offline-base.apk"
cp split_config.arm64_v8a.apk "$OUT/narwal-offline-arm64.apk"
cat <<DONE

Built:
  $OUT/narwal-offline-base.apk
  $OUT/narwal-offline-arm64.apk

Install BOTH (adb install-multiple, or \`pm install-create\` ritual):
  adb install-multiple $OUT/narwal-offline-base.apk $OUT/narwal-offline-arm64.apk

DNS: point *.emulator.local at your emulator box
(dnsmasq: address=/emulator.local/10.x.y.z)
Login with anything — the emulator accepts all credentials.
DONE
