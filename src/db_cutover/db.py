"""Connections to BLUE, GREEN and PgBouncer, and the catalog queries the steps share."""

from __future__ import annotations

import socket
import time
import uuid
from typing import Any

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from db_cutover.config import Config
from db_cutover.output import Check, fail, log
from db_cutover.shell import Aws

APPLICATION_NAME = "db_cutover"  # our sessions; excluded when counting the app's connections
INT4_MAX = 2**31 - 1

USER_SCHEMAS = "n.nspname NOT IN ('pg_catalog', 'information_schema') AND n.nspname !~ '^pg_toast'"
QUALIFIED = "quote_ident(n.nspname) || '.' || quote_ident(c.relname)"


class Context:
    """Everything a step needs: the config, AWS, and connections. One per command."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.aws = Aws(cfg)
        cfg.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        cfg.state_dir.chmod(0o700)

    # ---- instances

    @property
    def blue_id(self) -> str:
        if not self.cfg.blue.identifier:
            fail("BLUE's identifier is not set (or its Terraform resource wasn't found)")
        return self.cfg.blue.identifier

    @property
    def green_id(self) -> str:
        if not self.cfg.green.identifier:
            fail("GREEN's identifier is not set: create GREEN first (green-tf, then a Terraform apply)")
        return self.cfg.green.identifier

    @property
    def has_green(self) -> bool:
        return bool(self.cfg.green.identifier and self.aws.endpoint(self.cfg.green.identifier))

    @property
    def blue_endpoint(self) -> str:
        endpoint = self.aws.endpoint(self.blue_id)
        if not endpoint:
            fail(f"BLUE {self.blue_id} doesn't exist or has no endpoint")
        return endpoint

    @property
    def green_endpoint(self) -> str:
        endpoint = self.aws.endpoint(self.green_id)
        if not endpoint:
            fail(f"GREEN {self.green_id} doesn't exist yet or has no endpoint")
        return endpoint

    @property
    def blue_host(self) -> str:
        return self.cfg.blue.host or self.blue_endpoint

    @property
    def green_host(self) -> str:
        return self.cfg.green.host or self.green_endpoint

    # ---- connections

    def connect(
        self,
        host: str,
        *,
        user: str | None = None,
        password: str | None = None,
        dbname: str | None = None,
        timeout: int = 10,
    ) -> psycopg.Connection:
        return psycopg.connect(
            host=host,
            port=5432,
            user=user or self.cfg.admin_user,
            password=password if password is not None else self.cfg.secret("admin_password"),
            dbname=dbname or self.cfg.database,
            sslmode="require",
            connect_timeout=timeout,
            application_name=APPLICATION_NAME,
            autocommit=True,
            row_factory=dict_row,
            prepare_threshold=None,  # PgBouncer in transaction mode
        )

    def blue(self, **kw: Any) -> psycopg.Connection:
        return self.connect(self.blue_host, **kw)

    def green(self, **kw: Any) -> psycopg.Connection:
        return self.connect(self.green_host, **kw)

    def pgbouncer_app(self, **kw: Any) -> psycopg.Connection:
        if not self.cfg.pgbouncer.host:
            fail("[pgbouncer] host is not set (needed to connect through PgBouncer as the app)")
        return self.connect(
            self.cfg.pgbouncer.host, user=self.cfg.app_user, password=self.cfg.secret("app_password"), **kw
        )


def q(conn: psycopg.Connection, query: str | sql.Composable, params: Any = None) -> list[dict[str, Any]]:
    cur = conn.execute(query, params)
    return cur.fetchall() if cur.description else []


def q1(conn: psycopg.Connection, query: str | sql.Composable, params: Any = None) -> dict[str, Any] | None:
    rows = q(conn, query, params)
    return rows[0] if rows else None


def ddl(
    conn: psycopg.Connection,
    statement: str | sql.Composable,
    lock_timeout_ms: int = 2000,
    attempts: int = 30,
    retry_seconds: float = 10,
) -> None:
    """Runs a statement that locks a live table (ALTER TABLE takes ACCESS EXCLUSIVE) on BLUE. While it waits for
    its lock, every later query on that table queues behind it, so a long transaction holding the table would stall
    the app. lock_timeout caps that wait: the statement gives up, the queue drains, and it tries again later."""
    for attempt in range(1, attempts + 1):
        try:
            with conn.transaction():
                conn.execute(sql.SQL("SET LOCAL lock_timeout = {}").format(sql.Literal(f"{lock_timeout_ms}ms")))
                conn.execute(statement)
            return
        except psycopg.errors.LockNotAvailable:
            if attempt == attempts:
                fail(
                    f"no lock within {lock_timeout_ms} ms in {attempts} attempts: a long transaction holds the table "
                    "(status lists BLUE's long transactions). Nothing changed; run again later."
                )
            log(
                f"lock not free within {lock_timeout_ms} ms (attempt {attempt}/{attempts}); "
                f"retrying in {retry_seconds:.0f}s"
            )
            time.sleep(retry_seconds)


def resolve(host: str) -> set[str]:
    try:
        return {info[4][0] for info in socket.getaddrinfo(host, 5432, proto=socket.IPPROTO_TCP)}
    except socket.gaierror:
        return set()


def database_size(conn: psycopg.Connection) -> int:
    return q1(conn, "SELECT pg_database_size(current_database()) AS b")["b"]  # type: ignore[index]


def list_tables(conn: psycopg.Connection) -> list[str]:
    rows = q(
        conn,
        f"""SELECT {QUALIFIED} AS name
            FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relkind IN ('r', 'p') AND NOT c.relispartition AND {USER_SCHEMAS}
            ORDER BY 1""",
    )
    return [r["name"] for r in rows]


def tables_without_replica_identity(conn: psycopg.Connection) -> list[str]:
    """Tables whose UPDATEs and DELETEs fail while they're in a publication: no primary key, and REPLICA IDENTITY
    DEFAULT."""
    rows = q(
        conn,
        f"""SELECT {QUALIFIED} AS name
            FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relkind = 'r' AND {USER_SCHEMAS} AND c.relreplident = 'd'
              AND NOT EXISTS (SELECT 1 FROM pg_index i WHERE i.indrelid = c.oid AND i.indisprimary)
            ORDER BY 1""",
    )
    return [r["name"] for r in rows]


def integer_pk_tables(conn: psycopg.Connection) -> list[tuple[str, str]]:
    rows = q(
        conn,
        f"""SELECT {QUALIFIED} AS table_name, a.attname AS column_name
            FROM pg_index i
            JOIN pg_class c ON c.oid = i.indrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = i.indkey[0]
            WHERE i.indisprimary AND i.indnatts = 1 AND {USER_SCHEMAS}
              AND a.atttypid IN ('int2'::regtype, 'int4'::regtype, 'int8'::regtype)
            ORDER BY 1""",
    )
    return [(r["table_name"], r["column_name"]) for r in rows]


def max_ids(conn: psycopg.Connection, tables: list[tuple[str, str]]) -> dict[str, int]:
    """max(id) of every table in one round trip (each one an index lookup)."""
    if not tables:
        return {}
    parts = [
        sql.SQL("SELECT {name} AS t, (SELECT max({col}) FROM {tbl})::bigint AS m").format(
            name=sql.Literal(t), col=sql.Identifier(c), tbl=sql.SQL(t)
        )
        for t, c in tables
    ]
    return {r["t"]: r["m"] or 0 for r in q(conn, sql.SQL(" UNION ALL ").join(parts))}


def sequences(conn: psycopg.Connection) -> dict[str, dict[str, Any]]:
    """Every user sequence: last value (start - increment if never used), increment, limit, owning table."""
    rows = q(
        conn,
        """SELECT quote_ident(s.schemaname) || '.' || quote_ident(s.sequencename) AS name,
                  COALESCE(s.last_value, s.start_value - s.increment_by) AS last_value,
                  s.increment_by, s.max_value, s.cycle, s.data_type::text AS data_type,
                  (SELECT quote_ident(tn.nspname) || '.' || quote_ident(t.relname)
                     FROM pg_class sc JOIN pg_namespace sn ON sn.oid = sc.relnamespace
                     JOIN pg_depend d ON d.objid = sc.oid AND d.classid = 'pg_class'::regclass
                      AND d.refclassid = 'pg_class'::regclass AND d.deptype IN ('a', 'i')
                     JOIN pg_class t ON t.oid = d.refobjid JOIN pg_namespace tn ON tn.oid = t.relnamespace
                    WHERE sc.relname = s.sequencename AND sn.nspname = s.schemaname LIMIT 1) AS owner_table
           FROM pg_sequences s
           WHERE s.schemaname NOT IN ('pg_catalog', 'information_schema')
           ORDER BY 1""",
    )
    return {r["name"]: r for r in rows}


def plan_sequences(
    blue: psycopg.Connection, green: psycopg.Connection, gap: int, min_margin: int
) -> list[dict[str, Any]]:
    return compare_sequences(sequences(blue), sequences(green), gap, min_margin)


def compare_sequences(
    blue_seqs: dict[str, dict[str, Any]], green_seqs: dict[str, dict[str, Any]], gap: int, min_margin: int
) -> list[dict[str, Any]]:
    """For each sequence: does GREEN need to jump to BLUE's last value + gap?

    GREEN must stay above every id BLUE has issued or will issue before the last write reaches it. A sequence
    needs setting when GREEN is less than min_margin above BLUE. Pure function, so it's unit tested."""
    differ = set(blue_seqs) ^ set(green_seqs)
    if differ:
        raise ValueError(f"sequences differ between BLUE and GREEN: {sorted(differ)}")
    rows = []
    for name, b in sorted(blue_seqs.items()):
        g = green_seqs[name]
        if b["increment_by"] != 1 or g["increment_by"] != 1 or b.get("cycle") or g.get("cycle"):
            raise ValueError(f"sequence {name} must use INCREMENT BY 1 and NO CYCLE on both sides")
        target = b["last_value"] + gap
        rows.append(
            {
                "name": name,
                "blue": b["last_value"],
                "green": g["last_value"],
                "target": target,
                "needs_set": g["last_value"] < b["last_value"] + min_margin,
                "exceeds_max": target > min(b["max_value"], g["max_value"]),
                "crosses_int4": b["last_value"] <= INT4_MAX < target,
            }
        )
    return rows


def apply_sequences(green: psycopg.Connection, rows: list[dict[str, Any]]) -> None:
    """Sets every sequence in one statement: inside the switch, each round trip is time clients wait."""
    if not rows:
        return
    if any(r["crosses_int4"] or r["exceeds_max"] for r in rows):
        fail("sequence update would cross 2^31 or a sequence maximum; review the configured gap")
    calls = sql.SQL(", ").join(
        sql.SQL("setval({}::regclass, {}, true)").format(sql.Literal(r["name"]), sql.Literal(r["target"])) for r in rows
    )
    green.execute(sql.SQL("SELECT {}").format(calls))


def schema_migrations(conn: psycopg.Connection) -> str | None:
    """Latest Rails migration, if the database has a schema_migrations table (a cheap schema-drift check)."""
    row = q1(conn, "SELECT to_regclass('public.schema_migrations') IS NOT NULL AS present")
    if not row or not row["present"]:
        return None
    r = q1(conn, "SELECT max(version) AS v, count(*) AS n FROM public.schema_migrations")
    return f"{r['v']} ({r['n']} migrations)" if r else None


def subscription_state(green: psycopg.Connection, name: str) -> dict[str, Any] | None:
    sub = q1(
        green,
        """SELECT s.oid, s.subenabled, s.subslotname, s.subbinary, s.substream::text AS substream,
                  st.pid IS NOT NULL AS worker_running, st.received_lsn::text, st.latest_end_lsn::text,
                  st.latest_end_time,
                  COALESCE(ss.apply_error_count, 0) AS apply_errors, COALESCE(ss.sync_error_count, 0) AS sync_errors
           FROM pg_subscription s
           LEFT JOIN pg_stat_subscription st ON st.subid = s.oid AND st.relid IS NULL
           LEFT JOIN pg_stat_subscription_stats ss ON ss.subid = s.oid
           WHERE s.subname = %s""",
        (name,),
    )
    if not sub:
        return None
    states = q(
        green,
        """SELECT sr.srsubstate AS state, count(*) AS n,
                  array_agg(sr.srrelid::regclass::text ORDER BY sr.srrelid::regclass::text) AS tables
           FROM pg_subscription_rel sr WHERE sr.srsubid = %s GROUP BY 1""",
        (sub["oid"],),
    )
    sub["table_states"] = {r["state"]: r["n"] for r in states}
    sub["tables_not_ready"] = sorted(t for r in states if r["state"] != "r" for t in r["tables"])
    sub["copying"] = q(
        green,
        """SELECT relid::regclass::text AS table_name, bytes_processed, bytes_total, tuples_processed
           FROM pg_stat_progress_copy WHERE datname = current_database()""",
    )
    return sub


def slot_state(blue: psycopg.Connection, slot: str) -> dict[str, Any] | None:
    return q1(
        blue,
        """SELECT slot_name, active, wal_status,
                  pg_wal_lsn_diff(pg_current_wal_lsn(), confirmed_flush_lsn)::bigint AS lag_bytes,
                  pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn)::bigint AS retained_bytes
           FROM pg_replication_slots WHERE slot_name = %s""",
        (slot,),
    )


def replication_checks(cfg: Config, blue: psycopg.Connection, green: psycopg.Connection, endpoint: str) -> list[Check]:
    """A marker proves progress only for the subscription and full publication we intend to switch."""
    pub = q1(
        blue,
        "SELECT pubinsert, pubupdate, pubdelete, pubtruncate, pubviaroot FROM pg_publication WHERE pubname = %s",
        (cfg.publication,),
    )
    tables = q(
        blue,
        "SELECT schemaname, tablename, rowfilter, attnames FROM pg_publication_tables WHERE pubname = %s",
        (cfg.publication,),
    )
    expected = q(
        blue,
        f"""SELECT n.nspname AS schemaname, c.relname AS tablename,
                   array_agg(a.attname::text ORDER BY a.attnum) AS attnames
            FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
            WHERE c.relkind = 'r' AND c.relpersistence = 'p' AND {USER_SCHEMAS}
            GROUP BY n.nspname, c.relname""",
    )

    def columns(rows):
        return {(r["schemaname"], r["tablename"]): set(r["attnames"]) for r in rows}

    complete = bool(pub and all(pub[k] for k in ("pubinsert", "pubupdate", "pubdelete", "pubtruncate")))
    complete = complete and not pub["pubviaroot"] and not any(r["rowfilter"] for r in tables)
    complete = complete and columns(tables) == columns(expected)
    sub = q1(
        green,
        "SELECT subpublications, subslotname, substream::text, subconninfo FROM pg_subscription WHERE subname = %s",
        (cfg.subscription,),
    )
    # Never put subconninfo into diagnostics: it contains the replication password.
    connection = psycopg.conninfo.conninfo_to_dict(sub["subconninfo"]) if sub else {}
    matches = bool(
        sub
        and sub["subpublications"] == [cfg.publication]
        and sub["subslotname"] == cfg.slot
        and sub["substream"] in ("f", "t")
        and connection.get("host") == endpoint
        and connection.get("dbname") == cfg.database
    )
    return [
        Check("publication contains every persistent table and column, without filters, for every change", complete),
        Check("subscription uses the intended BLUE database, publication, slot and serial apply", matches),
    ]


def supported_data_checks(conn: psycopg.Connection) -> list[Check]:
    unsupported = q(
        conn,
        f"""SELECT {QUALIFIED} AS name FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE {USER_SCHEMAS} AND (c.relkind IN ('m', 'f') OR c.relpersistence = 'u') ORDER BY 1""",
    )
    large_objects = q1(conn, "SELECT EXISTS (SELECT 1 FROM pg_largeobject_metadata) AS present")
    return [
        Check(
            "no unlogged tables, materialized views or foreign tables (not copied by this workflow)",
            not unsupported,
            ", ".join(r["name"] for r in unsupported),
        ),
        Check("no PostgreSQL large objects (not logically replicated)", not large_objects["present"]),
    ]


def client_sessions(conn: psycopg.Connection) -> list[dict[str, Any]]:
    """Client sessions on this database, except this tool's."""
    return q(
        conn,
        """SELECT pid, usename, application_name, client_addr::text, state, backend_type,
                  now() - backend_start AS connected_for, now() - xact_start AS xact_age
           FROM pg_stat_activity
           WHERE datname = current_database() AND backend_type = 'client backend'
             AND pid <> pg_backend_pid() AND application_name <> %s
           ORDER BY usename, application_name""",
        (APPLICATION_NAME,),
    )


def summarize_sessions(sessions: list[dict[str, Any]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for s in sessions:
        key = f"{s['usename']}:{s['application_name'] or '-'}"
        out[key] = out.get(key, 0) + 1
    return out


def long_transactions(conn: psycopg.Connection, seconds: float) -> list[dict[str, Any]]:
    return q(
        conn,
        """SELECT pid, usename, application_name, state, now() - xact_start AS xact_age
           FROM pg_stat_activity
           WHERE datname = current_database() AND xact_start IS NOT NULL AND pid <> pg_backend_pid()
             AND backend_type = 'client backend' AND now() - xact_start > make_interval(secs => %s)
           ORDER BY xact_start""",
        (seconds,),
    )


def write_counter(conn: psycopg.Connection, exclude_table: str) -> int:
    """Rows inserted, updated and deleted in this database (cumulative), not counting the probe table."""
    row = q1(
        conn,
        """SELECT COALESCE(sum(n_tup_ins + n_tup_upd + n_tup_del), 0)::bigint AS n
           FROM pg_stat_user_tables WHERE relname <> %s""",
        (exclude_table,),
    )
    return int(row["n"]) if row else 0


def table_exists(conn: psycopg.Connection, qualified: str) -> bool:
    row = q1(conn, "SELECT to_regclass(%s) IS NOT NULL AS present", (qualified,))
    return bool(row and row["present"])


def privilege_gaps(conn: psycopg.Connection, role: str) -> list[str]:
    """Tables and sequences `role` can't fully use."""
    rows = q(
        conn,
        f"""SELECT {QUALIFIED} AS name, c.relkind
            FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE {USER_SCHEMAS} AND (
              (c.relkind IN ('r', 'p') AND NOT (
                  has_table_privilege(%(r)s, c.oid, 'SELECT') AND has_table_privilege(%(r)s, c.oid, 'INSERT')
                  AND has_table_privilege(%(r)s, c.oid, 'UPDATE') AND has_table_privilege(%(r)s, c.oid, 'DELETE')))
              OR (c.relkind = 'S' AND NOT has_sequence_privilege(%(r)s, c.oid, 'USAGE')))
            ORDER BY 1""",
        {"r": role},
    )
    return [f"{r['name']} ({'sequence' if r['relkind'] == 'S' else 'table'})" for r in rows]


def wait_for_marker(cfg: Config, blue: psycopg.Connection, green: psycopg.Connection, timeout: float) -> float:
    """Commits a marker row on BLUE and waits until GREEN has applied it; returns the seconds it took.

    Replication applies BLUE's transactions in commit order, so once the marker is on GREEN, everything BLUE
    committed before it is there too. (Comparing LSNs doesn't work: RDS writes WAL for other databases, which never
    shows up in this subscription's progress.)"""
    started = time.monotonic()
    table = sql.Identifier(cfg.probe_table)
    note = f"marker {uuid.uuid4()}"
    row = q1(
        blue,
        sql.SQL("INSERT INTO public.{} (source, note) VALUES ('marker', %s) RETURNING id").format(table),
        (note,),
    )
    marker_id = row["id"]  # type: ignore[index]
    query = sql.SQL("SELECT 1 FROM public.{} WHERE id = %s AND source = 'marker' AND note = %s").format(table)
    while time.monotonic() - started < timeout:
        if q(green, query, (marker_id, note)):
            return time.monotonic() - started
        time.sleep(0.02)
    raise TimeoutError(f"marker {marker_id} didn't reach GREEN within {timeout:.0f}s")
