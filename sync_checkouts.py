#!/usr/bin/env python3
"""Hold this panel checkout against the sibling project's copy of it.

The DCO-CONTROL-PANEL submodule is checked out once per project (DCO3-MONOSYNTH
and DCO4-REBORN), and work on the tool usually happens in whichever tree the
board being tested lives in. Both copies can sit on the same commit and still
disagree, because edits stay uncommitted for a while: that is exactly how the
preset-scroll frames ended up in one copy only. This script makes that visible
in one command instead of at the next `gen_midi_map.py --check` failure.

Model differences belong in models.py, not in per-checkout edits, so the code
files here are expected to be identical in both trees. Preset banks are working
data (each tree edits its own bank), so they are reported but never enforced.

Usage:
  python3 sync_checkouts.py                 diff the code files, exit 1 on drift
  python3 sync_checkouts.py --copy this     overwrite the sibling with this copy
  python3 sync_checkouts.py --copy other    overwrite this copy with the sibling
  python3 sync_checkouts.py --other PATH    point at a checkout explicitly
"""

from __future__ import annotations

import argparse
import difflib
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

# Everything that must match. Data (presets/) and caches are handled separately.
CODE_GLOBS = ("*.py", "README.md", "requirements.txt")
DATA_GLOBS = ("presets/*.json",)


def find_sibling() -> Path:
    """The same submodule path under the other project next to this one."""
    projects_root = HERE.parent.parent
    found = [d / HERE.name for d in sorted(projects_root.iterdir()) if d.is_dir()]
    found = [p for p in found if p.is_dir() and p != HERE]
    if not found:
        sys.exit(f"error: no sibling {HERE.name} found under {projects_root}; use --other")
    if len(found) > 1:
        names = ", ".join(str(p) for p in found)
        sys.exit(f"error: several sibling checkouts ({names}); pick one with --other")
    return found[0]


def relative_names(*roots: Path, globs: tuple[str, ...]) -> list[str]:
    """Union of matching files across both checkouts, so an absent file still shows up."""
    names: set[str] = set()
    for root in roots:
        for pattern in globs:
            names.update(str(p.relative_to(root)) for p in root.glob(pattern))
    return sorted(names)


def read(path: Path) -> list[str] | None:
    try:
        return path.read_text().splitlines(keepends=True)
    except OSError:
        return None


def compare(a: Path, b: Path, name: str) -> bool:
    """Print a unified diff of one file. Returns True when the two copies match."""
    left, right = read(a / name), read(b / name)
    if left == right:
        return True
    if left is None or right is None:
        missing, present = (a, b) if left is None else (b, a)
        print(f"{name}: missing in {missing}, present in {present}")
        return False
    diff = difflib.unified_diff(left, right, fromfile=f"{a}/{name}", tofile=f"{b}/{name}")
    sys.stdout.writelines(diff)
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--other", type=Path, help="the other DCO-CONTROL-PANEL checkout")
    ap.add_argument("--copy", choices=("this", "other"),
                    help="after diffing, copy the code files from the named side")
    args = ap.parse_args()

    other = (args.other.resolve() if args.other else find_sibling())
    if not (other / "app.py").is_file():
        sys.exit(f"error: {other} does not look like a DCO-CONTROL-PANEL checkout")
    print(f"this : {HERE}\nother: {other}\n")

    code = relative_names(HERE, other, globs=CODE_GLOBS)
    drifted = [name for name in code if not compare(HERE, other, name)]

    data = relative_names(HERE, other, globs=DATA_GLOBS)
    data_drifted = [name for name in data
                    if read(HERE / name) != read(other / name)]
    if data_drifted:
        print(f"\nnote: working data differs (not enforced): {', '.join(data_drifted)}")

    if not drifted:
        print(f"\nok: {len(code)} code files identical in both checkouts")
        return 0

    print(f"\ndrift: {len(drifted)} of {len(code)} code files — {', '.join(drifted)}")
    if not args.copy:
        print("run with --copy this|other to resolve it")
        return 1

    src, dst = (HERE, other) if args.copy == "this" else (other, HERE)
    for name in drifted:
        if not (src / name).is_file():
            print(f"skip {name}: absent in {src}, delete it by hand if it is stale")
            continue
        (dst / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src / name, dst / name)
        print(f"copied {name} -> {dst}")
    print("\nre-run gen_midi_map.py --check in both projects")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
