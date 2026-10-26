"""The switch from BLUE to GREEN (cutover) and the cleanup after it (finish)."""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime
import json
import time
from collections.abc import Callable
from typing import Any

import psycopg
from psycopg import sql

from db_cutover import db
from db_cutover.commands.inspect import direct_writer_replicas
from db_cutover.commands.replication import schema_diff
from db_cutover.db import Context, q, q1
from db_cutover.output import Check, error_detail, fail, log, now, print_checks
from db_cutover.pgbouncer import PgBouncer, Pod


def direct_sessions(ctx: Context, conn: psycopg.Connection) -> list[dict[str, Any]]:
    """Sessions of [direct_writers] application names."""
    names = [n.lower() for n in ctx.cfg.direct_writers.application_names]
    return [s for s in db.client_sessions(conn) if any(n in (s["application_name"] or "").lower() for n in names)]


def split_direct(ctx: Context, sessions: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(sessions of [direct_writers] application names, the rest)."""
    names = [n.lower() for n in ctx.cfg.direct_writers.application_names]
    direct = [s for s in sessions if any(n in (s["application_name"] or "").lower() for n in names)]
    return direct, [s for s in sessions if s not in direct]


def app_sessions(ctx: Context, conn: psycopg.Connection) -> list[dict[str, Any]]:
    """Every client role can be a writer; do not restrict this gate to the two configured roles."""
    return db.client_sessions(conn)


def describe_sessions(sessions: list[dict[str, Any]]) -> str:
    return "; ".join(
        f"pid {s['pid']} {s['usename']} {s['application_name'] or '-'!r} from {s['client_addr']} {s['state']}"
        for s in sessions
    )


def preconditions(
    ctx: Context, bouncer: PgBouncer, blue: psycopg.Connection, green: psycopg.Connection, args: argparse.Namespace
) -> list[Check]:
    cfg = ctx.cfg
    checks: list[Check] = []
    checks.extend(db.replication_checks(cfg, blue, green, ctx.blue_endpoint))
    checks.extend(db.supported_data_checks(blue))
    sub = db.subscription_state(green, cfg.subscription)
    checks.append(
        Check(
            "subscription enabled, every table ready",
            bool(sub and sub["subenabled"] and not sub["tables_not_ready"]),
            json.dumps({k: sub[k] for k in ("table_states", "apply_errors", "sync_errors")})
            if sub
            else "no subscription",
        )
    )
    if sub and (sub["apply_errors"] or sub["sync_errors"]):
        # Counted since the subscription started; the marker check below shows whether it applies now
        checks.append(
            Check(
                "no replication errors since the subscription started",
                False,
                f"apply {sub['apply_errors']}, sync {sub['sync_errors']}: read GREEN's log if they're still rising",
                blocking=False,
            )
        )
    if args.execute:
        try:
            elapsed = db.wait_for_marker(cfg, blue, green, timeout=10)
            checks.append(
                Check("replication caught up (marker applied within 5s)", elapsed < 5, f"{elapsed * 1000:.0f} ms")
            )
        except TimeoutError as e:
            checks.append(Check("replication caught up (marker applied within 5s)", False, str(e)))
    else:
        checks.append(Check("marker deferred until --execute", False, blocking=False))
    diff = schema_diff(ctx)
    checks.append(Check("schemas identical", not diff, f"{len(diff)} diff lines: run verify" if diff else ""))
    checks.append(
        Check(
            "schema_migrations identical",
            db.schema_migrations(blue) == db.schema_migrations(green),
            str(db.schema_migrations(blue)),
        )
    )
    gaps = db.privilege_gaps(green, cfg.app_user)
    checks.append(Check(f"{cfg.app_user} can use every table and sequence on GREEN", not gaps, ", ".join(gaps[:10])))
    try:
        with ctx.green(user=cfg.app_user, password=cfg.secret("app_password"), timeout=10) as app_green:
            q(app_green, "SELECT 1")
        checks.append(Check(f"{cfg.app_user} can log in to GREEN", True))
    except psycopg.Error as e:
        checks.append(Check(f"{cfg.app_user} can log in to GREEN", False, error_detail(e)))
    seq_rows = db.plan_sequences(blue, green, cfg.sequence_gap, cfg.sequence_min_margin)
    behind = [r["name"] for r in seq_rows if r["needs_set"]]
    checks.append(
        Check(
            f"GREEN's sequences at least {cfg.sequence_min_margin:,} above BLUE's (sequences --execute)",
            not behind,
            ", ".join(behind),
        )
    )
    green_inst = ctx.aws.instance(ctx.green_id, refresh=True) or {}
    checks.append(
        Check(
            "GREEN available, parameter group in-sync",
            green_inst.get("DBInstanceStatus") == "available"
            and all(g["ParameterApplyStatus"] == "in-sync" for g in green_inst.get("DBParameterGroups", [])),
            f"{green_inst.get('DBInstanceStatus')}, {green_inst.get('EngineVersion')}, "
            f"multi-AZ {green_inst.get('MultiAZ')}",
        )
    )
    # A big copy runs faster on a larger, single-AZ GREEN; it has to be back to BLUE's shape before it takes traffic
    blue_inst = ctx.aws.instance(ctx.blue_id, refresh=True) or {}
    checks.append(
        Check(
            "GREEN is Multi-AZ if BLUE is",
            bool(green_inst.get("MultiAZ")) or not blue_inst.get("MultiAZ"),
            f"BLUE {blue_inst.get('MultiAZ')}, GREEN {green_inst.get('MultiAZ')}",
        )
    )
    checks.append(
        Check(
            "GREEN has BLUE's instance class",
            green_inst.get("DBInstanceClass") == blue_inst.get("DBInstanceClass"),
            f"BLUE {blue_inst.get('DBInstanceClass')}, GREEN {green_inst.get('DBInstanceClass')}",
            blocking=False,
        )
    )

    pods = bouncer.list_pods()
    checks.append(
        Check(
            "PgBouncer pods ready, and the same pods this command connected to",
            [p.name for p in pods] == [p.name for p in bouncer.pods] and all(p.ready for p in pods),
            ", ".join(p.name for p in pods),
        )
    )
    file_hosts = {p.name: bouncer.file_host(p.name) for p in bouncer.pods}
    checks.append(
        Check(
            "every pod's pgbouncer.ini points at GREEN (config applied, and synced into the pods)",
            all(h == ctx.green_endpoint for h in file_hosts.values()),
            ", ".join(f"{k}: {v}" for k, v in file_hosts.items()),
            blocking=not args.rehearse,  # a rehearsal never reloads
        )
    )
    running = bouncer.on_all(bouncer.running_host, timeout=10)
    checks.append(
        Check(
            "every pod still runs on BLUE (no pod restarted onto GREEN's config)",
            all(h == ctx.blue_endpoint for h in running.values()),
            ", ".join(f"{k}: {v}" for k, v in running.items()),
        )
    )
    replicas = direct_writer_replicas(ctx)
    sessions = direct_sessions(ctx, blue)
    checks.append(
        Check(
            "direct writers scaled to 0 and gone from BLUE (PAUSE can't hold them)",
            all(r in ("0", "") for r in replicas.values()) and not sessions,
            f"replicas {replicas or '-'}, sessions on BLUE {len(sessions)}",
        )
    )
    long_tx = db.long_transactions(blue, args.max_transaction_seconds)
    checks.append(
        Check(
            f"no BLUE transaction older than {args.max_transaction_seconds}s (PAUSE waits for them)",
            not long_tx,
            "; ".join(f"pid {r['pid']} {r['usename']} {r['xact_age']}" for r in long_tx),
        )
    )
    return checks


class Aborted(Exception):
    pass


class Rehearsed(Exception):
    """--rehearse: the barrier ran up to RELOAD and every check passed."""


def in_parallel(*fns: Callable[[], Any]) -> list[Any]:
    with concurrent.futures.ThreadPoolExecutor(len(fns)) as pool:
        return [f.result() for f in [pool.submit(fn) for fn in fns]]


def cmd_cutover(ctx: Context, args: argparse.Namespace) -> None:
    """Moves the app from BLUE to GREEN through PgBouncer, with a barrier:

    1. PAUSE every pod: in-flight transactions finish, server connections close, new queries wait.
    2. Check nothing is left on BLUE (no app or admin sessions), so nothing can write there any more.
    3. Write a marker on BLUE and wait for it on GREEN: GREEN now has every committed write.
    4. Compare max(id) of every table, and move any GREEN sequence still close to BLUE's.
    5. RELOAD every pod (pgbouncer.ini already names GREEN) and check each one switched.
    6. RESUME: waiting queries run on GREEN.

    Any failure before step 5 attempts to resume on BLUE and reports failed recovery. --rehearse stops there on
    purpose: it measures the real pause and runs every check, then resumes on BLUE."""
    cfg = ctx.cfg
    report: dict[str, Any] = {
        "name": cfg.name,
        "blue": ctx.blue_id,
        "green": ctx.green_id,
        "started_at": datetime.datetime.now(datetime.UTC).isoformat(),
        "events": [],
    }

    def event(message: str, **data: Any) -> None:
        log(message)
        report["events"].append({"at": now(), "message": message, **data})

    blue_ips, green_ips = db.resolve(ctx.blue_endpoint), db.resolve(ctx.green_endpoint)
    with ctx.blue() as blue, ctx.green() as green, PgBouncer(cfg) as bouncer:
        if not print_checks(preconditions(ctx, bouncer, blue, green, args)):
            fail("preconditions failed: nothing was changed")
        if not args.execute:
            log("dry run: the preconditions pass. Run again with --execute (and --rehearse first) to switch.")
            return

        pk_tables = db.integer_pk_tables(blue)  # the schema is frozen; no need to read it while paused
        paused_at = time.monotonic()
        try:
            event(f"PAUSE on {len(bouncer.pods)} pods")
            results = bouncer.on_all(lambda pod: pod.admin("PAUSE"), timeout=args.pause_timeout)
            errors = {k: str(v) for k, v in results.items() if isinstance(v, Exception) and "already" not in str(v)}
            if errors:
                raise Aborted(f"PAUSE failed or timed out: {errors}")
            event(f"every pod paused in {(time.monotonic() - paused_at) * 1000:.0f} ms")

            # Server connections close as PAUSE returns; their backends can take a moment to leave pg_stat_activity.
            # Sessions of known direct writers never will, and anything else still there after --direct-wait is a
            # direct connection too. Either could keep writing to BLUE.
            deadline = time.monotonic() + args.direct_wait
            direct, other = split_direct(ctx, app_sessions(ctx, blue))
            if direct:
                raise Aborted(f"direct writers still on BLUE: {describe_sessions(direct)}")
            while other and time.monotonic() < deadline:
                time.sleep(0.05)
                direct, other = split_direct(ctx, app_sessions(ctx, blue))
            if other or direct:
                raise Aborted(f"sessions still on BLUE with PgBouncer paused: {describe_sessions(direct + other)}")
            event("no other client sessions left on BLUE")

            try:
                elapsed = db.wait_for_marker(cfg, blue, green, timeout=args.catchup_timeout)
            except TimeoutError as e:
                raise Aborted(str(e)) from e
            event(f"GREEN caught up: marker applied in {elapsed * 1000:.0f} ms")

            # BLUE's and GREEN's queries run side by side: each round trip is time every client spends waiting.
            (blue_max, blue_seqs), (green_max, green_seqs) = in_parallel(
                lambda: (db.max_ids(blue, pk_tables), db.sequences(blue)),
                lambda: (db.max_ids(green, pk_tables), db.sequences(green)),
            )
            mismatched = {t: (blue_max[t], green_max.get(t)) for t in blue_max if blue_max[t] != green_max.get(t)}
            if mismatched:
                raise Aborted(f"max ids differ between BLUE and GREEN: {mismatched}")
            event(f"max ids match on {len(blue_max)} tables")
            behind = [
                r
                for r in db.compare_sequences(blue_seqs, green_seqs, cfg.sequence_gap, cfg.sequence_min_margin)
                if r["needs_set"]
            ]
            if args.rehearse:
                raise Rehearsed(
                    f"rehearsal: every check passed{f' ({len(behind)} sequences would move)' if behind else ''}"
                )
            if behind:
                db.apply_sequences(green, behind)
                event(f"moved {len(behind)} GREEN sequences that were within {cfg.sequence_min_margin:,} of BLUE")
            if time.monotonic() - paused_at > args.max_paused:
                raise Aborted(f"paused for more than {args.max_paused}s before the reload")
        except BaseException as e:
            results = bouncer.resume_all()
            report["paused_ms"] = round((time.monotonic() - paused_at) * 1000)
            label = "REHEARSED" if isinstance(e, Rehearsed) else "ABORTED"
            resume_errors = {k: str(v) for k, v in results.items() if isinstance(v, Exception)}
            report["status"] = "RECOVERY_REQUIRED" if resume_errors else label
            event(
                f"{label}: {error_detail(e)}. BLUE remains primary; "
                f"RESUME results after {report['paused_ms']} ms: {results}"
            )
            save_report(ctx, report)
            if resume_errors:
                fail(f"could not confirm RESUME on BLUE: {resume_errors}. Inspect PgBouncer before continuing.")
            if isinstance(e, Rehearsed):
                return
            if isinstance(e, Aborted):
                raise SystemExit(1) from None
            raise

        # From the first RELOAD onwards, never resume mixed or unknown targets. One-way replication cannot make
        # concurrent writes to both databases safe (updates, deletes and unique constraints can conflict).
        def reload(pod: Pod) -> str | None:
            pod.admin("RELOAD")
            return bouncer.running_host(pod)

        try:
            results = bouncer.on_all(reload, timeout=10)
            if any(host != ctx.green_endpoint for host in results.values()):
                raise Aborted(f"not every pod confirmed GREEN: {results}")
            resume = bouncer.resume_all()
            if any(isinstance(r, Exception) for r in resume.values()):
                raise Aborted(f"not every pod confirmed RESUME: {resume}")
        except BaseException as e:
            report["status"] = "RECOVERY_REQUIRED"
            report["paused_ms"] = round((time.monotonic() - paused_at) * 1000)
            event(f"RECOVERY REQUIRED after RELOAD started: {error_detail(e)}. No automatic rollback.")
            event(
                "Keep writers stopped. Inspect every pod, align all hosts to GREEN, then resume. See docs/procedure.md."
            )
            save_report(ctx, report)
            if isinstance(e, Aborted):
                fail(str(e))
            raise
        report["paused_ms"] = round((time.monotonic() - paused_at) * 1000)
        event(f"every pod reloaded onto GREEN and RESUMED after {report['paused_ms']} ms paused")

        time.sleep(3)
        servers = bouncer.on_all(lambda pod: pod.admin("SHOW SERVERS"), timeout=10)
        addrs = {
            pod: sorted({s["addr"] for s in rows if s["database"] == cfg.database})
            for pod, rows in servers.items()
            if isinstance(rows, list)
        }
        event(f"server connections now {addrs} (GREEN {sorted(green_ips)}, BLUE {sorted(blue_ips)})")
        on_blue = sorted({a for v in addrs.values() for a in v if a in blue_ips})
        errors = {k: str(v) for k, v in servers.items() if isinstance(v, Exception)}
        left = app_sessions(ctx, blue)
        if errors or on_blue or left:
            report["status"] = "RECOVERY_REQUIRED"
            event(
                f"post-switch checks failed: server errors {errors}, BLUE addresses {on_blue}, BLUE clients {len(left)}"
            )
            save_report(ctx, report)
            fail("traffic resumed on GREEN, but verification failed: investigate before finish")
        report["status"] = "SWITCHED"
    save_report(ctx, report)
    log("switched. Next: move the direct writers to GREEN (their connection strings), then finish.")


def save_report(ctx: Context, report: dict[str, Any]) -> None:
    path = ctx.cfg.state_dir / f"cutover-{datetime.datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(json.dumps(report, indent=2, default=str))
    log(f"report: {path}")


def cmd_finish(ctx: Context, args: argparse.Namespace) -> None:
    """After the switch, once the direct writers use GREEN: stops replication and drops what this tool created.
    Gated on BLUE being quiet, because a write that reaches BLUE after the subscription is gone is lost."""
    cfg = ctx.cfg
    checks: list[Check] = []
    with ctx.blue() as blue, ctx.green() as green:
        with PgBouncer(cfg) as bouncer:
            running = bouncer.on_all(bouncer.running_host, timeout=10)
        checks.append(
            Check(
                "every PgBouncer pod runs on GREEN",
                all(h == ctx.green_endpoint for h in running.values()),
                ", ".join(f"{k}: {v}" for k, v in running.items()),
            )
        )
        left = app_sessions(ctx, blue)
        checks.append(Check("no app or admin sessions on BLUE", not left, describe_sessions(left)))
        before = db.write_counter(blue, cfg.probe_table)
        log(f"watching BLUE for writes for {args.quiet_seconds}s")
        time.sleep(args.quiet_seconds)
        written = db.write_counter(blue, cfg.probe_table) - before
        checks.append(Check(f"no writes on BLUE in {args.quiet_seconds}s", written == 0, f"{written} rows written"))
        if cfg.direct_writers.application_names:
            on_green = direct_sessions(ctx, green)
            checks.append(
                Check(
                    "direct writers connected to GREEN",
                    bool(on_green),
                    f"{len(on_green)} sessions" if on_green else "none: expected only while they're scaled down",
                    blocking=False,
                )
            )
        sub = db.subscription_state(green, cfg.subscription)
        if sub and args.execute:
            try:
                elapsed = db.wait_for_marker(cfg, blue, green, timeout=30)
                checks.append(Check("replication caught up", True, f"{elapsed * 1000:.0f} ms"))
            except TimeoutError as e:
                checks.append(Check("replication caught up", False, str(e)))
        elif sub:
            checks.append(Check("marker deferred until --execute", False, blocking=False))
        if not print_checks(checks):
            fail("a gate failed: nothing was dropped")
        if not args.execute:
            log("dry run: the gates pass. Run again with --execute to drop replication and the probe table.")
            return

        if sub:
            log(f"DROP SUBSCRIPTION {cfg.subscription} on GREEN (drops slot {cfg.slot} on BLUE too)")
            green.execute(sql.SQL("DROP SUBSCRIPTION {}").format(sql.Identifier(cfg.subscription)))
        if q1(blue, "SELECT 1 AS x FROM pg_replication_slots WHERE slot_name = %s", (cfg.slot,)):
            log(f"dropping leftover slot {cfg.slot} on BLUE")
            blue.execute("SELECT pg_drop_replication_slot(%s)", (cfg.slot,))
        if q1(blue, "SELECT 1 AS x FROM pg_publication WHERE pubname = %s", (cfg.publication,)):
            log(f"DROP PUBLICATION {cfg.publication} on BLUE")
            db.ddl(blue, sql.SQL("DROP PUBLICATION {}").format(sql.Identifier(cfg.publication)))
        for name, conn in (("GREEN", green), ("BLUE", blue)):
            if db.table_exists(conn, f"public.{cfg.probe_table}"):
                log(f"DROP TABLE {cfg.probe_table} on {name}")
                db.ddl(conn, sql.SQL("DROP TABLE public.{}").format(sql.Identifier(cfg.probe_table)))
        leftovers = {
            "publications": q(green, "SELECT pubname FROM pg_publication"),
            "subscriptions": q(green, "SELECT subname FROM pg_subscription"),
            "slots": q(green, "SELECT slot_name FROM pg_replication_slots"),
        }
        if any(leftovers.values()):
            fail(f"GREEN still has replication objects: {leftovers}")
        odd = [n for n, s in db.sequences(green).items() if s["increment_by"] != 1]
        if odd:
            fail(f"GREEN has sequences without INCREMENT BY 1: {odd}")
    log("replication removed: GREEN has no publications, subscriptions or slots. Next: promote-tf.")
