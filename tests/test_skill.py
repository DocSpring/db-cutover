import re
import tomllib
from pathlib import Path

import pytest

from db_cutover import __version__, cli

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / "skills" / "db-cutover"


def frontmatter() -> dict[str, str]:
    text = (SKILL / "SKILL.md").read_text()
    block = text.split("---\n")[1]
    return dict(re.findall(r"^\s*(\w+): (.+)$", block, flags=re.MULTILINE))


def test_versions_agree():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert project["version"] == __version__
    assert frontmatter()["version"].strip('"') == __version__
    assert f"## {__version__} " in (ROOT / "CHANGELOG.md").read_text()
    assert f"db-cutover=={__version__}" in (ROOT / "README.md").read_text()


def test_skill_follows_the_agent_skills_format():
    fields = frontmatter()
    assert fields["name"] == SKILL.name == cli.SKILL_NAME
    assert len(fields["description"]) <= 1024
    assert len((SKILL / "SKILL.md").read_text().splitlines()) < 500


def test_skill_references_exist():
    text = (SKILL / "SKILL.md").read_text()
    references = set(re.findall(r"`(references/[\w-]+\.md)`", text))
    assert references == {f"references/{p.name}" for p in (SKILL / "references").iterdir()}


def test_skill_names_every_command():
    """A new command needs a line in the skill's command reference."""
    commands = next(a for a in cli.parser()._actions if a.dest == "command").choices
    text = (SKILL / "references" / "commands.md").read_text()
    missing = [name for name in commands if f"`{name}" not in text]
    assert not missing


def test_skill_prints_its_directory(capsys):
    cli.main(["skill"])
    assert Path(capsys.readouterr().out.strip()) == SKILL


def test_skill_install_copies_and_refuses_to_overwrite(tmp_path, capsys):
    cli.main(["skill", "--install", str(tmp_path)])
    installed = tmp_path / "db-cutover"
    assert (installed / "SKILL.md").read_text() == (SKILL / "SKILL.md").read_text()
    assert (installed / "references" / "recovery.md").is_file()

    (installed / "SKILL.md").write_text("changed")
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["skill", "--install", str(tmp_path)])
    assert exit_info.value.code == 1
    assert (installed / "SKILL.md").read_text() == "changed"

    cli.main(["skill", "--install", str(tmp_path), "--force"])
    assert (installed / "SKILL.md").read_text() == (SKILL / "SKILL.md").read_text()


def test_version_flag(capsys):
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["--version"])
    assert exit_info.value.code == 0
    assert capsys.readouterr().out.strip() == f"db-cutover {__version__}"
