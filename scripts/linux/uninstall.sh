#!/usr/bin/env bash
#
# Remove the systemd units. Leaves the checkout, the venv and .env alone.
#
set -euo pipefail

USER_UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

echo "Stopping and removing the user units"
systemctl --user disable --now matterlights-sync.service matterlights-dashboard.service 2>/dev/null || true
systemctl --user stop matterlights-zone-ui.service 2>/dev/null || true
rm -f "$USER_UNIT_DIR/matterlights-sync.service" "$USER_UNIT_DIR/matterlights-dashboard.service"
systemctl --user daemon-reload

if [[ -f /etc/systemd/system/matterlights-lights-off.service ]]; then
    echo "Removing the system unit (needs sudo)"
    # Stop it explicitly first: stopping is what RUNS the turn-off, so this
    # leaves the lights off rather than stuck on whatever they were showing.
    sudo systemctl disable --now matterlights-lights-off.service 2>/dev/null || true
    sudo rm -f /etc/systemd/system/matterlights-lights-off.service
    sudo systemctl daemon-reload
fi

echo "Done. The checkout, .venv and .env are untouched."
