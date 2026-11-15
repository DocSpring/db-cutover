---
name: db-cutover
description: Plans, rehearses and runs a PostgreSQL major version upgrade on RDS with the db-cutover CLI, working with a human operator. The CLI copies the database to a new instance with logical replication and switches traffic through PgBouncer in Kubernetes. Covers what each command does, which commands need --execute, the checks before a switch, the rehearsal checklist, the points where you stop for the operator, and recovery. Use when the user wants to upgrade or move an RDS PostgreSQL database with little or no downtime, asks for a blue/green database cutover, mentions db-cutover, or has a db-cutover TOML config with [blue], [green] and [pgbouncer] sections. Not for RDS Blue/Green Deployments, pg_upgrade in place, databases older than PostgreSQL 16, or setups without PgBouncer.
license: MIT
metadata:
  author: DocSpring
  version: "0.2.0"
---

# db-cutover

`db-cutover` upgrades a PostgreSQL database by copying it to a new RDS instance (GREEN), keeping the copy current
with logical replication from the old one (BLUE), and switching the application through PgBouncer. At the switch
PgBouncer holds new queries, the tool proves GREEN has every committed write, and the queued queries run on GREEN.

You run the commands. A human operator owns the decisions. This skill says which is which.

## Rules that come first

1. **Every command that changes something is a dry run until you pass `--execute`.** Run the dry run, read its
   output to the operator, and run it with `--execute` only when the operator has agreed to that step. A dry run that
   passes is not permission to execute.
2. **A failed check is a stop.** Never retry with looser options, skip a check, or pass `--force` or
   `--allow-direct` to get past one. Report the check and what it printed, and let the operator decide.
3. **Never let the application write to both databases.** Replication goes one way, from BLUE to GREEN. Once GREEN
   has taken a write, BLUE is stale and there is no way back to it with this tool.
4. **After a cutover's RELOAD has started, never resume PgBouncer pods that point at different databases, or at a
   database you can't confirm.** Traffic stays paused until every pod names GREEN. Read
   `references/recovery.md` before you touch anything.
5. **Keep secrets and data out of the conversation.** Don't print the config's secrets, connection strings, schema
   dumps or database rows. Don't paste the state directory's files. The tool withholds PostgreSQL's error text for
   this reason: when it says to inspect something privately, ask the operator to look.
6. **Don't run the whole workflow again after an interruption.** Find out which step you're in with `status` and
   `pgbouncer config`, and continue from there.
7. **Use one fixed version of the tool for the whole migration.** Check `db-cutover --version` at the start and
   don't upgrade it between the rehearsal and the switch.

## Before you plan anything

Check the requirements with the operator. If one doesn't hold, say so and stop: the tool can't work around it.

- PostgreSQL 16 or newer on RDS, as both the source and the target. One database per run.
- PgBouncer in Kubernetes with transaction pooling, on pods used only for this application. The tool pauses every
  database on the pods it selects.
- `pgbouncer.ini` comes from a ConfigMap mounted as a directory, not with `subPath`, and nothing reloads PgBouncer
  on its own.
- Every application connection goes through those pods. Anything that connects to the database directly (job
  workers, consoles, schedulers, migrations) has to be stopped for the switch and kept from reconnecting.
- No schema changes from `copy-schema` until `finish`.
- Sequences use `INCREMENT BY 1` and don't cycle, and the application accepts gaps in IDs.
- No large objects, unlogged tables, materialized views or foreign tables: `preflight` blocks them.
- Request, proxy, job and PgBouncer timeouts are longer than the pause the rehearsal measures.
- On the machine that runs the tool: `aws`, `kubectl`, and `pg_dump` and `psql` at least as new as both servers.
  It needs to reach both databases and the Kubernetes API, and GREEN needs to reach BLUE.

The config is one TOML file per database, kept outside any repository. `examples/config.toml` in the
[repository](https://github.com/DocSpring/db-cutover) documents every key. Its secrets are references (`env:NAME`,
`op://...`, `cmd:...`), never literal values. A `cmd:` reference runs a shell command, so only use a config the
operator wrote or reviewed.

## The steps, in order

`C` is the config's path. Each step is "dry run, show the operator, then `--execute`" unless it says read-only.

| Step | Command | What `--execute` does |
| --- | --- | --- |
| 0 | `db-cutover "$C" env` and `preflight` | Read-only. The first `preflight` fails on steps not yet done. |
| 1 | `reboot-blue` | Reboots BLUE with PgBouncer paused. Only when `wal_level` isn't `logical` yet. |
| 2 | `prepare-blue` | Creates the probe table and sets `REPLICA IDENTITY FULL` on tables with no primary key. |
| 3 | `green-tf --engine-version V` | Writes GREEN's Terraform file. Optional. The operator applies it. |
| 4 | `copy-schema` | Creates the database on GREEN and restores BLUE's schema. GREEN must be empty. |
| 5 | `start-replication` | Creates the publication and subscription. The initial copy starts. |
| 5 | `wait-sync` | Waits for every table, then writes a marker on BLUE and waits for it on GREEN. |
| 6 | `verify` | Read-only without `--execute`. With it, also writes a marker to test replication. |
| 6 | `sequences` | Moves GREEN's sequences above BLUE's. |
| 6 | `cutover --execute --rehearse` | A real pause and every check, then resumes on BLUE. |
| 7 | `cutover` | The switch: PAUSE, checks, RELOAD onto GREEN, RESUME. |
| 9 | `finish` | Drops the subscription, slot, publication and probe table. |
| 9 | `promote-tf` | Rewrites the Terraform files. Optional. Prints state commands for the operator. |

The step numbers are the sections of the
[procedure](https://github.com/DocSpring/db-cutover/blob/main/docs/procedure.md), which covers what happens between
commands. Read its section for a step before you run that step. Read `references/commands.md` for each command's
options, its dry run and what can go wrong.

Run two monitors in the background for the whole migration and read their output before every step:

```sh
db-cutover "$C" watch              # health URLs, replication and PgBouncer: a line per change, ALERT for problems
db-cutover "$C" probe --execute    # after prepare-blue: writes a row through PgBouncer twice a second
```

`watch` prints only when something changes, and the probe prints its totals once a minute, so silence doesn't
prove `watch` is alive. Check the processes. Stop the probe before `finish`, which drops its table.

## Where you stop for the operator

Stop and wait for the operator's word at each of these, even if they told you earlier to carry on:

- **Before any `--execute`** that the operator hasn't agreed to for this step and this environment.
- **Before `reboot-blue --execute`.** It restarts the production database. Requests wait up to `--max-pause`
  seconds and direct connections drop.
- **Before the operator applies Terraform for GREEN.** They review the plan. A plan that replaces or destroys BLUE
  must never be applied.
- **After `copy-schema`.** The operator creates the application's roles, grants and extensions on GREEN. The schema
  copy leaves grants and ownership out.
- **Before `cutover --execute --rehearse`.** It pauses live traffic. The operator stops the direct writers first.
- **After the rehearsal.** Give the operator the measured pause (`paused_ms` in the report) and compare it with
  the application's timeouts together. Don't go on if the pause doesn't fit.
- **Before the PgBouncer ConfigMap changes to GREEN.** From that moment a restarted pod serves GREEN, so the window
  until the switch has to be short, with rollouts and autoscaling frozen. The operator makes this change.
- **Before `cutover --execute`.** This is the point of no return. Confirm the dry run just passed, the direct
  writers are stopped, both monitors are healthy and the operator is watching.
- **After the switch, before `finish --execute`.** The operator moves the direct writers to GREEN and blocks every
  path back to BLUE. `finish` removes replication, and a write that reaches BLUE after that is lost.
- **Whenever a report says `RECOVERY_REQUIRED`,** a command exits with a status other than 0, or `watch` prints an
  `ALERT`.

## Rehearsing

Rehearse the whole procedure on a staging or disposable environment with the same PgBouncer setup, networking,
extensions and timeouts before production, and run `cutover --execute --rehearse` on production itself before the
real switch. `references/rehearsal.md` has the checklist and what a rehearsal doesn't prove.

## Reading results

- Exit status 0: the command did what was asked in the mode it ran in. 1: a check or step failed. 2: a bad
  argument or config. 130: interrupted.
- Check lines are marked `[ok  ]`, `[warn]` or `[FAIL]`, and the list ends with `PASSED` or `FAILED`. A `FAIL`
  blocks. A `warn` doesn't, but tell the operator about it.
- `cutover` writes a report to the config's state directory. Its `status` is `REHEARSED`, `SWITCHED`, `ABORTED`
  (BLUE still primary, traffic resumed) or `RECOVERY_REQUIRED`. Tell the operator the status and `paused_ms`. Don't
  paste the file.
- After a switch, the probe should show one slow write, a change to GREEN and no errors.

## When something goes wrong

Read `references/recovery.md` first. In short: before RELOAD, the tool resumes traffic on BLUE and nothing has
moved, so fix the cause and try again. After RELOAD starts, a failure leaves traffic paused on purpose: get every
pod onto GREEN, then resume. Once GREEN has taken writes, fix problems on GREEN.
