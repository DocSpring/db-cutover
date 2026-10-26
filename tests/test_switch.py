"""Failure injection around the live switch. These tests never connect to a database or cloud service."""

import contextlib
import json
from types import SimpleNamespace

import pytest

from db_cutover import db
from db_cutover.cli import parser
from db_cutover.commands import switch
from db_cutover.output import Check, StepFailed


class FakePod:
    def __init__(self, name):
        self.name = name
        self.host = "blue.example"
        self.commands = []
        self.error_on = None
        self.error = RuntimeError("injected failure")
        self.stays_blue = False

    def admin(self, command):
        self.commands.append(command)
        if command == self.error_on:
            raise self.error
        if command == "RELOAD" and not self.stays_blue:
            self.host = "green.example"
        return []


class FakeBouncer:
    def __init__(self):
        self.pods = [FakePod("a"), FakePod("b")]
        self.resumed = []
        self.resume_error = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def on_all(self, fn, timeout=None):
        result = {}
        for pod in self.pods:
            try:
                result[pod.name] = fn(pod)
            except Exception as e:
                result[pod.name] = e
        return result

    def running_host(self, pod):
        return pod.host

    def resume_all(self):
        self.resumed.append([p.host for p in self.pods])
        return {"a": RuntimeError("resume failed") if self.resume_error else "resumed", "b": "resumed"}


@pytest.fixture
def rig(tmp_path, monkeypatch):
    cfg = SimpleNamespace(name="test", state_dir=tmp_path, database="app", sequence_gap=1000, sequence_min_margin=100)
    ctx = SimpleNamespace(
        cfg=cfg,
        blue_id="blue",
        green_id="green",
        blue_endpoint="blue.example",
        green_endpoint="green.example",
        blue=lambda: contextlib.nullcontext("blue"),
        green=lambda: contextlib.nullcontext("green"),
    )
    bouncer = FakeBouncer()
    monkeypatch.setattr(switch, "PgBouncer", lambda cfg: bouncer)
    monkeypatch.setattr(switch, "preconditions", lambda *args: [Check("fixture ready", True)])
    monkeypatch.setattr(switch, "app_sessions", lambda *args: [])
    monkeypatch.setattr(switch, "split_direct", lambda ctx, sessions: ([], sessions))
    monkeypatch.setattr(switch.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(db, "resolve", lambda host: {host})
    monkeypatch.setattr(db, "integer_pk_tables", lambda conn: [("public.items", "id")])
    monkeypatch.setattr(db, "max_ids", lambda *args: {"public.items": 42})
    monkeypatch.setattr(db, "sequences", lambda conn: {})
    monkeypatch.setattr(db, "wait_for_marker", lambda *args, **kwargs: 0.01)
    args = parser().parse_args(["unused.toml", "cutover", "--execute"])
    return ctx, args, bouncer


def report(ctx):
    return json.loads(next(ctx.cfg.state_dir.glob("cutover-*.json")).read_text())


def test_success_resumes_only_after_all_pods_confirm_green(rig):
    ctx, args, bouncer = rig
    switch.cmd_cutover(ctx, args)
    assert bouncer.resumed == [["green.example", "green.example"]]
    assert report(ctx)["status"] == "SWITCHED"


@pytest.mark.parametrize(
    "failure", [RuntimeError("pause failed"), TimeoutError("pause timed out"), KeyboardInterrupt()]
)
def test_pause_failure_or_interrupt_resumes_blue(rig, failure):
    ctx, args, bouncer = rig
    bouncer.pods[0].error_on = "PAUSE"
    bouncer.pods[0].error = failure
    with pytest.raises((SystemExit, KeyboardInterrupt)):
        switch.cmd_cutover(ctx, args)
    assert bouncer.resumed == [["blue.example", "blue.example"]]
    assert all("RELOAD" not in p.commands for p in bouncer.pods)


def test_rehearsal_resumes_blue_without_reload(rig):
    ctx, args, bouncer = rig
    args.rehearse = True
    switch.cmd_cutover(ctx, args)
    assert bouncer.resumed == [["blue.example", "blue.example"]]
    assert report(ctx)["status"] == "REHEARSED"


def test_rehearsal_resume_failure_is_not_success(rig):
    ctx, args, bouncer = rig
    args.rehearse = True
    bouncer.resume_error = True
    with pytest.raises(StepFailed, match="could not confirm RESUME"):
        switch.cmd_cutover(ctx, args)
    assert report(ctx)["status"] == "RECOVERY_REQUIRED"


@pytest.mark.parametrize(
    "failure", [RuntimeError("reload failed"), TimeoutError("reload timed out"), KeyboardInterrupt()]
)
def test_partial_reload_never_resumes_mixed_hosts(rig, failure):
    ctx, args, bouncer = rig
    bouncer.pods[1].error_on = "RELOAD"
    bouncer.pods[1].error = failure
    with pytest.raises((StepFailed, KeyboardInterrupt)):
        switch.cmd_cutover(ctx, args)
    assert bouncer.pods[0].host == "green.example"
    assert bouncer.pods[1].host == "blue.example"
    assert bouncer.resumed == []
    assert report(ctx)["status"] == "RECOVERY_REQUIRED"


def test_reload_ack_without_host_change_does_not_resume(rig):
    ctx, args, bouncer = rig
    bouncer.pods[1].stays_blue = True
    with pytest.raises(StepFailed, match="not every pod confirmed GREEN"):
        switch.cmd_cutover(ctx, args)
    assert not bouncer.resumed


def test_resume_failure_after_reload_is_not_success(rig):
    ctx, args, bouncer = rig
    bouncer.resume_error = True
    with pytest.raises(StepFailed, match="not every pod confirmed RESUME"):
        switch.cmd_cutover(ctx, args)
    assert report(ctx)["status"] == "RECOVERY_REQUIRED"


def test_marker_timeout_aborts_before_reload(rig, monkeypatch):
    ctx, args, bouncer = rig

    def timeout(*args, **kwargs):
        raise TimeoutError("replication stalled")

    monkeypatch.setattr(db, "wait_for_marker", timeout)
    with pytest.raises(SystemExit):
        switch.cmd_cutover(ctx, args)
    assert bouncer.resumed == [["blue.example", "blue.example"]]


def test_dry_run_never_enters_pause(rig):
    ctx, args, bouncer = rig
    args.execute = False
    switch.cmd_cutover(ctx, args)
    assert all(not p.commands for p in bouncer.pods)
    assert not bouncer.resumed


def test_post_switch_verification_failure_exits_unsuccessfully(rig):
    ctx, args, bouncer = rig
    bouncer.pods[0].error_on = "SHOW SERVERS"
    with pytest.raises(StepFailed, match="verification failed"):
        switch.cmd_cutover(ctx, args)
    assert report(ctx)["status"] == "RECOVERY_REQUIRED"
