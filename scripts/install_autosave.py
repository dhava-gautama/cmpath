#!/usr/bin/env python3
"""Install (or remove) the cmpath autosave hooks in ~/.kimi-code/config.toml.

The installer appends one managed region holding one ``[[hooks]]`` block per
managed event (TurnStarted, Stop, SessionEnd), all running the same script
with the same timeout. It never prints the config file contents — only the
region it manages. A timestamped backup (``config.toml.<YYYYMMDD-HHMMSS>.bak``)
is written next to the config before any change, and a second install is
refused. A *legacy* marker-less block — a top-level ``[[hooks]]`` section
running ``autosave_session.py`` that sits outside the marker comments — is
recognised and replaced by the managed region; a section pointing at any
other script is never touched.

Usage:
  install_autosave.py --print          show the block that would be added
  install_autosave.py                  install (backup + append + verify)
  install_autosave.py --uninstall      backup + strip the managed block,
                                       marked region or legacy section
  install_autosave.py --config PATH    operate on a different config file
  install_autosave.py --script PATH    hook command override (default: the
                                       autosave_session.py next to this file)
"""

from __future__ import annotations

import argparse
import datetime
import os
import re
import shutil
import sys
from pathlib import Path

DEFAULT_CONFIG = "~/.kimi-code/config.toml"
BEGIN = "# >>> cmpath-autosave managed block >>>"
END = "# <<< cmpath-autosave managed block <<<"
MANAGED_MARKERS = (BEGIN, END)
MANAGED_SCRIPT = "autosave_session.py"
MANAGED_EVENTS = ("TurnStarted", "Stop", "SessionEnd")
HOOK_HEADER_RE = re.compile(r"^\s*\[\[hooks\]\]")
MANAGED_COMMAND_RE = re.compile(r"^\s*command\s*=.*" + re.escape(MANAGED_SCRIPT),
                                re.M)


def default_script() -> str:
    return str(Path(__file__).resolve().parent / "autosave_session.py")


def render_block(script: str) -> str:
    hooks = "\n\n".join(
        "\n".join((
            "[[hooks]]",
            f'event = "{event}"',
            f'command = "python3 {script}"',
            "timeout = 10",
        ))
        for event in MANAGED_EVENTS
    )
    return f"{BEGIN}\n{hooks}\n{END}\n"


def read_config(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def is_installed(text: str) -> bool:
    """True when a managed *marker comment* is present in the config."""
    return any(marker in text for marker in MANAGED_MARKERS)


def marked_region(text: str) -> tuple[int, int] | None:
    """Line span of the BEGIN..END region as ``(first, last + 1)`` indices."""
    lines = text.splitlines(keepends=True)
    if not is_installed(text):
        return None
    try:
        first = next(i for i, line in enumerate(lines) if BEGIN in line)
        last = next(i for i in range(first, len(lines)) if END in lines[i])
    except StopIteration:  # END before BEGIN: not a region
        return None
    return first, last + 1


def legacy_section_spans(text: str) -> list[tuple[int, int]]:
    """Line spans of top-level ``[[hooks]]`` sections running the managed
    script that are *not* inside a marked region.

    A section starts at a line matching ``^\\s*\\[\\[hooks\\]\\]`` and ends
    immediately before the next table header (or EOF); trailing blank lines
    are left out of the span so the separators — and any comment that belongs
    to the next header — survive the removal.
    """
    lines = text.splitlines(keepends=True)
    region = marked_region(text)
    spans: list[tuple[int, int]] = []
    index = 0
    while index < len(lines):
        if not HOOK_HEADER_RE.match(lines[index]):
            index += 1
            continue
        end = index + 1
        while end < len(lines) and not lines[end].startswith("["):
            end += 1
        in_region = region is not None and index < region[1] and end > region[0]
        if not in_region and MANAGED_COMMAND_RE.search("".join(lines[index:end])):
            stop = end
            while stop > index + 1 and not lines[stop - 1].strip():
                stop -= 1
            spans.append((index, stop))
        index = end
    return spans


def strip_legacy_sections(text: str) -> str:
    """Drop every marker-less section that runs the managed script."""
    lines = text.splitlines(keepends=True)
    dropped = {i for start, stop in legacy_section_spans(text)
               for i in range(start, stop)}
    if not dropped:
        return text
    return "".join(line for i, line in enumerate(lines) if i not in dropped)


def backup(path: Path) -> Path | None:
    if not path.exists():
        return None
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    target = path.with_name(f"{path.name}.{stamp}.bak")
    shutil.copy2(path, target)
    return target


def verify_toml(text: str) -> bool:
    try:
        import tomllib
    except ImportError:  # Python 3.10: no stdlib TOML reader; append anyway
        return True
    try:
        tomllib.loads(text)
    except Exception:
        return False
    return True


def install(path: Path, script: str) -> int:
    text = read_config(path)
    if is_installed(text):
        print("autosave hook already installed; refusing to double-install")
        return 1
    block = render_block(script)
    parent = path.parent
    if not parent.is_dir():
        print(f"config directory does not exist: {parent}", file=sys.stderr)
        return 1
    backup(path)
    new_text = strip_legacy_sections(text)
    replaced = new_text != text
    if new_text and not new_text.endswith("\n"):
        new_text += "\n"
    new_text += "\n" + block
    if not verify_toml(new_text):
        print("resulting config would not parse as TOML; nothing was changed",
              file=sys.stderr)
        return 1
    path.write_text(new_text, encoding="utf-8")
    if replaced:
        print("replaced the legacy marker-less autosave hook")
    print(f"installed autosave hook in {path}")
    print("hook command: python3 " + script)
    return 0


def uninstall(path: Path) -> int:
    text = read_config(path)
    new_text = text
    if BEGIN in text and END in text:
        pattern = re.compile(re.escape(BEGIN) + r".*?" + re.escape(END) + r"\n?",
                             re.S)
        new_text = pattern.sub("", text)
    new_text = strip_legacy_sections(new_text)
    if new_text == text:
        print("no cmpath-autosave managed block found")
        return 1
    backup(path)
    path.write_text(new_text, encoding="utf-8")
    print(f"removed autosave hook from {path}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", default=os.path.expanduser(DEFAULT_CONFIG),
                        help="config.toml path (default: %(default)s)")
    parser.add_argument("--script", default=default_script(),
                        help="hook script path recorded in the block")
    parser.add_argument("--print", dest="show", action="store_true",
                        help="print only the managed TOML block; change nothing")
    parser.add_argument("--uninstall", action="store_true",
                        help="remove the managed block (marked region or "
                             "legacy marker-less section)")
    args = parser.parse_args(argv)

    if args.show:
        sys.stdout.write(render_block(args.script))
        return 0
    path = Path(args.config).expanduser()
    if args.uninstall:
        return uninstall(path)
    return install(path, args.script)


if __name__ == "__main__":
    sys.exit(main())
