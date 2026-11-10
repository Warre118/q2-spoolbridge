# q2-spoolbridge

**Spoolman integration for the QIDI Q2 with QIDI Box that works with the new firmware (01.01.03.x).**

spoolbridge automatically sets the right spool as active in Moonraker, based on the slot the QIDI Box has loaded. Moonraker then tracks filament usage and reports it to Spoolman as usual. You assign a spool to each box slot in Fluidd, just like before.

> **Status: beta.** Tested on a Q2 with one QIDI Box, firmware 01.01.03.x. Not yet confirmed: whether `loaded_slot` changes exactly at every tool change in a multi-colour print. Please report your results in the issues.

## Why

Since the recent QIDI firmware, every tool change goes through QIDI's `T_MANAGER`. The common community approach overrides `T0`–`T3` with custom macros that call `EXTRUDER_LOAD` directly. That bypasses QIDI's own bookkeeping, and **the QIDI Box gets out of sync** with the printer (which can mean re-flashing and reconfiguring the box).

spoolbridge does **not** touch tool changes. It only **watches** `multi_color_controller.box.loaded_slot` through the Moonraker API and sets the active spool. It never overrides QIDI macros and never operates the box. The only G-code it sends is `SAVE_VARIABLE` for its own `q2_*__spool_id` variables (see below). It also follows auto-refill, because it looks at the loaded slot, not at the T number.

## Files

| File | Goes to |
|---|---|
| `spoolbridge.py` | `/opt/spoolbridge/` (the service, Python 3.9, standard library only) |
| `spoolbridge.service` | `/etc/systemd/system/` (runs unprivileged with `DynamicUser`) |
| `spoolbridge.cfg` | `~/printer_data/config/customizations/` (empty Fluidd macros, one per slot) |
| `install.sh` | install, restore after an update, and `--uninstall` |

## Requirements

- QIDI Q2 with QIDI Box, firmware 01.01.03.x (with `T_MANAGER`).
- Spoolman configured in `moonraker.conf` (`[spoolman]` section) and working in Fluidd.
- SSH access as a user with `sudo`. On the Q2 that is `mks`; `qidi` has no sudo.

## Install

On the Q2, `/home/mks` is a symlink to `/home/qidi`, and `mks` cannot write there, so upload to `/tmp`. On your PC, in the folder that contains `q2-spoolbridge` (replace `<printer>` with your printer's hostname or IP):

```bash
scp -r q2-spoolbridge mks@<printer>:/tmp/
```

Then on the printer, as `mks`:

```bash
cd /tmp/q2-spoolbridge && bash install.sh
```

Then do a **RESTART** in Fluidd while the printer is idle, so Klipper loads the macros `Q2_SLOT1..4` and `Q2_EXTERN`.

The installer:
- keeps a full copy in `/opt/spoolbridge`;
- adds `[include customizations/spoolbridge.cfg]` before the `SAVE_CONFIG` block of `printer.cfg`, after making a backup (`printer.cfg.spoolbridge.bak`);
- keeps the owner and mode of your config files, so Fluidd and `SAVE_CONFIG` keep working;
- does a dry run before starting the service.

Running it again is safe.

**After a QIDI update:** `bash /opt/spoolbridge/install.sh`.
**After a full re-flash:** `/opt` is gone too, so upload again from your PC.

## Assign spools to slots

1. In Fluidd, open the Spoolman card. Each slot macro (`Q2_SLOT1` … `Q2_SLOT4`, `Q2_EXTERN`) gets a spool selector.
2. Pick the spool for each slot. spoolbridge saves the choice in `saved_variables.cfg` as `q2_slot1__spool_id`, so it survives a restart.
3. Done. When the box loads `slot0`, the spool of `Q2_SLOT1` becomes active, and so on.

| Box slot | Macro | On the screen |
|---|---|---|
| slot0 | Q2_SLOT1 | slot 1 |
| slot1 | Q2_SLOT2 | slot 2 |
| slot2 | Q2_SLOT3 | slot 3 |
| slot3 | Q2_SLOT4 | slot 4 |
| slot4–7 | Q2_BOX2_SLOT1–4 | second box (uncomment in `spoolbridge.cfg`) |
| slot16 | Q2_EXTERN | external holder |

**Optional:** set `SPOOLBRIDGE_LOCATION_PREFIX=Q2 ` in the service. A slot without a Fluidd assignment then falls back to the Spoolman spool with location `Q2 slot1` … `Q2 slot4` or `Q2 extern`.

## Behaviour

- QIDI reports `loaded_slot = slot16` even when the toolhead is empty. spoolbridge therefore only treats `slot16` as loaded when the toolhead filament sensor sees filament.
- The box loads filament into the toolhead only at print start or at a tool change, so the active spool changes at those moments, not when you insert a spool in the box.
- If the loaded slot has no spool assigned, spoolbridge **clears** the active spool. Better to book nothing than to book on the wrong spool.
- If you set a different active spool by hand in Fluidd, spoolbridge sets it back within a second. The slot is leading.
- **Fluidd 1.30.x bug:** the Fluidd shipped with QIDI firmware saves spool selections as `Q2_SLOT2__SPOOL_ID` (upper case), which Klipper rejects, so a selection is lost after a restart ([fixed upstream in fluidd#1563](https://github.com/fluidd-core/fluidd/pull/1563)). spoolbridge works around it without patching Fluidd: when a slot macro changes, it saves `SAVE_VARIABLE VARIABLE=q2_slot2__spool_id VALUE=<id>` itself, but only while the printer is not printing or paused. With a fixed Fluidd it sees the value is already saved and does nothing.
- **Purges and tool changes are booked correctly.** Moonraker only books extrusion when the extruder passes its highest position so far. A Q2 tool change pulls ~160 mm back into the box first, so Moonraker books the purge and the start of the next colour on *no* spool, and charges the outgoing spool for filament that went back into the box. spoolbridge keeps its own books per spool. A spool pays for everything that passed the cutter while it was loaded: what it printed and purged, plus the piece left in the hotend at its own cut. The next filament pushing that piece out costs the next spool nothing. In extruder terms: from the moment the new tip passes the cutter (toolhead sensor + `SPOOLBRIDGE_LOAD_OFFSET`, default 67 mm, measured against the slicer's totals) to the cut (highest extruder position before the retract). When the next spool takes over, it books the difference with Moonraker on that spool in Spoolman (negative = filament given back). The log shows each correction, e.g. `spool 7: used 60.7 mm, Moonraker booked 0.0 mm, corrected +60.7 mm`. Turn off with `SPOOLBRIDGE_USAGE_CORRECTION=0`.
- Clearing a slot in Fluidd clears it for real: spoolbridge does not fall back to the old saved value.
- Logs: `sudo journalctl -u spoolbridge -f`
- Test without writing: `python3 /opt/spoolbridge/spoolbridge.py --dry-run`

## Configuration

Environment variables in `spoolbridge.service`:

| Variable | Default | Meaning |
|---|---|---|
| `SPOOLBRIDGE_MOONRAKER` | `http://127.0.0.1:7125` | Moonraker URL |
| `SPOOLBRIDGE_POLL` | `1.0` | poll interval in seconds |
| `SPOOLBRIDGE_LOCATION_PREFIX` | empty | Spoolman location fallback (see above) |
| `SPOOLBRIDGE_USAGE_CORRECTION` | `1` | book purges and fix per-spool usage after tool changes; `0` = leave it to Moonraker |
| `SPOOLBRIDGE_LOAD_OFFSET` | `67` | mm of E after the toolhead sensor sees new filament before it counts as used (Q2, measured) |

The installer accepts `SPOOLBRIDGE_CFG_DIR` if your config is not in `/home/qidi/printer_data/config`.

## Uninstall

```bash
bash /opt/spoolbridge/install.sh --uninstall
```

This removes the service and the include. The macro file stays in place.

## Tests

```bash
python3 tests/test_spoolbridge.py
```

These are offline tests with a fake Moonraker; they need no printer.

## Disclaimer

Not affiliated with QIDI. Use at your own risk. spoolbridge only reads printer state and sets Moonraker's active spool, but you are modifying your printer's configuration.

## License

GPL-3.0-or-later, see [LICENSE](LICENSE). A Dutch README is available in [README.nl.md](README.nl.md).

---

*Developed with the help of [Claude](https://claude.ai) (Anthropic), an AI assistant. All code was reviewed and tested on a real Q2.*
