#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = ROOT / "pyproject.toml"
INIT_FILE = ROOT / "src" / "oep_client" / "__init__.py"
CHANGELOG = ROOT / "CHANGELOG.md"
LOCK = ROOT / "uv.lock"


def update_pyproject(version: str) -> None:
    text = PYPROJECT.read_text(encoding="utf-8")
    updated = re.sub(
        r'(?m)^version = "[^"]+"$',
        f'version = "{version}"',
        text,
        count=1,
    )
    if not re.search(r'(?m)^version = "[^"]+"$', text):
        raise RuntimeError("Failed to update version in pyproject.toml")
    PYPROJECT.write_text(updated, encoding="utf-8")


def update_init(version: str) -> None:
    text = INIT_FILE.read_text(encoding="utf-8")
    updated = re.sub(
        r'(?m)^__version__ = "[^"]+"$',
        f'__version__ = "{version}"',
        text,
        count=1,
    )
    if not re.search(r'(?m)^__version__ = "[^"]+"$', text):
        raise RuntimeError("Failed to update __version__ in __init__.py")
    INIT_FILE.write_text(updated, encoding="utf-8")


def update_lock(version: str) -> None:
    """The project's own entry in uv.lock (the editable one) carries the version too: set it here, so the release
    commit has uv.lock in step instead of the next `uv sync` rewriting it. Nothing else in the lock changes."""
    if not LOCK.exists():
        return
    text = LOCK.read_text(encoding="utf-8")
    name = re.search(r'(?m)^name = "([^"]+)"$', PYPROJECT.read_text(encoding="utf-8")).group(1)
    pattern = re.compile(
        r'(\[\[package\]\]\nname = "' + re.escape(name) + r'"\nversion = ")[^"]+("\nsource = \{ editable = "\." \})'
    )
    updated, count = pattern.subn(lambda m: m.group(1) + version + m.group(2), text)
    if count != 1:
        raise RuntimeError(f"Failed to update the version of {name} in uv.lock")
    LOCK.write_text(updated, encoding="utf-8")


def update_changelog(version: str) -> None:
    lines = CHANGELOG.read_text(encoding="utf-8").splitlines()
    try:
        unreleased_idx = lines.index("## Unreleased")
    except ValueError as exc:
        raise RuntimeError("CHANGELOG.md must contain '## Unreleased'") from exc

    tail_start = len(lines)
    for i in range(unreleased_idx + 1, len(lines)):
        if lines[i].startswith("## "):
            tail_start = i
            break

    unreleased_block = lines[unreleased_idx + 1 : tail_start]
    unreleased_items = [line for line in unreleased_block if line.strip()]

    new_lines: list[str] = []
    new_lines.extend(lines[: unreleased_idx + 1])
    new_lines.append("")
    new_lines.append(f"## {version}")
    if unreleased_items:
        new_lines.extend(unreleased_items)
    else:
        new_lines.append("- (EN) No user-facing changes recorded.")
        new_lines.append("- (JA) ユーザー向け変更の記録はありません。")
    if tail_start < len(lines):
        new_lines.append("")
        new_lines.extend(lines[tail_start:])

    CHANGELOG.write_text("\n".join(new_lines).rstrip() + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare release version and changelog.")
    parser.add_argument("--version", required=True, help="Release version (e.g. 0.1.0, 0.1.0b1)")
    args = parser.parse_args()

    version = args.version.strip()
    # X.Y.Z, optionally a PEP 440 pre-release (0.1.0b1, 1.0.0rc1)
    if not re.fullmatch(r"\d+\.\d+\.\d+((a|b|rc)\d+)?", version):
        raise SystemExit("Version must be X.Y.Z or X.Y.Z{a|b|rc}N")

    update_pyproject(version)
    update_init(version)
    update_lock(version)
    update_changelog(version)
    print(f"Prepared release: {version}")


if __name__ == "__main__":
    main()
