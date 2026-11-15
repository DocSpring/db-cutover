# Recovery

Find out the state before you change anything. These three are read-only:

```sh
db-cutover "$C" status
db-cutover "$C" pgbouncer config      # per pod: the host it runs with, and the host in its file
db-cutover "$C" pgbouncer pools       # cl_waiting above 0 on a paused pod means clients are queued
```

Then tell the operator what you found and what you propose. Recovery steps change live traffic, so each one needs
the operator's word.

## Which case you're in

| What happened | State | What to do |
| --- | --- | --- |
| `cutover` failed its preconditions | Nothing changed. | Fix the failed check. Run the dry run again. |
| The report says `ABORTED` | Traffic resumed on BLUE. BLUE is still primary. | Read why it aborted. Confirm with `pgbouncer config` that every pod runs BLUE. Fix the cause before another attempt. |
| The report says `REHEARSED` | Traffic resumed on BLUE. | Nothing to recover. |
| `RECOVERY_REQUIRED`, and the tool said it could not confirm RESUME on BLUE | RELOAD never ran. Some pods may still be paused. | Check every pod runs BLUE, then `pgbouncer resume --execute`. |
| `RECOVERY_REQUIRED` after RELOAD started | Pods may be on different databases. Traffic is paused on purpose. | See [After RELOAD started](#after-reload-started). |
| The tool was killed, or the machine lost its connection, during a switch | Unknown. | Work it out from the pods with `pgbouncer config`. Treat it as "after RELOAD started" unless every pod runs BLUE and GREEN has taken no application writes. |
| Traffic resumed on GREEN but the tool reported that verification failed | GREEN is primary. Something is still connected to BLUE. | Find what is on BLUE (`status`) and stop it. Don't run `finish` until BLUE is quiet. |
| The application has errors after the switch | GREEN is primary and has new writes. | Fix the problem on GREEN. Going back to BLUE loses those writes. |
| `reboot-blue` died with PgBouncer paused | BLUE is restarting or back. Pods are paused. | Once BLUE accepts connections, `pgbouncer resume --execute`. Check each pod's `query_wait_timeout` was restored. |

## After RELOAD started

The rule: **all pods on GREEN, then resume. Never resume while they differ.**

1. Keep the direct writers stopped.
2. Run `pgbouncer config`. Note which pods run GREEN, which run BLUE, and which can't be read.
3. For a pod that still runs BLUE: its file already names GREEN, so it needs a `RELOAD` on its admin console. The
   operator does this, or tells you to.
4. For a pod that can't be read: restore the connection to it first. Don't guess its state.
5. Don't delete or restart pods to fix this. A new pod starts unpaused and serves traffic at once.
6. When `pgbouncer config` shows every pod running GREEN, run `pgbouncer resume --execute`. It refuses if the pods
   differ.
7. Check the probe's writes land on GREEN and the application works, then continue from "move the direct writers".

Going back to BLUE at this point is possible only if no pod was ever resumed on GREEN. That is the operator's
decision, and it means changing the ConfigMap back and reloading every pod before any resume.

## Replication problems before the switch

| Symptom | What to do |
| --- | --- |
| `watch` alerts that the subscription is disabled | The operator reads GREEN's PostgreSQL log for the cause. After fixing it, `start-replication --execute` enables the subscription again. |
| Replication error counts keep rising | The same: the cause is in GREEN's log. Usually a conflicting row, a missing permission or a type that doesn't convert. |
| BLUE keeps more and more WAL | GREEN is behind or disconnected. Check BLUE's free storage now. If BLUE's disk is at risk, the operator decides whether to give up this GREEN: dropping the subscription and slot frees the WAL and means starting again from `copy-schema`. |
| `verify` shows a difference that stays | Stop. Don't switch. The operator investigates the table. |

## Giving up before the switch

Until GREEN takes application writes, BLUE is untouched and still primary. `finish` can't be used to clean up,
because it requires every pod to run on GREEN. To abandon the migration, the operator makes sure the ConfigMap names
BLUE and every pod runs BLUE, drops the subscription on GREEN (which drops the slot on BLUE), drops the publication
and the probe table on BLUE, and removes GREEN.

## What to tell the operator

Give the report's `status`, `paused_ms` and the last event line, each pod's running and file host, and whether
clients are waiting. Don't paste the report file, server logs, connection strings or rows.
