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


def st(slot=None, macros=None, svv=None, sensor=None, print_state="standby"):
    s = copy.deepcopy(BASE)
    s["print_stats"] = {"state": print_state}
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


scripts = []


def fake_post(url, payload):
    if url.endswith("/printer/gcode/script"):
        scripts.append(payload["script"])
        return {"result": "ok"}
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

# --- persisting Fluidd selections (Fluidd 1.30.x upper-case bug)
def rounds(states, p):
    scripts.clear()
    seq[:] = list(states)
    for _ in states:
        sb.run(once=True, persister=p)
    return list(scripts)


SV = "SAVE_VARIABLE VARIABLE=q2_slot2__spool_id VALUE="
p = sb.Persister()
check("restored macro == saved: no save",
      rounds([st("slot1", {"Q2_SLOT2": 6}, {"q2_slot2__spool_id": 6})] * 2, p), [])
check("macro 6 -> 4 while idle: saved lower case",
      rounds([st("slot1", {"Q2_SLOT2": 4}, {"q2_slot2__spool_id": 6})], p), [SV + "4"])
check("saved only once", rounds([st("slot1", {"Q2_SLOT2": 4}, {"q2_slot2__spool_id": 4})], p), [])

p = sb.Persister()
rounds([st("slot1", {"Q2_SLOT2": 6}, {"q2_slot2__spool_id": 6}, print_state="printing")], p)
check("change while printing: wait",
      rounds([st("slot1", {"Q2_SLOT2": 4}, {"q2_slot2__spool_id": 6}, print_state="printing")], p), [])
check("print done: save now",
      rounds([st("slot1", {"Q2_SLOT2": 4}, {"q2_slot2__spool_id": 6}, print_state="complete")], p), [SV + "4"])

p = sb.Persister()
check("start with unsaved Fluidd value: save",
      rounds([st("slot1", {"Q2_SLOT2": 4}, {"q2_slot2__spool_id": 6})], p), [SV + "4"])

p = sb.Persister()
s0 = st("slot1", {"Q2_SLOT2": None}, {"q2_slot2__spool_id": 6})
check("empty macro before restore: no save", rounds([s0], p), [])
check("empty macro before restore: use saved", sb.resolve(s0, None, p.cleared())[1], 6)

p = sb.Persister()
rounds([st("slot1", {"Q2_SLOT2": 6}, {"q2_slot2__spool_id": 6})], p)
s1 = st("slot1", {"Q2_SLOT2": None}, {"q2_slot2__spool_id": 6}, print_state="printing")
rounds([s1], p)
check("cleared in Fluidd: no fallback to stale saved value", sb.resolve(s1, None, p.cleared())[1], None)
check("cleared: saved as None after print",
      rounds([st("slot1", {"Q2_SLOT2": None}, {"q2_slot2__spool_id": 6})], p), [SV + "None"])

p = sb.Persister()
rounds([st("slot1", {"Q2_SLOT2": 6}, {"q2_slot2__spool_id": 6}), st("slot1", {"Q2_SLOT2": 4}, {"q2_slot2__spool_id": 6}, print_state="printing")], p)
rounds([dict(st("slot1"), webhooks={"state": "startup"})], p)
check("Klipper restart resets pending changes",
      rounds([st("slot1", {"Q2_SLOT2": None}, {"q2_slot2__spool_id": 6})], p), [])

p = sb.Persister()
check("dry-run: no save sent", (seq.__setitem__(slice(None), [st("slot1", {"Q2_SLOT2": 4}, {"q2_slot2__spool_id": 6})]),
                                sb.run(dry_run=True, once=True, persister=p), scripts[:])[2], [])

# failed save: hold off, no retry every second
real_post = sb.http_post_json


def failing_post(url, payload):
    if url.endswith("/printer/gcode/script"):
        scripts.append("FAIL")
        raise urllib.error.HTTPError(url, 500, "err", {}, io.BytesIO(b"x"))
    return real_post(url, payload)


sb.http_post_json = failing_post
p = sb.Persister()
check("failed save is not retried at once",
      rounds([st("slot1", {"Q2_SLOT2": 4}, {"q2_slot2__spool_id": 6})] * 3, p), ["FAIL"])
sb.http_post_json = real_post

# --- usage correction, replaying E positions recorded on a Q2 (tool change 1B -> 1C)
class FakeSpoolman:
    sync_rate = -0.5   # settle immediately in tests

    def __init__(self):
        self.used = {}
        self.uses = []
        self.down = False

    def used_length(self, sid):
        if self.down:
            raise OSError("unreachable")
        return self.used.get(sid, 0.0)

    def use(self, sid, mm):
        self.uses.append((sid, round(mm, 1)))
        self.used[sid] = self.used.get(sid, 0.0) + mm


def replay(trace, sm, tracker, moonraker_high=0.0):
    """trace: [(active spool, toolhead sensor, E)]; also plays Moonraker's high-water booking."""
    high = moonraker_high
    for sid, loaded, e in trace:
        if e > high:
            if sid is not None:
                sm.used[sid] = sm.used.get(sid, 0.0) + (e - high)
            high = e
        tracker.update(sid, e, loaded, 1.0, {})
    return high


TRACE = ([(4, False, 0.0), (4, True, 25.0), (4, True, 164.0), (4, True, 307.0), (4, True, 334.3),
          (4, False, 172.8)] +
         [(7, False, 172.8), (7, True, 197.8), (7, True, 264.8), (7, True, 291.4), (7, True, 304.7),
          (7, True, 302.7), (7, True, 307.7), (7, True, 317.5), (7, True, 325.5), (7, False, 164.0)] +
         [(None, False, 164.0), (None, False, 164.0)])
sm = FakeSpoolman()
tr = sb.UsageTracker(sm, load_offset=67.0)
replay(TRACE, sm, tr)
check("Moonraker alone would book 4:334.3, 7:0", True, True)
check("corrections: 4 gets load back, 7 gets purge + print", sm.uses, [(4, -92.0), (7, 60.7)])
check("spool 7 ends at the slicer's ~60 mm", round(sm.used[7], 1), 60.7)

sm = FakeSpoolman()
tr = sb.UsageTracker(sm, load_offset=67.0)
replay([(5, True, 100.0), (5, True, 150.0), (5, True, 148.0), (None, True, 148.0), (None, True, 148.0)], sm, tr, 100.0)
check("already loaded, Moonraker right: no correction", sm.uses, [])

sm = FakeSpoolman()
tr = sb.UsageTracker(sm, load_offset=67.0)
replay([(5, False, 10.0), (5, False, 10.0), (None, False, 10.0), (None, False, 10.0)], sm, tr)
check("never reached the toolhead: nothing booked", sm.uses, [])

sm = FakeSpoolman()
tr = sb.UsageTracker(sm, dry_run=True, load_offset=67.0)
replay(TRACE, sm, tr)
check("dry-run: no corrections written", sm.uses, [])

sm = FakeSpoolman()
sm.down = True
tr = sb.UsageTracker(sm, load_offset=67.0)
replay(TRACE, sm, tr)
check("Spoolman unreachable: skip, never guess", sm.uses, [])

sm = FakeSpoolman()
tr = sb.UsageTracker(sm, load_offset=67.0)
replay([(4, True, 0.0), (4, True, 50.0)], sm, tr)
tr.klipper_restart()
tr.update(4, 0.0, True, 0.5, {})
tr.update(None, 0.0, True, 1.0, {})
tr.update(None, 0.0, True, 1.5, {})
check("Klipper restart: period closed with what was known", sm.uses, [])

sm = FakeSpoolman()
tr = sb.UsageTracker(sm, load_offset=67.0)
replay([(4, True, 200.0), (4, True, 260.0)], sm, tr, 300.0)
tr.update(4, 5.0, True, 0.2, {})          # print time dropped: Klipper restarted unseen
tr.update(None, 5.0, True, 0.3, {})
tr.update(None, 5.0, True, 0.4, {})
check("unseen restart: first part corrected, E reset not booked", sm.uses, [(4, 60.0)])

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
