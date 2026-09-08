#!/usr/bin/env bash
#
# Guided Linux setup: the counterpart to scripts/guided-setup.ps1.
#
# Creates the virtualenv, installs the package, helps write .env, and installs
# the systemd units.
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
VENV="$REPO_ROOT/.venv"
USER_UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
SYSTEM_UNIT_DIR="/etc/systemd/system"

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
warn() { printf '\033[33m%s\033[0m\n' "$*" >&2; }
die()  { printf '\033[31m%s\033[0m\n' "$*" >&2; exit 1; }
ask()  { local reply; read -r -p "$1 [y/N] " reply; [[ "$reply" =~ ^[Yy]$ ]]; }

# --------------------------------------------------------------------------
# 1. Find an interpreter that can actually see the system GObject bindings.
#
# THIS IS THE STEP THAT WASTES THE MOST TIME IF IT GOES WRONG. PyGObject and the
# GStreamer PipeWire plugin are distribution packages, not pip packages --
# `pip install PyGObject` needs meson plus cairo/glib headers and usually fails.
# The venv therefore has to be built with --system-site-packages, AND from the
# system interpreter, because a venv inherits the site-packages of the python it
# was created from. A miniconda or pyenv python has its own, which do not
# contain gi, so the venv comes up looking fine and dies at first capture.
#
# `python3` on PATH is frequently miniconda. Prefer /usr/bin/python3 outright.
# --------------------------------------------------------------------------
SYSTEM_PYTHON="${MATTERLIGHTS_PYTHON:-/usr/bin/python3}"

[[ -x "$SYSTEM_PYTHON" ]] || die "No system Python at $SYSTEM_PYTHON. Set MATTERLIGHTS_PYTHON to one."

if [[ "$("$SYSTEM_PYTHON" -c 'import sys; print(sys.prefix)')" == *conda* ]]; then
    die "$SYSTEM_PYTHON looks like a conda interpreter.
MatterLights needs the DISTRIBUTION python, because PyGObject and GStreamer are
system packages and a conda environment cannot see them. Try /usr/bin/python3."
fi

if ! "$SYSTEM_PYTHON" - <<'PY' 2>/dev/null
import gi
gi.require_version("Gst", "1.0")
gi.require_version("GstApp", "1.0")
from gi.repository import Gst
Gst.init(None)
assert Gst.ElementFactory.find("pipewiresrc"), "pipewiresrc element missing"
PY
then
    die "$SYSTEM_PYTHON cannot import PyGObject/GStreamer with PipeWire support.

Install them with your package manager, then re-run. On Arch/Garuda:
    sudo pacman -S --needed python-gobject gstreamer gst-plugins-base gst-plugin-pipewire
On Debian/Ubuntu:
    sudo apt install python3-gi gstreamer1.0-pipewire gstreamer1.0-plugins-base"
fi

bold "Using $SYSTEM_PYTHON ($("$SYSTEM_PYTHON" -V))"

# --------------------------------------------------------------------------
# 2. Virtualenv and package
# --------------------------------------------------------------------------
if [[ ! -d "$VENV" ]]; then
    bold "Creating $VENV (--system-site-packages, so it can see PyGObject)"
    "$SYSTEM_PYTHON" -m venv --system-site-packages "$VENV"
fi
"$VENV/bin/python" -m pip install --quiet --upgrade pip
bold "Installing MatterLights"
"$VENV/bin/python" -m pip install --quiet -e "$REPO_ROOT"

# --------------------------------------------------------------------------
# 3. Configuration
# --------------------------------------------------------------------------
if [[ ! -f "$REPO_ROOT/.env" ]]; then
    cp "$REPO_ROOT/.env.example" "$REPO_ROOT/.env"
    chmod 600 "$REPO_ROOT/.env"
    warn "Created $REPO_ROOT/.env from the example."
    warn "Put your Home Assistant URL and long-lived token in it, then re-run this script."
    warn "List your light entities with:  $VENV/bin/python -m matterlights.discover"
    exit 0
fi
chmod 600 "$REPO_ROOT/.env"

bold "Checking Home Assistant"
if "$VENV/bin/python" -m matterlights.discover >/dev/null 2>&1; then
    echo "  Home Assistant reachable."
else
    warn "  Could not reach Home Assistant. Check HA_URL and HA_TOKEN in .env."
fi

# --------------------------------------------------------------------------
# 4. systemd units
# --------------------------------------------------------------------------
install_unit() {   # $1 = source unit, $2 = destination dir, $3 = sudo or empty
    local src="$REPO_ROOT/systemd/$1" dst="$2/$1"
    ${3:+sudo} mkdir -p "$2"
    sed "s|@INSTALL_DIR@|$REPO_ROOT|g" "$src" | ${3:+sudo} tee "$dst" >/dev/null
    echo "  installed $dst"
}

if ask "Install the user units (sync + dashboard, started with your desktop session)?"; then
    install_unit matterlights-sync.service      "$USER_UNIT_DIR"
    install_unit matterlights-dashboard.service "$USER_UNIT_DIR"
    systemctl --user daemon-reload
    systemctl --user enable --now matterlights-sync.service matterlights-dashboard.service
    bold "Enabled. Status:"
    systemctl --user --no-pager --lines=0 status matterlights-sync.service || true
fi

if ask "Install the system unit that turns the lights off at shutdown (needs sudo)?"; then
    install_unit matterlights-lights-off.service "$SYSTEM_UNIT_DIR" sudo
    sudo systemctl daemon-reload
    sudo systemctl enable --now matterlights-lights-off.service
    echo "  It does nothing at boot; the work is in ExecStop, which runs at shutdown."
fi

bold "Done."
echo "  Dashboard:      http://127.0.0.1:$(grep -E '^DASHBOARD_PORT=' "$REPO_ROOT/.env" | cut -d= -f2 || echo 8770)"
echo "  Sync logs:      journalctl --user -u matterlights-sync -f"
echo "  Stop for now:   systemctl --user stop matterlights-sync"
echo "  Remove it all:  scripts/linux/uninstall.sh"
