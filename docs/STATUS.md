# Status — what works and what's blocked (updated 2026-11-18)

## Works, live-verified on the real robot

- **Filesystem read** of any regular file over MQTT (`config_content/get`)
  — text + binary. **The earlier "reads return empty" degradation was
  client-side bugs in `narwal_access.py` (bad auth frame, paho callback
  arity, missing response-frame strip) — fixed; reads are reliable.**
  Ceiling ≥1.2 MB per read (big files are slow, not capped: 1.2 MB ≈ 76 s).
- **Filesystem overwrite** of any existing file (`config_content/set`)
  — byte-exact (274 KB CA-bundle round-trip proven).
- **CA installation** into `/etc/ssl/certs/ca-certificates.crt` — the
  174th cert in the robot's store; verified still present 2026-11-17.
- **IPv6 disabled robot-side (2026-11-17)** — appended
  `net.ipv6.conf.{all,default}.disable_ipv6=1` to `/etc/sysctl.conf`
  (owner directive; original kept in `robot_dump/etc_sysctl.conf.orig`),
  rebooted, verified: v6 address dead, v4 alive, robot healthy on cloud.
  This also permanently kills the "robot prefers IPv6" DNS-hijack leak.
- **OTA-channel TLS trust**: with the CA installed and the robot
  rebooted, its `us-open` OTA client accepted our CA-signed leaf on the
  first attempt (previously first attempts always failed).
- **OTA oracle** (parent project): the robot downloads packages from any
  URL our OTA response names and rejects tampered packages via the RSA
  signature (E1/E2/E3 experiments).
- **Emulator HTTPS** — SNI-routed, serving all Narwal hosts (now with
  SNI logging on handshake failures — the 2026-11-17 evening window
  showed 120 `unknown_ca` rejections from the robot with no SNI record,
  which that experiment needed).
- **Offline broker** — mosquitto MQTT5 + our leaf, accepting app-side
  connections (`control.py` speaks the full command protocol).

## Robot internals mapped (2026-11-17/18 — `robot_dump/`)

ROS Kinetic system launched by `/oem/DISK/bin/pita/start.sh`; nodes:
map_server, hal, hal_monitor, power_health, mapping, planning,
application, perception, **wireless_communicator** (the MQTT/cloud
client), toolkit. Custom libs under `/oem/DISK/bin/pita/lib/`
(`librobokit_*` all pulled + `libcommon.so` + `libpaho-mqtt3as.so`).
`toolkit_recorder` (1.05 MB) and the framework libs are in hand for
decompilation. Logger filenames: `<dir>/<YYYYMMDDHHMM>.<n>.<name>`.
Endpoints: `robot_dump/robotdata_static_robot_info_domain.ini`.

## Blocked

**The robot's MQTT + device-API channels reject our CA**
(`unknown_ca`). The 2026-11-18 sentinel window (see
`robot_dump/experiments/sentinel-2026-11-18/FINDINGS.md`) proved the
**bootstrap client (iot-auth-global) also uses the private trust
store** (strict unknown_ca for 10+ min with our CA in the system
bundle), and that every other cloud channel is gated behind bootstrap
at boot — the robot has a default/compiled-in MQTT endpoint
(us-01.mqtt, resolved but never connected). The trust decision lives in
the `wireless_communicator_node` binary (>read ceiling ~1.2–2 MB, so
far unreachable via `config_content/get`; no itls/ID² strings in any
pulled lib; no id2/cert files found under `/robotdata`, `/userdata`,
`robokit_platform/conf`).

## Next steps (ranked)

1. **Trigger the robot's log-upload flow as the account holder** — the
   robot reaches for `narwal-robot-logs-06.oss-us-west-1.aliyuncs.com`
   even while cloud-offline. Drive the app's upload-log API
   (`/sweeper-log-server/v2/file/getUploadToken` → robot gathers logs →
   OSS PUT) and fetch the upload: the communicator's logs should name
   its cert paths / TLS config — no binary needed.
2. **Bigger-file read tricks** — the read ceiling is a hard RPC cap
   between 1.2 MB (works, 76 s) and node-binary sizes (silent).
   Possible: `developer/file_upload` request fuzzing (factory-only
   proto), breakpad minidumps under `/userdata/mini_dump/*`.
3. **UART (Track D in the parent repo)** — now trivially productive:
   full v0 protocol documented, board pads known; the guaranteed path
   to the communicator binary + itls store if 1–2 stall.
4. Root-password crack (`$1$` MD5-crypt hash in hand) — mostly moot
   while file write exists, but useful for UART later.

## Operational notes

- **Never run two dynamic_params clients concurrently** against one
  robot — response topics are shared and replies cross-match (matching
  is now correlation-strict, but concurrent sessions still race).
- `/proc` reads return empty (the service reads by file size; procfs
  reports 0). Directories and symlinks also read as 0 B — use
  known-path probing (`tools/fsprobe.py`).
- The robot was rebooted 2026-11-17 23:22 (IPv6 apply + verify); it came
  back healthy on the real cloud, docked.
