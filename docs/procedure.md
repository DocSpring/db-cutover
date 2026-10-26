# Procedure

Work with a human operator and rehearse on a disposable or staging environment with the same pooling, networking,
permissions, extensions and application timeouts. This procedure covers RDS PostgreSQL 16+ with dedicated
PgBouncer pods in Kubernetes. It does not provision an entire deployment.

Set `C` to the absolute path of a private TOML config. Run each changing command without `--execute` first and
review its output. Keep the source and target identities fixed until cleanup is complete. Record which phase you
are in; do not rerun the whole workflow after an interruption.

## 0. Plan and configure

1. Install the tool and external clients from the README. Use a reviewed commit and the lockfile.
2. Copy `examples/config.toml` outside the repository. Fill in the AWS profile/region, BLUE/GREEN instance
   identifiers, PgBouncer Kubernetes context, secret references, health URLs and every direct writer.
3. Run `db-cutover "$C" env` and `db-cutover "$C" preflight`. Missing preparation steps can make this first
   preflight fail. Resolve each failure before it becomes a prerequisite for the next phase.
4. Test your application's SQL, extensions, collation behavior, grants and performance on the target major version.
   Use a current `pg_dump` and matching `psql` from that version or newer. Schema dumps are executable SQL and may
   include private function bodies: keep the state directory private and never paste dumps into an agent session.
5. Freeze schema changes, prepare backups and agree the recovery procedure. Initial copying reads every table and
   writes the entire destination. Budget storage, I/O, WAL retention and replication worker/slot capacity.
6. Inventory every client of BLUE, including other roles, consoles and periodic jobs. Configure direct-writer
   deployments for checks, but also prevent reconnection outside those deployments. The tool cannot fence external
   clients or make Kubernetes and database changes atomic.

Run monitoring in separate terminals:

```sh
db-cutover "$C" watch
# Start this only after prepare-blue has created the probe table:
db-cutover "$C" probe --execute
```

Set `[secrets] health_check_token` if the health endpoints require `X-Health-Check-Token`. Only configure
trusted HTTPS health URLs. The endpoint must respond directly: redirects are reported as failures to avoid
forwarding the token. Confirm the processes remain alive. The probe writes synthetic rows and reports slow writes, errors and target
changes. Health checks should return a simple status without private content. Stop the probe before dropping its
table in phase 9.

## 1. Enable logical replication

If BLUE already has `wal_level = logical`, skip the reboot. Otherwise set `rds.logical_replication = 1` in its RDS
parameter group with `apply_method = pending-reboot`, and apply the parameter change. The operator needs the RDS
permissions and database replication privileges described in the [AWS documentation](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/PostgreSQL.Concepts.General.FeatureSupport.LogicalReplication.html).

```sh
db-cutover "$C" reboot-blue
db-cutover "$C" reboot-blue --execute
```

Stop direct clients before rebooting. PgBouncer holds its clients while BLUE restarts; a request timeout shorter
than the restart will still fail. The command resumes after `--max-pause` even if BLUE is not back. Test a separate
maintenance plan if that delay exceeds your application's limits. `--failover` requests a Multi-AZ failover instead
of an in-place restart and adds DNS/cache considerations. Do not use `--allow-direct` to promise an uninterrupted
reboot: it explicitly permits direct connections to fail.

## 2. Prepare BLUE

```sh
db-cutover "$C" prepare-blue
db-cutover "$C" prepare-blue --execute
```

This creates the probe table and adds `REPLICA IDENTITY FULL` where a primary key is missing. Those ALTER statements
use a bounded lock wait and retry. FULL identity can make updates/deletes expensive and has data-type restrictions;
test tables without keys. Preflight blocks large objects, unlogged tables, foreign tables and materialized views
because this workflow does not copy their contents.

## 3. Provision GREEN

Choose a target version available in your AWS region and tested with your application. Set `TARGET_VERSION` to
that exact version. If your Terraform layout matches the example, generate the draft file:

```sh
db-cutover "$C" green-tf --engine-version "$TARGET_VERSION"
db-cutover "$C" green-tf --engine-version "$TARGET_VERSION" --execute
```

Review the HCL and a Terraform plan, then provision GREEN's instance and parameter group. The helper edits a
specific resource/module layout; it is not a general HCL parser. Review parameters removed or changed between
PostgreSQL major versions, encryption, backups, availability, storage and security groups. Never apply a plan that
replaces or destroys BLUE. A larger temporary GREEN may speed the initial copy, but restore its intended production
shape before switching. A Multi-AZ BLUE requires a Multi-AZ GREEN.

When using the helper, set `[terraform] dir` and resource names to your actual layout. Read GREEN's identifier
from the generated Terraform file using the example's commented identifier setting, or update the literal
`[green] identifier` after provisioning.

Terraform helpers are optional: provision GREEN using your own infrastructure tools and put its literal identifier
in `[green]`. Ensure GREEN can connect to BLUE for replication and the operator can reach both. Keep application
writers away from GREEN until cutover. Create required extension packages and any roles referenced by schema
objects before restoring the schema.

## 4. Copy schema and permissions

```sh
db-cutover "$C" copy-schema
db-cutover "$C" copy-schema --execute
```

GREEN must be empty. The tool creates a libc-locale database when needed and restores a schema-only dump in one
transaction. It excludes grants, ownership and replication objects. Create application roles and grants on GREEN
through your normal provisioning mechanism. The example Terraform app-user module can be enabled at this point.
Other roles, passwords, default privileges, database settings and extensions require explicit review.

`[schema] column_types` supports declared type changes on the empty destination. Use text replication, test the
conversion, and verify dependent sequences, indexes and application code. Widening a column does not automatically
widen its sequence. Keep the first migration simple unless the type change is part of your tested plan.

## 5. Replicate

```sh
db-cutover "$C" start-replication
db-cutover "$C" start-replication --execute
db-cutover "$C" wait-sync --execute
```

The subscription copies existing rows and streams later changes. Text replication is the default for portability
between versions. `wait-sync --execute` waits for every table, writes a unique marker on BLUE, and waits for it on
GREEN. Without `--execute`, it only describes that action.

Watch BLUE's retained WAL and free storage. A disconnected or slow subscriber can fill the source disk. Rising
replication errors require investigation in GREEN's private PostgreSQL logs. `start-replication --execute` can
re-enable a disabled subscription; it does not fix a conflicting row, incompatible type or missing permission.
Do not discard a slot or subscription merely to clear an alert: that can require rebuilding GREEN.

## 6. Verify, advance sequences and rehearse

```sh
db-cutover "$C" verify
db-cutover "$C" verify --execute
db-cutover "$C" sequences
db-cutover "$C" sequences --execute
```

`verify` compares schema, migration metadata, privileges, row counts and sampled row hashes. `--execute` adds a
replication marker. It is not an exhaustive data comparison or a consistent snapshot of both databases: concurrent
writes can produce transient mismatches. Resolve persistent mismatches. Row counts can scan entire tables,
particularly when statistics are stale or no integer primary key exists; schedule them for your workload.

Sequence values do not replicate. The tool advances GREEN's sequences with a configured gap and checks their
limits. The gap permits unused IDs; it does not permit simultaneous application writes to both databases.

Stop and fence every direct writer, then rehearse while PgBouncer's mounted config still points to BLUE:

```sh
db-cutover "$C" cutover --execute --rehearse
```

This is a real pause, marker and data/sequence check, followed by a resume on BLUE. It affects live requests.
Measure the pause against all application timeouts. It does not test RELOAD, GREEN's production performance or
recovery after a partial switch; test those separately in staging.

## 7. Prepare and switch

1. Keep direct writers stopped. Freeze PgBouncer rollouts, autoscaling and automatic config reloads. Verify
   transaction pooling and that the selected dedicated pods cover every application connection.
2. Change only the database host in the mounted ConfigMap to GREEN, without reloading PgBouncer. Wait for every
   pod's file to update. `db-cutover "$C" pgbouncer config` must show running BLUE and file GREEN on every pod.
3. Keep this window short. **A pod restarted now reads GREEN immediately.** The command checks for this before
   switching, but cannot eliminate a restart race. If your deployment cannot control that window, stop here and
   design a traffic gate appropriate to your platform before using this workflow.
4. Run the dry run, inspect every gate, then execute:

```sh
db-cutover "$C" cutover
db-cutover "$C" cutover --execute
```

The dry run reads readiness state but defers marker writes. Execution pauses every pod, drains in-flight
transactions, rejects remaining client sessions of every role, waits for a replicated marker, checks maximum IDs
and sequences, reloads all pods onto GREEN, and resumes only after every target is confirmed.

The report is stored under the private state directory. Confirm the probe moves to GREEN, real application
operations succeed, and no client remains on BLUE. A process exiting unsuccessfully is not a successful cutover.

## 8. Move direct writers and fence BLUE

Change direct connection strings to GREEN, deploy/restart those clients and confirm their sessions are on GREEN.
Prevent all application roles, consoles and periodic jobs from reconnecting to BLUE. Use deployment and network
controls appropriate to your environment while retaining the tool's administrative and replication access.

Do not return traffic to BLUE after GREEN accepts writes. Replication is one-way; BLUE does not contain GREEN's
new transactions. Retaining BLUE or its snapshot is useful for investigation and recovery planning, not an
immediately usable rollback.

## 9. Finish and reconcile infrastructure

Stop the probe. Keep health monitoring running. With application access to BLUE fenced:

```sh
db-cutover "$C" finish
db-cutover "$C" finish --execute
```

The tool checks PgBouncer targets, BLUE sessions, a quiet period in write statistics, and (with `--execute`) a final
marker. It then removes its subscription, slot, publication and probe table. Statistics and session checks are
observations, not a lock preventing future writers. The traffic fence is required before replication is removed.

For the supported Terraform layout:

```sh
db-cutover "$C" promote-tf
db-cutover "$C" promote-tf --execute
```

Review the rewritten files and run the printed state commands in order with your normal state backup and locking
practices. They change Terraform state; the helper does not execute them. A subsequent plan should not create,
replace or destroy either database. Update references to the new primary. Retain BLUE and snapshots according to
your recovery/retention plan, then remove them through a separately reviewed infrastructure change.

## Recovery

| State | Action |
| --- | --- |
| Failure before RELOAD | The tool attempts RESUME on BLUE. Confirm it succeeded on every pod before retrying. |
| `RECOVERY_REQUIRED` after RELOAD starts | Keep direct writers stopped. Inspect each pod's running host and pause state. Align all pods to GREEN while they remain paused, then resume. Do not delete pods as a shortcut: replacements can start serving immediately. |
| Mixed or unknown hosts | Do not resume traffic. Restore admin connectivity and resolve the targets first. One-way replication cannot reconcile two active writers. |
| All pods on the intended host, but a resume failed | Use `db-cutover "$C" pgbouncer resume --execute` after inspecting every pod. The command refuses mixed/unknown hosts. |
| Process killed, machine disconnected, or report missing | Determine whether RELOAD began from the pods themselves. No process can guarantee cleanup after SIGKILL or loss of connectivity. |
| Application errors after GREEN accepted writes | Fix forward on GREEN. A return to BLUE requires a separate reconciliation/reverse-replication plan. |

Use each pod's PgBouncer admin console for manual `SHOW DATABASES`, `SHOW POOLS`, `SHOW SERVERS`, `RELOAD` and
`RESUME` as needed. These are real traffic operations: follow the operator's authorization policy. Keep sensitive
configuration and server logs out of public issues and agent conversations.
