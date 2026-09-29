"""Audit-hook recorder used by the adversarial corpus-gate test.

This is a pytest plugin, not a test module (its filename does not match
``python_files``), so pytest never collects it. The adversarial test in
``tests/unit/test_cli_layering_adversarial.py`` loads it with ``-p`` and points
it at the real-corpus tree through two environment variables:

* ``RR_AUDIT_TARGET`` -- the absolute directory to watch (the ``resume/`` tree).
* ``RR_AUDIT_OUT`` -- a JSON file the recorded events are written to at session
  end.

It installs a ``sys.addaudithook`` so every ``open`` (and ``os.scandir`` /
``os.listdir``) whose resolved path falls under the target is recorded. This is
an *observation* of real file access, not a reading of the corpus conftest: the
default suite must open nothing under ``resume/``.
"""

from __future__ import annotations

import json
import os
import sys

_RECORDS: list[dict[str, str]] = []
_TARGET: str | None = None
_WATCHED = ("open", "os.scandir", "os.listdir")


def _under_target(path: object) -> str | None:
    try:
        resolved = os.path.abspath(os.fspath(path))  # type: ignore[arg-type]
    except (TypeError, ValueError, OSError):
        return None
    if not _TARGET:
        return None
    if resolved == _TARGET or resolved.startswith(_TARGET + os.sep):
        return resolved
    return None


def _install() -> None:
    def _hook(event: str, args: tuple) -> None:
        if event not in _WATCHED or not args:
            return
        resolved = _under_target(args[0])
        if resolved is not None:
            _RECORDS.append({"event": event, "path": resolved})

    sys.addaudithook(_hook)


def pytest_configure(config: object) -> None:
    global _TARGET
    _TARGET = os.environ.get("RR_AUDIT_TARGET") or None
    _install()


def pytest_sessionfinish(session: object, exitstatus: int) -> None:
    out = os.environ.get("RR_AUDIT_OUT")
    if out:
        with open(out, "w", encoding="utf-8") as handle:
            json.dump(_RECORDS, handle)
