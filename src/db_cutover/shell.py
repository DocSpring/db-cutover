"""Subprocesses: aws, kubectl, pg_dump, psql. Secrets go through the environment, never the command line."""

from __future__ import annotations

import datetime
import json
import os
import subprocess
from typing import Any

from db_cutover.config import Config


class CommandFailed(Exception):
    pass


def run(cmd: list[str], *, env: dict[str, str] | None = None, timeout: float = 120, check: bool = True) -> str:
    result = subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, env={**os.environ, **(env or {})}, check=False
    )
    if check and result.returncode != 0:
        # SQL tools can echo statements containing credentials; never relay their stderr to an agent.
        detail = "DBInstanceNotFound" if "DBInstanceNotFound" in result.stderr else "output withheld"
        raise CommandFailed(f"{os.path.basename(cmd[0])} exited {result.returncode}: {detail}; inspect privately")
    return result.stdout


class Aws:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._instances: dict[str, dict[str, Any]] = {}

    def call(self, *args: str, timeout: float = 60) -> Any:
        cmd = ["aws", *args, "--region", self.cfg.region, "--output", "json"]
        if self.cfg.aws_profile:
            cmd += ["--profile", self.cfg.aws_profile]
        out = run(cmd, timeout=timeout)
        return json.loads(out) if out.strip() else None

    def instance(self, identifier: str | None, refresh: bool = False) -> dict[str, Any] | None:
        """describe-db-instances for one identifier; None if it doesn't exist."""
        if not identifier:
            return None
        if refresh or identifier not in self._instances:
            try:
                data = self.call("rds", "describe-db-instances", "--db-instance-identifier", identifier)
            except CommandFailed as e:
                if "DBInstanceNotFound" in str(e):
                    return None
                raise
            self._instances[identifier] = data["DBInstances"][0]
        return self._instances[identifier]

    def endpoint(self, identifier: str | None) -> str | None:
        return ((self.instance(identifier) or {}).get("Endpoint") or {}).get("Address")

    def metric_latest(self, identifier: str, metric: str) -> float | None:
        end = datetime.datetime.now(datetime.UTC)
        start = end - datetime.timedelta(minutes=30)
        try:
            data = self.call(
                "cloudwatch",
                "get-metric-statistics",
                "--namespace",
                "AWS/RDS",
                "--metric-name",
                metric,
                "--dimensions",
                f"Name=DBInstanceIdentifier,Value={identifier}",
                "--start-time",
                start.isoformat(),
                "--end-time",
                end.isoformat(),
                "--period",
                "300",
                "--statistics",
                "Minimum",
            )
        except CommandFailed:
            return None
        points = sorted(data.get("Datapoints", []), key=lambda p: p["Timestamp"])
        return points[-1]["Minimum"] if points else None


def kubectl(context: str | None, *args: str, timeout: float = 60, check: bool = True) -> str:
    cmd = ["kubectl", *(["--context", context] if context else []), *args]
    return run(cmd, timeout=timeout, check=check)
