#!/usr/bin/env python3
"""
Comment-preserving config.ini edits.

configparser.write() throws away every comment and blank line, and config.ini
relies on its comments to document the audio / stream / MQTT options. This
edits the text line by line instead: only the values that change are touched,
everything else (comments, ordering, spacing) is kept as written.

Keys are matched case-insensitively, the way configparser reads them.
"""

import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import AbstractSet, Iterable

NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")
_SECTION_RE = re.compile(r"^\s*\[([^\]]+)\]\s*$")
_KEY_RE = re.compile(r"^([^#;\s][^=:]*?)\s*[=:]")


class IniEditError(ValueError):
    pass


def clean_value(v) -> str:
    """One line, no control characters — a newline in a value could inject new keys/sections."""
    s = "" if v is None else str(v)
    return re.sub(r"[\x00-\x1f\x7f]+", " ", s).strip()


def validate_name(kind: str, name: str) -> str:
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise IniEditError(f"invalid {kind} name: {name!r}")
    return name


def _blocks(lines: list[str]) -> list[tuple[str | None, int, int]]:
    """(section_name_lower, start, end) spans; the first span (None) is text before any section."""
    spans, name, start = [], None, 0
    for i, line in enumerate(lines):
        m = _SECTION_RE.match(line)
        if m:
            spans.append((name, start, i))
            name, start = m.group(1).strip().lower(), i
    spans.append((name, start, len(lines)))
    return spans


def _key_spans(lines: list[str], start: int, end: int) -> list[tuple[str, int, int]]:
    """(key_lower, first_line, end_line) for each option in a section, including continuation lines."""
    out, i = [], start + 1
    while i < end:
        m = _KEY_RE.match(lines[i])
        if m:
            j = i + 1
            while j < end and lines[j][:1] in (" ", "\t") and lines[j].strip() \
                    and not lines[j].lstrip().startswith(("#", ";")):
                j += 1
            out.append((m.group(1).strip().lower(), i, j))
            i = j
        else:
            i += 1
    return out


def update_ini_text(text: str, changes: dict, replace_sections: Iterable[str] = (),
                    protected: AbstractSet[tuple[str, str]] = frozenset()) -> str:
    """
    changes: {section: {key: value}}. Clearing a value removes the key (so defaults
    apply); keys that are already empty placeholders are left as written.
    replace_sections: sections whose existing keys are all dropped first (e.g. location_keys).
    protected: {(section, key)} pairs that are never written or removed.
    """
    replace = {s.lower() for s in replace_sections}
    nl = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines()

    for raw_section, keys in changes.items():
        section = validate_name("section", raw_section).lower()
        if not isinstance(keys, dict):
            raise IniEditError(f"section {raw_section!r} must map keys to values")
        wanted = {}
        for k, v in keys.items():
            key = validate_name("key", k).lower()
            if (section, key) in protected:
                continue
            wanted[key] = clean_value(v)

        span = next(((s, e) for name, s, e in _blocks(lines) if name == section), None)
        if span is None:
            new = [f"{k} = {v}" for k, v in wanted.items() if v]
            if new:
                while lines and not lines[-1].strip():
                    lines.pop()
                lines += ["", f"[{raw_section}]"] + new
            continue

        start, end = span
        existing = _key_spans(lines, start, end)
        drop = set()
        if section in replace:
            drop = {i for key, a, b in existing if (section, key) not in protected for i in range(a, b)}
        edits: dict[int, str | None] = {}
        for key, a, b in existing:
            if key in wanted and section not in replace:
                v = wanted.pop(key)
                if v:
                    m = re.match(r"^(\s*[^=:]*?\s*[=:]\s?)", lines[a])
                    edits[a] = (m.group(1) if m else f"{key} = ") + v
                    drop.update(range(a + 1, b))
                elif re.split(r"[=:]", lines[a], maxsplit=1)[-1].strip() or b - a > 1:
                    # Cleared a real value: remove the key so the code's default applies
                    drop.update(range(a, b))
                # else: already an empty placeholder like "host =" — leave it as documentation
        # New keys go after the section's last surviving option (or its header),
        # so trailing comments that introduce the next section stay where they are
        kept = [b for _, a, b in existing if a not in drop]
        if kept:
            last = max(kept)
        elif existing:
            last = existing[0][1]        # where the old keys were (keeps comments above them)
        else:                            # key-less section: after its last non-blank line
            last = start + 1
            for i in range(start + 1, end):
                if lines[i].strip():
                    last = i + 1
        insert = [f"{k} = {v}" for k, v in wanted.items() if v]

        out = []
        for i, line in enumerate(lines):
            if i == last and insert:
                out += insert
                insert = []
            if i in drop:
                continue
            out.append(edits.get(i, line))
        if insert:
            out += insert
        lines = out

    return nl.join(lines) + nl


def save_ini(path: Path, changes: dict, replace_sections: Iterable[str] = (),
             protected: AbstractSet[tuple[str, str]] = frozenset()) -> None:
    """Apply changes to the file atomically, keeping its permissions."""
    path = Path(path)
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    new = update_ini_text(text, changes, replace_sections, protected)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".config-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(new)
        if path.exists():
            shutil.copymode(path, tmp)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
