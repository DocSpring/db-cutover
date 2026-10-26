"""Commands that change BLUE before replication starts: reboot-blue and prepare-blue."""

from __future__ import annotations

import argparse
import time

import psycopg
from psycopg import sql

from db_cutover import db
from db_cutover.commands.inspect import direct_writer_replicas
from db_cutover.commands.switch import split_direct
from db_cutover.db import Context, q1
from db_cutover.output import fail, log
from db_cutover.pgbouncer import PgBouncer, Pod


def cmd_prepare_blue(ctx: Context, args: argparse.Namespace) -> None:
    """REPLICA IDENTITY FULL on tables without a primary key (their UPDATEs and DELETEs fail while they're in a
    publication otherwise), and the probe table that markers and `probe` write to. Idempotent."""
    cfg = ctx.cfg
    with ctx.blue() as blue:
        missing = db.tables_without_replica_identity(blue)
        has_probe = db.table_exists(blue, f"public.{cfg.probe_table}")
        if not args.execute:
            for table in missing:
                print(f"  would run: ALTER TABLE {table} REPLICA IDENTITY FULL")
            if not has_probe:
                print(f"  would create table public.{cfg.probe_table} and grant {cfg.app_user} INSERT and SELECT")
            log("dry run: nothing changed. Run again with --execute." if missing or not has_probe else "nothing to do")
            return
        for table in missing:
            log(f"ALTER TABLE {table} REPLICA IDENTITY FULL")
            db.ddl(blue, sql.SQL("ALTER TABLE {} REPLICA IDENTITY FULL").format(sql.SQL(table)))
        if has_probe:
            log(f"{cfg.probe_table} already exists on BLUE")
        else:
            log(f"creating public.{cfg.probe_table} on BLUE")
            table = sql.Identifier(cfg.probe_table)
            user = sql.Identifier(cfg.app_user)
            with blue.transaction():
                blue.execute(
                    sql.SQL(
                        """CREATE TABLE public.{} (
                             id bigserial PRIMARY KEY,
                             created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
                             source text NOT NULL,
                             note text)"""
                    ).format(table)
                )
                blue.execute(sql.SQL("GRANT SELECT, INSERT ON public.{} TO {}").format(table, user))
                blue.execute(
                    sql.SQL("GRANT USAGE ON SEQUENCE public.{} TO {}").format(
                        sql.Identifier(f"{cfg.probe_table}_id_seq"), user
                    )
                )
    log("BLUE prepared")


def _postmaster_start(ctx: Context) -> tuple[object, str] | None:
    try:
        with ctx.blue(timeout=5) as blue:
            row = q1(blue, "SELECT pg_postmaster_start_time() AS t, current_setting('wal_level') AS w")
            return (row["t"], row["w"]) if row else None
    except psycopg.Error:
        return None


def cmd_reboot_blue(ctx: Context, args: argparse.Namespace) -> None:
    """Applies BLUE's pending static parameters (rds.logical_replication) by rebooting it with PgBouncer paused, so
    clients wait instead of failing. BLUE restarts in place, Multi-AZ or not: the parameter group belongs to the
    instance, so a standby that takes over later starts with it too. --failover fails over instead, which
    takes longer (PgBouncer has to wait out its DNS cache for the new address). Traffic resumes after --max-pause
    seconds even if BLUE isn't back. Direct connections drop."""
    cfg = ctx.cfg
    inst = ctx.aws.instance(ctx.blue_id, refresh=True)
    assert inst is not None
    groups = [(g["DBParameterGroupName"], g["ParameterApplyStatus"]) for g in inst["DBParameterGroups"]]
    print(f"BLUE {ctx.blue_id}: {inst['DBInstanceStatus']}, multi-AZ {inst['MultiAZ']}, parameter groups {groups}")
    if not any(status == "pending-reboot" for _, status in groups) and not args.force:
        fail("no parameter group is pending-reboot: apply the parameter change first, or pass --force")
    if inst["DBInstanceStatus"] != "available":
        fail(f"BLUE is {inst['DBInstanceStatus']}, not available")
    failover = bool(inst["MultiAZ"]) and args.failover
    before = _postmaster_start(ctx)
    if not before:
        fail("can't connect to BLUE")
    # PgBouncer holds its clients through the restart; direct connections fail instead (a job runner logs
    # ConnectionNotEstablished until BLUE is back).
    replicas = direct_writer_replicas(ctx)
    with ctx.blue() as blue:
        direct, _ = split_direct(ctx, db.client_sessions(blue))
    if (direct or any(r not in ("0", "") for r in replicas.values())) and not args.allow_direct:
        fail(
            f"direct writers are running (replicas {replicas}, {len(direct)} sessions on BLUE): they'd get connection "
            "errors during the restart. Scale them to 0 first, or pass --allow-direct."
        )
    if not args.execute:
        log(
            f"dry run: would pause PgBouncer, reboot {ctx.blue_id}{' with failover' if failover else ''}, and resume "
            f"once BLUE accepts connections (or after {args.max_pause}s). Run again with --execute."
        )
        return

    with PgBouncer(cfg) as bouncer:
        configs = bouncer.on_all(lambda pod: pod.admin("SHOW CONFIG"), timeout=10)
        settings = {
            pod: {r["key"]: r["value"] for r in rows} for pod, rows in configs.items() if isinstance(rows, list)
        }
        if len(settings) != len(bouncer.pods):
            fail(f"SHOW CONFIG failed: {configs}")
        original_wait = {pod: s.get("query_wait_timeout") for pod, s in settings.items()}
        dns_ttl = max(float(s.get("dns_max_ttl", 15)) for s in settings.values())
        wait_timeout = int(args.max_pause + 60)

        def set_wait(pod: Pod, value: object) -> None:
            pod.admin(f"SET query_wait_timeout = {value}")

        def resume() -> None:
            results = bouncer.resume_all()
            if any(isinstance(r, Exception) for r in results.values()):
                fail(f"could not confirm RESUME on every pod: {results}; manual recovery required")

        try:
            results = bouncer.on_all(lambda pod: set_wait(pod, wait_timeout), timeout=10)
            if any(isinstance(r, Exception) for r in results.values()):
                fail(f"could not set query_wait_timeout on every pod: {results}")
            log(f"query_wait_timeout raised to {wait_timeout}s (was {original_wait})")
            started = time.monotonic()
            results = bouncer.on_all(lambda pod: pod.admin("PAUSE"), timeout=args.pause_timeout)
            errors = {k: v for k, v in results.items() if isinstance(v, Exception) and "already" not in str(v)}
            if errors:
                resume()
                fail(f"PAUSE failed; resumed, nothing rebooted: {errors}")
            log(f"PgBouncer paused in {(time.monotonic() - started) * 1000:.0f} ms; rebooting {ctx.blue_id}")
            reboot = ["rds", "reboot-db-instance", "--db-instance-identifier", ctx.blue_id]
            if failover:
                reboot.append("--force-failover")
            try:
                ctx.aws.call(*reboot)
            except Exception as e:
                resume()
                fail(f"the reboot call failed; resumed: {e}")

            back = None
            while True:
                elapsed = time.monotonic() - started
                if back is None:
                    now_start = _postmaster_start(ctx)
                    if now_start and now_start[0] != before[0]:
                        back = elapsed
                        log(f"{elapsed:.0f}s: BLUE is back (wal_level {now_start[1]})")
                        if failover:
                            log(f"waiting {dns_ttl:.0f}s for PgBouncer's DNS cache (dns_max_ttl) to see the failover")
                    else:
                        log(f"{elapsed:.0f}s: waiting for BLUE")
                ready = back is not None and (not failover or elapsed - back >= dns_ttl)
                if ready or elapsed > args.max_pause:
                    resume()
                    if ready:
                        log(f"RESUMED after {elapsed:.1f}s paused")
                    else:
                        fail(f"resumed after {elapsed:.0f}s without BLUE back: clients get errors until it is")
                    break
                time.sleep(1)
        except BaseException:
            resume()
            raise
        finally:
            restored = bouncer.on_all(lambda pod: set_wait(pod, original_wait[pod.name]), timeout=10)
            if any(isinstance(r, Exception) for r in restored.values()):
                fail(f"could not restore query_wait_timeout on every pod: {restored}; restore it manually")
            log("query_wait_timeout restored")

    # PostgreSQL accepts connections before RDS finishes its own post-reboot steps; traffic is already flowing.
    log("waiting for RDS to report the instance available and its parameters in-sync")
    deadline = time.monotonic() + 900
    while time.monotonic() < deadline:
        inst = ctx.aws.instance(ctx.blue_id, refresh=True) or {}
        groups = [(g["DBParameterGroupName"], g["ParameterApplyStatus"]) for g in inst.get("DBParameterGroups", [])]
        if inst.get("DBInstanceStatus") == "available" and all(status == "in-sync" for _, status in groups):
            after = _postmaster_start(ctx)
            log(f"BLUE available, parameter groups {groups}, wal_level {after[1] if after else 'unknown'}")
            if not after or after[1] != "logical":
                fail("wal_level is not logical after the reboot: check the parameter group")
            return
        time.sleep(10)
    fail(f"RDS still reports {inst.get('DBInstanceStatus')} / {groups} after 15 minutes: check RDS events")
