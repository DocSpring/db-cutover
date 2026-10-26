"""Output for humans and agents: timestamped log lines, check lists, and JSON."""

from __future__ import annotations

import datetime
import json
import sys
from dataclasses import asdict, dataclass
from typing import Any, NoReturn

import psycopg


def error_detail(error: Exception) -> str:
    """Server diagnostics can contain SQL, connection strings or row values. Keep those out of agent output."""
    if isinstance(error, psycopg.Error):
        return f"{type(error).__name__} (SQLSTATE {error.sqlstate or 'unavailable'}; inspect server logs privately)"
    return str(error)


def now() -> str:
    return datetime.datetime.now(datetime.UTC).strftime("%H:%M:%S.%f")[:-3] + "Z"


def log(message: str) -> None:
    print(f"[{now()}] {message}", flush=True)


class StepFailed(Exception):
    """A step refused to continue. The message says why and what to do."""


def fail(message: str) -> NoReturn:
    raise StepFailed(message)


def human_bytes(n: float | None) -> str:
    if n is None:
        return "-"
    value = float(n)
    for unit in ("B", "kB", "MB", "GB", "TB"):
        if abs(value) < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return str(value)


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    blocking: bool = True

    @property
    def label(self) -> str:
        if self.ok:
            return "ok  "
        return "FAIL" if self.blocking else "warn"


def print_checks(checks: list[Check], as_json: bool = False) -> bool:
    """Prints the checks; returns True when no blocking check failed."""
    passed = all(c.ok or not c.blocking for c in checks)
    if as_json:
        print(json.dumps({"passed": passed, "checks": [asdict(c) for c in checks]}, indent=2, default=str))
    else:
        for c in checks:
            print(f"  [{c.label}] {c.name}" + (f": {c.detail}" if c.detail else ""), flush=True)
        failures = sum(not c.ok and c.blocking for c in checks)
        print(f"\n{'PASSED' if passed else 'FAILED'}: {failures} blocking failure{'s' if failures != 1 else ''}")
    return passed


def print_json(data: Any) -> None:
    json.dump(data, sys.stdout, indent=2, default=str)
    print()
