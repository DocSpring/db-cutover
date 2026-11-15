# db-cutover

Upgrade a PostgreSQL database by copying it to a new instance, keeping that copy current with logical replication,
and switching application traffic through PgBouncer. The application keeps using the old database during the copy.
At the switch, PgBouncer holds new queries while in-flight transactions finish, then sends queued work to the new
instance.

`db-cutover` is the Python CLI that runs each step. It comes with an [example configuration](examples/config.toml),
a [step-by-step procedure](docs/procedure.md), [design notes](docs/design.md) and an
[agent skill](skills/db-cutover/SKILL.md). It is made for a human operator working with a coding agent: each command
has a narrow purpose, and every command that changes database, infrastructure or PgBouncer state is a dry run until
it gets `--execute`.

**Release candidate:** failure-path tests and PostgreSQL replication tests do not replace a rehearsal on your
application's RDS and PgBouncer deployment. Read the requirements below before planning a cutover.

## Who it's for

Teams that run PostgreSQL on RDS behind PgBouncer in Kubernetes and need a major version upgrade (or a move to a new
instance) without a maintenance window. It suits you if:

- you want to choose the moment of the switch and see every check before it, instead of handing the upgrade to a
  managed service;
- your application already connects through PgBouncer with transaction pooling, or can be made to;
- someone on the team can provision an RDS instance and change a Kubernetes ConfigMap.

It is not for databases older than PostgreSQL 16, setups without PgBouncer, or schemas that rely on large objects,
materialized views, unlogged tables or foreign tables. For those, look at
[RDS Blue/Green Deployments](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/blue-green-deployments.html) or
`pg_upgrade` with a maintenance window.

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

`db-cutover` needs Python 3.11 or newer. Install it as a standalone tool with [uv](https://docs.astral.sh/uv/) or
[pipx](https://pipx.pypa.io/):

```sh
uv tool install db-cutover==0.2.0     # or: pipx install db-cutover==0.2.0
db-cutover --version
db-cutover --help
```

Pin the version and keep it fixed from the first rehearsal to the end of the migration. To run from source instead,
clone the repository at a release tag and use its locked dependencies (`uv sync --frozen`, then `uv run db-cutover`).

The machine running the tool also needs `aws`, `kubectl`, and matching, current `pg_dump`/`psql` clients at least as
new as **both** database servers. `op` is optional for 1Password references; Terraform is optional for the two HCL
helpers. It needs network access to both databases and the Kubernetes API. GREEN must reach BLUE for replication.

Copy the [example](examples/config.toml) to a private working directory, fill in your identifiers and contexts,
and supply credentials through environment variables or a secret manager. Never commit the operational config,
schema dumps, reports, Terraform state or logs. `cmd:` secret references execute shell commands: treat configs as
trusted executable input.

## Working with an agent

The package includes an [Agent Skill](https://agentskills.io/specification) that teaches a coding agent (Claude
Code, Codex, Cursor and others) to plan, rehearse and run a cutover with you: what each command does, which ones
need `--execute`, the rehearsal checklist, the points where it must stop for your decision, and recovery.

```sh
db-cutover skill --install .claude/skills    # or .agents/skills, or your agent's skills directory
npx skills add DocSpring/db-cutover          # the same skill, straight from this repository
```

Give the agent the config path, the approved versions, the traffic controls and the limits of what it may execute.
Have it run each changing command without `--execute` first, explain the output, and follow your agreed execution
policy. A failed gate is a stop condition.

Exit status 0 means the command completed its requested mode, 1 means failure, 2 means an argument/config error,
and 130 means interruption. A dry-run success is not a completed migration. Long-running monitors continue until
stopped; quiet output alone is not proof that a monitor is still alive.

## Workflow

Read [the procedure](docs/procedure.md) for the provisioning, permissions, traffic controls and recovery steps
between these commands. In the examples below, `C` is a private config path and `TARGET_VERSION` is the RDS engine
version you have selected and tested. Prefix commands with `uv run` if you run from a source checkout.

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

## Rehearse first

Rehearse twice: the whole procedure on staging, then the pause on production.

1. **Build a staging setup that matches production** where it matters: the same PgBouncer image, pool mode and
   ConfigMap mount, the same PostgreSQL source and target versions and extensions, and the application with its
   real timeouts. Put traffic on it.
2. **Run every step there, including a real switch**, with `watch` and `probe --execute` running. Write down how
   long the copy took, how much WAL BLUE kept, and the pause the switch reports.
3. **Cause the failures on purpose.** Leave a direct writer connected, hold a transaction open on BLUE, press
   Ctrl-C during a rehearsal's pause, restart a PgBouncer pod after the ConfigMap names GREEN. Each one should stop
   the switch and leave traffic on BLUE. Practise the [recovery steps](docs/procedure.md#recovery) for the last one.
4. **On production, rehearse the pause** once replication has caught up and the direct writers are stopped:

   ```sh
   db-cutover "$C" cutover --execute --rehearse
   ```

   This pauses live traffic, runs every check the real switch runs, and resumes on BLUE. Its report gives the pause
   in milliseconds. Compare it with every timeout in the request path before you go on.

`--rehearse` never reloads PgBouncer, so only the staging switch tests RELOAD, GREEN under the application and
recovery from a partial switch. The skill's
[rehearsal checklist](skills/db-cutover/references/rehearsal.md) lists each thing to check and record.

## Guarantees and limits

What the tool does:

- **Nothing changes without `--execute`.** A dry run reads, checks and reports. It can connect, read data, create
  local state files and use database resources, but it never pauses clients or changes remote state.
- **No switch without proof that GREEN is current.** With every PgBouncer pod paused, the tool checks that no
  client session is left on BLUE, writes a marker on BLUE and waits for it on GREEN, and compares the highest ID of
  every table before any pod reloads.
- **A failure before RELOAD leaves BLUE as primary.** The tool resumes traffic on BLUE, including on Ctrl-C, and
  reports a resume it could not confirm.
- **It never resumes pods that point at different databases.** After RELOAD starts, mixed or unknown targets
  **remain paused** and the command fails. An operator aligns every pod to GREEN before resuming.
- **It keeps secrets and row data out of its output.** PostgreSQL diagnostics, query text and health response
  bodies are withheld. State files go in a private directory.

What it can't do:

- **Promise zero failed requests.** Queries wait during the pause. Requests succeed only if every timeout in their
  path is longer than the pause and recovery works. An RDS reboot can take much longer than a switch.
- **Fence clients it doesn't know about.** Anything that connects without PgBouncer has to be stopped and kept
  away by you. The tool checks for sessions; it can't prevent new ones.
- **Close the window between the ConfigMap change and the switch.** A PgBouncer pod restarted in that window
  serves GREEN early. The tool detects it before switching but can't stop it.
- **Go back to BLUE once GREEN has taken writes.** Replication runs one way. Never read replication or a sequence
  gap as permission to write to both databases. After the switch, fix forward on GREEN.
- **Survive its own process being killed.** After SIGKILL or a lost connection during a switch, an operator has to
  read the state from the pods.
- **Prove the data is identical.** `verify` compares schemas, counts and sampled rows. It is not a full comparison.
- **Copy everything.** Roles, grants, extensions, database settings and other databases need separate preparation.
  Sequence values are advanced, not replicated.

The [design notes](docs/design.md) give the reasoning, and [SECURITY.md](SECURITY.md) the limits of the tests.

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
handling and release checks, and [AGENTS.md](AGENTS.md) for the code's rules and how to release.

Built by [DocSpring](https://docspring.com). Licensed under [MIT](LICENSE).
