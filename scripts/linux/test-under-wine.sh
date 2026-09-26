#!/usr/bin/env bash
# Run the unit suite on Windows CPython, under Wine, from Linux.
#
# A green suite on Linux says little about Windows: pathlib, os, sys.platform and
# the locale's encoding all differ there, and a test that assumed a POSIX host
# passes here for the wrong reason. This runs the same suite on the real Windows
# build of CPython -- python.org's embeddable zip -- so sys.platform is win32 and
# the Windows code paths (capture, display power, service control, the per-OS
# .env.windows overlay) really run. Adapted from ambience-rgb's runner.
#
# What it cannot vouch for is Windows itself: DXGI, user32 and the rest are
# Wine's re-implementations. A pass is a good sign for those tests, not proof.
#
# Nothing is installed. The interpreter, its wheels and the Wine prefix live in
# one cache directory; delete it to start over.
#
#   scripts/linux/test-under-wine.sh                # the whole suite
#   scripts/linux/test-under-wine.sh -v -k config   # anything `unittest discover` takes
#
#   MATTERLIGHTS_WINE_PYTHON  Windows CPython version (default 3.12.10, the
#                             Windows half's interpreter)
#   MATTERLIGHTS_WINE_CACHE   where it all lives (default ~/.cache/matterlights-wine)

set -euo pipefail

PY_VERSION="${MATTERLIGHTS_WINE_PYTHON:-3.12.10}"
CACHE="${MATTERLIGHTS_WINE_CACHE:-${XDG_CACHE_HOME:-$HOME/.cache}/matterlights-wine}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PYDIR="$CACHE/python-$PY_VERSION"
MINOR="${PY_VERSION%.*}"        # 3.12
TAG="${MINOR/./}"               # 312

for tool in wine winepath curl unzip; do
    command -v "$tool" >/dev/null || { echo "test-under-wine: needs $tool" >&2; exit 2; }
done

if [ ! -x "$PYDIR/python.exe" ]; then
    # Any Linux Python with tomllib and pip will do; it only reads pyproject.toml
    # and downloads wheels. Never bare `python3`, which may be miniconda.
    # shellcheck source=linux-venv.sh
    source "$REPO/scripts/linux/linux-venv.sh"
    host_python="$MATTERLIGHTS_VENV/bin/python"
    [ -x "$host_python" ] || host_python=/usr/bin/python3
    mkdir -p "$PYDIR/Lib/site-packages" "$CACHE/wheels"
    zip="$CACHE/python-$PY_VERSION-embed-amd64.zip"
    curl -fsSL -o "$zip" "https://www.python.org/ftp/python/$PY_VERSION/python-$PY_VERSION-embed-amd64.zip"
    unzip -qo "$zip" -d "$PYDIR"
    # The runtime dependencies, from pyproject.toml, as Windows wheels. The
    # markers are dropped because pip judges them by THIS machine: the
    # win32-only dxcam would be skipped as not applying.
    mapfile -t deps < <("$host_python" -c '
import sys, tomllib
with open(sys.argv[1], "rb") as handle:
    project = tomllib.load(handle)["project"]
print("\n".join(dep.split(";")[0].strip() for dep in project["dependencies"]))
' "$REPO/pyproject.toml")
    "$host_python" -m pip download -q --only-binary=:all: --platform win_amd64 \
        --python-version "$MINOR" --implementation cp -d "$CACHE/wheels" "${deps[@]}"
    for wheel in "$CACHE"/wheels/*.whl; do
        unzip -qo "$wheel" -d "$PYDIR/Lib/site-packages"
    done
fi

export WINEPREFIX="$CACHE/prefix" WINEDEBUG=-all
# No desktop entries, and no Mono or Gecko installer prompts.
export WINEDLLOVERRIDES="winemenubuilder.exe=d;mscoree=d;mshtml=d"

# The embeddable build ignores PYTHONPATH: its ._pth file IS sys.path.
printf '%s\r\n' "python$TAG.zip" "." 'Lib\site-packages' "$(winepath -w "$REPO/src")" "import site" \
    > "$PYDIR/python$TAG._pth"

cd "$REPO"
set +e
wine "$PYDIR/python.exe" -m unittest discover -s tests "$@" 2>&1 \
    | grep -v -e '^MESA' -e 'pci id for fd'
exit "${PIPESTATUS[0]}"
