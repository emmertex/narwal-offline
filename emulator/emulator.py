#!/usr/bin/env python3
"""Narwal offline emulator — the robot's entire "cloud", on your LAN.

One process serves HTTPS on :443 for every Narwal host (SNI-routed certs
from pki/leaves/): device auth, bootstrap, OTA (always "no update"),
device-API catch-all, and the app-side REST (login/refresh/device-list).

MQTT runs separately (mosquitto.conf) on :8883 with a leaf from your
offline CA. The robot reaches all of it via DNS overrides
(dnsmasq-narwal.conf) once its CA bundle trusts your CA
(tools/narwal_access.py install-ca).

Usage:  python3 emulator.py --pki ../pki [--port 443]
Logs:   /tmp/narwal_emulator.log
"""
from __future__ import annotations

import argparse
import json
import os
import ssl
import struct
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    import paho.mqtt.client as mqtt_client
except ImportError:
    mqtt_client = None

ROBOT = {  # edit for your robot — `python3 ../tools/narwal_access.py devices`
    "productId": "EHf6cRNRGT",
    "deviceId": "0123456789abcdef0123456789abcdef",
    "robotName": "Freo X Ultra",
    "snNumber": "YF1AAM2443XX00X0000",
    "firmwareVersion": "v01.09.01.00",
}

FAKE_JWT = "offline-emulator-token." + "x" * 200
FAKE_REFRESH = "offline-emulator-refresh"

NO_UPDATE = {"code": 0, "msg": "succeed!", "success": True,
             "result": {"autoUpgrade": False, "confuse": False, "forceUpgrade": False,
                        "ignoreBattery": False, "robotApkSize": None,
                        "robotDownloadUrl": "", "robotSign": None,
                        "robotUpgradeType": None, "robotVersion": None,
                        "upgradeDescribe": None}}

LOG = os.environ.get("NARWAL_EMU_LOG", "/tmp/narwal_emulator.log")


def w(line: str):
    with open(LOG, "a") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {line}\n")


# ------------------------------------------------------------ live robot state
# work-status/get (HTTP, used for the device-LIST card) used to hardcode
# battery_percentage=80 forever, while the device HOMEPAGE shows the real
# number because it reads it off the robot's own MQTT status directly
# (peer-to-peer through mosquitto, bypassing us entirely). Rather than
# duplicate that mismatch, subscribe to the same MQTT topic here and feed
# the real number back into the HTTP response too.
#
# status/robot_base_status is a RobotBaseStatus protobuf
# (app_protol/proto/core/device_shadow/) — we don't have the full .proto,
# but the wire format is self-describing enough to pull one field out
# generically: field 2, wiretype 5 (fixed32), decodes as an IEEE-754 float
# and was empirically 100.0 while the app displayed "100%" live.
ROBOT_STATE = {"battery": 80}
_state_lock = threading.Lock()


def _decode_protobuf_fields(data: bytes) -> dict[int, list]:
    out: dict[int, list] = {}
    i = 0
    n = len(data)
    while i < n:
        tag, shift = 0, 0
        while True:
            b = data[i]; i += 1
            tag |= (b & 0x7f) << shift
            if not (b & 0x80):
                break
            shift += 7
        field_num, wire_type = tag >> 3, tag & 0x7
        if wire_type == 0:  # varint
            val, shift = 0, 0
            while True:
                b = data[i]; i += 1
                val |= (b & 0x7f) << shift
                if not (b & 0x80):
                    break
                shift += 7
        elif wire_type == 1:  # fixed64
            val = data[i:i + 8]; i += 8
        elif wire_type == 2:  # length-delimited
            length, shift = 0, 0
            while True:
                b = data[i]; i += 1
                length |= (b & 0x7f) << shift
                if not (b & 0x80):
                    break
                shift += 7
            val = data[i:i + length]; i += length
        elif wire_type == 5:  # fixed32
            val = data[i:i + 4]; i += 4
        else:
            break  # unsupported wire type; stop rather than misparse
        out.setdefault(field_num, []).append(val)
    return out


def _on_mqtt_message(_client, _userdata, msg):
    if not msg.topic.endswith("/status/robot_base_status"):
        return
    try:
        fields = _decode_protobuf_fields(msg.payload)
        raw = fields.get(2, [None])[0]
        if isinstance(raw, bytes) and len(raw) == 4:
            battery = struct.unpack("<f", raw)[0]
            with _state_lock:
                ROBOT_STATE["battery"] = round(battery)
    except Exception as e:
        w(f"mqtt state decode error: {e}")


def _start_mqtt_state_listener():
    if mqtt_client is None:
        w("paho-mqtt not installed; work-status/get will keep a static battery value")
        return

    def run():
        client = mqtt_client.Client(mqtt_client.CallbackAPIVersion.VERSION2)
        client.on_message = _on_mqtt_message

        def on_connect(c, _u, _f, _rc, _p=None):
            # Topics are published with a leading "/" (e.g.
            # "/EHf6cRNRGT/<deviceId>/status/robot_base_status"), i.e. an
            # empty first level — "+/+/status/..." doesn't match that,
            # subscribe to everything and filter by suffix in on_message.
            c.subscribe("#", qos=0)

        client.on_connect = on_connect
        while True:
            try:
                client.connect("127.0.0.1", 1884, 60)
                client.loop_forever()
            except Exception as e:
                w(f"mqtt state listener error, retrying: {e}")
                time.sleep(5)

    threading.Thread(target=run, daemon=True).start()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        w(f"{self.client_address[0]} {self.headers.get('Host','')} {fmt % args}")

    # ------------------------------------------------------------ plumbing
    def _json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except Exception as e:
            w(f"conn error: {e}")

    # ------------------------------------------------------------ routing
    def do_POST(self):
        body = self._read_body()
        host = (self.headers.get("Host") or "").split(":")[0]
        path = self.path.split("?")[0]
        w(f"POST {host}{path} body={body[:300]!r}")
        if path.endswith("/ota/check"):
            self._ota_check()
        elif "/login" in path or "/token/refresh" in path:
            self._login()
        elif "getDeviceInfoList" in path:
            self._devices(host)
        elif "policy/getPolicyStatus" in path:
            self._policy_status(body)
        elif "broker/discover" in path:
            self._broker_discover(host)
        elif "consumables/pull" in path:
            self._consumables_pull(body)
        elif "device-bootstrap" in path or "bootstrap" in path:
            # Sentinel experiment: if the robot honors a server-provided MQTT
            # endpoint, we see bootstrap-sentinel… in DNS and (port 1883) a
            # plaintext CONNECT attempt on the offline broker.
            self._json({"code": 0, "msg": "ok", "success": True,
                        "result": {"mqtt": {"host": "bootstrap-sentinel.narwal.offline",
                                            "port": 1884},
                                   "mqttHost": "bootstrap-sentinel.narwal.offline",
                                   "mqttPort": 1883,
                                   "broker": "bootstrap-sentinel.narwal.offline",
                                   "endpoints": {"mqtt": "bootstrap-sentinel.narwal.offline:1884"}}})
        else:
            self._json({"code": 0, "msg": "ok", "success": True, "result": {}})

    def do_GET(self):
        host = (self.headers.get("Host") or "").split(":")[0]
        path = self.path.split("?")[0]
        w(f"GET {host}{self.path}")
        if path.startswith("/fw/"):
            self._serve_fw(path[4:])
            return
        if "getDeviceInfoList" in path:
            self._devices(host)
        elif "/ota/check" in path:
            self._ota_check()
        elif "broker/discover" in path:
            self._broker_discover(host)
        elif "sweeper/getDeviceInfo" in path:
            self._sweeper_device_info(host)
        elif "work-status/get" in path:
            self._work_status()
        elif "oobe/getOobeStatus" in path:
            self._oobe_status()
        elif "schedule/getScheduleTaskList" in path:
            self._schedule_task_list()
        elif "bat/health/pull" in path:
            self._json({"code": 0, "msg": "ok", "success": True, "result": {}})
        else:
            self._json({"code": 0, "msg": "ok", "success": True, "result": {}})

    def do_DELETE(self):
        host = (self.headers.get("Host") or "").split(":")[0]
        w(f"DELETE {host}{self.path}")
        self._json({"code": 0, "msg": "ok", "success": True, "result": {}})

    def do_PUT(self):
        host = (self.headers.get("Host") or "").split(":")[0]
        w(f"PUT {host}{self.path}")
        self._json({"code": 0, "msg": "ok", "success": True, "result": {}})

    # ------------------------------------------------------------ OTA oracle
    def _ota_check(self):
        """Serve /var/tmp/narwal_cap/ota_override.json if present, else no-update.

        The override drives the robot's own genuine
        download -> verify -> DECRYPT -> stage pipeline from our server.
        """
        ov = os.path.join(os.path.dirname(LOG), "ota_override.json")
        try:
            with open(ov) as f:
                resp = json.load(f)
            w(f"ota/check: serving override {ov}")
            self._json(resp)
        except Exception as e:
            w(f"ota/check: no override ({e}); no-update")
            self._json(NO_UPDATE)

    def _serve_fw(self, name):
        fw_dir = "/var/tmp/narwal_cap/fw"
        safe = os.path.basename(name)
        p = os.path.join(fw_dir, safe)
        if not os.path.isfile(p):
            self.send_error(404)
            return
        size = os.path.getsize(p)
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(size))
        self.end_headers()
        with open(p, "rb") as f:
            while True:
                chunk = f.read(1 << 20)
                if not chunk:
                    break
                self.wfile.write(chunk)
        w(f"fw served: {safe} ({size}B)")

    # ------------------------------------------------------------ payloads
    def _login(self):
        # Standard app keys; the MQTT endpoint handed to the app/robot is
        # OUR broker. Mirror whichever domain family the client is
        # actually using (DNS only overrides one of these on a given
        # network): the real robot and the stock app see
        # narwaltech.com, the patched test APK sees emulator.local
        # (apk/make-custom-apk.sh) — handing back the wrong family means
        # the client resolves the MQTT host nowhere and never reaches
        # the local broker. (The app's MQTT client does not validate
        # certificates, so mosquitto's single narwaltech.com-named leaf
        # being served regardless is fine either way — only the
        # hostname needs to resolve.)
        host = (self.headers.get("Host") or "").split(":")[0]
        mqtt_host = ("us-01.mqtt.emulator.local" if host.endswith(".emulator.local")
                     else "us-01.mqtt.narwaltech.com")
        self._json({"code": 0, "msg": "ok", "success": True, "result": {
            "token": FAKE_JWT, "refresh_token": FAKE_REFRESH,
            "refreshToken": FAKE_REFRESH, "access_token": FAKE_JWT,
            "uuid": "offline-emulator-user",
            "mqtt_host": mqtt_host,
            "broker": mqtt_host,
            "host": mqtt_host,
            "mqtt": {"host": mqtt_host, "port": 8883},
            "endpoints": {"mqtt": f"{mqtt_host}:8883"},
            "server": mqtt_host,
            "domain": mqtt_host}})

    def _policy_status(self, body: bytes):
        # The app gates the device card on this (policy_type 3 = region/
        # feature availability, 5 = product-level) before it will let you
        # into the device homepage; our old generic {result:{}} catch-all
        # read as "no policy record = restricted", producing the "is not
        # supported for use in the current country/region... Due to
        # policy..." banner. Echo every requested policy_type back as
        # passing, under every plausible field name (status/pass/allow/
        # support) since we don't have the exact json_serializable key
        # without decompiling PolicyStatusModel — unknown extra keys are
        # harmless.
        try:
            req = json.loads(body or b"{}")
        except Exception:
            req = {}
        items = []
        for p in req.get("policy_type_list", [{"policy_type": 3}, {"policy_type": 5}]):
            items.append({**p, "status": 1, "pass": True, "allow": True,
                          "support": True, "policy_status": 1, "result": 1})
        self._json({"code": 0, "msg": "ok", "success": True, "result": items})

    def _broker_discover(self, host: str):
        # Same MQTT endpoint as _login, mirroring the domain family the
        # client actually has DNS for (see _login's comment) — the app
        # polls this independently of login to (re)learn the broker.
        #
        # NRMqttServersRequester (nr_networking/.../nr_mqtt_servers_requester.dart)
        # is consumed by a closure in device_hub_bootstrap.dart that does
        # `response.result` and AwaitWithTypeCheck's it directly against
        # `String?` — confirmed live via Frida (Gadget-injected APK, real
        # hardware), hooking the exact instruction right after that type
        # check and finding it never executes, because `result` here was a
        # nested dict, which fails the String? check and throws a silent
        # TypeError, aborting the whole MQTT connect attempt with no crash,
        # no log (the same silent-failure pattern as every other gate in
        # this app). `result` must be the bare hostname string.
        mqtt_host = ("us-01.mqtt.emulator.local" if host.endswith(".emulator.local")
                     else "us-01.mqtt.narwaltech.com")
        self._json({"code": 0, "msg": "ok", "success": True, "result": mqtt_host})

    def _work_status(self):
        # RobotWorkStatusManager._toSnapshot (device_card/business/
        # robot_work_status_manager.dart) derives the device card's
        # DeviceOnlineState from RobotWorkStatusWorkingStatus.isOffline(),
        # which is just `online == false` on this model
        # (device_card/models/robot_work_status.dart field "online").
        # Our old generic {result:{}} left "online" absent -> false ->
        # permanently "offline".
        #
        # But `online` alone isn't enough: DeviceCardStore.isDeviceOnline()
        # (device_card_store.dart) ALSO requires the snapshot's working
        # status to equal the exact DeviceWorkingStatus instance @1f69ee1.
        # DeviceWorkingStatus.fromStatus() (device_working_status.dart)
        # returns that same instance as its DEFAULT/fallback — when
        # `status` is null, or doesn't match any of the 21 recognized
        # status strings — so the "status" key is deliberately left out
        # here rather than guessed; omitting it is what actually produces
        # @1f69ee1. Sending the wrong status string (we previously tried
        # "free", which apparently maps to a *different*, non-default
        # instance) left isDeviceOnline() false, which is what was
        # silently swallowing "Go to Device Homepage": _gotoWorkHome
        # (device_card_list_controller.dart) checks isDeviceOnline() and,
        # if false, calls DeviceCardStore.classify() + shows
        # showScrubberOfflineDialog() and returns — no crash, no log,
        # just a silent bail confirmed live via Frida (Gadget-injected
        # APK, real hardware) tracing _gotoWorkHome's call graph.
        #
        # battery_status is `int?` (an index into a fixed BatteryStatus
        # list via firstWhere/fromValue) despite the human-readable
        # name — a string here hits `r3 as int?` in fromJson and throws
        # a CastError that silently breaks the whole status fetch
        # (caught upstream with no visible log), which is exactly what
        # was blocking "Go to Device Homepage" from doing anything.
        with _state_lock:
            battery = ROBOT_STATE["battery"]
        self._json({"code": 0, "msg": "ok", "success": True, "result": {
            "online": True,
            "battery_percentage": battery, "battery_status": 0,
            "fault": False, "need_subscribe": False}})

    def _sweeper_device_info(self, host: str = ""):
        # This is DeviceDetailInfo (nr_networking/model/device/device_detail_info.dart),
        # fetched by NRDeviceDetailRequester (path
        # /sweeper-app-server/v1/sweeper/getDeviceInfo/) and consumed by
        # two completely independent gates:
        #
        # 1. The device-card-level "Due to policy reasons... not supported
        #    in the country/region..." banner — satisfied by just having
        #    device_site_code match the account's country.
        # 2. _RobotNetDetector.detectSite() (robot_net_detector.dart),
        #    which site_policy_handler.dart's detectForNavigation() awaits
        #    as the final gate before "Go to Device Homepage" actually
        #    navigates anywhere — confirmed live via Frida (Gadget-injected
        #    APK on real hardware) tracing the whole _gotoWorkHome call
        #    graph down to this exact check. detectSite() reads
        #    `is_same_site` as `num?` (NOT bool — a bool here fails the
        #    type check and the field silently parses as something that
        #    never equals the value it's compared against). detectSite()
        #    does `cmp w1, #2` against the raw (DecompressPointer'd) field
        #    value, but Dart AOT arm64 tags SMIs as `value << 1`, so that
        #    raw immediate #2 decodes to the real Dart int 1 (confirmed via
        #    an AllocateArray call elsewhere in this same file that passes
        #    raw immediate #4 for an array that only ever stores 2
        #    elements). So the JSON value must be int 1, not 2. Anything
        #    else triggers an "environment mismatch" prompt path and
        #    returns false, making _gotoWorkHome bail out with no crash,
        #    no log.
        prefix = host.split("-", 1)[0]
        site = {"aus": "au"}.get(prefix, prefix) or "us"
        self._json({"code": 0, "msg": "ok", "success": True, "result": {
            "deviceId": ROBOT["deviceId"], "productId": ROBOT["productId"],
            "robotName": ROBOT["robotName"], "snNumber": ROBOT["snNumber"],
            "firmwareVersion": ROBOT["firmwareVersion"],
            "is_same_site": 1, "device_site_code": site,
            "support_country_list": [site],
            "online": True, "isOnline": True, "status": 1}})

    def _oobe_status(self):
        # GetOobeStatusModel (nr_networking/network/new/model/
        # get_oobe_status_model.dart) is just {"oobeStatus": int}.
        # NROobeMixin.queryOobeFinished() (nr_pita_home_controller.dart)
        # compares that int, tagged as a Dart AOT SMI (`value << 1`), to
        # raw immediate #2 -> real value 1 means "finished". Our old
        # generic {result:{}} catch-all parsed as oobeStatus=0 (not
        # finished), so every single navigation into the device (room
        # select, etc.) re-triggered the "Just a few more steps" OOBE
        # wizard — and since the wizard's own completion write only
        # covers the *local* isOobeFinished() flag, not whatever
        # subsequent call the app makes afterward, finishing or
        # cancelling it both just dumped you back at the device list.
        self._json({"code": 0, "msg": "ok", "success": True,
                    "result": {"oobeStatus": 1}})

    def _schedule_task_list(self):
        # TimingTaskModel (nr_networking/model/cleanplan/timing_task_model.dart)
        # is a single object with these four keys, each a list — our old
        # generic {result:{}} catch-all gave a dict missing all of them,
        # which the Timing Plan tab's fromJson may read with a bare `as
        # List` cast (no null-check), throwing a silently-swallowed
        # CastError that likely kept the whole tab from ever rendering
        # ("I cannot see them in the app either"). This is a cloud-side
        # cache/mirror of the schedules the robot holds — actually
        # creating/editing/deleting a schedule goes straight to the robot
        # over MQTT (app_protol/proto/mqtt/srv/schedule/*, topics like
        # /schedule/clean_schedule/{get,add,update,delete}), peer-to-peer
        # through mosquitto, with no involvement from this emulator at
        # all. So this fixes the tab from being stuck/blank, but any
        # schedule you had configured before won't reappear here until
        # you re-add it through the app — which should then round-trip
        # live to the real robot like normal.
        self._json({"code": 0, "msg": "ok", "success": True, "result": {
            "task_status_info": {},
            "scheduled_cleaning_tasks": [],
            "scheduled_video_cruise_tasks": [],
            "clean_records": []}})

    def _consumables_pull(self, body: bytes = b""):
        # NRConsumableInfoModel (nr_networking/network/new/model/
        # nr_consumable_info_model.dart) — one entry per requested "type"
        # code (the app sends {"type": [<codes>]} in the POST body). Our
        # old generic {result:{}} left every consumable's usage_duration
        # at 0, which the UI reads as "0% used" / "0 hours" for every
        # filter/brush/mop — not wrong exactly, just not live-tracked:
        # we have no persisted history of real runtime to report, so
        # this is a static, plausible-looking placeholder (life mostly
        # full) rather than a real number.
        try:
            req = json.loads(body or b"{}")
        except Exception:
            req = {}
        types = req.get("type") or [0]
        result = [{"type": t, "consumables_code": t, "item_type": 1,
                    "name": "", "subtitle": "", "icon": "",
                    "usage_duration": 20, "total_duration": 300,
                    "progress_bar_switch": True, "mall_bar_switch": False,
                    "reset_btn_switch": True, "mall_link": "",
                    "record_type": 0} for t in types]
        self._json({"code": 0, "msg": "ok", "success": True, "result": result})

    def _devices(self, host: str = ""):
        # deviceSiteCode has to match the account's registered country (set
        # by whatever region the app's first-run screen auto-picked from
        # the phone's locale/SIM) or the app refuses the device outright
        # ("Due to policy reasons, this device is not supported in the
        # country/region where your user account is registered."). Derive
        # it from the region prefix on the Host header the client is
        # actually using (e.g. "aus-app.emulator.local" -> "au") instead of
        # hardcoding one.
        prefix = host.split("-", 1)[0]
        site = {"aus": "au"}.get(prefix, prefix) or "us"  # aus -> au, us -> us
        self._json({"code": 0, "err_code": 10000, "msg": "ok", "result": {
            "deviceInfoList": [{
                "deviceId": ROBOT["deviceId"], "productId": ROBOT["productId"],
                "robotName": ROBOT["robotName"], "snNumber": ROBOT["snNumber"],
                "firmwareVersion": ROBOT["firmwareVersion"], "isAutoUpgrade": False,
                "deviceSiteCode": site, "extend": {},
            }],
            "productInfoList": [{
                "productId": ROBOT["productId"], "productModel": ROBOT["robotName"],
                "productGroup": "J4",
            }]}})


class SNIServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr, pki_dir: str):
        super().__init__(addr, Handler)
        self.pki = pki_dir
        self.ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        leaves = os.path.join(pki_dir, "leaves")
        if not self._load_any(leaves):
            raise SystemExit("no leaf certs in pki/leaves — run issue-leaves.sh first")
        self.ssl_ctx.set_servername_callback(self._sni)

    def _load_any(self, leaves):
        for fn in sorted(os.listdir(leaves) if os.path.isdir(leaves) else []):
            if fn.endswith(".pem") and os.path.exists(os.path.join(leaves, fn[:-4] + ".key")):
                self.ssl_ctx.load_cert_chain(os.path.join(leaves, fn),
                                             os.path.join(leaves, fn[:-4] + ".key"))
                return fn
        return None

    def _sni(self, sslobj, servername, ctx):
        import traceback
        try:
            w(f"sni: {servername}")
            leaves = os.path.join(self.pki, "leaves")
            pem = os.path.join(leaves, f"{servername}.pem")
            key = os.path.join(leaves, f"{servername}.key")
            if not (os.path.exists(pem) and os.path.exists(key)) \
                    and servername and servername.endswith(".emulator.local"):
                # wildcard leaf covers every patched-app host
                pem = os.path.join(leaves, "emulator.local.pem")
                key = os.path.join(leaves, "emulator.local.key")
            if os.path.exists(pem) and os.path.exists(key):
                new = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                new.load_cert_chain(pem, key)
                sslobj.context = new
            else:
                w(f"sni: NO LEAF for {servername} (falls back to default)")
            return None
        except Exception:
            w("sni callback error:\n" + traceback.format_exc())
            return None

    def get_request(self):
        sock, addr = super().get_request()
        try:
            sock = self.ssl_ctx.wrap_socket(sock, server_side=True)
        except ssl.SSLError as e:
            w(f"tls handshake fail from {addr[0]}: {e}")
            try:
                sock.close()
            except Exception:
                pass
            raise
        return sock, addr


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pki", default="../pki")
    ap.add_argument("--port", type=int, default=443)
    args = ap.parse_args()
    srv = SNIServer(("0.0.0.0", args.port), os.path.abspath(args.pki))
    _start_mqtt_state_listener()
    w(f"=== emulator on :{args.port}, pki={args.pki} ===")
    print(f"emulator listening on :{args.port} (logs: {LOG})")
    srv.serve_forever()


if __name__ == "__main__":
    main()
