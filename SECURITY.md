# Security and operational data

This tool operates with administrative database and infrastructure access. Follow your organization's change
authorization policy. A successful dry run is not authorization to execute a production change.

## Private inputs and output

- Keep operational TOML files outside the source repository. Use environment variables or a secret manager,
  never literal credentials in config or on a command line. `cmd:` references execute shell commands; configs,
  Terraform settings and schema dumps are trusted executable inputs, not safe inputs from strangers.
- Keep schema dumps, reports, Terraform state/plans and logs private. Schema functions and comments can contain
  secrets even without table rows. The state directory uses mode 0700.
- PostgreSQL diagnostics and subprocess error output are withheld because they can contain SQL, credentials and
  row values. Inspect failures privately. Session inspection excludes query text; health monitoring omits response
  bodies. Identifiers, URLs, Terraform dry-run output and reports may still contain operator-supplied private data.
- PostgreSQL stores the subscription's connection information. Administrators and some server logging settings can
  expose it. Review logging before creating a subscription with a password.
- Database connections use `sslmode=require`, which encrypts without verifying hostname identity. Restrict access
  and use trusted networks. PgBouncer admin connections use local Kubernetes port-forwards and `sslmode=prefer`.
  Certificate verification is not configured by this CLI.

Never attach live config, Terraform state, schema dumps, connection strings, customer rows, filled documents or raw
logs to public issues. Use synthetic fixtures and error categories. Revoke committed credentials first: deleting
their current file does not remove Git history, cached artifacts or copies.

## Source release review

Review every published ref and its history. Run a redacted secret scan, then inspect for internal endpoints,
account identifiers, infrastructure names, private incident notes and copied data that a credential detector will
not recognize. Check GitHub metadata, release archives and build artifacts separately.

Package only tracked source and synthetic tests. Exclude operational configs, `.db_cutover`, `.env`, credentials,
Terraform state, logs, virtual environments and Git history from source archives. A clean scan records the checks
run; it does not prove that every possible secret is absent.

## Validation scope

Unit tests cover cutover failures, interrupts, resume failures, dry runs, output handling and pure logic. Disposable
PostgreSQL tests cover 16-to-17 text replication, publication coverage, markers and sequences. These do not validate
RDS restart timing, Kubernetes pod churn, real PgBouncer network failures or application timeout budgets. Rehearse
those in staging and keep an operator available during a live switch.
