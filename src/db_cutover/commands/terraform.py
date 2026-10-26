"""Terraform files for GREEN (green-tf) and for promoting it (promote-tf).

Mechanical text edits of HCL, kept as pure functions so they're unit tested. Review `git diff` and a plan before
applying anything they write.

Why promote-tf moves state instead of writing `moved` blocks: GREEN has to take BLUE's addresses, and BLUE has to
leave them, in the same change. Terraform rejects that as moved blocks ("Moved object still exists": a moved block's
`from` can't still be declared), and doing it in two applies leaves a step where nothing is declared under the
primary address that other files (backups, checks, outputs) refer to.
"""

from __future__ import annotations

import argparse
import datetime
import re
import shutil
from pathlib import Path

from db_cutover.config import Config, TerraformLayout
from db_cutover.db import Context
from db_cutover.output import fail, log
from db_cutover.shell import run

LOGICAL_REPLICATION_PARAMETER = """
  # Logical replication, so this instance can be the BLUE side of the next cutover. Static: applied by a reboot.
  parameter {
    name         = "rds.logical_replication"
    value        = "1"
    apply_method = "pending-reboot"
  }"""

TOGGLE_COMMENT = "# false until db-cutover copy-schema has created the database and its tables"


def block_span(text: str, header: str) -> tuple[int, int] | None:
    """(start, end) of the top-level block whose header matches the regex `header`, braces balanced. The end
    includes the newline after the closing brace."""
    match = re.search(rf"^{header}\s*\{{", text, re.M)
    if not match:
        return None
    depth = 0
    for i in range(match.end() - 1, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                return match.start(), end + (1 if text[end : end + 1] == "\n" else 0)
    raise ValueError(f"unbalanced braces after {header}")


def take_block(text: str, header: str) -> str:
    span = block_span(text, header)
    if not span:
        raise ValueError(f"no block matching {header}")
    return text[span[0] : span[1]]


def replace_block(text: str, header: str, new: str) -> str:
    span = block_span(text, header)
    if not span:
        raise ValueError(f"no block matching {header}")
    return text[: span[0]] + new + text[span[1] :]


def resource_header(kind: str, name: str) -> str:
    return rf'resource\s+"{re.escape(kind)}"\s+"{re.escape(name)}"'


def module_header(name: str) -> str:
    return rf'module\s+"{re.escape(name)}"'


def rename_block(block: str, old: str, new: str) -> str:
    """Renames the block's label on its first line."""
    first, _, rest = block.partition("\n")
    return first.replace(f'"{old}"', f'"{new}"', 1) + "\n" + rest


def rename_refs(text: str, prefix: str, old: str, new: str) -> str:
    """aws_db_instance.rds -> aws_db_instance.rds_green (whole names only)."""
    return re.sub(rf"\b{re.escape(prefix)}\.{re.escape(old)}\b", f"{prefix}.{new}", text)


def get_attr(block: str, name: str) -> str | None:
    for line in top_level_lines(block):
        match = re.match(rf"\s*{re.escape(name)}\s*=\s*(.+?)\s*$", line)
        if match:
            return match.group(1)
    return None


def top_level_lines(block: str) -> list[str]:
    """The block's lines that sit directly inside it, not inside a nested block."""
    out, depth = [], 0
    for i, line in enumerate(block.split("\n")):
        if i > 0 and depth == 1:
            out.append(line)
        depth += line.count("{") - line.count("}")
    return out


def set_attr(block: str, name: str, value: str) -> str:
    """Sets a top-level `name = value` (value is raw HCL), adding it at the end of the block when missing.
    Alignment is left to `terraform fmt`."""
    lines = block.split("\n")
    depth = 0
    for i, line in enumerate(lines):
        if i > 0 and depth == 1 and re.match(rf"\s*{re.escape(name)}\s*=", line):
            indent = re.match(r"\s*", line).group(0)  # type: ignore[union-attr]
            lines[i] = f"{indent}{name} = {value}"
            return "\n".join(lines)
        depth += line.count("{") - line.count("}")
    closing = max(i for i, line in enumerate(lines) if line.strip() == "}")
    lines.insert(closing, f"  {name} = {value}")
    return "\n".join(lines)


def remove_attr(block: str, name: str) -> str:
    lines = block.split("\n")
    depth = 0
    for i, line in enumerate(lines):
        if i > 0 and depth == 1 and re.match(rf"\s*{re.escape(name)}\s*=", line):
            del lines[i]
            return "\n".join(lines)
        depth += line.count("{") - line.count("}")
    return block


def family_for(engine_version: str, blue_family: str) -> str:
    """postgres17 -> postgres18 when the major version changes."""
    match = re.match(r"([a-z-]+?)(\d+)$", blue_family)
    major = engine_version.split(".")[0]
    if not match:
        raise ValueError(f"can't derive a parameter group family from {blue_family!r}: pass --family")
    return f"{match.group(1)}{major}"


def green_file(
    blue_text: str,
    layout: TerraformLayout,
    *,
    green_id: str,
    green_host: str | None,
    engine_version: str,
    family: str | None = None,
    storage_type: str | None = None,
    settings: list[str] | None = None,
    today: datetime.date,
    config_name: str,
) -> str:
    inst, pg, mod = layout.instance, layout.parameter_group, layout.app_user_module
    instance = take_block(blue_text, resource_header("aws_db_instance", inst))
    instance = rename_block(instance, inst, f"{inst}_green")
    instance = rename_refs(instance, "aws_db_parameter_group", pg, f"{pg}_green")
    instance = set_attr(instance, "identifier", f'"{green_id}"')
    instance = set_attr(instance, "engine_version", f'"{engine_version}"')
    if storage_type:
        instance = set_attr(instance, "storage_type", f'"{storage_type}"')
    for setting in settings or []:
        name, sep, value = setting.partition("=")
        if not sep:
            raise ValueError(f"--set takes name=value (HCL), got {setting!r}")
        instance = set_attr(instance, name.strip(), value.strip())

    params = take_block(blue_text, resource_header("aws_db_parameter_group", pg))
    params = rename_block(params, pg, f"{pg}_green")
    blue_family = (get_attr(params, "family") or "").strip('"')
    params = set_attr(params, "name", f'"{green_id}-parameters"')
    params = set_attr(params, "family", f'"{family or family_for(engine_version, blue_family)}"')
    if "rds.logical_replication" not in params:
        params = params.rstrip().removesuffix("}").rstrip("\n") + "\n" + LOGICAL_REPLICATION_PARAMETER + "\n}\n"

    module = ""
    if mod:
        module = take_block(blue_text, module_header(mod))
        module = rename_block(module, mod, f"{mod}_green")
        module = rename_refs(module, "aws_db_instance", inst, f"{inst}_green")
        if green_host:
            module = set_attr(module, layout.host_attribute, f'"{green_host}"')
        if layout.app_user_toggle:
            module = set_attr(module, layout.app_user_toggle, "false")
            module = module.replace(
                f"  {layout.app_user_toggle} = false", f"  {TOGGLE_COMMENT}\n  {layout.app_user_toggle} = false", 1
            )
        module += "\n"

    return f"""# GREEN instance of the blue/green cutover started {today.isoformat()}.
# Written by `db-cutover {config_name} green-tf`. After the switch, `promote-tf` moves it to the primary names.

{instance.rstrip()}

{params.rstrip()}

{module}output "green_rds" {{
  value = {{
    address    = aws_db_instance.{inst}_green.address
    identifier = aws_db_instance.{inst}_green.identifier
  }}
  description = "GREEN instance of the blue/green cutover in progress"
}}
"""


def promote_files(
    blue_text: str, green_text: str, layout: TerraformLayout, *, primary_host: str | None, today: datetime.date
) -> tuple[str, str]:
    """(new primary file, old file): GREEN's blocks under BLUE's names in BLUE's file (everything else in it, such
    as outputs, stays), and BLUE's instance and parameter group renamed `_old` in a file of their own."""
    inst, pg, mod = layout.instance, layout.parameter_group, layout.app_user_module

    instance = take_block(green_text, resource_header("aws_db_instance", f"{inst}_green"))
    instance = rename_block(instance, f"{inst}_green", inst)
    instance = rename_refs(instance, "aws_db_parameter_group", f"{pg}_green", pg)
    params = take_block(green_text, resource_header("aws_db_parameter_group", f"{pg}_green"))
    params = rename_block(params, f"{pg}_green", pg)
    primary = replace_block(blue_text, resource_header("aws_db_instance", inst), instance)
    primary = replace_block(primary, resource_header("aws_db_parameter_group", pg), params)
    if mod:
        module = take_block(green_text, module_header(f"{mod}_green"))
        module = rename_block(module, f"{mod}_green", mod)
        module = rename_refs(module, "aws_db_instance", f"{inst}_green", inst)
        module = module.replace(f"  {TOGGLE_COMMENT}\n", "")
        if layout.app_user_toggle:
            module = remove_attr(module, layout.app_user_toggle)
        if primary_host:
            module = set_attr(module, layout.host_attribute, f'"{primary_host}"')
        primary = replace_block(primary, module_header(mod), module)
    # A header written by the 2025 orchestrator or by green-tf no longer describes the file.
    primary = re.sub(r"\A(?:(?://|#)[^\n]*(?:db_cutover|Generated by|GREEN instance)[^\n]*\n)+\n?", "", primary)

    blue_instance = take_block(blue_text, resource_header("aws_db_instance", inst))
    old_id = (get_attr(blue_instance, "identifier") or "").strip('"')
    green_id = (get_attr(instance, "identifier") or "").strip('"')
    old_instance = rename_block(blue_instance, inst, f"{inst}_old")
    old_instance = rename_refs(old_instance, "aws_db_parameter_group", pg, f"{pg}_old")
    old_params = rename_block(take_block(blue_text, resource_header("aws_db_parameter_group", pg)), pg, f"{pg}_old")
    old = f"""# The previous primary, {old_id}: replaced by {green_id} on {today.isoformat()}
# in a blue/green cutover. Retained for investigation; it is stale after the new primary accepts writes.
# To delete it, review retention requirements, turn off deletion_protection, then remove this file.

{old_instance.rstrip()}

{old_params.rstrip()}

output "old_rds" {{
  value = {{
    address    = aws_db_instance.{inst}_old.address
    identifier = aws_db_instance.{inst}_old.identifier
  }}
  description = "The previous primary, until it's deleted"
}}
"""
    return primary, old


def state_commands(layout: TerraformLayout) -> list[str]:
    inst, pg, mod, st = layout.instance, layout.parameter_group, layout.app_user_module, layout.state_command
    commands = []
    if mod:
        # Forget BLUE's app user module rather than destroying it: a destroy drops the role through whichever
        # host the module's provider points at, which is the new primary by then.
        commands.append(f"{st} rm module.{mod}")
    commands += [
        f"{st} mv aws_db_instance.{inst} aws_db_instance.{inst}_old",
        f"{st} mv aws_db_parameter_group.{pg} aws_db_parameter_group.{pg}_old",
        f"{st} mv aws_db_instance.{inst}_green aws_db_instance.{inst}",
        f"{st} mv aws_db_parameter_group.{pg}_green aws_db_parameter_group.{pg}",
    ]
    if mod:
        commands.append(f"{st} mv module.{mod}_green module.{mod}")
    return commands


def _layout(cfg: Config) -> TerraformLayout:
    if not cfg.terraform:
        fail(f"no [terraform] table in {cfg.path}")
    return cfg.terraform


def _fmt(*paths: Path) -> None:
    if shutil.which("terraform"):
        run(["terraform", "fmt", *map(str, paths)], check=False)


def cmd_green_tf(ctx: Context, args: argparse.Namespace) -> None:
    """Writes GREEN's Terraform file from BLUE's: the instance (new identifier and engine version), BLUE's
    parameters plus logical replication, the app user module (switched off until copy-schema), and an output."""
    cfg, layout = ctx.cfg, _layout(ctx.cfg)
    blue_path, target = layout.dir / layout.blue_file, layout.dir / layout.green_file
    if target.exists() and not args.force:
        fail(f"{target} exists (--force rewrites it)")
    stamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
    green_id = layout.green_identifier.replace("{timestamp}", stamp)
    content = green_file(
        blue_path.read_text(),
        layout,
        green_id=green_id,
        green_host=cfg.green.host,
        engine_version=args.engine_version,
        family=args.family,
        storage_type=args.storage_type,
        settings=args.set,
        today=datetime.date.today(),
        config_name=str(cfg.path.name),
    )
    if not args.execute:
        print(content)
        log(f"dry run: would write {target}. Run again with --execute.")
        return
    target.write_text(content)
    _fmt(target)
    log(f"wrote {target} (GREEN {green_id}, engine {args.engine_version}). Review it, then plan.")


def cmd_promote_tf(ctx: Context, args: argparse.Namespace) -> None:
    """After finish: GREEN's blocks take BLUE's names in BLUE's file, BLUE's become `_old` in their own file, and
    GREEN's file goes. Prints the `terraform state` commands that match, to run before planning."""
    cfg, layout = ctx.cfg, _layout(ctx.cfg)
    blue_path, green_path, old_path = (layout.dir / f for f in (layout.blue_file, layout.green_file, layout.old_file))
    if not green_path.exists():
        fail(f"{green_path} doesn't exist")
    if old_path.exists() and not args.force:
        fail(f"{old_path} exists: delete the previous old instance (and its file) first")
    primary, old = promote_files(
        blue_path.read_text(),
        green_path.read_text(),
        layout,
        primary_host=cfg.blue.host,
        today=datetime.date.today(),
    )
    commands = state_commands(layout)
    if not args.execute:
        print(f"==== {blue_path}\n{primary}\n==== {old_path}\n{old}")
        print("==== then, in this order:\n" + "\n".join(f"  {c}" for c in commands))
        log(f"dry run: would write both files and delete {green_path}. Run again with --execute.")
        return
    blue_path.write_text(primary)
    old_path.write_text(old)
    green_path.unlink()
    _fmt(blue_path, old_path)
    print("Now move the state, in this order (each command changes only state, not infrastructure):")
    print("\n".join(f"  {c}" for c in commands))
    log("then plan: expect no create, replace or destroy of the instances, only references that follow the rename")
