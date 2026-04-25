"""
Package entrypoint — `python -m ghost_shell [monitor|dashboard|scheduler]`.

This dispatches to the appropriate sub-module so users don't need to
remember the full module path. Defaults to `monitor` (the old `main.py`
behaviour) when invoked with no sub-command.

We use runpy so sub-modules execute under `__name__ == "__main__"` —
their existing `if __name__ == "__main__":` blocks handle signal setup,
run-finish persistence, and error banners. Without runpy those blocks
would be skipped and Chrome zombies would leak on SIGTERM.
"""

__author__ = "Mykola Kovhanko"
__email__ = "thuesdays@gmail.com"

import runpy
import sys


def _monitor():   runpy.run_module("ghost_shell.main",                run_name="__main__")
def _dashboard(): runpy.run_module("ghost_shell.dashboard.server",    run_name="__main__")
def _scheduler(): runpy.run_module("ghost_shell.scheduler.scheduler", run_name="__main__")


COMMANDS = {
    "monitor":   _monitor,
    "dashboard": _dashboard,
    "scheduler": _scheduler,
}


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else "monitor"
    if cmd in ("-h", "--help"):
        print("Usage: python -m ghost_shell [monitor|dashboard|scheduler]")
        print("Default command is `monitor` when none is given.")
        return 0
    if cmd not in COMMANDS:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        print(f"Valid: {', '.join(COMMANDS)}", file=sys.stderr)
        return 2
    # runpy modifies sys.argv[0] internally; strip the sub-command so
    # nested argparse in those modules sees a clean argv.
    sys.argv = [sys.argv[0]] + argv[1:]
    COMMANDS[cmd]()
    return 0


if __name__ == "__main__":
    sys.exit(main())
