#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 q2-spoolbridge contributors
"""spoolbridge - maps the loaded QIDI Box slot to the active Spoolman spool.

How it works (Moonraker API on localhost only, standard library only):
  1. Every POLL seconds, read `multi_color_controller.box.loaded_slot`.
  2. Find the spool ID for that slot:
       a. the Fluidd macro for that slot (Q2_SLOT1..4, Q2_EXTERN): variable spool_id;
       b. otherwise saved_variables: <macro>__spool_id;
       c. optionally: the Spoolman spool whose location is "<LOCATION_PREFIX><slot>".
  3. If it differs from Moonraker's active spool, call
     POST /server/spoolman/spool_id. No spool found -> clear the active spool,
     so usage is never booked on the wrong spool.
  4. Fluidd 1.30.x saves spool selections under an upper-case name, which
     Klipper's save_variables rejects, so the selection is lost on restart.
     When a slot macro changes, spoolbridge saves it itself, while the printer
     is not printing, as `SAVE_VARIABLE VARIABLE=<macro lower>__spool_id`.

  5. Moonraker misses purges and charges the wrong spool after a Q2 tool
     change (see UsageTracker). spoolbridge books the difference per spool
     directly in Spoolman (PUT /api/v1/spool/<id>/use). Disable with
     SPOOLBRIDGE_USAGE_CORRECTION=0.

That SAVE_VARIABLE, for its own q2_*__spool_id variables only, is the only
G-code this script sends. It never touches QIDI variables and never operates
the box. Python 3.9 compatible.
"""
import argparse
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

MOONRAKER = os.environ.get("SPOOLBRIDGE_MOONRAKER", "http://127.0.0.1:7125").rstrip("/")
POLL = float(os.environ.get("SPOOLBRIDGE_POLL", "1.0"))
# Empty = no Spoolman location fallback. Example: "Q2 " -> location "Q2 slot1".
LOCATION_PREFIX = os.environ.get("SPOOLBRIDGE_LOCATION_PREFIX", "")
# 1 = book purges and fix Moonraker's per-spool usage after tool changes (see UsageTracker).
# Filament path from the toolhead sensor to the nozzle on a Q2, in mm of E.
SENSOR_TO_NOZZLE = float(os.environ.get("SPOOLBRIDGE_SENSOR_TO_NOZZLE", "67"))
USAGE_CORRECTION = os.environ.get("SPOOLBRIDGE_USAGE_CORRECTION", "1").strip().lower() not in ("0", "false", "no", "off")
TIMEOUT = 5

SLOT_MACROS = {
    "slot0": "Q2_SLOT1",
    "slot1": "Q2_SLOT2",
    "slot2": "Q2_SLOT3",
    "slot3": "Q2_SLOT4",
    "slot4": "Q2_BOX2_SLOT1",
    "slot5": "Q2_BOX2_SLOT2",
    "slot6": "Q2_BOX2_SLOT3",
    "slot7": "Q2_BOX2_SLOT4",
    "slot16": "Q2_EXTERN",
}
# QIDI reports loaded_slot "slot16" (external holder) even when the toolhead is
# empty. slot16 therefore only counts as loaded when this sensor sees filament.
# (The sensor reports filament_detected even while it is "disabled".)
TOOLHEAD_SENSOR = "filament_switch_sensor filament_switch_sensor"
EXTERNAL_SLOT = "slot16"

LOCATION_NAMES = {
    "slot0": "slot1", "slot1": "slot2", "slot2": "slot3", "slot3": "slot4",
    "slot4": "box2 slot1", "slot5": "box2 slot2", "slot6": "box2 slot3", "slot7": "box2 slot4",
    "slot16": "extern",
}

log = logging.getLogger("spoolbridge")


# ---------------------------------------------------------------- HTTP
def http_get(url):
    with urllib.request.urlopen(url, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode())


def http_post_json(url, payload):
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode())


# ---------------------------------------------------------------- logic (pure, testable)
def to_spool_id(value):
    """Convert a macro or save_variables value to a positive int, or None."""
    if value is None or isinstance(value, bool):
        return None
    try:
        sid = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return sid if sid > 0 else None


def query_url(macros):
    objs = ["multi_color_controller=box", "save_variables=variables", "webhooks=state",
            "print_stats=state", "toolhead=position,estimated_print_time",
            "%s=filament_detected" % TOOLHEAD_SENSOR]
    objs += ["gcode_macro %s=spool_id" % m for m in macros]
    return MOONRAKER + "/printer/objects/query?" + "&".join(
        urllib.parse.quote(o, safe="=") for o in objs
    )


def resolve(status, location_lookup=None, cleared=()):
    """Return (loaded_slot, spool_id or None, reason) from an objects query.

    `cleared`: macros the user emptied in Fluidd this session; those must not
    fall back to the (stale) saved_variables value.
    """
    box = (status.get("multi_color_controller") or {}).get("box") or {}
    slot = box.get("loaded_slot")
    if not isinstance(slot, str) or slot not in SLOT_MACROS:
        return slot, None, "no slot loaded"
    if slot == EXTERNAL_SLOT:
        sensor = status.get(TOOLHEAD_SENSOR)
        if sensor is not None and sensor.get("filament_detected") is False:
            return slot, None, "slot16 but toolhead sensor sees no filament: nothing loaded"
    macro = SLOT_MACROS[slot]
    mstat = status.get("gcode_macro " + macro)
    if mstat is not None:
        sid = to_spool_id(mstat.get("spool_id"))
        if sid:
            return slot, sid, "macro " + macro
        if macro in cleared:
            return slot, None, "spool cleared in Fluidd for " + macro
    svv =(status.get("save_variables") or {}).get("variables") or {}
    sid = to_spool_id(svv.get(macro.lower() + "__spool_id"))
    if sid:
        return slot, sid, "saved_variables " + macro.lower() + "__spool_id"
    if location_lookup is not None:
        sid = location_lookup(slot)
        if sid:
            return slot, sid, "Spoolman location"
    return slot, None, "no spool assigned to " + macro


# ---------------------------------------------------------------- persist Fluidd selections
BUSY_STATES = ("printing", "paused")


class Persister:
    """Track the slot macros and save changed selections to saved_variables.

    Fluidd 1.30.x tries `SAVE_VARIABLE VARIABLE=Q2_SLOT2__SPOOL_ID ...`, which
    Klipper rejects (upper case), so only the macro holds the new value.
    The first value seen per Klipper session is the baseline (that is what the
    restore macro put there); only later changes count as a user selection.
    """

    RETRY = 60           # seconds to wait after a failed save

    def __init__(self):
        self.baseline = {}   # macro -> spool id (or None) last seen
        self.dirty = set()   # macros changed by the user, not yet saved
        self.hold_until = 0.0

    def reset(self):
        """Klipper restarted: macros are reloaded, start over."""
        self.baseline.clear()
        self.dirty.clear()

    def observe(self, status):
        for macro in set(SLOT_MACROS.values()):
            mstat = status.get("gcode_macro " + macro)
            if mstat is None:
                continue
            value = to_spool_id(mstat.get("spool_id"))
            if macro not in self.baseline:
                self.baseline[macro] = value
                # A spool id in the macro that saved_variables does not have
                # can only come from Fluidd: save it. (An empty macro may just
                # mean the restore macro has not run yet: ignore that.)
                svv = (status.get("save_variables") or {}).get("variables") or {}
                if value is not None and value != to_spool_id(svv.get(macro.lower() + "__spool_id")):
                    self.dirty.add(macro)
            elif value != self.baseline[macro]:
                self.baseline[macro] = value
                self.dirty.add(macro)

    def cleared(self):
        return {m for m in self.dirty if self.baseline.get(m) is None}

    def pending(self, status):
        """Return [(variable, value)] that still has to be saved."""
        svv = (status.get("save_variables") or {}).get("variables") or {}
        out = []
        for macro in sorted(self.dirty):
            var = macro.lower() + "__spool_id"
            value = self.baseline.get(macro)
            if to_spool_id(svv.get(var)) == value:
                self.dirty.discard(macro)   # already saved (e.g. newer Fluidd)
            else:
                out.append((var, value))
        return out

    def done(self, var):
        self.dirty = {m for m in self.dirty if m.lower() + "__spool_id" != var}


def save_variable(var, value):
    script = "SAVE_VARIABLE VARIABLE=%s VALUE=%s" % (var, value if value is not None else "None")
    http_post_json(MOONRAKER + "/printer/gcode/script", {"script": script})


# ---------------------------------------------------------------- Spoolman access
class Spoolman:
    """Direct access to the Spoolman server configured in Moonraker."""

    def __init__(self):
        self.server = None
        self.sync_rate = 5.0

    def _server(self):
        if self.server is None:
            cfg = http_get(MOONRAKER + "/server/config")["result"]["config"]
            sm = cfg.get("spoolman") or {}
            self.server = sm.get("server", "").rstrip("/") or ""
            try:
                self.sync_rate = float(sm.get("sync_rate", 5))
            except (TypeError, ValueError):
                pass
        return self.server

    def used_length(self, sid):
        """Used length (mm) of a spool as Spoolman has it now."""
        return float(http_get("%s/api/v1/spool/%d" % (self._server(), sid)).get("used_length") or 0.0)

    def use(self, sid, length):
        """Book `length` mm on a spool (negative gives filament back)."""
        req = urllib.request.Request(
            "%s/api/v1/spool/%d/use" % (self._server(), sid),
            data=json.dumps({"use_length": round(length, 2)}).encode(),
            headers={"Content-Type": "application/json"},
            method="PUT",
        )
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            r.read()

    def spools_at(self, location):
        url = self._server() + "/api/v1/spool?" + urllib.parse.urlencode(
            {"location": location, "allow_archived": "false"}
        )
        return http_get(url)


# ---------------------------------------------------------------- usage correction
class UsageTracker:
    """Book what really went through the nozzle per spool, including purges.

    Moonraker only books extrusion when the extruder position (E) passes its
    highest value so far. On a Q2 every tool change first pulls ~160 mm back
    into the box, so the purge and the start of the next colour stay below that
    high-water mark: they are booked on no spool at all, while the outgoing
    spool was charged for the filament that went back into the box.

    spoolbridge therefore keeps its own books per period in which one spool is
    active:
      * the period starts at E0;
      * if the toolhead was empty at the start, the box loads first: the E at
        which the toolhead sensor first sees filament, plus SENSOR_TO_NOZZLE,
        is where the filament reaches the nozzle (the load itself is not used);
      * used = highest E in the period - that start point (purge + print;
        the unload at the end goes back into the box and does not count).
    When the period ends, spoolbridge compares this with what Moonraker booked
    on that spool in the same period (Spoolman used_length plus Moonraker's
    pending reports) and books the difference.
    """

    MIN_MM = 0.5         # ignore smaller differences
    MAX_MM = 50000.0     # more in one period means something odd: skip

    def __init__(self, spoolman, dry_run=False, sensor_to_nozzle=None):
        self.sm = spoolman
        self.dry_run = dry_run
        self.sensor_to_nozzle = SENSOR_TO_NOZZLE if sensor_to_nozzle is None else sensor_to_nozzle
        self.session = None    # current period, see _start
        self.closed = []       # finished periods waiting for Moonraker to flush
        self.last_ept = None   # toolhead.estimated_print_time: drops on a Klipper restart

    def _booked(self, sid, pending):
        try:
            return self.sm.used_length(sid) + pending.get(sid, 0.0)
        except Exception as e:
            log.warning("usage correction: cannot read spool %s from Spoolman: %s", sid, e)
            return None

    def _start(self, sid, epos, loaded, pending):
        l0 = self._booked(sid, pending)
        # A finished period of the same spool that was not settled yet: settle it
        # now, then read again so the new period starts after that correction.
        same = [c for c in self.closed if c["spool"] == sid]
        for c in same:
            self.closed.remove(c)
            self._settle(c, l0)
        if same and l0 is not None:
            l0 = self._booked(sid, pending)
        self.session = {"spool": sid, "e0": epos, "emax": epos, "l0": l0,
                        "loading": loaded is False, "trigger": None}

    def _close(self):
        s = self.session
        self.session = None
        if s is None or s["l0"] is None:
            return
        if s["loading"]:
            if s["trigger"] is None:
                used = 0.0   # never reached the toolhead: nothing used
            else:
                used = s["emax"] - (s["trigger"] + self.sensor_to_nozzle)
        else:
            used = s["emax"] - s["e0"]
        self.closed.append({"spool": s["spool"], "used": max(0.0, used), "l0": s["l0"],
                            "at": time.monotonic()})

    def _settle(self, c, l1):
        if l1 is None:
            log.warning("usage correction for spool %s skipped (Spoolman unreachable)", c["spool"])
            return
        booked = l1 - c["l0"]
        diff = c["used"] - booked
        if c["used"] > self.MAX_MM:
            log.warning("usage correction for spool %s skipped: %.1f mm is implausible",
                        c["spool"], c["used"])
            return
        if abs(diff) < self.MIN_MM:
            return
        if self.dry_run:
            log.info("[dry-run] spool %s: used %.1f mm, Moonraker booked %.1f mm, would book %+.1f mm",
                     c["spool"], c["used"], booked, diff)
            return
        self.sm.use(c["spool"], diff)
        log.info("spool %s: used %.1f mm, Moonraker booked %.1f mm, corrected %+.1f mm",
                 c["spool"], c["used"], booked, diff)

    def klipper_restart(self):
        """Klipper restarts reset E: end the period with what is known so far."""
        self._close()
        self.last_ept = None

    def update(self, sid, epos, loaded, ept, pending):
        """Call once per poll.

        sid: active spool, epos: toolhead E, loaded: toolhead sensor
        (True/False/None), ept: toolhead.estimated_print_time,
        pending: Moonraker's pending reports {spool id: mm}.
        """
        if epos is None:
            return
        if ept is not None and self.last_ept is not None and ept < self.last_ept:
            self._close()   # Klipper restarted between two polls
        self.last_ept = ept
        s = self.session
        if s is not None and s["spool"] != sid:
            self._close()
            s = None
        if s is None and sid is not None:
            self._start(sid, epos, loaded, pending)
            s = self.session
        if s is not None:
            if s["loading"] and s["trigger"] is None and loaded:
                s["trigger"] = epos
            if epos > s["emax"]:
                s["emax"] = epos
        # Settle finished periods once Moonraker has flushed its reports for them.
        now = time.monotonic()
        for c in list(self.closed):
            if c["spool"] in pending or now - c["at"] < 2 * self.sm.sync_rate + 1:
                continue
            self.closed.remove(c)
            self._settle(c, self._booked(c["spool"], pending))


# ---------------------------------------------------------------- Spoolman location (optional)
class LocationLookup:
    """Look up spools by Spoolman location; results cached for 60 s."""

    def __init__(self, prefix, spoolman=None):
        self.prefix = prefix
        self.sm = spoolman or Spoolman()
        self.cache = {}

    def __call__(self, slot):
        name = self.prefix + LOCATION_NAMES.get(slot, slot)
        hit = self.cache.get(name)
        if hit and time.monotonic() - hit[1] < 60:
            return hit[0]
        sid = None
        try:
            if self.sm._server():
                spools = self.sm.spools_at(name)
                if isinstance(spools, list) and len(spools) == 1:
                    sid = to_spool_id(spools[0].get("id"))
                elif isinstance(spools, list) and len(spools) > 1:
                    log.warning("%d spools with location %r; pick one.", len(spools), name)
        except Exception as e:  # a network error to Spoolman must not stop the loop
            log.warning("Spoolman location lookup failed: %s", e)
        self.cache[name] = (sid, time.monotonic())
        return sid


# ---------------------------------------------------------------- main loop
def get_spoolman_status():
    """Return (active spool id, {spool id: pending mm}) from Moonraker."""
    res = http_get(MOONRAKER + "/server/spoolman/status")["result"]
    pending = {}
    for p in res.get("pending_reports") or []:
        sid = to_spool_id(p.get("spool_id"))
        if sid:
            pending[sid] = pending.get(sid, 0.0) + float(p.get("filament_used") or 0.0)
    return to_spool_id(res.get("spool_id")), pending


def get_active_spool():
    return get_spoolman_status()[0]


def set_active_spool(sid):
    # Moonraker reads spool_id with get_int("spool_id", None): a JSON null is
    # rejected (HTTP 400). Clearing = leaving the key out.
    payload = {"spool_id": sid} if sid is not None else {}
    http_post_json(MOONRAKER + "/server/spoolman/spool_id", payload)


def run(dry_run=False, once=False, persister=None, tracker=None):
    spoolman = Spoolman()
    lookup = LocationLookup(LOCATION_PREFIX, spoolman) if LOCATION_PREFIX else None
    persister = persister if persister is not None else Persister()
    if tracker is None and USAGE_CORRECTION:
        tracker = UsageTracker(spoolman, dry_run)
    macros = sorted(set(SLOT_MACROS.values()))
    url = query_url(macros)
    last_key = None   # last logged state
    last_err = None   # last logged error (log each error only once)
    while True:
        err = None
        try:
            data = http_get(url)["result"]["status"]
            if (data.get("webhooks") or {}).get("state") != "ready":
                persister.reset()
                if tracker is not None:
                    tracker.klipper_restart()
                raise RuntimeError("Klipper not ready")
            persister.observe(data)
            busy = (data.get("print_stats") or {}).get("state") in BUSY_STATES
            if not busy and time.monotonic() >= persister.hold_until:
                for var, value in persister.pending(data):
                    if dry_run:
                        log.info("[dry-run] would save %s = %s", var, value)
                    else:
                        try:
                            save_variable(var, value)
                        except Exception:
                            persister.hold_until = time.monotonic() + persister.RETRY
                            raise
                        log.info("saved %s = %s (Fluidd selection)", var, value)
                    persister.done(var)
            slot, want, why = resolve(data, lookup, persister.cleared())
            have, pending = get_spoolman_status()
            key = (slot, want, have)
            changed = key != last_key
            if changed:
                log.info("loaded slot=%s wanted spool=%s (%s) active=%s", slot, want, why, have)
                last_key = key
            if want != have:
                if dry_run:
                    if changed:
                        log.info("[dry-run] would set active spool to %s", want)
                else:
                    set_active_spool(want)
                    log.info("active spool set to %s", want)
                    have = want
            if tracker is not None:
                th = data.get("toolhead") or {}
                pos = th.get("position") or []
                epos = float(pos[3]) if len(pos) > 3 and pos[3] is not None else None
                sensor = (data.get(TOOLHEAD_SENSOR) or {}).get("filament_detected")
                tracker.update(have, epos, sensor, th.get("estimated_print_time"), pending)
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode(errors="replace")[:200]
            except Exception:
                pass
            err = "Moonraker rejected request: HTTP %s %s" % (e.code, body or e.reason)
        except (urllib.error.URLError, OSError, RuntimeError, KeyError, ValueError) as e:
            err = "Moonraker/Klipper unreachable or not ready: %s" % e
        if err and err != last_err:
            log.warning(err)
        elif not err and last_err:
            log.info("recovered")
        last_err = err
        if once:
            return
        time.sleep(POLL)


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--dry-run", action="store_true", help="write nothing, only log")
    p.add_argument("--once", action="store_true", help="check once and exit")
    a = p.parse_args()
    logging.basicConfig(level=logging.INFO, stream=sys.stdout, format="%(levelname)s %(message)s")
    log.info("spoolbridge started: moonraker=%s poll=%ss location-prefix=%r usage-correction=%s dry-run=%s",
             MOONRAKER, POLL, LOCATION_PREFIX, USAGE_CORRECTION, a.dry_run)
    run(dry_run=a.dry_run, once=a.once)


if __name__ == "__main__":
    main()
