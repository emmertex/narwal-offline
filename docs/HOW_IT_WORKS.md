# How all of this works — the complete write-up

Every mechanism in this repo, what the robot does, and how we found it.
Hardware: Narwal Freo X Ultra (product `EHf6cRNRGT`, board IP1221-S /
`mr813-ip1221_j3s`, Allwinner sun50iw10p1, Tina Linux 3.5.1), firmware
v01.09.01.00, app `com.narwal.pita_global` 2.11.90.

## 1. The cloud architecture (recon)

The robot and the app live on two parallel protocol planes:

- **App plane**: REST (`{region}-app.narwaltech.com`, JWT via
  `Auth-Token` header) + MQTT 5 over TLS :8883
  (`{region}-01.mqtt.narwaltech.com`, username = user uuid, password =
  the JWT). Commands are protobuf messages on
  `/{productKey}/{deviceId}/{command}` topics with MQTT5
  ResponseTopic + CorrelationData; payloads are wrapped in a small
  "Narwal frame" (`0x01 varint(len) uuid uuid`).
- **Robot plane**: the robot authenticates itself with a per-device
  RSA-2048 key (stored at `/robotdata/static/robot_info/private_key.pem`)
  against `sweeper-auth-server/v1/login`, bootstraps via
  `iot-auth-global.narwaltech.com/device-bootstrap-server/v1/bootstrap`,
  polls OTA at `us-open.narwaltech.com/open-platform-manage-ota/v1/iot-platform/ota/check`
  with an RS256 device JWT, reports telemetry to `us.device.narwaltech.com`,
  and connects to the same MQTT broker (client id prefix `sweeper_`).

We captured all of this with a dnsmasq + TLS-impersonation rig on the
LAN (the app's MQTT client does not validate certificates — that
yielded the account JWT; the robot's own TLS clients do validate, which
is the central obstacle this repo deals with).

## 2. The filesystem access primitive (the crown jewel)

The robot's MQTT surface includes a "dynamic params" service the app
uses to manage the robot's ~150 OEM config files:

```
dynamic_params/config_file_list/get    (empty request → registry listing)
dynamic_params/config_content/get      {FileInfo{name, absolutePath, oemPath, isDefault, isActivated}}
dynamic_params/config_content/set      {FileInfo, content}   ← overwrite
dynamic_params/config/apply            {FileInfo}            ← after set
```

`FileInfo.absolutePath` is passed to the robot's file layer with **no
validation, no sandbox, and no path restrictions**:

- `get` reads **any regular file** on the robot — text or binary (we
  pulled a full 555 KB ELF through it), as the service user (root):
  `/etc/shadow`, `/etc/ssl/certs/*`, `/robotdata/...`, anything.
  Nonexistent paths return an empty reply; directories and procfs
  return empty content.
- `set` overwrites **any existing file** (new paths are refused with
  "not exists"). There is no content validation. `write` fidelity is
  byte-exact (proven by writing the 274 KB CA bundle back over itself
  and watching the robot's TLS keep working).

`tools/narwal_access.py` packages exactly this: cloud login (the access
token conveniently doubles as its own refresh token), MQTT app-client
with the Narwal frame + correlation matching, and the three file
operations.

Quirk we hit (documented so you don't panic): after heavy use, content
reads started returning empty while list/set kept working — cause not
fully identified (suspect: a read quota or service state after ~2 MB of
transfers; a reboot did not fix it). Writes and lists remained solid.

## 3. The PKI move

Because `set` can overwrite the system CA bundle, the play is:

1. Generate an offline CA (`pki/offline-ca.*`).
2. `install-ca` = read `/etc/ssl/certs/ca-certificates.crt`, append your
   CA PEM, write it back (the tool backs up the original first).
3. Issue per-host leaf certs (`pki/issue-leaves.sh`) and serve them.

Result, verified live: TLS clients that use the **system bundle** now
trust us — the OTA client connected first-attempt to a `us-open`
endpoint with our leaf. ⚠️ But not every client uses the system bundle
(see §5).

## 4. The emulator

`emulator/emulator.py` impersonates every HTTPS host the robot/app use,
on one port, SNI-routed:

- `/sweeper-auth-server/v1/login`, `/token/refresh` → fixed offline
  tokens (and — during the broker-discovery experiment — sentinel
  fields to learn which key the robot honors for its MQTT endpoint).
- `device-bootstrap-server/v1/bootstrap` → an endpoint document.
- `/ota/check` → always "no update" (the exact real JSON shape).
- `getDeviceInfoList`, iot-ota, app-ota-server → the shapes the real
  cloud returns, filled for your robot.
- everything else → `{code:0}` (the robot tolerates this).

The MQTT broker (mosquitto, MQTT5, `allow_anonymous`, leaf from your
CA) hosts the `/{product}/{device}/...` topic space; `control.py`
speaks the same command protocol as the cloud integration, so with the
robot connected to it you have full offline control.

DNS is the routing layer: `emulator/dnsmasq-narwal.conf` maps every
`narwaltech.com` host to the emulator. The `local=/narwaltech.com/`
line is load-bearing: dnsmasq's `address=` alone still forwards AAAA
queries upstream, and **the robot prefers IPv6** — without it, the
hijack silently leaks to the real cloud.

## 5. The TLS trust story (solved)

The robot's MQTT client (inside `application_node`, paho MQTTAsync)
configures `set_enable_server_cert_auth(true)` with
`ca_path("/etc/ssl/certs")` — an OpenSSL **directory** trust store that
only consults `<subject-hash>.0` entries. That is why appending a CA to
any bundle file changes nothing for MQTT: you must add your CA as
`/etc/ssl/certs/<your-CA-subject-hash>.0` (a hash-named file — the
setup flow installs it through the command channel; a plain file copy
cannot be done over the MQTT write primitive because it cannot create
new files). With that entry in place the robot verifies a leaf from
your CA and connects to your broker — verified live end-to-end.

The other clients: the OTA checker (us-open) is lenient and also honors
the system bundle (your CA can be appended there too); the bootstrap
and telemetry clients still reject third-party CAs — they don't matter
for local control (bootstrap only gates first-boot provisioning).

### 5a. A likely simpler path: no device write needed at all (unverified live)

A third party reported (for a Freo Z, not this X Ultra, but the cloud
stack looks shared) that the robot's MQTT client validates the
certificate *chain* but never checks the hostname/CN the leaf was
issued for — meaning a publicly-trusted certificate for **any** domain
you control, served from your own broker, plus a DNS override for the
real `narwaltech.com` hosts, is enough. No on-device file write (§2's
exploit chain) required at all. They specifically called out ZeroSSL
working and Let's Encrypt not working.

Static analysis backs this up on two independent fronts:

- The stock firmware's `/etc/ssl/certs` (extracted from the `.npkg`,
  `robot_dump/firmware_plaintext/.../rootfs_tree/etc/ssl/certs/`) has
  `USERTrust_RSA_Certification_Authority.crt` present as a proper
  hash-named file (`fc5a8f99.0` — the exact chain root ZeroSSL's
  free-tier certs use) but no `ISRG_Root_X1.crt` (Let's Encrypt's
  actual root) — only the long-expired `DST_Root_CA_X3` cross-sign.
  Matches the report exactly.
- `application_node`'s imports from `libpaho-mqtt3as.so` (the Paho
  C++ wrapper) are only `mqtt::ssl_options::set_enable_server_cert_auth`
  and `::ca_path` (`nm -D robot_dump/binaries/application_node`) — no
  call anywhere to a hostname/CN verification setter, even though the
  wrapper exposes one and the underlying C library's `X509_check_host`
  is linked into `libpaho-mqtt3as.so` itself (confirmed via `strings`,
  `"X509_check_host for hostname %.*s failed, rc %d"`). The capability
  exists in the library; nothing in the robot's own code turns it on.

Not yet verified live (needs a real domain for ZeroSSL's ACME
validation — ACME needs to prove domain ownership over the real
internet, not just local DNS). If you have a domain and want to
confirm it end-to-end: get a cert for any hostname via ZeroSSL, serve
it as mosquitto's cert instead of the offline CA's leaf, point DNS for
the real `*.narwaltech.com` MQTT host at your broker, and see whether
an unmodified robot connects without ever touching its filesystem. If
this holds, it's a meaningfully simpler and more broadly repeatable
technique than §2's primitive, worth writing up on its own.

## 6. The firmware side (why we didn't need it)

For completeness: firmware updates are `.npkg` containers — encrypted
body (per-package AES key, RSA-wrapped, robot-side only) + signing
cert chain + signature + a 256-byte plaintext trailer (CRC-checked).
The OTA check channel accepts fully attacker-controlled responses
(verified: the robot downloaded packages from our LAN server on cue),
but the RSA signature verification rejects any modified package — we
proved that and left it at that. No firmware was ever modified or
installed; everything in this repo works through configuration and
networking only.

## 7. The custom APK

`apk/make-custom-apk.sh` rebuilds the official app with every
`narwaltech.com` (14 chars) replaced by `emulator.local` (also 14) in
the Dart AOT image — a same-length binary patch, so no offsets move.
The patched app then resolves all its hosts via your DNS to the
emulator, and logs in against the emulator's fake tokens. Use it to
test the emulator's app-plane before pointing a real robot at it.

The string patch alone is not enough to log in. The app's
`network_security_config` carries no `trust-anchors` override, so (per
the stock Android default) its REST client trusts **system CAs only**
— unlike the robot plane, which the MITM rig confirmed is lenient only
for MQTT, not REST (`tools/mitm/README.md` in narwal-decompile). Our
offline CA's leaf on `*.emulator.local` would be rejected and login
would never complete. So the script also decodes `base.apk` with
apktool (`-s`/`--no-src`, to skip smali — a full disassemble/reassemble
of this app's dex OOMs apktool's default heap and we don't need
source), bundles the offline CA as a raw resource, and rewrites
`res/xml/network_security_config.xml` to trust it (plus `system`) —
no "install this CA on your phone" step required.

The emulator's `/login` response also has to hand back an MQTT host
the *client actually has DNS for*: the robot and stock app only have
`narwaltech.com` overridden, the patched APK only has `emulator.local`.
`_login` mirrors the `Host` header it was called on to pick the right
family (`us-01.mqtt.narwaltech.com` vs `us-01.mqtt.emulator.local`) —
mosquitto itself only serves one fixed leaf regardless, which is fine
since the app's MQTT client doesn't validate the cert anyway.

One more install-time gotcha: `base.apk`'s manifest declares
`android:requiredSplitTypes="base__abi,base__density"` — the device
install is rejected (`INSTALL_FAILED_MISSING_SPLIT`) unless **both** an
ABI split and a density split are present, not just the arm64 one.
`make-custom-apk.sh` now pulls in every `split_config.*dpi.apk` sitting
next to the arm64 split (unmodified content, just re-signed with the
rest — every split in one install must share the same signing cert)
and prints the full `adb install-multiple` line with all of them.

### 7a. Flutter's own TLS stack (a second, separate trust store)

Getting a clean install wasn't enough to log in. Dart's own networking
(`dart:io`, used for every REST call) is a completely separate BoringSSL
build baked into `libflutter.so` — it reads Android's **system** CA
directory (`/system/etc/security/cacerts`) directly and never consults
`network_security_config.xml` at all. That file only ever covered
Android's own Java-level stack (OkHttp, WebView). Same underlying
problem as the robot's hash-dir CA store (§5), but this time
unreachable: the test phone isn't rooted, so there's no way to drop a
hash-named cert file into that directory.

The fix: [`reflutter`](https://github.com/null0x4d/reflutter) (`pip
install reflutter`) identifies the app's exact Flutter engine build from
the snapshot hash embedded in `libapp.so` and fetches a pre-patched
`libflutter.so` for that build with Dart's own certificate validation
disabled — a pure binary swap, no runtime instrumentation needed.
`make-custom-apk.sh` runs this automatically on the arm64 split.

### 7b. The region-policy gate (not a login problem)

Even with TLS fully working, the app blocked the device card with "Due
to policy reasons, this device is not supported in the country/region
where your user account is registered." This looks like a login/account
problem but isn't — it's unrelated to `user-device-platform-server/
policy/getPolicyStatus` (that endpoint is actually
`PolicyStatusModel`/privacy-policy-agreement plumbing, confirmed by
decompiling it with [blutter](https://github.com/worawit/blutter)).

The real source, traced through the blutter-decompiled Dart source:
`RobotNetDetector.detectCurrentSite` → `RobotNetCheckRepo.
getDeviceDetailInfo` → `NRDeviceDetailRequester`, which hits
`GET /sweeper-app-server/v1/sweeper/getDeviceInfo/`. The response model
(`nr_networking/model/device/device_detail_info.dart`) carries a field
literally named `is_same_site` (bool) — the emulator's old generic
`{result: {}}` catch-all left it absent, which the app reads as false.
`emulator.py` now serves this endpoint directly with `is_same_site:
true`, `device_site_code`, and `support_country_list`, plus a proper
`/iot-broker-discover/app/v1/broker/discover` response (the endpoint the
app actually polls for the MQTT broker address, distinct from the one
`_login` returns).

When reverse-engineering a response shape blind, decompiling the Dart
AOT snapshot with blutter and grepping the `_$<Model>FromJson` method
for its literal JSON key strings is far more reliable than guessing
field names from REST traffic alone — `json_serializable`'s generated
code embeds every key as a plain string constant right next to the
field read.
