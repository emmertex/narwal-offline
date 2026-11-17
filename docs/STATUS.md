# Status — what works and what's blocked (2026-11-17)

## Works, live-verified on the real robot

- **Filesystem read** of any regular file over MQTT (`config_content/get`)
  — text + binary, including `/etc/shadow`, the device RSA key, ELF
  binaries, the system log.
- **Filesystem overwrite** of any existing file (`config_content/set`)
  — byte-exact (274 KB CA-bundle round-trip proven; robot TLS kept
  working through a forced reconnect).
- **CA installation** into `/etc/ssl/certs/ca-certificates.crt` — the
  174th cert in the robot's store.
- **OTA-channel TLS trust**: with the CA installed and the robot
  rebooted, its `us-open` OTA client accepted our CA-signed leaf on
  the first attempt (previously first attempts always failed).
- **OTA oracle** (from the parent project): the robot downloads
  packages from any URL our OTA response names and rejects tampered
  packages via the RSA signature (E1/E2/E3 experiments).
- **Emulator HTTPS** — running, SNI-routed, serving all Narwal hosts;
  robot connections arrive and negotiate TLS where the client uses the
  system bundle.
- **Offline broker** — mosquitto MQTT5 + our leaf, accepting app-side
  connections (`control.py` speaks the full command protocol).

## Blocked

**The robot's MQTT + device-API channels reject our CA** (`unknown_ca`
on every attempt, before and after CA install, across rebooks). These
channels use Alibaba's ID²/itls stack with a trust store separate from
the system bundle (per-device provisioned certs and/or compiled-in
CAs). Until that store is found and extended, the robot will not
connect to the offline broker, and full offline control waits on it.

Notes from the investigation:
- The `us-open` acceptance pattern initially looked like strict CA
  validation; on balance of evidence (the channel's known
  fail-then-lenient-retry behavior) it may be the lenient path
  accepting our leaf — but the strict-first-attempt behavior did
  change after the CA install, so the bundle is in play for at least
  one real client.
- During testing the robot went cloud-offline whenever DNS pointed
  MQTT at us; it self-recovered the moment DNS was reverted. No
  lasting state was observed. The robot is currently back on the real
  cloud, docked, healthy.
- The config-file **read** service started returning empty content
  after ~2 MB of transfers (list/set stayed fine; reboot didn't clear
  it) — an unexplained service state; revisit with a fresh robot
  window before relying on bulk reads.

## Next steps (ranked)

1. **Find the itls/ID² store** — once reads recover, walk
   `/userdata/`, `/oem/DISK/bin/pita/robokit/`, and any `id2`/`itls`
   paths for the device's TLS material and the communicator binary;
   dump + reverse the binary if needed (its config showed the
   auth flow; the MQTT trust decision lives in it).
2. **Sentinel broker experiment** — the emulator's login handler
   already returns sentinel hostnames in every plausible response
   field; when the robot's auth traffic flows through the emulator,
   watch DNS to learn which field (if any) selects its MQTT endpoint.
   If the endpoint is server-controlled, we may not need TLS trust at
   all (point it at a plaintext port).
3. **Robot MQTT CONNECT identity** — never captured (the robot
   rejects our broker before CONNECT). Worth one more armed window
   with `mosquitto` logging to catch it if the itls store is solved.
4. Root-password crack (`$1$` MD5-crypt hash in hand) — mostly
   moot while file write exists, but useful for UART later.
