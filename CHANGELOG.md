# Changelog

## 0.2.0 (2026-11-16)

The first release on PyPI.

- Install with `uv tool install db-cutover` or `pipx install db-cutover`. `db-cutover --version` prints the version.
- Add an agent skill (`skills/db-cutover`) for planning, rehearsing and running a cutover with an operator: each
  command and its dry run, the stop points, the rehearsal checklist and recovery. It ships inside the package:
  `db-cutover skill` prints its directory and `db-cutover skill --install DIR` copies it into an agent's skills
  directory.
- README: who the tool is for, a rehearsal walkthrough, and its guarantees and limits.
- Stop and require recovery after a partial or unknown PgBouncer reload. Never resume mixed BLUE/GREEN targets.
  Report failed resumes and post-switch verification failures with unsuccessful exit status.
- Require `--execute` for `probe`, `wait-sync`, and `pgbouncer resume`. `verify` is read-only by default;
  `verify --execute` adds a replication marker. Cutover and finish dry runs no longer write markers.
- Remove `cutover --allow-direct`: every client role must drain before switching. Failed deployment lookups block
  the operation. Reboot's separate `--allow-direct` option still explicitly permits direct-client errors.
- Default to text replication. Validate publication coverage, subscription identity, sequence limits and marker
  tokens. Reject unsupported data objects before switching.
- Withhold PostgreSQL/subprocess/secret-helper diagnostics that may expose credentials or row values. Remove query
  text and health response bodies from inspection output; keep state files in a private directory.
- Bound port-forward startup and settle pending admin commands before confirming recovery from a late PAUSE.
- Add synthetic fault tests, PostgreSQL 16-to-17 integration tests, an MIT license, standalone examples and an
  expanded operator/agent runbook. Infrastructure and application rehearsals remain required before live use.
