# db-cutover

Upgrade a PostgreSQL database by copying it to a new instance, keeping that copy current with logical replication,
and switching application traffic through PgBouncer. The application keeps using the old database during the copy.
At the switch, PgBouncer holds new queries while in-flight transactions finish, then sends queued work to the new
instance.

This repository contains the Python CLI, an [example configuration](examples/config.toml), a
[step-by-step procedure](docs/procedure.md), and [design notes](docs/design.md). It is intended for a human operator
working with a coding agent. Each command has a narrow purpose and every command that changes database,
infrastructure, or PgBouncer state requires `--execute`.

**Release candidate:** failure-path tests and PostgreSQL replication tests do not replace a rehearsal on your
application's RDS and PgBouncer deployment. Read the requirements below before planning a cutover.

## Requirements

- PostgreSQL **16 or newer**, running on RDS. BLUE is the existing primary; GREEN is a separate instance at the
  target version. One database is migrated at a time. Test the specific source/target versions and extensions.
- PgBouncer in Kubernetes, with transaction pooling. Every foreground database client must use the selected pods.
  These commands pause/resume **all databases on those pods**, and RELOAD applies the entire configuration.
  Use dedicated pods for this workflow.
- A directory-mounted ConfigMap containing `pgbouncer.ini`, with no automatic reload. `subPath` mounts do not receive
  ConfigMap updates. Prevent rollouts, scaling and automatic restarts during the prepared-config window; see the
  [procedure](docs/procedure.md#7-prepare-and-switch).
- All connections bypassing PgBouncer must be stopped and prevented from reconnecting during the switch. Inventory
  consoles, schedulers, maintenance jobs, migrations and every application role, not just your web process.
- A schema freeze from `copy-schema` through `finish`. Roles, grants, extensions, settings and other databases need
  separate preparation. Large objects, unlogged tables, materialized views and foreign tables are blocked.
- Sequences with `INCREMENT BY 1`, no cycling, sufficient capacity, and application code that tolerates gaps in IDs.
- Request, proxy, job and PgBouncer timeouts longer than the measured pause. Queries wait during the switch;
  **zero failed requests depends on those timeouts and successful recovery**. An RDS reboot can take much longer.

## Install

Install Python 3.11+ and [uv](https://docs.astral.sh/uv/). Clone the repository and use its locked dependencies:

```sh
git clone https://github.com/DocSpring/db-cutover.git
cd db-cutover
uv sync --frozen
uv run db-cutover --help
```

For an operational run, check out a reviewed release tag or commit and keep it fixed throughout the migration.
A standalone installation can use `uv tool install .` from that checkout.

The machine running the tool also needs `aws`, `kubectl`, and matching, current `pg_dump`/`psql` clients at least as
new as **both** database servers. `op` is optional for 1Password references; Terraform is optional for the two HCL
helpers. It needs network access to both databases and the Kubernetes API. GREEN must reach BLUE for replication.

Copy the [example](examples/config.toml) to a private working directory, fill in your identifiers and contexts,
and supply credentials through environment variables or a secret manager. Never commit the operational config,
schema dumps, reports, Terraform state or logs. `cmd:` secret references execute shell commands: treat configs as
trusted executable input.

## Workflow

Read [the procedure](docs/procedure.md) for the provisioning, permissions, traffic controls and recovery steps
between these commands. In the examples below, `C` is a private config path and `TARGET_VERSION` is the RDS engine
version you have selected and tested. Prefix commands with `uv run` if you used the checkout installation.

```sh
db-cutover "$C" preflight
# Enable logical replication; reboot-blue --execute only if the static parameter needs it.
db-cutover "$C" prepare-blue --execute
db-cutover "$C" green-tf --engine-version "$TARGET_VERSION" --execute
# Review and provision GREEN, including version-compatible parameters and network access.
db-cutover "$C" copy-schema --execute
# Create application roles, grants and required extensions on GREEN.
db-cutover "$C" start-replication --execute
db-cutover "$C" wait-sync --execute
db-cutover "$C" verify --execute
db-cutover "$C" sequences --execute
# Stop and fence direct writers before the rehearsal.
db-cutover "$C" cutover --execute --rehearse
# Prepare PgBouncer's config as described in the procedure, without reloading it.
db-cutover "$C" cutover
db-cutover "$C" cutover --execute
# Move direct writers to GREEN and prevent any application writes to BLUE.
db-cutover "$C" finish --execute
db-cutover "$C" promote-tf --execute
# Review and run the printed state moves, then verify the Terraform plan.
```

If health endpoints require authentication, set `[secrets] health_check_token`; `watch` sends it as
`X-Health-Check-Token` to every configured health URL. Only configure trusted HTTPS endpoints. Redirects are reported
as failures rather than followed, so the header cannot be forwarded to a different endpoint.

Use `watch` to monitor health/replication and `probe --execute` to write synthetic probe rows through PgBouncer.
Start the probe after `prepare-blue`; stop it before `finish` drops its table. `verify` without `--execute` is
read-only; `--execute` adds a marker write to test replication. Dry runs can connect, read data, create local state
files, and consume database resources. They do not pause clients or modify remote state.

## Working with an agent

Give the agent [AGENTS.md](AGENTS.md), the config path, the approved versions, the traffic controls and the
operator's authorization boundaries. Have it run each changing command without `--execute` first, explain the
output, and follow the operator's agreed execution policy. A failed gate is a stop condition.

Exit status 0 means the command completed its requested mode, 1 means failure, 2 means an argument/config error,
and 130 means interruption. A dry-run success is not a completed migration. Long-running monitors continue until
stopped; quiet output alone is not proof that a monitor is still alive.

Before RELOAD, a cutover failure attempts to resume BLUE and reports any recovery failure. After RELOAD starts,
unknown or mixed targets **remain paused** and the command fails. An operator must align every pod to GREEN before
resuming. Never interpret replication or a sequence gap as permission to write to both databases.

## Development

```sh
uv sync --frozen --group dev
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv build
```

The default suite uses no cloud credentials. [Integration tests](tests/integration/README.md) use disposable
PostgreSQL 16 and 17 containers with synthetic data. CI runs both suites. See [SECURITY.md](SECURITY.md) for data
handling and release checks. Licensed under [MIT](LICENSE).
