"""Command-line entry point."""

from __future__ import annotations

import argparse
import logging
import os
import sys

from quotaglance import APP_NAME, __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="quotaglance",
        description=f"{APP_NAME}: your AI coding quotas at a glance.",
    )
    parser.add_argument("--status", action="store_true",
                        help="print current usage in the terminal and exit")
    parser.add_argument("--json", action="store_true",
                        help="print current usage as JSON and exit")
    parser.add_argument("--demo", action="store_true",
                        help="use realistic mock data instead of your accounts")
    parser.add_argument("--background", action="store_true",
                        help="start without opening a window (used for autostart)")
    parser.add_argument("--widget", action="store_true", help="toggle the desktop widget")
    parser.add_argument("--preferences", action="store_true", help="open Preferences")
    parser.add_argument("--quit", action="store_true", help="quit the running instance")
    parser.add_argument("--debug", action="store_true", help="verbose logging")
    parser.add_argument("--claude-statusline", action="store_true",
                        help="internal: Claude Code status-line bridge (reads JSON on stdin)")
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    if "--claude-statusline" in argv[1:]:
        # Claude Code status-line tap: must stay fast, so no GTK and no argparse.
        from quotaglance.claude_statusline import tap_main

        return tap_main()
    args, _unknown = build_parser().parse_known_args(argv[1:])
    logging.basicConfig(
        level=logging.DEBUG if args.debug or os.environ.get("QUOTAGLANCE_DEBUG") else
        logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    demo = args.demo or os.environ.get("QUOTAGLANCE_DEMO", "").lower() in ("1", "true", "yes")
    if args.status or args.json:
        from quotaglance.cli import run_status

        return run_status(json_output=args.json, demo=demo)

    from quotaglance.gui.application import run

    return run(argv)


if __name__ == "__main__":
    sys.exit(main())
