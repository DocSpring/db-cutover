"""Real PostgreSQL 16 -> 17 replication, using only disposable databases with synthetic rows."""

import os
import time
from types import SimpleNamespace

import psycopg
import pytest
from psycopg import sql
from psycopg.rows import dict_row

from db_cutover import db


@pytest.fixture(scope="module")
def databases():
    blue_dsn, green_dsn = os.environ.get("CUTOVER_TEST_BLUE_DSN"), os.environ.get("CUTOVER_TEST_GREEN_DSN")
    if not blue_dsn or not green_dsn:
        pytest.skip("requires disposable PostgreSQL containers; see tests/integration/README.md")
    with (
        psycopg.connect(blue_dsn, autocommit=True, row_factory=dict_row) as blue,
        psycopg.connect(green_dsn, autocommit=True, row_factory=dict_row) as green,
    ):
        assert db.q1(blue, "SELECT current_database() AS name")["name"] == "cutover_test"
        assert db.q1(green, "SELECT current_database() AS name")["name"] == "cutover_test"
        for conn in (blue, green):
            conn.execute("CREATE TABLE items (id bigserial PRIMARY KEY, value text NOT NULL)")
            conn.execute("CREATE TABLE probe (id bigserial PRIMARY KEY, source text, note text)")
        blue.execute("INSERT INTO items (value) SELECT 'synthetic-' || n FROM generate_series(1, 100) AS n")
        blue.execute("CREATE PUBLICATION cutover_pub FOR TABLE items, probe")
        host = os.environ.get("CUTOVER_TEST_SOURCE_HOST", "blue")
        connection = psycopg.conninfo.make_conninfo(
            host=host, dbname="cutover_test", user="postgres", password="synthetic-test-password", sslmode="disable"
        )
        green.execute(
            sql.SQL("CREATE SUBSCRIPTION cutover_sub CONNECTION {} PUBLICATION cutover_pub WITH (binary=false)").format(
                sql.Literal(connection)
            )
        )
        cfg = SimpleNamespace(
            database="cutover_test",
            publication="cutover_pub",
            subscription="cutover_sub",
            slot="cutover_sub",
            probe_table="probe",
        )
        deadline = time.monotonic() + 60
        while db.subscription_state(green, cfg.subscription)["tables_not_ready"]:
            if time.monotonic() > deadline:
                pytest.fail("initial copy did not complete")
            time.sleep(0.1)
        try:
            yield cfg, blue, green, host
        finally:
            green.execute("DROP SUBSCRIPTION cutover_sub")
            blue.execute("DROP PUBLICATION cutover_pub")
            for conn in (blue, green):
                conn.execute("DROP TABLE items, probe")


def test_marker_waits_for_insert_update_delete_across_major_versions(databases):
    cfg, blue, green, _ = databases
    blue.execute("UPDATE items SET value = 'updated' WHERE id = 1")
    blue.execute("DELETE FROM items WHERE id = 2")
    blue.execute("INSERT INTO items (value) VALUES ('new')")
    db.wait_for_marker(cfg, blue, green, timeout=10)
    assert db.q(blue, "SELECT * FROM items ORDER BY id") == db.q(green, "SELECT * FROM items ORDER BY id")


def test_unsupported_objects_block_the_workflow(databases):
    _, blue, _, _ = databases
    assert all(check.ok for check in db.supported_data_checks(blue))
    blue.execute("CREATE UNLOGGED TABLE scratch (id int)")
    try:
        assert not db.supported_data_checks(blue)[0].ok
    finally:
        blue.execute("DROP TABLE scratch")


def test_replication_contract_detects_missing_tables_and_wrong_source(databases):
    cfg, blue, green, host = databases
    assert all(check.ok for check in db.replication_checks(cfg, blue, green, host))
    assert not all(check.ok for check in db.replication_checks(cfg, blue, green, "wrong.example"))
    blue.execute("ALTER PUBLICATION cutover_pub DROP TABLE items")
    try:
        assert not db.replication_checks(cfg, blue, green, host)[0].ok
    finally:
        blue.execute("ALTER PUBLICATION cutover_pub ADD TABLE items")


def test_sequences_are_advanced_on_green_without_changing_blue(databases):
    cfg, blue, green, _ = databases
    db.wait_for_marker(cfg, blue, green, timeout=10)
    before = db.sequences(blue)
    plan = db.plan_sequences(blue, green, gap=1000, min_margin=100)
    db.apply_sequences(green, [row for row in plan if row["needs_set"]])
    assert db.sequences(blue) == before
    assert all(not row["needs_set"] for row in db.plan_sequences(blue, green, gap=1000, min_margin=100))
