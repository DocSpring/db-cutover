"""Inspection and monitoring; probe and pgbouncer resume require --execute to make changes."""

from __future__ import annotations

import argparse
import contextlib
import datetime
import json
import re
import shutil
import time
import urllib.error
import urllib.request
from typing import Any

import psycopg
from psycopg import sql

from db_cutover import __version__, config, db
from db_cutover.commands.replication import other_roles_with_privileges
from db_cutover.db import Context, q, q1
from db_cutover.output import Check, error_detail, fail, human_bytes, log, now, print_checks, print_json
from db_cutover.pgbouncer import PgBouncer
from db_cutover.shell import kubectl, run


def describe(ctx: Context, identifier: str | None) -> dict[str, Any]:
    inst = ctx.aws.instance(identifier)
    if not inst:
        return {"identifier": identifier, "status": "not created" if identifier else "not configured"}
    return {
        "identifier": identifier,
        "status": inst["DBInstanceStatus"],
        "engine_version": inst["EngineVersion"],
        "class": inst["DBInstanceClass"],
        "storage": f"{inst['StorageType']} {inst['AllocatedStorage']} GB (max {inst.get('MaxAllocatedStorage')})",
        "multi_az": inst["MultiAZ"],
        "endpoint": (inst.get("Endpoint") or {}).get("Address"),
        "parameter_groups": [
            f"{g['DBParameterGroupName']} ({g['ParameterApplyStatus']})" for g in inst["DBParameterGroups"]
        ],
        "pending": inst.get("PendingModifiedValues") or {},
    }


def cmd_env(ctx: Context, args: argparse.Namespace) -> None:
    cfg = ctx.cfg
    print_json(
        {
            "name": cfg.name,
            "config": str(cfg.path),
            "database": cfg.database,
            "admin_user": cfg.admin_user,
            "app_user": cfg.app_user,
            "hosts": {"blue": cfg.blue.host, "green": cfg.green.host, "pgbouncer": cfg.pgbouncer.host},
            "kube_context": cfg.pgbouncer.kube_context,
            "state_dir": str(cfg.state_dir),
            "blue": describe(ctx, cfg.blue.identifier),
            "green": describe(ctx, cfg.green.identifier),
        }
    )


def cmd_preflight(ctx: Context, args: argparse.Namespace) -> None:
    cfg = ctx.cfg
    checks: list[Check] = []
    for tool in ("psql", "pg_dump", "kubectl", "aws"):
        checks.append(Check(f"{tool} on PATH", shutil.which(tool) is not None))
    if not all(c.ok for c in checks):
        print_checks(checks, args.json)
        raise SystemExit(1)
    tool_versions = {}
    for tool in ("pg_dump", "psql"):
        version = run([tool, "--version"]).strip()
        match = re.search(r"(\d+)\.(\d+)", version)
        tool_versions[tool] = int(match.group(1)) if match else 0
    checks.append(
        Check(
            "psql major version is at least pg_dump's (use matching current client tools)",
            tool_versions["psql"] >= tool_versions["pg_dump"] > 0,
        )
    )

    blue_inst = ctx.aws.instance(cfg.blue.identifier)
    checks.append(Check("BLUE instance exists", blue_inst is not None, str(cfg.blue.identifier)))
    if blue_inst:
        groups = blue_inst["DBParameterGroups"]
        checks.append(
            Check(
                "BLUE parameter group in-sync (nothing waiting for a reboot)",
                all(g["ParameterApplyStatus"] == "in-sync" for g in groups),
                ", ".join(f"{g['DBParameterGroupName']}={g['ParameterApplyStatus']}" for g in groups),
            )
        )
    try:
        with ctx.blue(timeout=10) as blue:
            checks.extend(db.supported_data_checks(blue))
            settings = {
                r["name"]: r["setting"]
                for r in q(
                    blue,
                    """SELECT name, setting FROM pg_settings WHERE name IN
                       ('wal_level', 'max_replication_slots', 'max_wal_senders',
                        'server_version', 'server_version_num')""",
                )
            }
            checks.append(Check("BLUE reachable", True, f"PostgreSQL {settings.get('server_version')}"))
            major = int(settings["server_version_num"]) // 10000
            checks.append(
                Check("BLUE is PostgreSQL 16+ and pg_dump can read it", 16 <= major <= tool_versions["pg_dump"])
            )
            checks.append(
                Check(
                    "BLUE wal_level = logical (rds.logical_replication = 1, applied by a reboot)",
                    settings.get("wal_level") == "logical",
                    f"wal_level={settings.get('wal_level')}",
                )
            )
            slots = q(blue, "SELECT slot_name FROM pg_replication_slots")
            senders = q1(blue, "SELECT count(*) AS n FROM pg_stat_replication") or {"n": 0}
            free_slots = int(settings.get("max_replication_slots", 0)) - len(slots)
            checks.append(
                Check(
                    "BLUE has replication slots free (1 + a temporary one per table sync worker)",
                    free_slots >= 3,
                    f"{free_slots} free of {settings.get('max_replication_slots')}",
                )
            )
            checks.append(
                Check("BLUE has WAL senders free", int(settings.get("max_wal_senders", 0)) - senders["n"] >= 3)
            )
            other_slots = [s["slot_name"] for s in slots if s["slot_name"] != cfg.slot]
            checks.append(Check("no replication slots on BLUE but ours", not other_slots, ", ".join(other_slots)))
            pubs = [
                r["pubname"] for r in q(blue, "SELECT pubname FROM pg_publication") if r["pubname"] != cfg.publication
            ]
            checks.append(Check("no publications on BLUE but ours", not pubs, ", ".join(pubs)))
            subs = [r["subname"] for r in q(blue, "SELECT subname FROM pg_subscription")]
            checks.append(Check("no subscriptions on BLUE", not subs, ", ".join(subs)))
            missing = db.tables_without_replica_identity(blue)
            checks.append(
                Check(
                    "every BLUE table has a primary key or REPLICA IDENTITY FULL (prepare-blue sets it)",
                    not missing,
                    ", ".join(missing),
                )
            )
            seqs = db.sequences(blue)
            odd = [n for n, s in seqs.items() if s["increment_by"] != 1]
            checks.append(
                Check(
                    "BLUE sequences all INCREMENT BY 1",
                    not odd,
                    ", ".join(odd) or f"{len(seqs)} sequences",
                    blocking=False,
                )
            )
            risky = [
                n
                for n, s in seqs.items()
                if s["last_value"] <= db.INT4_MAX < s["last_value"] + cfg.sequence_gap
                or s["last_value"] + cfg.sequence_gap > s["max_value"]
            ]
            checks.append(
                Check(
                    f"no sequence crosses 2^31 or its maximum with the {cfg.sequence_gap:,} gap",
                    not risky,
                    ", ".join(risky),
                )
            )
            checks.append(
                Check(
                    f"probe table {cfg.probe_table} on BLUE (prepare-blue creates it)",
                    db.table_exists(blue, f"public.{cfg.probe_table}"),
                    blocking=False,
                )
            )
            long_tx = db.long_transactions(blue, 300)
            checks.append(
                Check(
                    "no BLUE transaction older than 5 minutes",
                    not long_tx,
                    "; ".join(f"pid {r['pid']} {r['usename']} {r['xact_age']}" for r in long_tx),
                    blocking=False,
                )
            )
            extra_roles = other_roles_with_privileges(blue, (cfg.admin_user, cfg.app_user))
            checks.append(
                Check(
                    "no other roles with grants on BLUE's tables (the schema copy leaves grants behind)",
                    not extra_roles,
                    ", ".join(extra_roles),
                    blocking=False,
                )
            )
            for key, new_type in cfg.column_types.items():
                schema, table, column = key.split(".")
                row = q1(
                    blue,
                    """SELECT format_type(a.atttypid, a.atttypmod) AS type FROM pg_attribute a
                       WHERE a.attrelid = to_regclass(%s) AND a.attname = %s AND NOT a.attisdropped""",
                    (f"{schema}.{table}", column),
                )
                checks.append(
                    Check(
                        f"[schema] column {key} exists on BLUE (GREEN gets {new_type})",
                        row is not None,
                        f"BLUE: {row['type']}" if row else "not found",
                    )
                )
                if row and row["type"] == new_type:
                    checks.append(
                        Check(
                            f"[schema] column {key} still needs changing",
                            False,
                            f"BLUE already has {new_type}: remove it from [schema] column_types (and the "
                            "[replication] binary = false it needed)",
                            blocking=False,
                        )
                    )
            checks.append(Check("BLUE database size", True, human_bytes(db.database_size(blue))))
    except psycopg.Error as e:
        checks.append(Check("BLUE reachable", False, error_detail(e)))

    if blue_inst:
        free = ctx.aws.metric_latest(ctx.blue_id, "FreeStorageSpace")
        allocated = blue_inst["AllocatedStorage"] * 1024**3
        autoscaling = (blue_inst.get("MaxAllocatedStorage") or 0) > blue_inst["AllocatedStorage"]
        roomy = free is not None and free / allocated > 0.15
        checks.append(
            Check(
                "BLUE has over 15% storage free (its slot keeps WAL until GREEN has applied it)",
                roomy,
                f"{human_bytes(free)} free of {blue_inst['AllocatedStorage']} GB"
                + (f", autoscaling up to {blue_inst['MaxAllocatedStorage']} GB" if autoscaling else ""),
                blocking=not autoscaling,
            )
        )

    if cfg.green.identifier:
        green_inst = ctx.aws.instance(cfg.green.identifier)
        checks.append(Check("GREEN instance exists", green_inst is not None, cfg.green.identifier, blocking=False))
        if green_inst:
            groups = green_inst["DBParameterGroups"]
            checks.append(
                Check(
                    "GREEN parameter group in-sync",
                    all(g["ParameterApplyStatus"] == "in-sync" for g in groups),
                    ", ".join(f"{g['DBParameterGroupName']}={g['ParameterApplyStatus']}" for g in groups),
                )
            )
            try:
                with ctx.green(dbname="postgres", timeout=10) as green:
                    row = q1(
                        green,
                        "SELECT current_setting('wal_level') AS w, current_setting('server_version') AS v, "
                        "current_setting('server_version_num')::int / 10000 AS major",
                    )
                    assert row is not None
                    checks.append(Check("GREEN reachable", True, f"PostgreSQL {row['v']}"))
                    checks.append(
                        Check(
                            "GREEN is PostgreSQL 16+ and pg_dump can read it",
                            16 <= row["major"] <= tool_versions["pg_dump"],
                        )
                    )
                    checks.append(
                        Check(
                            "GREEN wal_level = logical, so it can be BLUE next time",
                            row["w"] == "logical",
                            f"wal_level={row['w']}",
                        )
                    )
            except psycopg.Error as e:
                checks.append(Check("GREEN reachable", False, error_detail(e), blocking=False))

    try:
        bouncer = PgBouncer(cfg)
        pods = bouncer.list_pods()
        checks.append(
            Check(
                "PgBouncer pods ready",
                bool(pods) and all(p.ready for p in pods),
                ", ".join(f"{p.name}{'' if p.ready else ' (not ready)'}" for p in pods),
            )
        )
        hosts = {p.name: bouncer.file_host(p.name) for p in pods}
        checks.append(
            Check(
                "PgBouncer's pgbouncer.ini points at BLUE",
                all(h == ctx.blue_endpoint for h in hosts.values()),
                ", ".join(f"{k}={v}" for k, v in hosts.items()),
                blocking=False,
            )
        )
    except Exception as e:
        checks.append(Check("kubectl reaches PgBouncer", False, str(e)))
    if not print_checks(checks, args.json):
        raise SystemExit(1)


def status_snapshot(ctx: Context) -> dict[str, Any]:
    cfg = ctx.cfg
    snap: dict[str, Any] = {"at": datetime.datetime.now(datetime.UTC).isoformat(), "name": cfg.name}
    try:
        with ctx.blue(timeout=5) as blue:
            snap["blue"] = {
                "size": db.database_size(blue),
                "wal_level": q1(blue, "SELECT current_setting('wal_level') AS w")["w"],  # type: ignore[index]
                "sessions": db.summarize_sessions(db.client_sessions(blue)),
                "slot": db.slot_state(blue, cfg.slot),
                "publication": bool(
                    q1(blue, "SELECT 1 AS x FROM pg_publication WHERE pubname = %s", (cfg.publication,))
                ),
            }
    except psycopg.Error as e:
        snap["blue"] = {"error": error_detail(e)}
    if ctx.has_green:
        try:
            with ctx.green(timeout=5) as green:
                snap["green"] = {
                    "size": db.database_size(green),
                    "sessions": db.summarize_sessions(db.client_sessions(green)),
                    "subscription": db.subscription_state(green, cfg.subscription),
                }
        except psycopg.Error as e:
            snap["green"] = {"error": error_detail(e)}
    try:
        names = {ctx.blue_endpoint: "BLUE"}
        if ctx.has_green:
            names[ctx.green_endpoint] = "GREEN"
        with PgBouncer(cfg) as bouncer:
            running = bouncer.on_all(bouncer.running_host, timeout=10)
            pools = bouncer.on_all(lambda pod: pod.admin("SHOW POOLS"), timeout=10)
        snap["pgbouncer"] = {}
        for pod, host in sorted(running.items()):
            pod_pools = pools[pod] if isinstance(pools.get(pod), list) else []
            snap["pgbouncer"][pod] = {
                "database": f"error {host}" if isinstance(host, Exception) else names.get(host, str(host)),
                "clients_waiting": sum(p["cl_waiting"] for p in pod_pools if p["database"] == cfg.database),
            }
    except Exception as e:
        snap["pgbouncer"] = {"error": str(e)}
    return snap


def cmd_status(ctx: Context, args: argparse.Namespace) -> None:
    snap = status_snapshot(ctx)
    if args.json:
        print_json(snap)
        return
    blue, green = snap.get("blue", {}), snap.get("green")
    if "error" in blue:
        print(f"BLUE  {ctx.cfg.blue.identifier}: ERROR {blue['error']}")
    else:
        print(
            f"BLUE  {ctx.cfg.blue.identifier}: {human_bytes(blue['size'])}, wal_level {blue['wal_level']}, "
            f"publication {'yes' if blue['publication'] else 'no'}, sessions {blue['sessions']}"
        )
        if blue.get("slot"):
            s = blue["slot"]
            print(
                f"      slot {s['slot_name']}: active {s['active']}, lag {human_bytes(s['lag_bytes'])}, "
                f"WAL kept {human_bytes(s['retained_bytes'])} ({s['wal_status']})"
            )
    if green is not None:
        if "error" in green:
            print(f"GREEN {ctx.cfg.green.identifier}: ERROR {green['error']}")
        else:
            print(f"GREEN {ctx.cfg.green.identifier}: {human_bytes(green['size'])}, sessions {green['sessions']}")
            sub = green.get("subscription")
            if sub:
                print(
                    f"      subscription enabled {sub['subenabled']}, worker {sub['worker_running']}, tables "
                    f"{sub['table_states']}, errors apply {sub['apply_errors']} sync {sub['sync_errors']}"
                )
                for c in sub["copying"]:
                    print(
                        f"      copying {c['table_name']}: {human_bytes(c['bytes_processed'])}, "
                        f"{c['tuples_processed']:,} rows"
                    )
            else:
                print("      no subscription")
    print(f"PgBouncer {json.dumps(snap.get('pgbouncer'))}")


class HealthRedirectHandler(urllib.request.HTTPRedirectHandler):
    """A health URL must answer directly; never forward its authentication header to a redirect target."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def cmd_watch(ctx: Context, args: argparse.Namespace) -> None:
    """Long-running: one line whenever something changes, ALERT lines for problems. Made for Claude Code's Monitor
    tool or a background shell. Health URLs every --health-interval seconds, databases every --interval."""
    cfg = ctx.cfg
    last: dict[str, str] = {}
    last_errors = (0, 0)  # replication error counts are cumulative: alert when they go up
    last_db = 0.0
    alerted: dict[str, float] = {}  # url -> when its current failure was last reported
    # Cloudflare's bot protection answers 403 to urllib's default user agent.
    health_headers = {"User-Agent": f"db-cutover/{__version__}", **cfg.health_check_headers()}
    health_client = urllib.request.build_opener(HealthRedirectHandler())
    log(f"watching {cfg.name}: {len(cfg.health_urls)} health URLs, databases every {args.interval:.0f}s")
    while True:
        for url in cfg.health_urls:
            started = time.monotonic()
            try:
                request = urllib.request.Request(url, headers=health_headers)
                with health_client.open(request, timeout=args.health_timeout) as resp:
                    resp.read(2000)  # never print a health response body: it may contain private data
                    state = "ok" if resp.status == 200 else f"HTTP {resp.status}"
            except urllib.error.HTTPError as e:
                state = f"HTTP {e.code}"
            except Exception as e:
                state = f"{type(e).__name__}: {e}"
            ms = (time.monotonic() - started) * 1000
            if state == "ok" and ms > args.slow_ms:
                state = "slow"
            if state != "ok":
                # Report a failure when it starts or changes, then once a minute while it lasts.
                if last.get(url) != state or time.monotonic() - alerted.get(url, 0) > 60:
                    log(f"ALERT {url}: {state} in {ms:.0f} ms")
                    alerted[url] = time.monotonic()
            elif last.get(url) != "ok":
                log(f"{url}: ok ({ms:.0f} ms)")
            last[url] = state
        if time.monotonic() - last_db >= args.interval:
            last_db = time.monotonic()
            try:
                snap = status_snapshot(ctx)
            except Exception as e:
                log(f"ALERT status failed: {e}")
                snap = {}
            sub = (snap.get("green") or {}).get("subscription") or {}
            slot = (snap.get("blue") or {}).get("slot") or {}
            for key, value, alert in (
                ("BLUE", (snap.get("blue") or {}).get("error", "ok"), "error" in (snap.get("blue") or {})),
                (
                    "GREEN",
                    (snap["green"].get("error", "ok") if "green" in snap else "not created"),
                    "error" in (snap.get("green") or {}),
                ),
                (
                    "PgBouncer",
                    json.dumps(snap.get("pgbouncer"), sort_keys=True),
                    "error" in json.dumps(snap.get("pgbouncer")),
                ),
                (
                    "subscription",
                    json.dumps({k: sub.get(k) for k in ("subenabled", "table_states", "apply_errors", "sync_errors")})
                    if sub
                    else "none",
                    bool(sub)
                    and (
                        not sub.get("subenabled")
                        or (sub.get("apply_errors") or 0) > last_errors[0]
                        or (sub.get("sync_errors") or 0) > last_errors[1]
                    ),
                ),
            ):
                if last.get(key) != value:
                    log(f"{'ALERT ' if alert else ''}{key}: {value}")
                    last[key] = value
            if sub:
                last_errors = (sub.get("apply_errors") or 0, sub.get("sync_errors") or 0)
            retained = slot.get("retained_bytes") or 0
            if retained > args.max_retained_gb * 1024**3:
                log(f"ALERT BLUE is keeping {human_bytes(retained)} of WAL for the slot")
        time.sleep(args.health_interval)


def cmd_probe(ctx: Context, args: argparse.Namespace) -> None:
    """Long-running: writes a row through PgBouncer as the app user every --interval seconds on one connection, like
    the app does, and prints a line for every error, slow write, or change of database. Through a switch it should
    show one slow write (the pause), the move to GREEN, and no errors."""
    cfg = ctx.cfg
    if not args.execute:
        log("dry run: would write probe rows through PgBouncer. Run again with --execute to start.")
        return
    names: dict[str, str] = {}
    last_resolved = 0.0

    def where(ip: str) -> str:
        """BLUE or GREEN for a server address. Re-reads the config (GREEN may not have existed at start) and DNS
        when an address is new, at most once a minute."""
        nonlocal names, last_resolved
        if ip not in names and time.monotonic() - last_resolved > 60:
            last_resolved = time.monotonic()
            fresh = Context(config.load(cfg.path))
            names = {a: "BLUE" for a in db.resolve(fresh.blue_endpoint)}
            if fresh.has_green:
                names.update({a: "GREEN" for a in db.resolve(fresh.green_endpoint)})
        return names.get(ip, ip)

    insert = sql.SQL(
        "INSERT INTO public.{} (source, note) VALUES ('probe', %s) RETURNING id, host(inet_server_addr()) AS ip"
    ).format(sql.Identifier(cfg.probe_table))
    conn: psycopg.Connection | None = None
    stats = {"ok": 0, "errors": 0, "slow": 0}
    last_db, last_report = "", time.monotonic()
    log(f"writing through {cfg.pgbouncer.host} every {args.interval}s")
    while True:
        started = time.monotonic()
        try:
            if conn is None or conn.closed:
                conn = ctx.pgbouncer_app(timeout=10)
                connect_ms = (time.monotonic() - started) * 1000
                if connect_ms > args.slow_ms:
                    log(f"connected in {connect_ms:.0f} ms (not counted as a slow write)")
            write_started = time.monotonic()
            row = q1(conn, insert, (f"probe {now()}",)) or {}
            done, finished = time.monotonic(), now()
            ms = (done - write_started) * 1000
            ip = row.get("ip") or ""
            server = where(ip)
            # Naming a new address re-reads the config, which can take seconds: say when the write really finished
            late = f" (the write finished at {finished})" if time.monotonic() - done > 0.5 else ""
            stats["ok"] += 1
            if server != last_db:
                log(f"writes land on {server} ({ip}), id {row.get('id')}{late}")
                last_db = server
            if ms > args.slow_ms:
                stats["slow"] += 1
                log(f"slow write: {ms:.0f} ms on {server}{late}")
        except psycopg.Error as e:
            stats["errors"] += 1
            log(f"ALERT write failed after {(time.monotonic() - started) * 1000:.0f} ms: {error_detail(e)}")
            if conn is not None:
                with contextlib.suppress(Exception):
                    conn.close()
            conn = None
        if time.monotonic() - last_report > 60:
            log(f"probe totals {stats}")
            last_report = time.monotonic()
        time.sleep(max(0.0, args.interval - (time.monotonic() - started)))


def cmd_pgbouncer(ctx: Context, args: argparse.Namespace) -> None:
    """SHOW ... on every pod; `config` compares the host each pod runs with to the host in its pgbouncer.ini;
    `resume` resumes every pod (the way out if a cutover or reboot-blue died while PgBouncer was paused)."""
    if args.what == "resume" and not args.execute:
        log("dry run: would resume every selected PgBouncer pod. Check config, then pass --execute.")
        return
    with PgBouncer(ctx.cfg) as bouncer:
        if args.what == "resume":
            hosts = bouncer.on_all(bouncer.running_host, timeout=10)
            if any(isinstance(h, Exception) or not h for h in hosts.values()) or len(set(hosts.values())) != 1:
                fail("refusing RESUME: pods have mixed or unknown database hosts. Align every pod first.")
            results = bouncer.resume_all()
            for pod, result in sorted(results.items()):
                print(f"{pod}: {result}")
            if any(isinstance(r, Exception) for r in results.values()):
                fail("RESUME failed on one or more pods: inspect their admin consoles")
            return
        if args.what == "config":
            for pod in bouncer.pods:
                print(f"{pod.name}: running {bouncer.running_host(pod)}, file {bouncer.file_host(pod.name)}")
            return
        results = bouncer.on_all(lambda pod: pod.admin(f"SHOW {args.what.upper()}"), timeout=15)
        for pod, rows in sorted(results.items()):
            print(f"== {pod}")
            if isinstance(rows, Exception):
                print(f"   error: {rows}")
                continue
            for row in rows:
                print("   " + json.dumps(row, default=str))
        if any(isinstance(r, Exception) for r in results.values()):
            fail("one or more PgBouncer queries failed")


def direct_writer_replicas(ctx: Context) -> dict[str, str]:
    """Replica counts of every configured deployment; lookup failures block the operation."""
    out = {}
    for ref in ctx.cfg.direct_writers.deployments:
        namespace, _, name = ref.partition("/")
        out[ref] = kubectl(
            ctx.cfg.direct_writers.kube_context,
            "-n",
            namespace,
            "get",
            "deployment",
            name,
            "-o",
            "jsonpath={.spec.replicas}",
        ).strip()
        if not out[ref].isdigit():
            fail(f"could not determine replicas for {ref}")
    return out
