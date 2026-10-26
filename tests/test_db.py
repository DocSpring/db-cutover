import pytest

from db_cutover.commands.replication import normalize_dump
from db_cutover.db import INT4_MAX, compare_sequences
from db_cutover.pgbouncer import parse_database_host


def seq(last: int, max_value: int = 2**63 - 1) -> dict:
    return {"last_value": last, "max_value": max_value, "increment_by": 1}


def test_green_far_behind_needs_set() -> None:
    [row] = compare_sequences({"s": seq(5_000)}, {"s": seq(0)}, gap=1_000_000, min_margin=100_000)
    assert row["needs_set"]
    assert row["target"] == 1_005_000


def test_green_already_ahead() -> None:
    [row] = compare_sequences({"s": seq(5_000)}, {"s": seq(1_005_000)}, gap=1_000_000, min_margin=100_000)
    assert not row["needs_set"]


def test_blue_caught_up_with_the_margin() -> None:
    # GREEN was set earlier; BLUE has since issued 950k ids, so GREEN is now only 50k ahead: move it again.
    [row] = compare_sequences({"s": seq(955_000)}, {"s": seq(1_005_000)}, gap=1_000_000, min_margin=100_000)
    assert row["needs_set"]
    assert row["target"] == 1_955_000


def test_crossing_int4() -> None:
    [row] = compare_sequences({"s": seq(INT4_MAX - 10)}, {"s": seq(0)}, gap=1_000_000, min_margin=100_000)
    assert row["crosses_int4"]
    [row] = compare_sequences({"s": seq(INT4_MAX + 10)}, {"s": seq(0)}, gap=1_000_000, min_margin=100_000)
    assert not row["crosses_int4"]  # already past: int4 columns can't hold it either way


def test_exceeds_max() -> None:
    [row] = compare_sequences({"s": seq(10, max_value=100)}, {"s": seq(0)}, gap=1_000, min_margin=10)
    assert row["exceeds_max"]


def test_sequence_sets_differ() -> None:
    with pytest.raises(ValueError, match="differ"):
        compare_sequences({"a": seq(1)}, {"b": seq(1)}, gap=1, min_margin=1)


INI = """
[databases]
other = host=other.example port=5432
app = host=blue.abc.us-east-1.rds.amazonaws.com port=5432 dbname=app

[pgbouncer]
app = host=not-a-database
"""


def test_parse_database_host() -> None:
    assert parse_database_host(INI, "app") == "blue.abc.us-east-1.rds.amazonaws.com"
    assert parse_database_host(INI, "other") == "other.example"
    assert parse_database_host(INI, "missing") is None
    assert parse_database_host(INI, "ap") is None


def test_normalize_dump() -> None:
    a = "--\n-- PostgreSQL database dump\n--\n\\restrict abc\nCREATE TABLE t (id bigint);\n\n\\unrestrict abc\n"
    b = "-- Dumped by pg_dump version 17.7\n\\restrict xyz\n\nCREATE TABLE t (id bigint);\n\\unrestrict xyz\n"
    assert normalize_dump(a) == normalize_dump(b) == "CREATE TABLE t (id bigint);"


AUDITS = """CREATE TABLE public.audits (
    id bigint NOT NULL,
    action character varying,
    associated_id integer,
    associated_type character varying,
    auditable_id integer,
    user_id integer,
    username character varying,
    version integer DEFAULT 0
);
CREATE TABLE public.users (
    id bigint NOT NULL,
    user_id integer
);
CREATE INDEX auditable_index ON public.audits USING btree (auditable_id, auditable_type);"""

AUDIT_TYPES = {
    "public.audits.auditable_id": "bigint",
    "public.audits.associated_id": "bigint",
    "public.audits.user_id": "bigint",
}


def test_expected_green_changes_only_the_listed_columns() -> None:
    from db_cutover.commands.replication import expected_green

    out = expected_green(AUDITS, AUDIT_TYPES)
    assert "    associated_id bigint," in out
    assert "    auditable_id bigint," in out
    assert "    user_id bigint,\n    username" in out  # audits.user_id
    assert "    version integer DEFAULT 0" in out  # not listed
    assert "    id bigint NOT NULL," in out
    assert "CREATE TABLE public.users (\n    id bigint NOT NULL,\n    user_id integer\n);" in out  # other table
    assert out.endswith("USING btree (auditable_id, auditable_type);")
    assert expected_green(AUDITS, {}) == AUDITS


def test_expected_green_keeps_constraints() -> None:
    from db_cutover.commands.replication import expected_green

    text = "CREATE TABLE public.t (\n    n integer DEFAULT 0 NOT NULL,\n    m integer NOT NULL\n);"
    out = expected_green(text, {"public.t.n": "bigint", "public.t.m": "bigint"})
    assert out == "CREATE TABLE public.t (\n    n bigint DEFAULT 0 NOT NULL,\n    m bigint NOT NULL\n);"


def test_column_type_sql() -> None:
    from db_cutover.commands.replication import column_type_sql

    assert column_type_sql(AUDIT_TYPES) == [
        "ALTER TABLE public.audits ALTER COLUMN auditable_id TYPE bigint, ALTER COLUMN associated_id TYPE bigint, "
        "ALTER COLUMN user_id TYPE bigint;"
    ]
