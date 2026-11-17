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
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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
            self._json(NO_UPDATE)
        elif "/login" in path or "/token/refresh" in path:
            self._login()
        elif "getDeviceInfoList" in path:
            self._devices()
        elif "device-bootstrap" in path or "bootstrap" in path:
            self._json({"code": 0, "msg": "ok", "success": True,
                        "result": {"mqtt": {"host": "us-01.mqtt.narwaltech.com", "port": 8883}}})
        else:
            self._json({"code": 0, "msg": "ok", "success": True, "result": {}})

    def do_GET(self):
        host = (self.headers.get("Host") or "").split(":")[0]
        w(f"GET {host}{self.path}")
        path = self.path.split("?")[0]
        if "getDeviceInfoList" in path:
            self._devices()
        elif "/ota/check" in path:
            self._json(NO_UPDATE)
        else:
            self._json({"code": 0, "msg": "ok", "success": True, "result": {}})

    # ------------------------------------------------------------ payloads
    def _login(self):
        # Standard app keys + sentinel broker fields: we learn which key the
        # ROBOT honors by watching DNS for sentinel-narwal-offline.test.
        self._json({"code": 0, "msg": "ok", "success": True, "result": {
            "token": FAKE_JWT, "refresh_token": FAKE_REFRESH,
            "refreshToken": FAKE_REFRESH, "access_token": FAKE_JWT,
            "uuid": "offline-emulator-user",
            "mqtt_host": "sentinel-narwal-offline.test",
            "broker": "sentinel-narwal-offline.test",
            "host": "sentinel-narwal-offline.test",
            "mqtt": {"host": "sentinel-narwal-offline.test", "port": 8883},
            "endpoints": {"mqtt": "sentinel-narwal-offline.test:8883"},
            "server": "sentinel-narwal-offline.test",
            "domain": "sentinel-narwal-offline.test"}})

    def _devices(self):
        self._json({"code": 0, "err_code": 10000, "msg": "ok", "result": {
            "deviceInfoList": [{
                "deviceId": ROBOT["deviceId"], "productId": ROBOT["productId"],
                "robotName": ROBOT["robotName"], "snNumber": ROBOT["snNumber"],
                "firmwareVersion": ROBOT["firmwareVersion"], "isAutoUpgrade": False,
                "deviceSiteCode": "us", "extend": {},
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
            leaves = os.path.join(self.pki, "leaves")
            pem = os.path.join(leaves, f"{servername}.pem")
            key = os.path.join(leaves, f"{servername}.key")
            if os.path.exists(pem) and os.path.exists(key):
                new = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                new.load_cert_chain(pem, key)
                sslobj.context = new
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
    w(f"=== emulator on :{args.port}, pki={args.pki} ===")
    print(f"emulator listening on :{args.port} (logs: {LOG})")
    srv.serve_forever()


if __name__ == "__main__":
    main()
