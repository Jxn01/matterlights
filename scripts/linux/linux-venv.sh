# Sourced by the Linux scripts that need the virtualenv -- never run on its own.
# The ONE place the Linux venv's location is decided; tests/test_linux_venv.py
# fails the build when a script or unit reaches for another.
#
# 🚨 The Linux venv lives OUTSIDE the checkout, on purpose. The checkout can be
# shared with Windows -- on the author's machine it is one tree on an NTFS drive
# that both halves of a dual boot mount, reached by the same path from each --
# and Windows' venv is `.venv` in the checkout: its scheduled tasks run
# `.venv\Scripts\pythonw.exe`. A Linux venv in the same place cannot coexist
# with it: there is only one `pyvenv.cfg`, and `lib`/`Lib` and
# `include`/`Include` differ only in case, which Windows cannot tell apart. A
# Linux `python -m venv .venv` run over Windows' venv breaks Windows' tasks.
#
# Override with MATTERLIGHTS_VENV.
MATTERLIGHTS_VENV="${MATTERLIGHTS_VENV:-${XDG_DATA_HOME:-$HOME/.local/share}/venvs/matterlights}"
