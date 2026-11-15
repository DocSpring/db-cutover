"""db-cutover CONFIG COMMAND [options]. Run `db-cutover --help` for the commands, and see docs/procedure.md."""

from __future__ import annotations

import argparse
import math
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Callable
from pathlib import Path

import psycopg

from db_cutover import __version__, config
from db_cutover.commands import blue, inspect, replication, switch, terraform
from db_cutover.db import Context
from db_cutover.output import StepFailed, error_detail
from db_cutover.shell import CommandFailed

Command = Callable[[Context, argparse.Namespace], None]


def nonnegative_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise argparse.ArgumentTypeError("must be a finite number >= 0")
    return number


def positive_float(value: str) -> float:
    number = nonnegative_float(value)
    if number == 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return number


def positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be an integer greater than 0")
    return number


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="db-cutover",
        description="Blue/green PostgreSQL cutover on RDS with logical replication and PgBouncer. Every command "
        "that changes something is a dry run without --execute.",
        epilog="`db-cutover skill` prints where the packaged agent skill is, and `db-cutover skill --install DIR` "
        "copies it into an agent's skills directory.",
    )
    p.add_argument("--version", action="version", version=f"db-cutover {__version__}")
    p.add_argument("config", help="path to the cutover's TOML config")
    sub = p.add_subparsers(dest="command", required=True, metavar="COMMAND")

    def add(name: str, fn: Command, help: str) -> argparse.ArgumentParser:
        sp = sub.add_parser(name, help=help, description=(fn.__doc__ or help).strip())
        sp.set_defaults(fn=fn)
        return sp

    def execute(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--execute", action="store_true", help="make the change (without it: a dry run)")

    # Read-only
    add("env", inspect.cmd_env, "identifiers, hosts and instance settings")
    sp = add("preflight", inspect.cmd_preflight, "read-only checks before starting")
    sp.add_argument("--json", action="store_true")
    sp = add("status", inspect.cmd_status, "one-shot status of BLUE, GREEN, replication and PgBouncer")
    sp.add_argument("--json", action="store_true")
    sp = add("watch", inspect.cmd_watch, "long-running: a line per change, ALERT lines for problems")
    sp.add_argument("--interval", type=positive_float, default=30, help="seconds between database checks (default 30)")
    sp.add_argument(
        "--health-interval", type=positive_float, default=5, help="seconds between health checks (default 5)"
    )
    sp.add_argument(
        "--slow-ms", type=positive_float, default=3000, help="health check slower than this alerts (default 3000)"
    )
    sp.add_argument(
        "--health-timeout", type=positive_float, default=30, help="seconds before a health check fails (default 30)"
    )
    sp.add_argument(
        "--max-retained-gb", type=positive_float, default=20, help="alert when BLUE keeps more WAL (default 20)"
    )
    sp = add("probe", inspect.cmd_probe, "long-running: writes through PgBouncer and reports errors and pauses")
    execute(sp)
    sp.add_argument("--interval", type=positive_float, default=0.5, help="seconds between writes (default 0.5)")
    sp.add_argument(
        "--slow-ms", type=positive_float, default=1000, help="writes slower than this are reported (default 1000)"
    )
    sp = add("pgbouncer", inspect.cmd_pgbouncer, "SHOW ... on every PgBouncer pod, or resume them all")
    execute(sp)
    sp.add_argument("what", choices=["pools", "servers", "clients", "databases", "stats", "config", "resume"])

    # Before replication
    sp = add("reboot-blue", blue.cmd_reboot_blue, "apply BLUE's pending parameters by rebooting it, PgBouncer paused")
    execute(sp)
    sp.add_argument("--force", action="store_true", help="reboot even if nothing is pending-reboot")
    sp.add_argument("--allow-direct", action="store_true", help="reboot even with direct writers connected to BLUE")
    sp.add_argument(
        "--failover",
        action="store_true",
        help="Multi-AZ: fail over to the standby instead of restarting in place (longer: adds PgBouncer's DNS wait)",
    )
    sp.add_argument(
        "--max-pause", type=positive_float, default=90, help="resume after this many seconds regardless (default 90)"
    )
    sp.add_argument(
        "--pause-timeout", type=positive_float, default=30, help="give up if PAUSE takes longer (default 30)"
    )
    execute(add("prepare-blue", blue.cmd_prepare_blue, "REPLICA IDENTITY FULL where needed, and the probe table"))

    # Replication
    execute(add("copy-schema", replication.cmd_copy_schema, "create GREEN's database and copy BLUE's schema"))
    execute(add("start-replication", replication.cmd_start_replication, "publication on BLUE, subscription on GREEN"))
    sp = add("wait-sync", replication.cmd_wait_sync, "block until the initial copy is done and GREEN has caught up")
    execute(sp)
    sp.add_argument("--interval", type=positive_float, default=30)
    sp.add_argument("--timeout", type=nonnegative_float, default=0, help="minutes; 0 waits forever")
    sp = add("verify", replication.cmd_verify, "compare BLUE and GREEN (read-only)")
    execute(sp)
    sp.add_argument("--json", action="store_true")
    sp.add_argument("--exact-count-rows", type=positive_int, default=100_000, help="count(*) tables up to this size")
    sp.add_argument(
        "--window-rows", type=positive_int, default=2_000, help="rows per hashed id window on bigger tables"
    )
    sp.add_argument("--tables", nargs="*", help="only these tables")
    execute(add("sequences", replication.cmd_sequences, "move GREEN's sequences above BLUE's"))

    # The switch
    sp = add("cutover", switch.cmd_cutover, "switch PgBouncer from BLUE to GREEN")
    execute(sp)
    sp.add_argument(
        "--rehearse", action="store_true", help="with --execute: run the barrier, then resume on BLUE instead of RELOAD"
    )
    sp.add_argument("--pause-timeout", type=positive_float, default=15, help="abort if PAUSE takes longer (default 15)")
    sp.add_argument(
        "--direct-wait", type=positive_float, default=3, help="seconds for BLUE's sessions to close (default 3)"
    )
    sp.add_argument("--catchup-timeout", type=positive_float, default=15, help="seconds for the marker (default 15)")
    sp.add_argument(
        "--max-paused", type=positive_float, default=20, help="abort if paused longer before RELOAD (default 20)"
    )
    sp.add_argument(
        "--max-transaction-seconds", type=positive_float, default=5, help="older BLUE transactions block (default 5)"
    )
    sp = add("finish", switch.cmd_finish, "drop replication and the probe table once BLUE is quiet")
    execute(sp)
    sp.add_argument("--quiet-seconds", type=positive_float, default=60, help="BLUE must see no writes for this long")

    # Terraform
    sp = add("green-tf", terraform.cmd_green_tf, "write GREEN's Terraform file from BLUE's")
    execute(sp)
    sp.add_argument("--engine-version", required=True)
    sp.add_argument("--family", help="parameter group family (default: derived from the engine version)")
    sp.add_argument("--storage-type", help="e.g. gp3 (default: BLUE's)")
    sp.add_argument("--set", action="append", metavar="NAME=HCL", help="another instance argument, e.g. multi_az=false")
    sp.add_argument("--force", action="store_true", help="overwrite an existing GREEN file")
    sp = add("promote-tf", terraform.cmd_promote_tf, "after finish: GREEN takes BLUE's Terraform names")
    execute(sp)
    sp.add_argument("--force", action="store_true", help="overwrite an existing old file")
    return p


SKILL_NAME = "db-cutover"


def skill_dir() -> Path:
    """The agent skill: inside the installed package, or at the repository root in a source checkout."""
    package = Path(__file__).resolve().parent
    for candidate in (package / "skills" / SKILL_NAME, package.parents[1] / "skills" / SKILL_NAME):
        if (candidate / "SKILL.md").is_file():
            return candidate
    raise FileNotFoundError(f"the {SKILL_NAME} skill is missing from this installation")


def skill_command(argv: list[str]) -> None:
    """`db-cutover skill`: takes no config, so it is parsed apart from the cutover commands."""
    p = argparse.ArgumentParser(
        prog="db-cutover skill",
        description="The agent skill that teaches a coding agent to plan, rehearse and run a cutover with an "
        "operator. Without options, prints the skill's directory.",
    )
    p.add_argument(
        "--install", metavar="DIR", help="copy the skill to DIR/db-cutover (e.g. .claude/skills or .agents/skills)"
    )
    p.add_argument("--force", action="store_true", help="with --install: replace a db-cutover skill already there")
    args = p.parse_args(argv)
    source = skill_dir()
    if not args.install:
        print(source)
        return
    target = Path(args.install).expanduser() / SKILL_NAME
    if target.exists():
        if not args.force:
            print(f"{target} already exists: pass --force to replace it", file=sys.stderr)
            sys.exit(1)
        shutil.rmtree(target)
    shutil.copytree(source, target)
    print(f"installed the {SKILL_NAME} skill (version {__version__}) to {target}")


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["skill"]:
        skill_command(argv[1:])
        return
    args = parser().parse_args(argv)
    try:
        ctx = Context(config.load(args.config))
        args.fn(ctx, args)
    except config.ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        sys.exit(2)
    except StepFailed as e:
        print(f"\nFAILED: {e}", file=sys.stderr)
        sys.exit(1)
    except psycopg.Error as e:
        print(f"\nFAILED: {error_detail(e)}", file=sys.stderr)
        sys.exit(1)
    except subprocess.TimeoutExpired:
        print("\nFAILED: external command timed out (command and output withheld)", file=sys.stderr)
        sys.exit(1)
    except (CommandFailed, OSError, ValueError, tomllib.TOMLDecodeError) as e:
        print(f"\nFAILED: {type(e).__name__}; inspect configuration and local tool setup privately", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        sys.exit(130)
