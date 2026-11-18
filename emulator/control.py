#!/usr/bin/env python3
"""control.py — drive the robot through the OFFLINE broker (no cloud).

Same MQTT command protocol as the cloud: publish to
/{productKey}/{deviceId}/{command} with MQTT5 ResponseTopic +
CorrelationData; the reply lands on .../{command}/response.

  python3 control.py status
  python3 control.py wake                 # register + wake (docked-asleep)
  python3 control.py start | pause | resume | stop | dock | locate
  python3 control.py fan 3 | wetness 2    # mid-clean only
  python3 control.py raw common/yell
  python3 control.py listen               # stream status pushes

Env: NARWAL_BROKER, NARWAL_PRODUCT_KEY (EHf6cRNRGT), NARWAL_DEVICE_ID,
     NARWAL_UUID, NARWAL_TOKEN (any non-empty values on the emulator).
"""
from __future__ import annotations

import argparse
import os
import sys
import threading
import time
import uuid

import paho.mqtt.client as mqtt

PUSH_TOPICS = ("status/robot_base_status", "status/working_status",
               "status/upgrade_status", "map/display_map")


def varint(n: int) -> bytes:
    out = b""
    while True:
        b = n & 0x7F
        n >>= 7
        out += bytes([b | (0x80 if n else 0)])
        if not n:
            return out


class Link:
    def __init__(self, broker, product, device, user, token):
        self.base = f"/{product}/{device}"
        self.user = user
        self.pending = {}
        self.ev_ok = threading.Event()
        self.cli = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, protocol=mqtt.MQTTv5,
                               client_id=f"app_{user}_{uuid.uuid4()}")
        self.cli.username_pw_set(user, token)
        self.cli.tls_set()
        self.cli.on_connect = lambda *a, **k: self.ev_ok.set()
        self.cli.on_message = self._on_msg
        print(f"connecting {broker}:8883 …")
        self.cli.connect(broker, 8883, keepalive=30)
        self.cli.loop_start()
        if not self.ev_ok.wait(15):
            sys.exit("broker connect failed")

    def _on_msg(self, c, u, m):
        props = getattr(m, "properties", None)
        corr = getattr(props, "CorrelationData", None) if props else None
        if corr is not None and corr in self.pending and isinstance(self.pending[corr], threading.Event):
            ev = self.pending[corr]
            self.pending[corr] = m.payload
            ev.set()
            return
        if "/response" in m.topic:
            for k, v in list(self.pending.items()):
                if isinstance(v, threading.Event):
                    self.pending[k] = m.payload
                    v.set()
                    break
        else:
            print(f"[push] {m.topic.rsplit('/',1)[-1]:24s} {len(m.payload):4d}B {m.payload[:6].hex()}")

    def frame(self) -> bytes:
        u = self.user.encode()
        return b"\x01" + varint(len(u)) + u + u

    def request(self, command: str, body: bytes = b"", timeout: float = 30) -> bytes | None:
        resp = f"{self.base}/{command}/response"
        self.cli.subscribe(resp, qos=0)
        time.sleep(0.1)
        corr = uuid.uuid4().bytes
        ev = threading.Event()
        self.pending[corr] = ev
        props = mqtt.Properties(mqtt.PacketTypes.PUBLISH)
        props.ResponseTopic = resp
        props.CorrelationData = corr
        self.cli.publish(f"{self.base}/{command}", self.frame() + body, qos=0, properties=props)
        if ev.wait(timeout):
            out = self.pending.pop(corr)
            return out if isinstance(out, (bytes, bytearray)) else b""
        self.pending.pop(corr, None)
        return None

    def keepalive_register(self):
        """active_robot_publish request-body (topicsDuration ~30000ms)."""
        u = self.user.encode()
        inner = (b"\x08\x01\x09\x002/\n" + self.base.encode() +
                 b"/common/active_robot_publish/response\x12$" + u[:0] +
                 b"\x0a\x0b\x08\xd0\x0f\x10\xd0\x0f\x18\xd0\x0f@\x00\x010")
        return self.request("common/active_robot_publish", inner, timeout=12)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("cmd", help="status|wake|start|pause|resume|stop|dock|locate|fan|wetness|raw|listen")
    ap.add_argument("arg", nargs="?", help="fan level 1-5 / wetness 1-3 / raw topic")
    ap.add_argument("--broker", default=os.environ.get("NARWAL_BROKER", "us-01.mqtt.narwaltech.com"))
    ap.add_argument("--product", default=os.environ.get("NARWAL_PRODUCT_KEY", "EHf6cRNRGT"))
    ap.add_argument("--device", default=os.environ.get("NARWAL_DEVICE_ID"))
    ap.add_argument("--user", default=os.environ.get("NARWAL_UUID", "offline-emulator-user"))
    ap.add_argument("--token", default=os.environ.get("NARWAL_TOKEN", "offline-emulator-token"))
    a = ap.parse_args()
    if not a.device:
        sys.exit("set --device or NARWAL_DEVICE_ID (32-hex MQTT device name)")

    lnk = Link(a.broker, a.product, a.device, a.user, a.token)
    try:
        if a.cmd == "listen":
            for t in PUSH_TOPICS:
                lnk.cli.subscribe(f"{lnk.base}/{t}", qos=0)
            print("listening (Ctrl-C) …; registering for pushes")
            lnk.keepalive_register()
            while True:
                time.sleep(1)
        r = None
        if a.cmd == "status":
            lnk.keepalive_register()
            r = lnk.request("status/get_device_base_status")
        elif a.cmd == "wake":
            r = lnk.keepalive_register()
        elif a.cmd == "start":
            r = lnk.request("clean/start_clean")
        elif a.cmd == "pause":
            r = lnk.request("task/pause")
        elif a.cmd == "resume":
            r = lnk.request("task/resume")
        elif a.cmd == "stop":
            r = lnk.request("task/force_end")
        elif a.cmd == "dock":
            r = lnk.request("supply/recall")
        elif a.cmd == "locate":
            r = lnk.request("common/yell")
        elif a.cmd == "fan":
            lvl = int(a.arg or 2)
            r = lnk.request("clean/set_fan_level", bytes([0x08, lvl, 0x10, 0x01]))
        elif a.cmd == "wetness":
            lvl = int(a.arg or 2)
            r = lnk.request("clean/set_mop_humidity", bytes([0x08, lvl, 0x10, 0x01]))
        elif a.cmd == "raw":
            r = lnk.request(a.arg or "")
        if r is None:
            print("no reply (robot asleep? run wake first)")
            sys.exit(1)
        print("reply:", r.hex()[:400] if r else "(empty ack)")
    finally:
        lnk.cli.loop_stop()


if __name__ == "__main__":
    main()
