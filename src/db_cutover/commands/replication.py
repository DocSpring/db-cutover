"""Building GREEN: copy-schema, start-replication, wait-sync, verify, sequences."""

from __future__ import annotations

import argparse
import concurrent.futures
import difflib
import re
import time
from pathlib import Path

import psycopg
from psycopg import sql

from db_cutover import db
from db_cutover.db import Context, q, q1
from db_cutover.output import Check, fail, human_bytes, log, print_checks
from db_cutover.shell import run

PG_DUMP_FLAGS = [
    "--schema-only",
    "--no-owner",
    "--no-acl",
    # Do not carry source replication objects into the destination: a leftover publication can make
    # UPDATE/DELETE fail on tables without replica identity after the cutover.
    "--no-publications",
    "--no-subscriptions",
    "--no-security-labels",
    "--no-tablespaces",
]


def dump_schema(ctx: Context, host: str, path: Path) -> str:
    run(
        [
            "pg_dump",
            *PG_DUMP_FLAGS,
            f"--host={host}",
            "--port=5432",
            f"--username={ctx.cfg.admin_user}",
            f"--dbname={ctx.cfg.database}",
            f"--file={path}",
        ],
        env={"PGPASSWORD": ctx.cfg.secret("admin_password"), "PGSSLMODE": "require", "PGAPPNAME": db.APPLICATION_NAME},
        timeout=900,
    )
    return path.read_text()


def normalize_dump(text: str) -> str:
    """Drops what differs between two dumps of the same schema: comments (they carry versions and timestamps),
    blank lines, and psql meta-commands (pg_dump 17.6+ wraps the dump in \\restrict with a random key)."""
    return "\n".join(
        line for line in text.splitlines() if line.strip() and not line.startswith("--") and not line.startswith("\\")
    )


COLUMN_LINE = re.compile(r"^    (\S+) (.+?)( NOT NULL.*| DEFAULT .*| COLLATE .*| GENERATED .*)?(,?)$")


def expected_green(blue: str, column_types: dict[str, str]) -> str:
    """BLUE's normalized dump with the [schema] column_types changes applied: what GREEN's dump should be."""
    if not column_types:
        return blue
    out, table = [], None
    for line in blue.splitlines():
        start = re.match(r"^CREATE TABLE (\S+) \($", line)
        if start:
            table = start.group(1)
        elif line.startswith(");"):
            table = None
        elif table:
            column = COLUMN_LINE.match(line)
            if column and f"{table}.{column.group(1)}" in column_types:
                new_type = column_types[f"{table}.{column.group(1)}"]
                line = f"    {column.group(1)} {new_type}{column.group(3) or ''}{column.group(4)}"
        out.append(line)
    return "\n".join(out)


def column_type_sql(column_types: dict[str, str]) -> list[str]:
    """ALTER TABLE statements for [schema] column_types, one per table."""
    by_table: dict[str, list[str]] = {}
    for key, new_type in column_types.items():
        schema, table, column = key.split(".")
        by_table.setdefault(f"{schema}.{table}", []).append(f"ALTER COLUMN {column} TYPE {new_type}")
    return [f"ALTER TABLE {table} {', '.join(changes)};" for table, changes in sorted(by_table.items())]


def schema_diff(ctx: Context) -> list[str]:
    """Unified diff of GREEN's schema against BLUE's (with [schema] column_types applied); empty when they match."""
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        blue_dump = pool.submit(dump_schema, ctx, ctx.blue_host, ctx.cfg.state_dir / "schema_blue.sql")
        green_dump = pool.submit(dump_schema, ctx, ctx.green_host, ctx.cfg.state_dir / "schema_green.sql")
        blue = expected_green(normalize_dump(blue_dump.result()), ctx.cfg.column_types)
        green = normalize_dump(green_dump.result())
    if blue == green:
        return []
    return list(difflib.unified_diff(blue.splitlines(), green.splitlines(), "blue", "green", lineterm=""))


def other_roles_with_privileges(conn: psycopg.Connection, roles: tuple[str, ...]) -> list[str]:
    """Roles other than `roles` (and RDS's own) holding privileges on tables. pg_dump --no-acl leaves their grants
    behind, so GREEN needs them recreated."""
    rows = q(
        conn,
        """SELECT DISTINCT grantee FROM information_schema.role_table_grants
           WHERE table_schema NOT IN ('pg_catalog', 'information_schema')
             AND grantee <> ALL(%s) AND grantee NOT LIKE 'rds%%' AND grantee <> 'PUBLIC'
           ORDER BY 1""",
        (list(roles),),
    )
    return [r["grantee"] for r in rows]


def cmd_copy_schema(ctx: Context, args: argparse.Namespace) -> None:
    """Creates the database on GREEN (BLUE's encoding and collation) and restores BLUE's schema into it in one
    transaction, then checks the two schemas match. No data: replication copies that."""
    cfg = ctx.cfg
    green_endpoint = ctx.green_endpoint  # fails early when GREEN doesn't exist
    with ctx.blue() as blue:
        if db.tables_without_replica_identity(blue) or not db.table_exists(blue, f"public.{cfg.probe_table}"):
            fail("run prepare-blue first: the dump must include the probe table and REPLICA IDENTITY settings")
        locale = q1(
            blue,
            """SELECT pg_encoding_to_char(encoding) AS encoding, datcollate, datctype, datlocprovider::text AS provider
               FROM pg_database WHERE datname = current_database()""",
        )
        assert locale is not None
        extra_roles = other_roles_with_privileges(blue, (cfg.admin_user, cfg.app_user))
    if extra_roles:
        log(f"note: {', '.join(extra_roles)} have grants on BLUE that pg_dump --no-acl won't copy; recreate them")
    with ctx.green(dbname="postgres") as green_pg:
        exists = q1(green_pg, "SELECT 1 AS x FROM pg_database WHERE datname = %s", (cfg.database,))
    if not exists and locale["provider"] != "c":
        fail(f"BLUE uses locale provider {locale['provider']!r}; copy-schema only creates libc databases")
    if not args.execute:
        if cfg.column_types:
            log("GREEN's column type changes, in the restore: " + " ".join(column_type_sql(cfg.column_types)))
        log(
            f"dry run: would {'use the existing' if exists else 'create'} database {cfg.database} on GREEN "
            f"({locale['encoding']}, {locale['datcollate']}), restore BLUE's schema into it, and compare. "
            "Run again with --execute."
        )
        return
    if not exists:
        log(f"creating database {cfg.database} on GREEN ({locale['encoding']}, {locale['datcollate']})")
        with ctx.green(dbname="postgres") as green_pg:
            green_pg.execute(
                sql.SQL("CREATE DATABASE {} TEMPLATE template0 ENCODING {} LC_COLLATE {} LC_CTYPE {}").format(
                    sql.Identifier(cfg.database),
                    sql.Literal(locale["encoding"]),
                    sql.Literal(locale["datcollate"]),
                    sql.Literal(locale["datctype"]),
                )
            )
    with ctx.green() as green:
        green_locale = q1(
            green,
            """SELECT pg_encoding_to_char(encoding) AS encoding, datcollate, datctype FROM pg_database
               WHERE datname = current_database()""",
        )
        assert green_locale is not None
        if (green_locale["encoding"], green_locale["datcollate"], green_locale["datctype"]) != (
            locale["encoding"],
            locale["datcollate"],
            locale["datctype"],
        ):
            fail(f"GREEN's database locale {green_locale} differs from BLUE's {locale}")
        existing = db.list_tables(green)
    if existing:
        diff = schema_diff(ctx)
        if diff:
            log("schema mismatch: compare the private schema files in the state directory")
            fail(
                f"GREEN already has {len(existing)} tables and its schema differs from BLUE's. Drop GREEN's database "
                "(nothing reads it yet) and run copy-schema again."
            )
        log(f"GREEN already has BLUE's schema ({len(existing)} tables): nothing to do")
        return

    dump_path = cfg.state_dir / "schema_blue.sql"
    log(f"dumping BLUE's schema to {dump_path}")
    dump_schema(ctx, ctx.blue_host, dump_path)
    if cfg.column_types:
        # Same transaction as the schema, while the tables are still empty: changing a type rewrites nothing.
        changes = column_type_sql(cfg.column_types)
        log("GREEN's column type changes: " + " ".join(changes))
        restore_path = cfg.state_dir / "schema_restore.sql"
        restore_path.write_text(dump_path.read_text() + "\n" + "\n".join(changes) + "\n")
        dump_path = restore_path
    log(f"restoring it on GREEN ({green_endpoint}) in one transaction")
    run(
        [
            "psql",
            "-X",
            "-q",
            "-v",
            "ON_ERROR_STOP=1",
            "--single-transaction",
            f"--host={ctx.green_host}",
            "--port=5432",
            f"--username={cfg.admin_user}",
            f"--dbname={cfg.database}",
            f"--file={dump_path}",
        ],
        env={"PGPASSWORD": cfg.secret("admin_password"), "PGSSLMODE": "require", "PGAPPNAME": db.APPLICATION_NAME},
        timeout=900,
    )
    diff = schema_diff(ctx)
    if diff:
        log("schema mismatch: compare the private schema files in the state directory")
        fail("GREEN's schema differs from BLUE's after the restore")
    with ctx.green() as green:
        tables = len(db.list_tables(green))
        leftovers = q(green, "SELECT pubname FROM pg_publication") + q(green, "SELECT subname FROM pg_subscription")
    if leftovers:
        fail(f"GREEN has replication objects after the restore: {leftovers}")
    log(f"GREEN's schema matches BLUE's ({tables} tables). Next: create the app's role and grants on GREEN.")


def cmd_start_replication(ctx: Context, args: argparse.Namespace) -> None:
    """Publication on BLUE (an explicit table list), subscription on GREEN. Creating the subscription starts the
    initial copy and a replication slot on BLUE, which keeps WAL until GREEN has applied it."""
    cfg = ctx.cfg
    blue_endpoint = ctx.blue_endpoint
    with ctx.blue() as blue, ctx.green() as green:
        if not db.list_tables(green):
            fail("GREEN has no tables: run copy-schema")
        if db.tables_without_replica_identity(blue):
            fail("run prepare-blue first")
        wal_level = q1(blue, "SELECT current_setting('wal_level') AS w")["w"]  # type: ignore[index]
        if wal_level != "logical":
            fail(f"BLUE's wal_level is {wal_level}: set rds.logical_replication = 1 and run reboot-blue")
        diff = schema_diff(ctx)
        if diff:
            log("schema mismatch: compare the private schema files in the state directory")
            fail("BLUE's and GREEN's schemas differ: was there a migration since copy-schema?")
        has_pub = q1(blue, "SELECT 1 AS x FROM pg_publication WHERE pubname = %s", (cfg.publication,))
        sub = db.subscription_state(green, cfg.subscription)
        has_sub = sub is not None
        tables = db.list_tables(blue)
        if not args.execute:
            if sub and not sub["subenabled"]:
                log(
                    f"dry run: subscription {cfg.subscription} is disabled (apply errors {sub['apply_errors']}, sync "
                    f"errors {sub['sync_errors']}): would enable it again, with disable_on_error off. It resumes "
                    "where it stopped. Read GREEN's log first: if a change failed to apply, it fails again."
                )
                return
            log(
                f"dry run: would create publication {cfg.publication} on BLUE for {len(tables)} tables "
                f"({'exists' if has_pub else 'new'}) and subscription {cfg.subscription} on GREEN "
                f"({'exists' if has_sub else 'new'}, starts the initial copy). Run again with --execute."
            )
            return
        if has_pub:
            log(f"publication {cfg.publication} already exists on BLUE")
        else:
            log(f"creating publication {cfg.publication} on BLUE for {len(tables)} tables")
            db.ddl(
                blue,
                sql.SQL("CREATE PUBLICATION {} FOR TABLE {}").format(
                    sql.Identifier(cfg.publication), sql.SQL(", ").join(sql.SQL(t) for t in tables)
                ),
            )
        if sub and not sub["subenabled"]:
            name = sql.Identifier(cfg.subscription)
            log(f"enabling subscription {cfg.subscription} again (disable_on_error off); it resumes where it stopped")
            green.execute(sql.SQL("ALTER SUBSCRIPTION {} SET (disable_on_error = false)").format(name))
            green.execute(sql.SQL("ALTER SUBSCRIPTION {} ENABLE").format(name))
            log("enabled. Follow it with wait-sync, status, or watch.")
            return
        if has_sub:
            log(f"subscription {cfg.subscription} already exists on GREEN")
            return
        conninfo = psycopg.conninfo.make_conninfo(
            host=blue_endpoint,  # GREEN connects inside AWS, not through this machine's host
            port=5432,
            dbname=cfg.database,
            user=cfg.admin_user,
            password=cfg.secret("admin_password"),
            sslmode="require",
            application_name=cfg.subscription,
        )
        log(f"creating subscription {cfg.subscription} on GREEN: the initial copy starts now")
        # Binary is opt-in: matching type names alone do not guarantee compatibility across versions.
        # streaming=on receives large transactions into temporary files before commit, then applies them serially.
        # disable_on_error stays off: it also stops the subscription on a dropped connection, which happens (BLUE
        # closes the walsender after wal_sender_timeout while the apply worker waits for a big table to catch up).
        # Off, the worker reconnects by itself; a change that keeps failing shows as rising error counts, which
        # wait-sync and watch report, and the marker checks in verify and cutover fail.
        green.execute(
            sql.SQL(
                "CREATE SUBSCRIPTION {} CONNECTION {} PUBLICATION {} WITH (copy_data = true, create_slot = true, "
                "slot_name = {}, binary = {}, streaming = on, disable_on_error = false)"
            ).format(
                sql.Identifier(cfg.subscription),
                sql.Literal(conninfo),
                sql.Identifier(cfg.publication),
                sql.Literal(cfg.slot),
                sql.Literal(cfg.replication_binary),
            )
        )
    log("subscription created. Follow it with wait-sync (blocks until done), status, or watch.")


def cmd_wait_sync(ctx: Context, args: argparse.Namespace) -> None:
    """Blocks until every table is ready and a marker written on BLUE has reached GREEN."""
    if not args.execute:
        log("dry run: would wait for the initial copy, then write a replication marker. Use --execute to wait.")
        return
    started = time.monotonic()
    last_line, last_print = "", 0.0
    errors: tuple[int, int] | None = None
    with ctx.blue() as blue, ctx.green() as green:
        blue_size = db.database_size(blue)
        while True:
            sub = db.subscription_state(green, ctx.cfg.subscription)
            if not sub:
                fail("no subscription on GREEN: run start-replication")
            if not sub["subenabled"]:
                fail(
                    f"the subscription is disabled (apply errors {sub['apply_errors']}, sync errors "
                    f"{sub['sync_errors']}): read GREEN's PostgreSQL log. After a dropped connection, "
                    "start-replication --execute enables it again."
                )
            now_errors = (sub["apply_errors"], sub["sync_errors"])
            if errors is not None and now_errors > errors:
                log(
                    f"ALERT replication errors went up: apply {now_errors[0]}, sync {now_errors[1]}. The worker "
                    "retries by itself; if they keep rising, read GREEN's PostgreSQL log."
                )
            errors = now_errors
            states = sub["table_states"]
            total, ready = sum(states.values()), states.get("r", 0)
            slot = db.slot_state(blue, ctx.cfg.slot) or {}
            copying = ", ".join(f"{c['table_name']} {human_bytes(c['bytes_processed'])}" for c in sub["copying"])
            line = (
                f"tables ready {ready}/{total} {dict(states)} | GREEN {human_bytes(db.database_size(green))} of "
                f"BLUE {human_bytes(blue_size)} | slot lag {human_bytes(slot.get('lag_bytes'))}, WAL kept on BLUE "
                f"{human_bytes(slot.get('retained_bytes'))}" + (f" | copying {copying}" if copying else "")
            )
            if line != last_line or time.monotonic() - last_print > 300:
                log(line)
                last_line, last_print = line, time.monotonic()
            if total and ready == total:
                try:
                    elapsed = db.wait_for_marker(ctx.cfg, blue, green, timeout=60)
                except TimeoutError as e:
                    log(f"every table is ready, but {e}; still catching up")
                else:
                    log(
                        f"in sync: every table ready, marker applied in {elapsed * 1000:.0f} ms "
                        f"(waited {(time.monotonic() - started) / 60:.1f} min)"
                    )
                    return
            if args.timeout and time.monotonic() - started > args.timeout * 60:
                fail(f"not in sync after {args.timeout} minutes")
            time.sleep(args.interval)


def cmd_verify(ctx: Context, args: argparse.Namespace) -> None:
    """Compares BLUE and GREEN while replication runs: schemas, exact row counts for small tables, and row counts
    and content hashes over id windows (oldest, middle, recent) for big ones. --execute adds a marker write."""
    cfg = ctx.cfg
    checks: list[Check] = []
    with ctx.blue() as blue, ctx.green() as green:
        sub = db.subscription_state(green, cfg.subscription)
        checks.append(Check("subscription exists and is enabled", bool(sub and sub["subenabled"])))
        if sub:
            checks.append(
                Check(
                    "every table ready",
                    not sub["tables_not_ready"],
                    ", ".join(sub["tables_not_ready"]) or f"{sum(sub['table_states'].values())} tables",
                )
            )
            checks.append(
                Check(
                    "no apply or sync errors since the subscription started (the marker check shows it applies now)",
                    sub["apply_errors"] == 0 and sub["sync_errors"] == 0,
                    f"apply {sub['apply_errors']}, sync {sub['sync_errors']}",
                    blocking=False,
                )
            )
        diff = schema_diff(ctx)
        checks.append(Check("schemas identical", not diff, f"{len(diff)} diff lines" if diff else ""))
        blue_migrations, green_migrations = db.schema_migrations(blue), db.schema_migrations(green)
        checks.append(
            Check(
                "schema_migrations identical",
                blue_migrations == green_migrations,
                f"BLUE {blue_migrations}, GREEN {green_migrations}",
            )
        )
        if args.execute:
            try:
                elapsed = db.wait_for_marker(cfg, blue, green, timeout=60)
                checks.append(Check("marker round trip", True, f"{elapsed * 1000:.0f} ms"))
            except TimeoutError as e:
                checks.append(Check("marker round trip", False, str(e)))
        else:
            checks.append(Check("marker not written (use --execute to test replication)", False, blocking=False))
        gaps = db.privilege_gaps(green, cfg.app_user)
        checks.append(
            Check(
                f"{cfg.app_user} can use every table and sequence on GREEN",
                not gaps,
                ", ".join(gaps[:10]) + (" ..." if len(gaps) > 10 else ""),
            )
        )

        live = {
            r["name"]: r["n_live_tup"]
            for r in q(
                blue,
                "SELECT quote_ident(schemaname) || '.' || quote_ident(relname) AS name, n_live_tup "
                "FROM pg_stat_user_tables",
            )
        }
        pk_columns = dict(db.integer_pk_tables(blue))
        only = set(args.tables or [])
        for table in db.list_tables(blue):
            if table.endswith(f".{cfg.probe_table}") or (
                only and table not in only and table.split(".")[-1] not in only
            ):
                continue
            column = pk_columns.get(table)
            if live.get(table, 0) <= args.exact_count_rows or not column:
                count = sql.SQL("SELECT count(*) AS n FROM {}").format(sql.SQL(table))
                b, g = q1(blue, count)["n"], q1(green, count)["n"]  # type: ignore[index]
                if b != g:  # rows may have changed between the two counts: let GREEN catch up, then count again
                    time.sleep(1)
                    b, g = q1(blue, count)["n"], q1(green, count)["n"]  # type: ignore[index]
                checks.append(Check(f"{table} row count", b == g, f"BLUE {b:,}, GREEN {g:,}"))
            else:
                checks.append(verify_windows(ctx, blue, green, table, column, args.window_rows))
    if not print_checks(checks, args.json):
        raise SystemExit(1)


def verify_windows(
    ctx: Context, blue: psycopg.Connection, green: psycopg.Connection, table: str, column: str, window: int
) -> Check:
    """Row count and an md5 of every row's text in three windows of `window` rows: the oldest, the middle, and a
    recent one far enough back (20 windows) that its rows have mostly stopped changing. Windows start at ids that
    exist, so the jumps a cutover leaves in the ids (the sequence gap) don't leave them empty."""
    windows = window_bounds(blue, table, column, window)
    query = sql.SQL(
        "SELECT count(*) AS n, md5(string_agg(md5(t::text), '' ORDER BY t.{c})) AS h FROM {t} t "
        "WHERE t.{c} >= %s AND t.{c} < %s"
    ).format(c=sql.Identifier(column), t=sql.SQL(table))
    details = []
    for start, end in windows:
        b, g = q1(blue, query, (start, end)), q1(green, query, (start, end))
        if b != g:
            time.sleep(1)
            b, g = q1(blue, query, (start, end)), q1(green, query, (start, end))
        assert b is not None and g is not None
        if b != g:
            return Check(
                f"{table} id windows",
                False,
                f"[{start:,}, {end:,}): BLUE {b['n']:,} rows, GREEN {g['n']:,} rows, hashes differ "
                "(a row that keeps changing can cause this: run verify again)",
            )
        details.append(f"[{start:,}, {end:,}) {b['n']:,} rows")
    return Check(f"{table} id windows", True, "; ".join(details))


def window_bounds(blue: psycopg.Connection, table: str, column: str, window: int) -> list[tuple[int, int]]:
    """[start, end) id ranges on BLUE holding about `window` rows each: from the lowest id, from the median id
    (ANALYZE's histogram, so it's the median row, not the midpoint of the id range), and from 20 windows before the
    highest id. Every lookup walks the primary key index, so this stays cheap on big tables."""
    c, t = sql.Identifier(column), sql.SQL(table)
    first = q1(blue, sql.SQL("SELECT min({c}) AS lo, max({c}) AS hi FROM {t}").format(c=c, t=t))
    if not first or first["lo"] is None:
        return []
    starts = [first["lo"]]
    stats = q1(
        blue,
        """SELECT (s.histogram_bounds::text)::numeric[] AS bounds FROM pg_stats s
           JOIN pg_class r ON r.relname = s.tablename
           JOIN pg_namespace n ON n.oid = r.relnamespace AND n.nspname = s.schemaname
           WHERE r.oid = to_regclass(%s) AND s.attname = %s""",
        (table, column),
    )
    if stats and stats["bounds"]:
        median = int(stats["bounds"][len(stats["bounds"]) // 2])
        row = q1(blue, sql.SQL("SELECT min({c}) AS id FROM {t} WHERE {c} >= %s").format(c=c, t=t), (median,))
        if row and row["id"] is not None:
            starts.append(row["id"])
    # A small table has no row 20 windows from the end: its last window is the recent one
    for offset in (window * 20, window - 1):
        recent = q1(
            blue, sql.SQL("SELECT {c} AS id FROM {t} ORDER BY {c} DESC OFFSET %s LIMIT 1").format(c=c, t=t), (offset,)
        )
        if recent:
            starts.append(recent["id"])
            break
    bounds = []
    for start in sorted(set(starts)):
        row = q1(
            blue,
            sql.SQL("SELECT {c} AS id FROM {t} WHERE {c} >= %s ORDER BY {c} OFFSET %s LIMIT 1").format(c=c, t=t),
            (start, window),
        )
        bounds.append((start, row["id"] if row else first["hi"] + 1))
    return bounds


def cmd_sequences(ctx: Context, args: argparse.Namespace) -> None:
    """Moves GREEN's sequences to BLUE's last value + the gap, so ids GREEN issues after the switch can't collide
    with ids BLUE issued before it. BLUE isn't touched, and nothing needs resetting afterwards."""
    cfg = ctx.cfg
    with ctx.blue() as blue, ctx.green() as green:
        rows = db.plan_sequences(blue, green, cfg.sequence_gap, cfg.sequence_min_margin)
        for r in rows:
            action = f"set to {r['target']:,}" if r["needs_set"] else "ok"
            print(f"  {r['name']:<60} BLUE {r['blue']:>15,}  GREEN {r['green']:>15,}  {action}")
        bad = [r["name"] for r in rows if r["crosses_int4"] or r["exceeds_max"]]
        if bad:
            fail(
                f"the gap would push these past 2^31 or their maximum: {', '.join(bad)}. Use a smaller [sequences] gap."
            )
        todo = [r for r in rows if r["needs_set"]]
        if not todo:
            log(f"every GREEN sequence is at least {cfg.sequence_min_margin:,} above BLUE's")
            return
        if not args.execute:
            log(f"dry run: {len(todo)} sequences need setting. Run again with --execute.")
            return
        db.apply_sequences(green, todo)
        log(f"set {len(todo)} GREEN sequences to BLUE's value + {cfg.sequence_gap:,}")
