"""Safe read-modify-write helpers for ``config.json``.

Background (2026-09-14 incident): the real ``config.json`` was reduced to two
keys because ~35 call sites persisted config with the pattern::

    cfg = {}
    if config_file.exists():
        try:
            cfg = json.loads(<file contents>)
        except Exception:
            pass
    cfg[key] = value
    <overwrite file with json.dumps(cfg)>

If the read fails for any reason (torn read during a concurrent
``write_text`` -- which truncates before writing -- or a malformed file) the
writer silently overwrites the whole file with a single key.

This module is the single sanctioned way to persist config:

* ``read_config_file`` never swallows a malformed file -- it raises
  ``ConfigReadError``; ``{}`` is returned only when the file truly does not
  exist.
* ``update_config_file`` serialises writers through a lock, refuses to write
  when the read failed, refuses suspicious shrinks, and writes atomically
  (tmp file + fsync + ``os.replace``) so a concurrent reader can never observe
  a truncated file.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Iterable, Mapping

__all__ = [
    "ConfigReadError",
    "ConfigWriteRefused",
    "read_config_file",
    "update_config_file",
    "SHRINK_GUARD_MIN_KEYS",
]

# Shrink guard: only enforced when the existing file already has at least this
# many keys (a tiny config legitimately grows/shrinks by a large ratio).
SHRINK_GUARD_MIN_KEYS = 8

# Module-level default lock used when a caller does not supply one.
_default_lock = threading.Lock()

# Resolved paths that must never be written (populated by tests/conftest.py
# with the real project config.json, so a test that forgets config_file=
# fails with a traceback at the write instead of silently clobbering it).
_WRITE_FORBIDDEN: set[str] = set()


class ConfigReadError(Exception):
    """config.json exists but could not be parsed as a JSON object."""


class ConfigWriteRefused(Exception):
    """update_config_file refused to write (unreadable file or shrink guard)."""


def read_config_file(config_file: Path | str) -> dict:
    """Return the parsed contents of ``config_file``.

    Returns ``{}`` only when the file does not exist. Raises
    ``ConfigReadError`` when the file exists but is not valid JSON, is not a
    JSON object, or cannot be read.
    """
    path = Path(config_file)
    if not path.exists():
        return {}
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as e:
        raise ConfigReadError(f"could not read {path}: {e}") from e
    try:
        data = json.loads(raw)
    except ValueError as e:
        raise ConfigReadError(f"{path} is not valid JSON: {e}") from e
    if not isinstance(data, dict):
        raise ConfigReadError(
            f"{path} must contain a JSON object, got {type(data).__name__}"
        )
    return data


def _atomic_write_json(path: Path, data: dict) -> None:
    """Write ``data`` to ``path`` atomically (tmp file in the same directory,
    fsync, ``os.replace``). A crash mid-write leaves the old file intact."""
    tmp_path = path.with_suffix(".json.tmp")
    payload = json.dumps(data, indent=2) + "\n"
    try:
        with open(tmp_path, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    finally:
        # os.replace consumed the tmp file on success; on failure remove it so
        # we never leave a stray .json.tmp behind.
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass


def update_config_file(
    config_file: Path | str,
    updates: Mapping | None,
    *,
    lock: threading.Lock | None = None,
    remove: Iterable[str] = (),
) -> dict:
    """Atomically merge ``updates`` into ``config_file`` (and drop ``remove``
    keys), returning the resulting dict.

    Guards:
    * Refuses (``ConfigWriteRefused``) when the existing file cannot be read
      as a JSON object -- never overwrite a file we could not parse.
    * Refuses when the file already has >= ``SHRINK_GUARD_MIN_KEYS`` keys and
      the result would have fewer than half of them (the exact failure mode
      from the 2026-09-14 incident).

    ``lock`` is a ``threading.Lock`` held for the whole read-modify-write; a
    module-level default lock is used when ``None``.
    """
    path = Path(config_file)
    updates = dict(updates or {})
    remove = list(remove)
    if lock is None:
        lock = _default_lock

    if _WRITE_FORBIDDEN and os.path.realpath(path) in _WRITE_FORBIDDEN:
        raise ConfigWriteRefused(
            f"write to {path} is forbidden in this process (test isolation); "
            f"keys requested={sorted(updates)} remove={remove}"
        )

    with lock:
        try:
            existing = read_config_file(path)
        except ConfigReadError as e:
            print(f"[Config] REFUSED write to {path}: existing file unreadable "
                  f"({e}); keys requested={sorted(updates)} remove={remove}")
            raise ConfigWriteRefused(str(e)) from e

        new_cfg = dict(existing)
        new_cfg.update(updates)
        for key in remove:
            new_cfg.pop(key, None)

        existing_count = len(existing)
        new_count = len(new_cfg)
        if existing_count >= SHRINK_GUARD_MIN_KEYS and new_count < existing_count / 2:
            msg = (f"shrink guard: existing file has {existing_count} keys, "
                   f"write would leave {new_count}")
            print(f"[Config] REFUSED write to {path}: {msg}; "
                  f"keys requested={sorted(updates)} remove={remove}")
            raise ConfigWriteRefused(msg)

        _atomic_write_json(path, new_cfg)
        return new_cfg
