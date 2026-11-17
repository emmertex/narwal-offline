#!/usr/bin/env bash
# make-custom-apk.sh — build an offline-testing APK.
#
# Patches the app's Dart AOT image (libapp.so) so every
# "narwaltech.com" (14 chars) becomes "emulator.local" (14 chars) —
# same length, so no offsets shift — then rebuilds and signs.
# The patched app resolves *.emulator.local via YOUR DNS to the
# emulator; the emulator's login accepts any credentials.
#
# Requirements: apktool, zipalign + apksigner (Android build-tools)
# or jarsigner, and the app bundle. Works with the split APKM from
# APKMirror (base.apk + split_config.arm64_v8a.apk).
#
# Usage: ./make-custom-apk.sh /path/to/APKM-or-dir outdir
set -euo pipefail

SRC="${1:?usage: make-custom-apk.sh <apkm-or-dir> <outdir>}"
OUT="${2:?outdir}"
HERE="$(cd "$(dirname "$0")" && pwd)"

mkdir -p "$OUT/work"
if [ -f "$SRC" ]; then
  unzip -o -q "$SRC" -d "$OUT/work"
fi
BASE="$OUT/work/base.apk"
ARM="$OUT/work/split_config.arm64_v8a.apk"
[ -f "$BASE" ] || BASE="$SRC/base.apk"
[ -f "$ARM" ] || ARM="$SRC/split_config.arm64_v8a.apk"
[ -f "$BASE" ] && [ -f "$ARM" ] || { echo "base + arm64 split required"; exit 1; }

# 1. patch libapp.so inside the base split
cd "$OUT/work"
unzip -o -q "$ARM" lib/arm64-v8a/libapp.so
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

# 2. sign all splits with one key
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

Install BOTH (adb install-multiple, or `pm install-create` ritual):
  adb install-multiple $OUT/narwal-offline-base.apk $OUT/narwal-offline-arm64.apk

DNS: point *.emulator.local at your emulator box
(dnsmasq: address=/emulator.local/10.x.y.z)
Login with anything — the emulator accepts all credentials.
DONE
