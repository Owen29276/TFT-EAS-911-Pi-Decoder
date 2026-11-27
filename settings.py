#!/usr/bin/env python3
"""
One place to read config.ini (and secrets.ini) for every program in the project.

- Interpolation is off everywhere, so a '%' in a value (URL-encoded topics,
  passwords) is just a character instead of a crash.
- Only callers that need credentials ask for secrets.ini (the logger: MQTT,
  transcription and stream passwords). The dashboard never loads it.
- Writes go through ini_edit.save_ini so comments in config.ini survive.
"""

import configparser
import os
from pathlib import Path

APP_DIR      = Path(__file__).parent
CONFIG_PATH  = APP_DIR / "config.ini"
SECRETS_PATH = APP_DIR / "secrets.ini"


def read_config(path: Path | None = None, with_secrets: bool = False) -> configparser.ConfigParser:
    """Parse config.ini (plus secrets.ini if asked). Missing files are simply skipped."""
    c = configparser.ConfigParser(interpolation=None)
    files = [Path(path) if path else CONFIG_PATH]
    if with_secrets:
        files.append(SECRETS_PATH)
    c.read(files, encoding="utf-8")
    return c


def resolve_path(p: str) -> str:
    """Expand ~ and make relative paths relative to the project directory."""
    p = os.path.expanduser(p)
    return p if os.path.isabs(p) else str(APP_DIR / p)
