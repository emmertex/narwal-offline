#!/usr/bin/env python3
"""narwal_access.py — standalone read/write access to Narwal robot vacuums.

Works against any Narwal robot whose firmware ships the dynamic_params
MQTT services (verified: Freo X Ultra / J4, fw v01.09.01.00). No repo
dependencies — only `paho-mqtt` (pip install paho-mqtt).

What it does
------------
1. Authenticates to the Narwal cloud (email+password, or a saved JWT —
   the access token also works as its own refresh token) and connects to
   the account's MQTT broker as an app client.
2. Enumerates the robot's dynamic_params config files.
3. Reads ANY regular file on the robot (`config_content/get` has no path
   validation — the service is the config-file mechanism and happily
   serves /etc/shadow, binaries, anything).
4. Overwrites ANY EXISTING file (`config_content/set`; new files are
   refused). The classic use: append your own CA to
   /etc/ssl/certs/ca-certificates.crt so the robot trusts your local
   server emulator's TLS (see the emulator/ directory).

Usage
-----
  python3 narwal_access.py login --email you@example.com --password X --region aus
  python3 narwal_access.py devices
  python3 narwal_access.py list-files
  python3 narwal_access.py read /etc/openwrt_release
  python3 narwal_access.py read /bin/busybox --out busybox.elf
  python3 narwal_access.py write /etc/ssl/certs/ca-certificates.crt --from-file ca-bundle-with-yours.crt
  python3 narwal_access.py install-ca my-ca.crt          # read-modify-write helper
  python3 narwal_access.py status

Credentials are cached in narwal_tokens.json next to this script.

Safety notes
------------
* `write` replaces the whole file. Always `read` first and keep a copy.
* The service may rate-limit large reads; big files come back in one
  MQTT response (~0.5 MB verified).
* Read+write go through the cloud broker; the robot must be online.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import ssl
import struct
import sys
import time
import urllib.request

try:
    import paho.mqtt.client as mqtt
except ImportError:
    sys.exit("pip install paho-mqtt")

REGIONS = {
    "us": "us-app.narwaltech.com", "eu": "eu-app.narwaltech.com",
    "il": "il-app.narwaltech.com", "cn": "cn-app.narwaltech.com",
    "aus": "aus-app.narwaltech.com", "de": "de-app.narwaltech.com",
}
BROKERS = {"us": "us-01.mqtt.narwaltech.com", "eu": "eu-01.mqtt.narwaltech.com",
           "il": "us-01.mqtt.narwaltech.com", "cn": "cn-mqtt.narwaltech.com",
           "aus": "us-01.mqtt.narwaltech.com", "de": "eu-01.mqtt.narwaltech.com"}
TOKENS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "narwal_tokens.json")

# --------------------------------------------------------------------------
# minimal protobuf codec (only what these services need)
# --------------------------------------------------------------------------

def _varint(n: int) -> bytes:
    out = b""
    while True:
        b = n & 0x7F
        n >>= 7
        out += bytes([b | (0x80 if n else 0)])
        if not n:
            return out

def enc_str(field: int, s: str | bytes) -> bytes:
    if isinstance(s, str):
        s = s.encode()
    return bytes([(field << 3) | 2]) + _varint(len(s)) + s

def enc_msg(field: int, inner: bytes) -> bytes:
    return bytes([(field << 3) | 2]) + _varint(len(inner)) + inner

def dec_fields(buf: bytes) -> dict:
    """Decode one protobuf level: {field: value}; nested msgs stay bytes."""
    out, i = {}, 0
    while i < len(buf):
        tag, i = _read_varint(buf, i)
        f, wt = tag >> 3, tag & 7
        if wt == 2:
            ln, i = _read_varint(buf, i)
            out.setdefault(f, b"")
            out[f] = buf[i:i+ln] if not isinstance(out[f], list) else out[f]
            i += ln
        elif wt == 0:
            v, i = _read_varint(buf, i)
            out[f] = v
        else:
            break
    return out

def _read_varint(buf: bytes, i: int):
    v, sh = 0, 0
    while True:
        b = buf[i]; i += 1
        v |= (b & 0x7F) << sh
        if not (b & 0x80):
            return v, i
        sh += 7

def fileinfo(name="", path="") -> bytes:
    inner = (enc_str(1, name) if name else b"") + (enc_str(2, path) if path else b"")
    return inner


def strip_narwal_frame(raw: bytes) -> bytes:
    """Drop the 0x01 + varint(len) + auth-protobuf header; return the message.

    Responses on the response topic carry the same identity frame as
    requests (0x01, varint length, then that many bytes of auth protobuf).
    """
    if len(raw) < 2 or raw[0] != 0x01:
        return raw
    length = shift = 0
    pos = 1
    while pos < len(raw):
        byte = raw[pos]
        pos += 1
        length |= (byte & 0x7F) << shift
        shift += 7
        if not byte & 0x80:
            break
    return raw[pos + length:]

REQ_LIST = b""  # GetConfigFileList_Request has zero fields
REQ_GET = lambda name, path: enc_msg(1, fileinfo(name, path))
REQ_SET = lambda name, path, content: enc_msg(1, fileinfo(name, path)) + enc_str(2, content)

# --------------------------------------------------------------------------
# cloud + MQTT
# --------------------------------------------------------------------------

class Cloud:
    def __init__(self, region="us"):
        self.host = REGIONS.get(region, REGIONS["us"])
        self.broker = BROKERS.get(region, BROKERS["us"])
        self.tok = self._load()

    def _load(self) -> dict:
        if os.path.exists(TOKENS):
            return json.load(open(TOKENS))
        return {}

    def _save(self):
        json.dump(self.tok, open(TOKENS, "w"), indent=1)
        os.chmod(TOKENS, 0o600)

    def login(self, email: str, password: str):
        body = json.dumps({"email": email, "password": password}).encode()
        r = self._post("/user-authentication-server/v2/login/loginByEmail", body)
        self.tok = {"token": r["result"]["token"], "refresh": r["result"].get("refresh_token") or r["result"]["token"],
                    "uuid": r["result"]["uuid"], "region": self.host}
        self._save()

    def refresh(self):
        body = json.dumps({"refreshToken": self.tok.get("refresh") or self.tok["token"]}).encode()
        r = self._post("/user-authentication-server/v1/token/refresh", body)
        self.tok.update({"token": r["result"]["token"],
                         "refresh": r["result"].get("refreshToken") or r["result"]["token"]})
        self._save()

    def _post(self, path, body):
        req = urllib.request.Request(f"https://{self.host}{path}", data=body,
                                     headers={"Content-Type": "application/json",
                                              "Auth-Token": self.tok.get("token", "")})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.load(resp)

    def devices(self) -> list[dict]:
        req = urllib.request.Request(
            f"https://{self.host}/user-device-platform-server/device-info/getDeviceInfoList",
            headers={"Auth-Token": self.tok["token"]})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.load(resp)["result"]["deviceInfoList"]


class RobotLink:
    """MQTT app-client with request/response correlation for one robot."""

    def __init__(self, product_key: str, device: str, uuid: str, token: str, broker: str):
        self.pk, self.dev = product_key, device
        self.base = f"/{product_key}/{device}"
        self.uuid, self.token = uuid, token
        self.broker = broker
        self.pending: dict[bytes, object] = {}
        self._setup()

    def _setup(self):
        import uuid as uuidlib
        self.cli = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, protocol=mqtt.MQTTv5,
                               client_id=f"app_{self.uuid}_{uuidlib.uuid4()}")
        self.cli.username_pw_set(self.uuid, self.token)
        self.cli.tls_set()
        self.cli.on_message = self._on_msg
        self.connected = False
        self.cli.on_connect = self._on_connect

    def _on_connect(self, c, u, f, rc, props=None):
        self.connected = True

    def _on_msg(self, c, u, m):
        # match ONLY by MQTT5 correlation data. Response topics are shared
        # between concurrent app clients (the broker fan-outs to every
        # subscriber), so fallback-to-first-pending cross-matches strangers.
        props = getattr(m, "properties", None)
        corr = getattr(props, "CorrelationData", None) if props else None
        if corr is not None and corr in self.pending:
            ev = self.pending.pop(corr)
            self.pending[corr] = m.payload
            try:
                ev.set()
            except Exception:
                pass

    def connect(self, timeout=20):
        self.cli.connect(self.broker, 8883, keepalive=30)
        self.cli.loop_start()
        t0 = time.time()
        while not self.connected and time.time() - t0 < timeout:
            time.sleep(0.2)
        if not self.connected:
            raise RuntimeError("broker connect timeout")

    def request(self, command: str, body: bytes, timeout=25, retries=2) -> bytes | None:
        import uuid as uuidlib
        resp_topic = f"{self.base}/{command}/response"
        self.cli.subscribe(resp_topic, qos=0)
        time.sleep(0.15)
        corr = uuidlib.uuid4().bytes
        ev = __import__("threading").Event()
        self.pending[corr] = ev
        props = mqtt.Properties(mqtt.PacketTypes.PUBLISH)
        props.ResponseTopic = resp_topic
        props.CorrelationData = corr
        # Narwal frame: 0x01 varint(len) {1: uuid, 2: uuid}  (identity), then the message
        inner = enc_str(1, self.uuid) + enc_str(2, self.uuid)
        frame = b"\x01" + _varint(len(inner)) + inner
        self.cli.publish(f"{self.base}/{command}", frame + body, qos=0, properties=props)
        ok = ev.wait(timeout)
        if ok:
            out = self.pending.pop(corr)
            return out if isinstance(out, (bytes, bytearray)) else None
        if retries:
            return self.request(command, body, timeout, retries - 1)
        return None

    # -- dynamic_params ----------------------------------------------------
    def list_files(self) -> list[dict]:
        raw = self.request("dynamic_params/config_file_list/get", REQ_LIST, timeout=40)
        return flatten_list(raw or b"")

    def read_file(self, path: str, timeout=40) -> bytes | None:
        name = os.path.basename(path)
        raw = self.request("dynamic_params/config_content/get", REQ_GET(name, path), timeout=timeout)
        if raw is None:
            return None
        top = dec_fields(strip_narwal_frame(raw))
        content = top.get(2)
        return content if isinstance(content, bytes) else b""

    def write_file(self, path: str, content: bytes, timeout=60) -> str:
        name = os.path.basename(path)
        raw = self.request("dynamic_params/config_content/set",
                           REQ_SET(name, path, content), timeout=timeout)
        if raw is None:
            return "no answer"
        top = dec_fields(strip_narwal_frame(raw))
        return (top.get(3) or b"").decode("utf-8", "replace") or str(top)

    def wake(self):
        self.request("common/active_robot_publish", b"", timeout=15)

    def status(self) -> bytes | None:
        return self.request("status/get_device_base_status", b"", timeout=30)


def flatten_list(raw: bytes) -> list[dict]:
    """GetConfigFileList_Response {Result{NodeConfig{nodeName, ConfigFile{oemPath, FileInfo[]}}}}"""
    files = []
    def walk_result(buf):
        for f, v in dec_fields(buf).items():
            if isinstance(v, bytes):
                walk_node(v)
    def walk_node(buf):
        d = dec_fields(buf)
        for f, v in d.items():
            if isinstance(v, bytes) and f == 2:
                walk_cfg(v)
    def walk_cfg(buf):
        d = dec_fields(buf)
        for f, v in d.items():
            if isinstance(v, bytes) and f == 2:
                fi = dec_fields(v)
                nm = (fi.get(1) or b"").decode("utf-8", "replace")
                pth = (fi.get(2) or b"").decode("utf-8", "replace")
                if pth or nm:
                    files.append({"name": nm, "path": pth})
    try:
        top = dec_fields(strip_narwal_frame(raw))
        res = top.get(1)
        if isinstance(res, bytes):
            for f, v in dec_fields(res).items():
                if isinstance(v, bytes):
                    walk_node(v)
    except Exception:
        pass
    return files


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def load_link(args) -> RobotLink:
    tok = json.load(open(TOKENS)) if os.path.exists(TOKENS) else None
    if not tok:
        sys.exit("run `login` first (or place a narwal_tokens.json here)")
    cloud = Cloud(tok.get("region", "us").split("-")[0] if "-" in tok.get("region","us") else tok.get("region","us"))
    cloud.tok = tok
    pk = args.product_key or os.environ.get("NARWAL_PRODUCT_KEY", "EHf6cRNRGT")
    dev = args.device or os.environ.get("NARWAL_DEVICE_ID")
    if not dev:
        devs = cloud.devices()
        if not devs:
            sys.exit("no devices on this account")
        d0 = devs[0]
        pk, dev = d0["productId"], d0["deviceId"]
        print(f"using {d0.get('robotName')} ({pk}/{dev}, fw {d0.get('firmwareVersion')})")
    broker = BROKERS.get(tok.get("region_style", "us"), BROKERS["us"])
    return RobotLink(pk, dev, tok["uuid"], tok["token"], broker)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    lg = sub.add_parser("login"); lg.add_argument("--email", required=True)
    lg.add_argument("--password", required=True); lg.add_argument("--region", default="us")
    sub.add_parser("devices")
    sub.add_parser("list-files")
    rd = sub.add_parser("read"); rd.add_argument("path"); rd.add_argument("--out")
    wr = sub.add_parser("write"); wr.add_argument("path"); wr.add_argument("--from-file", required=True)
    ca = sub.add_parser("install-ca"); ca.add_argument("ca_crt")
    st = sub.add_parser("status")
    for sub_p in (rd, wr, ca, st):
        sub_p.add_argument("--device"); sub_p.add_argument("--product-key")
    args = p.parse_args()

    if args.cmd == "login":
        c = Cloud(args.region); c.login(args.email, args.password)
        print("logged in; tokens cached in", TOKENS); return
    if args.cmd == "devices":
        tok = json.load(open(TOKENS))
        c = Cloud(); c.tok = tok
        for d in c.devices():
            print(d.get("robotName"), d["productId"], d["deviceId"], d.get("firmwareVersion"))
        return

    link = load_link(args)
    link.connect()
    try:
        link.wake()
        if args.cmd == "list-files":
            for f in link.list_files():
                print(f["path"] or f["name"])
        elif args.cmd == "read":
            data = link.read_file(args.path)
            if data is None:
                print("(no answer)", file=sys.stderr); sys.exit(1)
            if args.out:
                open(args.out, "wb").write(data)
                print(f"{len(data)} bytes -> {args.out}")
            else:
                sys.stdout.buffer.write(data)
        elif args.cmd == "write":
            content = open(args.from_file, "rb").read()
            print("robot says:", link.write_file(args.path, content))
        elif args.cmd == "install-ca":
            path = "/etc/ssl/certs/ca-certificates.crt"
            cur = link.read_file(path)
            if cur is None:
                sys.exit("cannot read current bundle — aborting (never install blind)")
            add = open(args.ca_crt, "rb").read()
            if add in cur:
                print("CA already present"); return
            backup = path.strip("/").replace("/", "_") + ".bak"
            open(backup, "wb").write(cur)
            new = cur + (b"\n" if not cur.endswith(b"\n") else b"") + add
            msg = link.write_file(path, new)
            print("write:", msg, "| original backed up to", backup)
        elif args.cmd == "status":
            print((link.status() or b"").hex()[:200])
    finally:
        link.cli.loop_stop()


if __name__ == "__main__":
    main()
