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
            "print_stats=state", "%s=filament_detected" % TOOLHEAD_SENSOR]
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


# ---------------------------------------------------------------- Spoolman location (optional)
class LocationLookup:
    """Look up spools by Spoolman location; results cached for 60 s."""

    def __init__(self, prefix):
        self.prefix = prefix
        self.server = None
        self.cache = {}

    def _server(self):
        if self.server is None:
            cfg = http_get(MOONRAKER + "/server/config")["result"]["config"]
            self.server = (cfg.get("spoolman") or {}).get("server", "").rstrip("/") or ""
        return self.server

    def __call__(self, slot):
        name = self.prefix + LOCATION_NAMES.get(slot, slot)
        hit = self.cache.get(name)
        if hit and time.monotonic() - hit[1] < 60:
            return hit[0]
        sid = None
        try:
            server = self._server()
            if server:
                url = server + "/api/v1/spool?" + urllib.parse.urlencode(
                    {"location": name, "allow_archived": "false"}
                )
                spools = http_get(url)
                if isinstance(spools, list) and len(spools) == 1:
                    sid = to_spool_id(spools[0].get("id"))
                elif isinstance(spools, list) and len(spools) > 1:
                    log.warning("%d spools with location %r; pick one.", len(spools), name)
        except Exception as e:  # a network error to Spoolman must not stop the loop
            log.warning("Spoolman location lookup failed: %s", e)
        self.cache[name] = (sid, time.monotonic())
        return sid


# ---------------------------------------------------------------- main loop
def get_active_spool():
    res = http_get(MOONRAKER + "/server/spoolman/status")["result"]
    return to_spool_id(res.get("spool_id"))


def set_active_spool(sid):
    # Moonraker reads spool_id with get_int("spool_id", None): a JSON null is
    # rejected (HTTP 400). Clearing = leaving the key out.
    payload = {"spool_id": sid} if sid is not None else {}
    http_post_json(MOONRAKER + "/server/spoolman/spool_id", payload)


def run(dry_run=False, once=False, persister=None):
    lookup = LocationLookup(LOCATION_PREFIX) if LOCATION_PREFIX else None
    persister = persister if persister is not None else Persister()
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
            have = get_active_spool()
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
    log.info("spoolbridge started: moonraker=%s poll=%ss location-prefix=%r dry-run=%s",
             MOONRAKER, POLL, LOCATION_PREFIX, a.dry_run)
    run(dry_run=a.dry_run, once=a.once)


if __name__ == "__main__":
    main()
