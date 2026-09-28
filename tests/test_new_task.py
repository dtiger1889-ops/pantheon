"""`pantheon new`: write one task file into the standalone folder without opening
Obsidian. `tests/test_standalone.py` proves the standalone source reads whatever this writes;
these tests only cover the writer itself, including the refusal when it would touch the vault.
"""
from __future__ import annotations

from pathlib import Path

import frontmatter
import pytest

from pantheon.tasks import new_task
from pantheon.tasks.obsidian_base import row_from_post


def _toml_path(tmp_path: Path, vault: Path, standalone_folder: Path,
                sprints_folder: str | None = None) -> Path:
    vault_text = str(vault).replace("\\", "/")
    folder_text = str(standalone_folder).replace("\\", "/")
    lines = [f'vault = "{vault_text}"']
    if sprints_folder:
        lines.append(f'sprints_folder = "{sprints_folder}"')
    lines.append("[standalone]")
    lines.append(f'folder = "{folder_text}"')
    path = tmp_path / "pantheon.toml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_writes_a_file_with_the_right_frontmatter(tmp_path):
    standalone = tmp_path / "tasks"
    cfg_path = _toml_path(tmp_path, tmp_path / "vault", standalone)

    rc = new_task.main([
        "Water the plants", "--project", "personal", "--tier", "soon", "--agent",
        "--config", str(cfg_path),
    ])
    assert rc == 0

    files = list(standalone.glob("*.md"))
    assert len(files) == 1
    post = frontmatter.loads(files[0].read_text(encoding="utf-8"))
    row = row_from_post(files[0], post)
    assert row.summary == "Water the plants"
    assert row.project == "personal" and row.tier == "soon"
    assert row.agent is True and row.done is False and row.next is False
    assert row.created is not None


def test_default_tier_and_no_agent(tmp_path):
    standalone = tmp_path / "tasks"
    cfg_path = _toml_path(tmp_path, tmp_path / "vault", standalone)
    rc = new_task.main(["Buy stamps", "--project", "errands", "--config", str(cfg_path)])
    assert rc == 0
    post = frontmatter.loads(next(standalone.glob("*.md")).read_text(encoding="utf-8"))
    row = row_from_post(next(standalone.glob("*.md")), post)
    assert row.tier == "now" and row.agent is False


def test_a_second_task_with_the_same_summary_gets_a_distinct_file(tmp_path):
    standalone = tmp_path / "tasks"
    cfg_path = _toml_path(tmp_path, tmp_path / "vault", standalone)
    new_task.main(["Water the plants", "--project", "personal", "--config", str(cfg_path)])
    new_task.main(["Water the plants", "--project", "personal", "--config", str(cfg_path)])
    assert len(list(standalone.glob("*.md"))) == 2


def test_refuses_to_write_into_the_vault_sprints_folder(tmp_path):
    vault = tmp_path / "vault"
    sprints = vault / "Projects" / "Sprints"
    cfg_path = _toml_path(tmp_path, vault, sprints)  # [standalone] folder IS the vault's Sprints

    rc = new_task.main(["Water the plants", "--project", "personal", "--config", str(cfg_path)])
    assert rc == 2
    assert not sprints.exists() or not list(sprints.glob("*.md"))


def test_refuses_when_standalone_folder_matches_a_custom_sprints_folder(tmp_path, capsys):
    vault = tmp_path / "vault"
    custom_sprints = vault / "Work" / "Tasks"
    cfg_path = _toml_path(tmp_path, vault, custom_sprints, sprints_folder="Work/Tasks")

    rc = new_task.main(["Water the plants", "--project", "personal", "--config", str(cfg_path)])
    assert rc == 2
    err = capsys.readouterr().err
    assert "the vault is never written to" in err


def test_refuses_any_folder_inside_the_vault(tmp_path, capsys):
    """D7 covers the whole vault, not just the Sprints folder: a standalone folder parked at
    `<vault>/Projects/tasks` is still a vault write and is refused."""
    vault = tmp_path / "vault"
    elsewhere_in_vault = vault / "Projects" / "tasks"
    cfg_path = _toml_path(tmp_path, vault, elsewhere_in_vault)

    rc = new_task.main(["Water the plants", "--project", "personal", "--config", str(cfg_path)])
    assert rc == 2
    assert not elsewhere_in_vault.exists()
    assert "inside the vault" in capsys.readouterr().err


def test_allows_a_folder_outside_the_vault(tmp_path):
    vault = tmp_path / "vault"
    outside = tmp_path / "pantheon-tasks"
    cfg_path = _toml_path(tmp_path, vault, outside)

    rc = new_task.main(["Water the plants", "--project", "personal", "--config", str(cfg_path)])
    assert rc == 0
    assert len(list(outside.glob("*.md"))) == 1


def test_the_old_fable_flag_still_hands_a_task_to_the_agents_plate(tmp_path):
    standalone = tmp_path / "tasks"
    cfg_path = _toml_path(tmp_path, tmp_path / "vault", standalone)
    assert new_task.main(["Old habit", "--project", "x", "--fable", "--config", str(cfg_path)]) == 0
    text = next(standalone.glob("*.md")).read_text(encoding="utf-8")
    assert "\nagent: true\n" in text and "fable:" not in text
