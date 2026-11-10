#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 q2-spoolbridge contributors
# Install or restore spoolbridge (e.g. after a QIDI update or a fresh flash).
# Run as a user with sudo (on the Q2: mks), from this directory:   bash install.sh
# Uninstall:                                                         bash install.sh --uninstall
#
# The Klipper config stays owned by the user that runs Klipper/Moonraker
# (qidi on the Q2); owner and mode are copied from printer.cfg, so SAVE_CONFIG
# and Fluidd can keep writing these files.
#
# Override the config directory if yours differs:
#   SPOOLBRIDGE_CFG_DIR=/home/qidi/printer_data/config bash install.sh
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
CFG_DIR="${SPOOLBRIDGE_CFG_DIR:-/home/qidi/printer_data/config}"
CFG_FILE="customizations/spoolbridge.cfg"
INCLUDE="[include ${CFG_FILE}]"
PRINTER_CFG="${CFG_DIR}/printer.cfg"

sudo -v || { echo "This user has no sudo. On the Q2, run as mks."; exit 1; }
sudo test -f "${PRINTER_CFG}" || { echo "${PRINTER_CFG} not found (set SPOOLBRIDGE_CFG_DIR)."; exit 1; }
OWNER="$(sudo stat -c '%U:%G' "${PRINTER_CFG}")"
MODE="$(sudo stat -c '%a' "${PRINTER_CFG}")"
echo "Klipper config is owned by ${OWNER} (mode ${MODE}); keeping it that way."

# Edit printer.cfg, keeping owner and mode.
edit_printer_cfg() {   # $1 = sed expression
  sudo cp -p "${PRINTER_CFG}" "${PRINTER_CFG}.spoolbridge.bak"
  sudo sed -i "$1" "${PRINTER_CFG}"
  sudo chown "${OWNER}" "${PRINTER_CFG}"
  sudo chmod "${MODE}" "${PRINTER_CFG}"
}

if [[ "${1:-}" == "--uninstall" ]]; then
  sudo systemctl disable --now spoolbridge.service 2>/dev/null || true
  sudo rm -f /etc/systemd/system/spoolbridge.service
  sudo rm -rf /opt/spoolbridge
  sudo systemctl daemon-reload
  if sudo grep -qF "${INCLUDE}" "${PRINTER_CFG}"; then
    edit_printer_cfg "\|^\[include ${CFG_FILE}\]|d"
    echo "Include removed from printer.cfg (backup: printer.cfg.spoolbridge.bak)."
  fi
  echo "spoolbridge removed. ${CFG_DIR}/${CFG_FILE} is left in place."
  echo "Do a RESTART in Fluidd while the printer is idle."
  exit 0
fi

echo "== 1. Install service to /opt/spoolbridge"
sudo install -d -o root -g root -m 755 /opt/spoolbridge
if [[ "${HERE}" != "/opt/spoolbridge" ]]; then
  # Keep a full copy, so you can restore later with:
  #   bash /opt/spoolbridge/install.sh
  for f in spoolbridge.py spoolbridge.service spoolbridge.cfg install.sh README.md README.nl.md LICENSE; do
    [[ -f "${HERE}/${f}" ]] && sudo install -o root -g root -m 644 "${HERE}/${f}" "/opt/spoolbridge/${f}"
  done
fi
sudo install -o root -g root -m 644 "${HERE}/spoolbridge.service" /etc/systemd/system/spoolbridge.service
sudo systemctl daemon-reload

echo "== 2. Fluidd macros in ${CFG_DIR}/${CFG_FILE}"
sudo install -d -o "${OWNER%%:*}" -g "${OWNER##*:}" -m 755 "${CFG_DIR}/customizations"
sudo install -o "${OWNER%%:*}" -g "${OWNER##*:}" -m 644 "${HERE}/spoolbridge.cfg" "${CFG_DIR}/${CFG_FILE}"

echo "== 3. Include in printer.cfg"
if sudo grep -qF "${INCLUDE}" "${PRINTER_CFG}"; then
  echo "   already there."
elif sudo grep -q "^#\*# <-* SAVE_CONFIG" "${PRINTER_CFG}"; then
  # Must go before the SAVE_CONFIG block, otherwise Klipper rejects the config.
  edit_printer_cfg "0,/^#\*# <-* SAVE_CONFIG/s||${INCLUDE}\n\n&|"
  echo "   added before SAVE_CONFIG (backup: printer.cfg.spoolbridge.bak)."
else
  edit_printer_cfg "\$a\\
\\
${INCLUDE}"
  echo "   appended at the end (backup: printer.cfg.spoolbridge.bak)."
fi
sudo ls -l "${PRINTER_CFG}" "${CFG_DIR}/${CFG_FILE}"

echo "== 4. Dry run (writes nothing)"
/usr/bin/python3 /opt/spoolbridge/spoolbridge.py --dry-run --once || true

echo "== 5. Start service"
sudo systemctl enable --now spoolbridge.service
sudo systemctl restart spoolbridge.service
sleep 2
sudo systemctl --no-pager --lines=5 status spoolbridge.service || true

cat <<EOF

Done. Still to do:
  * Do a RESTART in Fluidd (only while nothing is printing), so Klipper
    loads the macros Q2_SLOT1..4 and Q2_EXTERN.
  * Then pick the spool for each slot in Fluidd (Spoolman card).
  * Follow the logs:  sudo journalctl -u spoolbridge -f
EOF
