"""Entry point for ``python -m matterlights``.

The ``if __name__`` guard is load-bearing, not ceremony. Without it, merely
*importing* this module starts the sync loop -- which means any tool that walks
the package (a test that checks every module imports, a documentation builder, an
IDE indexer) launches a real screen-capture daemon and starts driving the lights.
``python -m matterlights`` still sets ``__name__`` to ``"__main__"``, so the
guard costs nothing at run time.
"""

from matterlights.main import main

if __name__ == "__main__":
    raise SystemExit(main())
