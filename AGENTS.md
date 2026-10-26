# db_cutover_orchestrator

`db-cutover`: a Python CLI for zero-downtime blue/green PostgreSQL cutovers on RDS (logical replication plus a
PgBouncer switch). `README.md` covers use, `docs/procedure.md` the steps of a cutover, `docs/design.md` the
reasoning. Read `SECURITY.md` before handling operational configuration or logs.

## Layout

- `src/db_cutover/cli.py`: argument parsing; one subcommand per step.
- `config.py`: the TOML config (`examples/config.toml` documents every key; keep it in sync).
- `db.py`: connections and catalog queries. `pgbouncer.py`: the admin console of every pod through
  `kubectl port-forward`. `shell.py`: aws, kubectl, pg_dump, psql. `output.py`: log lines, checks, JSON.
- `commands/`: `inspect.py` (read-only and long-running), `blue.py`, `replication.py`, `switch.py` (cutover,
  finish), `terraform.py` (green-tf, promote-tf).

## Rules

- Every command that changes anything is a dry run unless `--execute` is passed. Keep it that way for new commands.
- Never print a secret or a connection string that holds one. Secrets go to subprocesses through the environment
  (`PGPASSWORD`), never the command line.
- The switch (`commands/switch.py`) must resume on BLUE for any failure before RELOAD, including Ctrl-C. Don't add
  steps between PAUSE and RESUME without measuring the pause on a staging run.
- After RELOAD starts, never resume mixed or unknown targets. Report failed resumes as failures. A sequence gap
  and one-way replication cannot make concurrent writers on BLUE and GREEN safe.
- Probe writes, replication markers and manual RESUME require `--execute`, including retry paths.
- Inspect all client roles when draining BLUE. A failed deployment lookup must not be interpreted as zero replicas.
- Never print query text, server row diagnostics or a secret helper's stderr. Keep fixtures synthetic and review
  all history before public release; removing a value from the current tree does not remove it from history.
- Logic that can be pure (sequence planning, HCL edits, ini parsing) stays pure and has tests in `tests/`.
- Long-running commands print one line per change, not per poll, so they work as background monitors.
- Before committing: `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .` (CI runs the same).
- When behaviour changes, update `README.md`, `docs/procedure.md` and `docs/design.md` in the same commit.
