"""Offline tests for spoolbridge with a fake Moonraker. Run: python3 tests/test_spoolbridge.py"""
import copy
import io
import logging
import os
import sys
import urllib.error

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import spoolbridge as sb  # noqa: E402

SEN = sb.TOOLHEAD_SENSOR
# Minimal status as reported by a Q2 at rest: box idle on slot16, toolhead empty.
BASE = {
    "multi_color_controller": {"box": {"loaded_slot": "slot16"}},
    "save_variables": {"variables": {}},
    "webhooks": {"state": "ready"},
    SEN: {"filament_detected": False, "enabled": False},
}
fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print("PASS" if ok else "FAIL", name, "->", got, "" if ok else "(expected %r)" % (want,))


def st(slot=None, macros=None, svv=None, sensor=None):
    s = copy.deepcopy(BASE)
    if slot is not None:
        s["multi_color_controller"]["box"]["loaded_slot"] = slot
    for m, v in (macros or {}).items():
        s["gcode_macro " + m] = {"spool_id": v}
    s["save_variables"]["variables"].update(svv or {})
    if sensor is not None:
        s[SEN]["filament_detected"] = sensor
    return s


# --- resolve()
check("rest state: slot16, empty toolhead", sb.resolve(st())[1], None)
check("slot0 via macro", sb.resolve(st("slot0", {"Q2_SLOT1": 12}))[1:], (12, "macro Q2_SLOT1"))
check("slot2 via saved_variables", sb.resolve(st("slot2", {"Q2_SLOT3": None}, {"q2_slot3__spool_id": 7}))[1], 7)
check("macro 'None' -> none", sb.resolve(st("slot1", {"Q2_SLOT2": "None"}))[1], None)
check("slot16 + filament -> Q2_EXTERN", sb.resolve(st("slot16", {"Q2_EXTERN": 3}, sensor=True))[1], 3)
check("slot16 + no filament -> none", sb.resolve(st("slot16", {"Q2_EXTERN": 6}))[1], None)
check("box slot ignores sensor", sb.resolve(st("slot0", {"Q2_SLOT1": 12}, sensor=False))[1], 12)
check("unknown slot -> none", sb.resolve(st("slot-1", {"Q2_SLOT1": 12}))[1], None)
check("no box data -> none", sb.resolve({"multi_color_controller": {"box": {}}})[1], None)
check("location fallback", sb.resolve(st("slot3"), lambda s: 99 if s == "slot3" else None)[1:], (99, "Spoolman location"))
check("spool_id 0 -> none", sb.resolve(st("slot0", {"Q2_SLOT1": 0}))[1], None)
u = sb.query_url(["Q2_SLOT1"])
check("query url", ("multi_color_controller=box" in u, "gcode_macro%20Q2_SLOT1=spool_id" in u,
                    "filament_switch_sensor%20filament_switch_sensor=filament_detected" in u), (True, True, True))

# --- main loop with fake HTTP
logging.basicConfig(level=logging.INFO, format="   log: %(message)s")
active = {"id": None}
posts, payloads = [], []
seq = []


def fake_get(url):
    if url.endswith("/server/spoolman/status"):
        return {"result": {"spool_id": active["id"]}}
    return {"result": {"status": seq.pop(0)}}


def fake_post(url, payload):
    # Behaves like Moonraker: null is rejected, leaving the key out clears.
    payloads.append(payload)
    if "spool_id" in payload and payload["spool_id"] is None:
        raise urllib.error.HTTPError(url, 400, "Bad Request", {}, io.BytesIO(b"Unable to convert argument [spool_id]"))
    sid = payload.get("spool_id")
    posts.append(sid)
    active["id"] = sid
    return {}


sb.http_get, sb.http_post_json = fake_get, fake_post
seq[:] = [st("slot0", {"Q2_SLOT1": 12}), st("slot0", {"Q2_SLOT1": 12}), st("slot2", {"Q2_SLOT3": 5}),
          dict(st("slot2"), webhooks={"state": "startup"})]
for _ in range(4):
    sb.run(once=True)
check("loop: posts on change only", posts, [12, 5])

seq[:] = [st("slot1", {"Q2_SLOT2": 8})]
posts.clear()
sb.run(dry_run=True, once=True)
check("dry-run writes nothing", posts, [])

active["id"] = 6
posts.clear()
payloads.clear()
seq[:] = [st("slot16")]
sb.run(once=True)
check("clearing sends {} and succeeds", (payloads, active["id"]), ([{}], None))

# --- an error is logged only once
records = []


class H(logging.Handler):
    def emit(self, r):
        records.append(r.getMessage())


sb.log.addHandler(H())


def bad_get(url):
    raise urllib.error.HTTPError(url, 400, "Bad", {}, io.BytesIO(b"x"))


sb.http_get = bad_get
calls = {"n": 0}
real_sleep = sb.time.sleep


def stop_after_3(_):
    calls["n"] += 1
    if calls["n"] >= 3:
        raise KeyboardInterrupt


sb.time.sleep = stop_after_3
try:
    sb.run()
except KeyboardInterrupt:
    pass
sb.time.sleep = real_sleep
check("error logged once over 3 rounds", sum("rejected" in m for m in records), 1)

print("fails:", fails)
sys.exit(1 if fails else 0)
