import subprocess
from types import SimpleNamespace

import psycopg
import pytest
from test_switch import FakeBouncer

from db_cutover import config, db, shell
from db_cutover.cli import parser
from db_cutover.commands import inspect, replication
from db_cutover.output import StepFailed, error_detail


@pytest.mark.parametrize("command", ["probe", "wait-sync"])
def test_write_helpers_do_nothing_without_execute(command):
    args = parser().parse_args(["unused.toml", command])
    # Any attempted connection would fail: these dry runs must return before touching the context.
    ctx = SimpleNamespace(cfg=SimpleNamespace())
    args.fn(ctx, args)


def test_resume_dry_run_never_connects(monkeypatch):
    def unexpected(*args):
        pytest.fail("dry run opened a PgBouncer connection")

    monkeypatch.setattr(inspect, "PgBouncer", unexpected)
    args = parser().parse_args(["unused.toml", "pgbouncer", "resume"])
    inspect.cmd_pgbouncer(SimpleNamespace(), args)


@pytest.mark.parametrize("unknown", [False, True])
def test_manual_resume_refuses_mixed_or_unknown_hosts(monkeypatch, unknown):
    bouncer = FakeBouncer()
    bouncer.pods[1].host = None if unknown else "green.example"
    monkeypatch.setattr(inspect, "PgBouncer", lambda cfg: bouncer)
    args = parser().parse_args(["unused.toml", "pgbouncer", "resume", "--execute"])
    with pytest.raises(StepFailed, match="mixed or unknown"):
        inspect.cmd_pgbouncer(SimpleNamespace(cfg=None), args)
    assert not bouncer.resumed


def test_missing_deployment_is_not_treated_as_zero_replicas(monkeypatch):
    monkeypatch.setattr(inspect, "kubectl", lambda *args, **kwargs: "")
    ctx = SimpleNamespace(
        cfg=SimpleNamespace(direct_writers=SimpleNamespace(deployments=["app/jobs"], kube_context="test"))
    )
    with pytest.raises(StepFailed, match="determine replicas"):
        inspect.direct_writer_replicas(ctx)


def test_secret_lookup_failure_does_not_echo_command_output(monkeypatch):
    canary = "synthetic-secret-that-must-not-appear"
    monkeypatch.setattr(
        config.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=1, stderr=canary, stdout=canary)
    )
    for ref in ["cmd:example", "op://example/item/password", canary]:
        with pytest.raises(config.ConfigError) as caught:
            config.read_secret(ref)
        assert canary not in str(caught.value)


def test_subprocess_failure_withholds_sql_and_credentials(monkeypatch):
    canary = "synthetic-private-row-or-password"
    monkeypatch.setattr(
        shell.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess([], 1, "", f"ERROR: {canary}"),
    )
    with pytest.raises(shell.CommandFailed) as caught:
        shell.run(["psql", "--command", canary])
    assert canary not in str(caught.value)


def test_postgres_diagnostics_do_not_echo_row_values():
    assert "private-value" not in error_detail(psycopg.errors.UniqueViolation("DETAIL: private-value"))


def test_sequence_limit_is_checked_at_the_point_of_write():
    with pytest.raises(StepFailed, match="maximum"):
        db.apply_sequences(None, [{"crosses_int4": False, "exceeds_max": True}])


def test_marker_matches_unique_token_not_just_sequence_id(monkeypatch):
    captured = []
    monkeypatch.setattr(db, "q1", lambda conn, query, params: captured.append(params) or {"id": 7})

    def read(conn, query, params):
        assert "note = %s" in query.as_string()
        assert params == (7, captured[0][0])
        return [{"present": 1}]

    monkeypatch.setattr(db, "q", read)
    db.wait_for_marker(SimpleNamespace(probe_table="probe"), None, None, timeout=1)


def test_verify_windows_retry_is_read_only(monkeypatch):
    monkeypatch.setattr(replication, "window_bounds", lambda *args: [(1, 10)])
    replies = iter([{"n": 1, "h": "a"}, {"n": 1, "h": "b"}, {"n": 1, "h": "a"}, {"n": 1, "h": "a"}])
    monkeypatch.setattr(replication, "q1", lambda *args: next(replies))
    monkeypatch.setattr(replication.time, "sleep", lambda *args: None)
    monkeypatch.setattr(db, "wait_for_marker", lambda *args, **kwargs: pytest.fail("read-only verify wrote a marker"))
    assert replication.verify_windows(None, None, None, "public.items", "id", 10).ok


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf"])
def test_pause_limits_must_be_positive_and_finite(value):
    with pytest.raises(SystemExit) as caught:
        parser().parse_args(["unused.toml", "cutover", "--max-paused", value])
    assert caught.value.code == 2
