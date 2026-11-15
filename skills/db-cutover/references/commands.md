# Commands

Every command is `db-cutover CONFIG COMMAND [options]`. `db-cutover CONFIG COMMAND --help` prints a command's
options. Commands marked "changes" do nothing but print what they would do until you pass `--execute`.

A dry run still connects to the databases, reads from them, and can write files to the local state directory. It
never pauses clients or changes anything remote.

## Contents

- [Read-only](#read-only)
- [Monitors](#monitors)
- [Before replication](#before-replication)
- [Replication](#replication)
- [The switch](#the-switch)
- [Terraform helpers](#terraform-helpers)
- [Without a config](#without-a-config)

## Read-only

| Command | What it shows |
| --- | --- |
| `env` | The identifiers, hosts and instance settings the config resolves to. Run it first to check the config. |
| `preflight [--json]` | Every check that must pass before starting: client tools, BLUE's version, `wal_level`, free replication slots, unsupported objects, sequences, storage, PgBouncer. |
| `status [--json]` | One snapshot of BLUE, GREEN, the replication slot, the subscription and each PgBouncer pod. Use it to find out which step you're in. |
| `pgbouncer pools\|servers\|clients\|databases\|stats` | `SHOW ...` on every selected pod. |
| `pgbouncer config` | For each pod, the host it runs with and the host in its `pgbouncer.ini`. Before the switch every pod must say running BLUE, file GREEN. |
| `verify` | Compares BLUE and GREEN: schema, migration metadata, grants, row counts, and hashes of sampled rows. See [Replication](#replication). |

`preflight` fails the first time on steps you haven't done yet (`wal_level`, the probe table, GREEN). That is
expected. Read each failure and match it to the step that fixes it.

## Monitors

Both run until stopped. Start them in the background.

| Command | Changes | What it does |
| --- | --- | --- |
| `watch` | No | Requests the config's health URLs every 5 seconds and checks the databases, the subscription and PgBouncer every 30. Prints a line when something changes and an `ALERT` line for a failed or slow health check, a database it can't reach, a disabled subscription, a rising replication error count, or BLUE keeping more than `--max-retained-gb` of WAL. |
| `probe --execute` | Yes: writes rows to the probe table | Writes a row through PgBouncer as the application's role every half second. Prints an `ALERT` for each failed write, a line for each slow one, a line when writes start landing on a different database, and totals once a minute. |

The probe needs the table that `prepare-blue` creates. Stop it before `finish`.

## Before replication

| Command | Changes | Notes |
| --- | --- | --- |
| `reboot-blue` | Reboots BLUE | Only when BLUE's parameter group is `pending-reboot` (after setting `rds.logical_replication = 1`). Pauses PgBouncer, reboots, and resumes when BLUE accepts connections or after `--max-pause` seconds (default 90), whichever is first. Refuses while direct writers are connected. |
| `prepare-blue` | DDL on BLUE | Creates the probe table and sets `REPLICA IDENTITY FULL` on tables without a primary key. Safe to run again. |

`reboot-blue` options to leave alone unless the operator asks: `--force` (reboot with nothing pending),
`--allow-direct` (accept that direct connections will fail), `--failover` (fail over to the Multi-AZ standby
instead of restarting in place, which pauses for longer).

## Replication

| Command | Changes | Notes |
| --- | --- | --- |
| `copy-schema` | Creates the database and schema on GREEN | GREEN must be empty. Leaves out grants, ownership, publications and subscriptions. Applies `[schema] column_types` from the config. |
| `start-replication` | Publication on BLUE, subscription on GREEN | Starts the initial copy. BLUE keeps WAL until GREEN has applied it, so watch BLUE's free storage. Run it again with `--execute` to re-enable a subscription that stopped. |
| `wait-sync` | Writes a marker row on BLUE | Blocks until every table is ready and the marker reaches GREEN. `--timeout` is in minutes, 0 waits forever. |
| `verify` | With `--execute`: writes a marker row | Read-only without `--execute`. Not a full comparison: writes that land during the check can show as differences that go away. A difference that stays needs the operator. `--tables` limits it to some tables. |
| `sequences` | Sets sequence values on GREEN | Moves each GREEN sequence to BLUE's last value plus the config's gap. BLUE isn't touched. |

If replication errors rise, the cause is in GREEN's PostgreSQL log, which the operator reads. Never drop the slot
or the subscription to clear an alert: GREEN would have to be rebuilt.

## The switch

| Command | Changes | Notes |
| --- | --- | --- |
| `cutover` | Nothing | Prints every precondition. The marker check is deferred until `--execute`. |
| `cutover --execute --rehearse` | Pauses live traffic | PAUSE on every pod, the session check, the marker, the max ID and sequence comparison, then RESUME on BLUE. Never reloads. The ConfigMap can still point at BLUE. |
| `cutover --execute` | Moves traffic to GREEN | The same checks, then RELOAD on every pod and RESUME once every pod confirms GREEN. |
| `pgbouncer resume --execute` | Resumes every pod | The way out when a command died with PgBouncer paused. Refuses when pods name different databases or one can't be read. |
| `finish` | Drops replication and the probe table | Checks every pod runs on GREEN, BLUE has no sessions, and BLUE sees no writes for `--quiet-seconds` (default 60). |

What `cutover` checks before it pauses anything: the subscription is enabled with every table ready, the schemas
and migration versions match, the application's role can log in to GREEN and use every table, GREEN's sequences are
ahead, GREEN is available with BLUE's shape, the PgBouncer pods are ready and haven't changed, every pod's file
names GREEN and every pod still runs on BLUE, the direct writers are scaled to 0 and gone, and BLUE has no
transaction older than `--max-transaction-seconds` (default 5).

The limits that abort a switch and resume on BLUE: `--pause-timeout` (PAUSE took longer, default 15 seconds),
`--direct-wait` (sessions still on BLUE, default 3), `--catchup-timeout` (the marker didn't arrive, default 15),
`--max-paused` (paused too long before RELOAD, default 20). Change them only with the operator, from the
rehearsal's measurements.

## Terraform helpers

Optional, and only for the layout the example config describes. They edit files. They never run Terraform.

| Command | Changes | Notes |
| --- | --- | --- |
| `green-tf --engine-version V` | Writes GREEN's Terraform file | A copy of BLUE's instance and parameter group at the new version. The operator reviews it and applies it. |
| `promote-tf` | Rewrites both Terraform files | After `finish`. GREEN's blocks take BLUE's names. Prints the `terraform state` commands for the operator to run in order. |

## Without a config

| Command | What it does |
| --- | --- |
| `db-cutover --version` | Prints the version. |
| `db-cutover skill` | Prints the directory of this skill inside the installed package. |
| `db-cutover skill --install DIR` | Copies this skill to `DIR/db-cutover`, for example `.claude/skills` or `.agents/skills`. |
