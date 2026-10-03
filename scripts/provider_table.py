#!/usr/bin/env python3
"""Regenerate the provider table in README.md from the provider registry.

    python3 scripts/provider_table.py          # rewrite README.md in place
    python3 scripts/provider_table.py --check  # exit 1 if README.md is stale (CI)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from quotaglance.providers import PROVIDER_CLASSES  # noqa: E402

START = "<!-- providers:start -->"
END = "<!-- providers:end -->"
CATEGORIES = {
    "agents": "Coding agents",
    "editors": "Editors & IDEs",
    "api": "Coding plans & API platforms",
    "media": "Voice & media",
}


def table() -> str:
    lines = ["| Provider | Category | Data source | Setup |", "| --- | --- | --- | --- |"]
    order = list(CATEGORIES)
    for cls in sorted(PROVIDER_CLASSES, key=lambda c: (order.index(c.category), c.name.lower())):
        name = f"[{cls.name}]({cls.homepage})" if cls.homepage else cls.name
        source = cls.source_summary.replace("|", "\\|")
        setup = cls.setup_hint.replace("|", "\\|")
        local = " (offline)" if cls.local_only else ""
        lines.append(f"| {name} | {CATEGORIES[cls.category]} | {source}{local} | {setup} |")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    readme = ROOT / "README.md"
    text = readme.read_text(encoding="utf-8")
    if START not in text or END not in text:
        print("README.md has no provider table markers", file=sys.stderr)
        return 1
    head, rest = text.split(START, 1)
    _old, tail = rest.split(END, 1)
    updated = f"{head}{START}\n{table()}\n{END}{tail}"
    if args.check:
        if updated != text:
            print("README.md provider table is out of date; run scripts/provider_table.py")
            return 1
        return 0
    readme.write_text(updated, encoding="utf-8")
    print(f"Updated provider table ({len(PROVIDER_CLASSES)} providers)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
