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

## 5. The remaining blocker: the itls trust store

The robot runs at least three TLS client stacks:

| Channel | Stack | Trusts our installed CA? |
|---|---|---|
| OTA check (us-open) | system-CA client | **YES** (first-attempt accept after install+reboot) |
| device API (us.device) | Alibaba ID²/itls | no — `unknown_ca` every time |
| MQTT :8883 | Alibaba ID²/itls (client id `sweeper_*`) | no — `unknown_ca` every time |

So the CA-bundle write alone does not flip the channels that matter
most. The itls store is per-device provisioned (ID²) and/or compiled
in; finding it is the open task — likely via the same file-read
primitive once reads recover (hunt `/userdata/...` ID² paths), or by
dumping the communicator binary. This is also why the robot ended up
cloud-offline during testing: its MQTT kept rejecting our broker while
DNS pointed there. Everything is currently reverted and the robot is
back on the real cloud.

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
