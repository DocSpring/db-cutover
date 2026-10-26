# Disposable PostgreSQL integration tests

These tests create and drop tables and replication objects in databases named `cutover_test`. Use only the
containers below. They verify PostgreSQL 16 to 17 text replication, the marker, publication coverage and sequence
updates. They do not exercise RDS, Kubernetes, PgBouncer's network behavior or application timeouts.

```sh
docker compose -p db-cutover-tests -f tests/integration/compose.yml up -d --wait
CUTOVER_TEST_BLUE_DSN='host=127.0.0.1 port=55432 dbname=cutover_test user=postgres password=synthetic-test-password' \
CUTOVER_TEST_GREEN_DSN='host=127.0.0.1 port=55433 dbname=cutover_test user=postgres password=synthetic-test-password' \
  uv run pytest tests/integration
docker compose -p db-cutover-tests -f tests/integration/compose.yml down -v
```

The password is a public test fixture. Both exposed ports bind only to loopback. The tests skip unless both DSN
variables are set. Do not use production credentials or copy production data into these containers.
