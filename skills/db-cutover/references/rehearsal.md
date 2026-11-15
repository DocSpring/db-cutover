# Rehearsing a cutover

There are two rehearsals. Do both.

1. **A full run on staging**, or on a disposable copy of the setup: every step from `preflight` to `finish`,
   including a real switch. This is the only way to test RELOAD, the application on the new version and recovery.
2. **`cutover --execute --rehearse` on production**, shortly before the real switch. It measures the real pause
   under real traffic and runs every check, then resumes on BLUE.

## What the staging environment needs to share with production

- The same PgBouncer image, pool mode, ConfigMap mount and number of pods.
- The same PostgreSQL source and target versions, and the same extensions.
- The same application, with its request, proxy, job and database timeouts.
- The same network path from the operator's machine to the databases and the cluster.
- Enough data and traffic for the copy and the pause to mean something. A load generator or the `probe` is the
  minimum; real application traffic is better.

## Checklist for the staging run

Work through it with the operator and write down each result.

- [ ] `db-cutover --version` recorded. The same version will be used on production.
- [ ] `env` shows the identifiers and hosts you expect for staging, and nothing from production.
- [ ] `preflight` passes after `prepare-blue`, with any warning explained.
- [ ] `watch` and `probe --execute` run in the background and their output is being read.
- [ ] `reboot-blue --execute`, if needed: note how long requests waited and whether any failed.
- [ ] `copy-schema --execute` ends with matching schemas. Roles, grants and extensions created on GREEN afterwards.
- [ ] `wait-sync --execute` finishes. Note how long the initial copy took and the most WAL that BLUE kept.
- [ ] `verify --execute` passes, with no difference that stays on a second run.
- [ ] `sequences --execute` done.
- [ ] Direct writers stopped. `cutover --execute --rehearse` reports `REHEARSED`. Note `paused_ms`.
- [ ] Run the rehearsal at least once more under load. The pause should be about the same.
- [ ] The pause, with room to spare, is shorter than every timeout in the request path.
- [ ] ConfigMap changed to GREEN without a reload. `pgbouncer config` shows running BLUE, file GREEN on every pod.
- [ ] `cutover` (dry run) passes. `cutover --execute` reports `SWITCHED`. Note `paused_ms`.
- [ ] The probe shows one slow write, the move to GREEN, and no errors. The application's own checks pass.
- [ ] Direct writers moved to GREEN and running. Nothing can reconnect to BLUE.
- [ ] Probe stopped. `finish --execute` leaves GREEN with no publications, subscriptions or slots.
- [ ] `promote-tf` and the state moves, if used: the next Terraform plan creates, replaces and destroys nothing.

## Failures to cause on purpose in staging

The switch is built to fail safely. See it do so before production depends on it.

- [ ] Leave a direct writer connected and run `cutover --execute --rehearse`. It must refuse or abort, resume on
      BLUE and exit with status 1.
- [ ] Hold a transaction open on BLUE for longer than `--max-transaction-seconds`. The preconditions must fail.
- [ ] Press Ctrl-C during a rehearsal's pause. Traffic must resume on BLUE.
- [ ] Restart one PgBouncer pod after the ConfigMap names GREEN. The next `cutover` dry run must report that a pod
      no longer runs on BLUE. Then practise the recovery in `recovery.md`.

## What a rehearsal doesn't prove

- `--rehearse` never runs RELOAD. Only a real switch in staging tests that.
- A staging run doesn't show production's copy time, WAL growth or pause. Production's `--rehearse` measures the
  pause, and the copy has to be watched as it runs.
- Neither shows how the application performs on the new version under production load. Test the application's
  queries on the target version separately.
- A passing rehearsal yesterday says nothing about a long transaction or a new direct writer today. The
  preconditions run again at the real switch for that reason.

## Before the production switch

- [ ] The staging checklist is complete, on the same tool version.
- [ ] Schema changes are frozen and backups are in place. The operator has agreed what happens on each failure.
- [ ] Production's `cutover --execute --rehearse` reported `REHEARSED` with a pause that fits the timeouts.
- [ ] Rollouts, autoscaling and restarts of the PgBouncer pods are frozen for the window.
- [ ] The operator is present and has said to go.
