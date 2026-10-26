import json
from pathlib import Path

import pytest

from db_cutover import config

CONFIG = """
name = "staging"
database = "app"
admin_user = "admin"
app_user = "app_user"

[vars]
rack = { json = "vars.json", key = "rack.name" }
env = "staging"

[aws]
region = "us-east-1"

[blue]
identifier = { terraform = "rds.tf", resource = "aws_db_instance.rds" }
host = "rds-{env}.example.test"

[green]
identifier = { terraform = "rds_green.tf", resource = "aws_db_instance.rds_green" }

[secrets]
op_account = "example.1password.com"
admin_password = "env:ADMIN_PASSWORD"

[pgbouncer]
kube_context = "ctx-{rack}"
host = "pgbouncer-{env}.example.test"

[direct_writers]
deployments = ["{rack}-app/worker"]
application_names = ["good_job"]

[terraform]
dir = "."
blue_file = "rds.tf"
instance = "rds"
green_identifier = "db-{env}-{timestamp}"
"""

RDS_TF = """
resource "aws_db_instance" "rds" {
  identifier     = "db-staging-1"
  engine_version = "17.6"
}
"""


@pytest.fixture
def cfg_path(tmp_path: Path) -> Path:
    (tmp_path / "vars.json").write_text(json.dumps({"rack": {"name": "staging-3-17"}}))
    (tmp_path / "rds.tf").write_text(RDS_TF)
    path = tmp_path / "cutover.toml"
    path.write_text(CONFIG)
    return path


def test_load(cfg_path: Path) -> None:
    cfg = config.load(cfg_path)
    assert cfg.blue.identifier == "db-staging-1"
    assert cfg.blue.host == "rds-staging.example.test"
    assert cfg.green.identifier is None  # no GREEN file yet
    assert cfg.pgbouncer.kube_context == "ctx-staging-3-17"
    assert cfg.direct_writers.deployments == ("staging-3-17-app/worker",)
    assert cfg.direct_writers.kube_context == "ctx-staging-3-17"  # defaults to PgBouncer's
    assert cfg.op_account == "example.1password.com"
    assert "op_account" not in cfg.secrets
    assert cfg.state_dir == cfg_path.parent / ".db_cutover" / "staging"
    assert cfg.terraform is not None
    assert cfg.terraform.green_identifier == "db-staging-{timestamp}"  # filled in by green-tf
    assert cfg.sequence_gap == 1_000_000


def test_green_identifier_from_terraform(cfg_path: Path) -> None:
    (cfg_path.parent / "rds_green.tf").write_text(
        'resource "aws_db_instance" "rds_green" {\n  identifier = "db-staging-2"\n}\n'
    )
    assert config.load(cfg_path).green.identifier == "db-staging-2"


def test_unknown_placeholder(cfg_path: Path) -> None:
    cfg_path.write_text(cfg_path.read_text().replace("rds-{env}", "rds-{nope}"))
    with pytest.raises(config.ConfigError, match="nope"):
        config.load(cfg_path)


def test_unknown_key(cfg_path: Path) -> None:
    cfg_path.write_text(cfg_path.read_text().replace('host = "pgbouncer-', 'hots = "pgbouncer-'))
    with pytest.raises(config.ConfigError, match=r"unknown key\(s\) in \[pgbouncer\]: hots"):
        config.load(cfg_path)


def test_secret_from_env(cfg_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_PASSWORD", "s3cret")
    assert config.load(cfg_path).secret("admin_password") == "s3cret"


def test_health_check_headers(cfg_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert config.load(cfg_path).health_check_headers() == {}

    cfg_path.write_text(
        cfg_path.read_text().replace(
            'admin_password = "env:ADMIN_PASSWORD"',
            'admin_password = "env:ADMIN_PASSWORD"\nhealth_check_token = "env:HEALTH_CHECK_TOKEN"',
        )
    )
    monkeypatch.setenv("HEALTH_CHECK_TOKEN", "t0ken")
    assert config.load(cfg_path).health_check_headers() == {"X-Health-Check-Token": "t0ken"}


def test_missing_secret(cfg_path: Path) -> None:
    with pytest.raises(config.ConfigError, match="app_password"):
        config.load(cfg_path).secret("app_password")


def test_bad_secret_reference() -> None:
    with pytest.raises(config.ConfigError, match="op://, env: or cmd:"):
        config.read_secret("plaintext-password")


def test_column_types_need_text_replication(cfg_path: Path) -> None:
    cfg_path.write_text(
        cfg_path.read_text()
        + '\n[replication]\nbinary = true\n[schema]\ncolumn_types = { "public.audits.user_id" = "bigint" }\n'
    )
    with pytest.raises(config.ConfigError, match="binary = false"):
        config.load(cfg_path)
    cfg_path.write_text(cfg_path.read_text().replace("binary = true", "binary = false"))
    cfg = config.load(cfg_path)
    assert cfg.column_types == {"public.audits.user_id": "bigint"}
    assert cfg.replication_binary is False


def test_column_types_key_shape(cfg_path: Path) -> None:
    cfg_path.write_text(
        cfg_path.read_text()
        + '\n[replication]\nbinary = false\n[schema]\ncolumn_types = { "audits.user_id" = "bigint" }\n'
    )
    with pytest.raises(config.ConfigError, match=r"schema\.table\.column"):
        config.load(cfg_path)


def test_text_replication_is_the_default(cfg_path):
    assert config.load(cfg_path).replication_binary is False


@pytest.mark.parametrize("value", ['"false"', "1"])
def test_binary_requires_a_boolean(cfg_path, value):
    cfg_path.write_text(cfg_path.read_text() + f"\n[replication]\nbinary = {value}\n")
    with pytest.raises(config.ConfigError, match="boolean"):
        config.load(cfg_path)


@pytest.mark.parametrize("gap,margin", [(0, 1), (10, 20), (10, -1), (True, 1)])
def test_invalid_sequence_margins_are_rejected(cfg_path, gap, margin):
    cfg_path.write_text(cfg_path.read_text() + f"\n[sequences]\ngap = {str(gap).lower()}\nmin_margin = {margin}\n")
    with pytest.raises(config.ConfigError, match="min_margin"):
        config.load(cfg_path)


def test_same_source_and_target_are_rejected(cfg_path):
    cfg_path.write_text(
        cfg_path.read_text().replace(
            'identifier = { terraform = "rds_green.tf", resource = "aws_db_instance.rds_green" }',
            'identifier = "db-staging-1"',
        )
    )
    with pytest.raises(config.ConfigError, match="different instances"):
        config.load(cfg_path)


def test_reference_example_loads_without_external_files():
    example = Path(__file__).parents[1] / "examples" / "config.toml"
    cfg = config.load(example)
    assert cfg.blue.identifier == "example-blue"
    assert cfg.green.identifier == "example-green"
