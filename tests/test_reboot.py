import contextlib
from types import SimpleNamespace

import pytest
from test_switch import FakeBouncer

from db_cutover import db
from db_cutover.cli import parser
from db_cutover.commands import blue
from db_cutover.output import StepFailed


@pytest.mark.parametrize("failure", ["resume", "restore", "set_wait"])
def test_reboot_does_not_report_success_when_pool_control_fails(monkeypatch, failure):
    bouncer = FakeBouncer()
    bouncer.resume_error = failure == "resume"
    rebooted = []
    instance = {
        "DBParameterGroups": [{"DBParameterGroupName": "test", "ParameterApplyStatus": "pending-reboot"}],
        "MultiAZ": False,
        "DBInstanceStatus": "available",
    }
    ctx = SimpleNamespace(
        cfg=None,
        blue_id="blue",
        blue=lambda: contextlib.nullcontext(None),
        aws=SimpleNamespace(instance=lambda *args, **kwargs: instance, call=lambda *args: rebooted.append(True)),
    )

    def admin(command):
        if command == "SHOW CONFIG":
            return [{"key": "query_wait_timeout", "value": "120"}, {"key": "dns_max_ttl", "value": "15"}]
        if failure == "restore" and command == "SET query_wait_timeout = 120":
            raise RuntimeError("restore failed")
        if failure == "set_wait" and command == "SET query_wait_timeout = 150":
            raise RuntimeError("set failed")
        return []

    for pod in bouncer.pods:
        pod.admin = admin
    monkeypatch.setattr(blue, "PgBouncer", lambda cfg: bouncer)
    monkeypatch.setattr(blue, "direct_writer_replicas", lambda ctx: {})
    monkeypatch.setattr(blue, "split_direct", lambda *args: ([], []))
    monkeypatch.setattr(db, "client_sessions", lambda conn: [])
    starts = iter([(1, "replica"), (2, "logical")])
    monkeypatch.setattr(blue, "_postmaster_start", lambda ctx: next(starts))
    args = parser().parse_args(["unused.toml", "reboot-blue", "--execute"])
    with pytest.raises(StepFailed, match="could not"):
        blue.cmd_reboot_blue(ctx, args)
    assert bool(rebooted) == (failure != "set_wait")
