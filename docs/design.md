# Design

BLUE serves the application while logical replication copies its tables to GREEN and keeps them current. A short
PgBouncer pause provides a boundary for moving all foreground traffic to GREEN. The [procedure](procedure.md)
spells out the external traffic controls and recovery steps the CLI cannot enforce.

## What the pause establishes

1. PAUSE runs on every selected PgBouncer pod. With transaction pooling, it waits for active transactions to
   finish and releases server connections. New queries queue, subject to configured timeouts.
2. The tool checks for remaining client sessions on BLUE, across all roles except its own connections. External
   deployment/network controls must prevent new direct sessions from appearing after this observation.
3. The tool commits a unique marker on BLUE and waits for the matching ID **and token** on GREEN. A matching ID
   alone could be an unrelated pre-existing row.
4. Maximum integer primary-key values are compared, and any sequence too close to BLUE's range is advanced.
5. Every pod reloads its mounted configuration. Every pod must confirm GREEN through SHOW DATABASES before any
   RESUME is issued.
6. Queued queries continue on GREEN. The tool checks server addresses and remaining BLUE client sessions, then
   writes a private report with a final status.

Before the first RELOAD, failures and interrupts attempt to resume BLUE. A failed resume is reported as recovery
required. Once RELOAD starts, a mixed or unknown target is a failure that leaves traffic paused. One-way logical
replication cannot safely combine application writes to both databases: updates, deletes and unique constraints
can conflict even when sequence values differ.

A successful pause can avoid failed requests only if it fits inside every relevant timeout. Failed reloads or
lost control connections may require a longer pause and manual recovery. SIGKILL, host failure and a broken
network cannot be handled by an in-process exception handler.

## Scope of the replication marker

A marker demonstrates progress in the configured subscription after the initial copy has finished. It is not a
checksum of the database. The preconditions check the subscription's source, publication, slot and serial apply
mode, and require the publication to include every persistent table/column with INSERT, UPDATE, DELETE and
TRUNCATE enabled, without row filters. The schema must remain frozen.

The tool creates subscriptions with `streaming = on`, not parallel apply. PostgreSQL preserves transaction order
within a subscription. That does not synchronize independent subscriptions, data outside the publication or
external effects such as notifications. See PostgreSQL's [logical replication overview](https://www.postgresql.org/docs/17/logical-replication.html).

Text replication is the default because binary representation is less portable between versions and types.
Binary replication is an explicit opt-in requiring compatibility testing. See
[CREATE SUBSCRIPTION](https://www.postgresql.org/docs/17/sql-createsubscription.html).

## Sequences

Logical replication carries column values, not sequence counters. GREEN's sequence counters must be advanced
before it starts assigning IDs. The tool uses BLUE's last reserved value plus a configurable gap, then checks
again while PgBouncer is paused. It keeps INCREMENT BY 1, rejects cycling sequences and checks both sides' limits
before writing. Crossing the 32-bit boundary is blocked because dependent integer columns may still exist.

The gap is a conservative buffer, not a guarantee against future writes to BLUE. It must not be used as a split
traffic strategy. Sequences can still exhaust their range; choose the gap based on capacity and the application.

## Verification and its limits

Authenticated health checks send their token only to the configured URL. Redirects are not followed; a redirect
is reported as an HTTP failure so it cannot silently forward the token to another endpoint.

`verify` compares schema dumps, migration metadata and grants. It uses exact counts for small tables and tables
without a supported integer key, and counts/hashes of sampled ID windows for larger tables. These checks can find
many mistakes but are neither a full comparison nor a shared transaction snapshot. Concurrent writes can cause
transient mismatches; persistent mismatches require investigation.

`verify` without `--execute` never writes a marker, including on retries. `verify --execute`, `wait-sync --execute`,
executed cutovers and executed cleanup use marker writes. The probe is also opt-in through `--execute`.

## Schema and permissions

`pg_dump --schema-only --no-owner --no-acl --no-publications --no-subscriptions` copies schema objects without
carrying the old replication setup to the destination. Schema dumps are executable SQL: trust the source and keep
local dumps private. Roles, grants, extension availability and database/instance settings need separate review.

Tables without a primary key receive REPLICA IDENTITY FULL before replication. FULL has performance and type
limitations; a usable primary key or replica identity index is preferable where practical. Large objects,
unlogged tables, foreign tables and materialized views are not handled by this workflow and are blocked.

BLUE DDL that acquires table locks uses a two-second lock timeout, retries after ten seconds, and stops after
thirty attempts. Waiting indefinitely for an exclusive lock would also stall otherwise compatible queries behind
it. These bounds limit each wait; they do not make preparation free of application impact.

Declared `[schema] column_types` changes run on the empty destination before the copy. Text replication converts
values, and schema comparisons expect those exact changes. Dependent sequences, indexes, constraints, extensions
and application behavior still need testing. This is an optional migration feature, not automatic compatibility.

## Kubernetes and direct clients

The configuration file is changed before PAUSE to keep Kubernetes propagation outside the measured pause. This
creates a real race: a restarted pod reads GREEN before the coordinated switch. The operator must prevent
rollouts/restarts and automatic reloads during that window or design an external traffic gate. The readiness check
detects existing drift but cannot make the window atomic.

The CLI pauses all databases on the selected dedicated pods. It does not change other Kubernetes deployments or
credentials. Direct writers must be stopped and fenced by the operator before the pause, then moved to GREEN.

## Cleanup and rollback

`finish` checks routing, BLUE sessions and a quiet period in write statistics, then verifies a final marker before
removing replication. Statistics are delayed observations, not a write fence. Operator-controlled fencing must
remain in place so a later console, scheduled task or deployment cannot reconnect to BLUE.

Before GREEN accepts writes, an operator can abandon the destination and keep BLUE. After GREEN accepts writes,
BLUE is stale. Keeping it running or taking a snapshot does not provide an immediate rollback. Reverse replication
and data reconciliation are outside this tool; recover forward on GREEN unless a separate tested rollback plan
exists.

The optional Terraform helpers generate files and state-move commands for a particular layout. They do not apply
infrastructure or execute state commands. Review the files and plan, use state backups/locking, and stop if a plan
would unexpectedly create, replace or destroy a database.

## References

- [PgBouncer administration commands](https://www.pgbouncer.org/usage.html)
- [PostgreSQL logical replication restrictions](https://www.postgresql.org/docs/17/logical-replication-restrictions.html)
- [RDS logical replication prerequisites](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/PostgreSQL.Concepts.General.FeatureSupport.LogicalReplication.html)
