"""Loads a cutover config (TOML). One file per database you migrate; see examples/config.toml for every key.

Strings may contain {name} placeholders filled from the [vars] table. A var is a literal, or read from a JSON file:
    [vars]
    rack = { json = "../../racks/staging/vars.json", key = "name" }

Paths are relative to the config file.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Side:
    """BLUE (the current primary) or GREEN (the new instance)."""

    identifier: str | None  # RDS instance identifier; None when GREEN doesn't exist yet
    host: str | None  # how this machine reaches it; None = the RDS endpoint


@dataclass(frozen=True)
class PgBouncerConfig:
    kube_context: str
    namespace: str = "pgbouncer"
    selector: str = "app=pgbouncer"
    container: str = "pgbouncer"
    config_path: str = "/etc/pgbouncer/pgbouncer.ini"
    admin_user: str = "pgbouncer_admin"
    port: int = 5432
    host: str | None = None  # PgBouncer as the app reaches it, for `probe`


@dataclass(frozen=True)
class DirectWriters:
    """Sessions that bypass PgBouncer. The switch can't pause them, so they must be stopped first."""

    kube_context: str | None = None
    deployments: tuple[str, ...] = ()  # "namespace/name": must have 0 replicas for the switch
    application_names: tuple[str, ...] = ()  # substrings of application_name that identify them on BLUE


@dataclass(frozen=True)
class TerraformLayout:
    """Where green-tf and promote-tf read and write. Only those two commands need it.

    BLUE's file holds the aws_db_instance `instance`, the aws_db_parameter_group `parameter_group` and, optionally,
    the module `app_user_module` that creates the app's role and grants with the postgresql provider. GREEN gets
    the same names with a `_green` suffix in green_file; after the switch, promote-tf gives them BLUE's names and
    renames BLUE's to `_old` in old_file."""

    dir: Path
    blue_file: str = "rds.tf"
    green_file: str = "rds_green.tf"
    old_file: str = "rds_old.tf"
    instance: str = "rds"
    parameter_group: str = "rds"
    app_user_module: str | None = None
    host_attribute: str = "host"  # the app user module's argument holding the host it connects through
    app_user_toggle: str | None = None  # a bool argument that keeps GREEN's module off until its database exists
    green_identifier: str = "{timestamp}"  # {timestamp} is filled in when green-tf runs
    state_command: str = "terraform state"  # how promote-tf's printed commands run `terraform state`


@dataclass(frozen=True)
class Config:
    path: Path
    name: str
    database: str
    admin_user: str
    app_user: str
    region: str
    aws_profile: str | None
    blue: Side
    green: Side
    secrets: dict[str, str] = field(repr=False)
    op_account: str | None
    pgbouncer: PgBouncerConfig
    direct_writers: DirectWriters
    health_urls: tuple[str, ...]
    state_dir: Path
    terraform: TerraformLayout | None
    publication: str = "db_cutover_pub"
    subscription: str = "db_cutover_sub"
    slot: str = "db_cutover_slot"
    probe_table: str = "db_cutover_probe"
    sequence_gap: int = 1_000_000
    replication_binary: bool = False
    column_types: dict[str, str] = field(default_factory=dict)  # "schema.table.column" -> type GREEN gets
    sequence_min_margin: int = 100_000
    _secret_cache: dict[str, str] = field(default_factory=dict, compare=False, repr=False)

    def secret(self, key: str) -> str:
        """Reads a secret from [secrets] once per process. Never print the result."""
        if key not in self._secret_cache:
            ref = self.secrets.get(key)
            if not ref:
                raise ConfigError(f"[secrets] {key} is not set in {self.path}")
            self._secret_cache[key] = read_secret(ref, self.op_account)
        return self._secret_cache[key]

    def health_check_headers(self) -> dict[str, str]:
        """The token `watch` sends to [monitor] health_urls, when [secrets] health_check_token is set. An app can answer
        its health checks that touch the database only to requests that carry one."""
        if not self.secrets.get("health_check_token"):
            return {}
        return {"X-Health-Check-Token": self.secret("health_check_token")}


def read_secret(ref: str, op_account: str | None = None) -> str:
    if ref.startswith("op://"):
        cmd = ["op", "read", "--no-newline", ref]
        if op_account:
            cmd += ["--account", op_account]
        result = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=30)
        if result.returncode != 0:
            raise ConfigError("1Password secret lookup failed; inspect it privately (output withheld)")
        return result.stdout
    if ref.startswith("env:"):
        name = ref[4:]
        if name not in os.environ:
            raise ConfigError(f"environment variable {name} is not set")
        return os.environ[name]
    if ref.startswith("cmd:"):
        result = subprocess.run(ref[4:], shell=True, capture_output=True, text=True, check=False, timeout=30)
        if result.returncode != 0:
            raise ConfigError("secret command failed; inspect it privately (output withheld)")
        return result.stdout.rstrip("\n")
    raise ConfigError("secret references start with op://, env: or cmd:; literal values are not accepted")


def terraform_identifier(path: Path, address: str) -> str | None:
    """`identifier = "..."` of the aws_db_instance at `address` (e.g. aws_db_instance.rds) in a Terraform file.
    None when the file or the resource doesn't exist (GREEN before green-tf)."""
    kind, _, name = address.partition(".")
    if not path.exists():
        return None
    text = path.read_text()
    match = re.search(rf'^resource\s+"{re.escape(kind)}"\s+"{re.escape(name)}"\s*\{{(.*?)^\}}', text, re.S | re.M)
    if not match:
        return None
    ident = re.search(r'^\s*identifier\s*=\s*"([^"]+)"', match.group(1), re.M)
    return ident.group(1) if ident else None


def _vars(raw: dict[str, Any], base: Path) -> dict[str, str]:
    out = {}
    for key, value in raw.items():
        if isinstance(value, dict) and "json" in value:
            data = json.loads((base / value["json"]).read_text())
            for part in str(value["key"]).split("."):
                data = data[part]
            out[key] = str(data)
        else:
            out[key] = str(value)
    return out


RUNTIME_PLACEHOLDERS = ("timestamp",)  # left in place at load time, filled in by the command that uses them


class _Vars(dict):
    def __missing__(self, key: str) -> str:
        if key in RUNTIME_PLACEHOLDERS:
            return "{" + key + "}"
        raise KeyError(key)


def _expand(value: Any, variables: dict[str, str]) -> Any:
    if isinstance(value, str):
        try:
            return value.format_map(_Vars(variables))
        except KeyError as e:
            raise ConfigError(f"unknown placeholder {e}: add it to [vars]") from None
    if isinstance(value, list):
        return [_expand(v, variables) for v in value]
    if isinstance(value, dict):
        return {k: _expand(v, variables) for k, v in value.items()}
    return value


def _build(cls: type, table: dict[str, Any], where: str, **extra: Any) -> Any:
    """cls(**table), with an error naming the table for keys it doesn't know."""
    known = {f.name for f in fields(cls)}
    unknown = sorted(set(table) - known)
    if unknown:
        raise ConfigError(f"unknown key(s) in [{where}]: {', '.join(unknown)} (known: {', '.join(sorted(known))})")
    return cls(**{k: (tuple(v) if isinstance(v, list) else v) for k, v in table.items()}, **extra)


def _side(raw: dict[str, Any], base: Path) -> Side:
    ident = raw.get("identifier")
    if isinstance(ident, dict):
        ident = terraform_identifier(base / ident["terraform"], ident["resource"])
    return Side(identifier=ident, host=raw.get("host"))


def load(path: str | Path) -> Config:
    path = Path(path).resolve()
    if not path.exists():
        raise ConfigError(f"no config at {path}")
    base = path.parent
    with path.open("rb") as f:
        raw = tomllib.load(f)
    variables = _vars(raw.pop("vars", {}), base)
    raw = _expand(raw, variables)

    def need(table: dict[str, Any], key: str, where: str) -> Any:
        if key not in table:
            raise ConfigError(f"{where}{key} is required in {path}")
        return table[key]

    aws = raw.get("aws", {})
    pgb = need(raw, "pgbouncer", "")
    writers = raw.get("direct_writers", {})
    replication = raw.get("replication", {})
    sequences = raw.get("sequences", {})
    column_types = {str(k): str(v) for k, v in raw.get("schema", {}).get("column_types", {}).items()}
    for key in column_types:
        if len(key.split(".")) != 3:
            raise ConfigError(f"[schema] column_types keys are schema.table.column (got {key!r})")
    binary = replication.get("binary", False)
    if not isinstance(binary, bool):
        raise ConfigError("[replication] binary must be a TOML boolean")
    if column_types and binary:
        raise ConfigError(
            "[schema] column_types needs [replication] binary = false: binary replication can't change a column's type"
        )
    tf = raw.get("terraform")
    secrets = dict(raw.get("secrets", {}))
    op_account = secrets.pop("op_account", None)
    gap, margin = sequences.get("gap", 1_000_000), sequences.get("min_margin", 100_000)
    if type(gap) is not int or type(margin) is not int or not 0 < margin <= gap:
        raise ConfigError("[sequences] gap and min_margin must be integers with 0 < min_margin <= gap")
    blue, green = _side(need(raw, "blue", ""), base), _side(raw.get("green", {}), base)
    if (blue.identifier and blue.identifier == green.identifier) or (blue.host and blue.host == green.host):
        raise ConfigError("BLUE and GREEN must be different instances and hosts")
    return Config(
        path=path,
        name=need(raw, "name", ""),
        database=need(raw, "database", ""),
        admin_user=need(raw, "admin_user", ""),
        app_user=need(raw, "app_user", ""),
        region=need(aws, "region", "[aws] "),
        aws_profile=aws.get("profile"),
        blue=blue,
        green=green,
        secrets=secrets,
        op_account=op_account,
        pgbouncer=_build(PgBouncerConfig, pgb, "pgbouncer"),
        direct_writers=DirectWriters(
            kube_context=writers.get("kube_context", pgb.get("kube_context")),
            deployments=tuple(writers.get("deployments", ())),
            application_names=tuple(writers.get("application_names", ())),
        ),
        health_urls=tuple(raw.get("monitor", {}).get("health_urls", ())),
        state_dir=(base / raw.get("state_dir", f".db_cutover/{raw.get('name', 'default')}")).resolve(),
        terraform=_build(
            TerraformLayout,
            {k: v for k, v in tf.items() if k != "dir"},
            "terraform",
            dir=(base / tf.get("dir", ".")).resolve(),
        )
        if tf
        else None,
        publication=replication.get("publication", "db_cutover_pub"),
        subscription=replication.get("subscription", "db_cutover_sub"),
        slot=replication.get("slot", "db_cutover_slot"),
        probe_table=replication.get("probe_table", "db_cutover_probe"),
        sequence_gap=gap,
        replication_binary=binary,
        column_types=column_types,
        sequence_min_margin=margin,
    )
