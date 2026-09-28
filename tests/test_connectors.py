"""The connector gap list: reads `.claude.json` and a
project's `.mcp.json` off disk, never the network. Every fixture here lives under `tmp_path`."""
from __future__ import annotations

import json

from pantheon import connectors


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def test_reads_user_scope_servers(tmp_path):
    user_json = tmp_path / ".claude.json"
    _write(user_json, {"mcpServers": {"github": {}, "obsidian": {}}})
    names, problems = connectors.find_mcp_servers(str(tmp_path / "project"), str(user_json))
    assert names == ["github", "obsidian"]
    assert problems == []


def test_reads_the_per_project_entry_by_normalized_path(tmp_path):
    project = tmp_path / "project_lanterns"
    user_json = tmp_path / ".claude.json"
    _write(user_json, {"projects": {str(project).replace("/", "\\"): {"mcpServers": {"loom-os": {}}}}})
    names, _ = connectors.find_mcp_servers(str(project), str(user_json))
    assert names == ["loom-os"]


def test_reads_the_projects_own_mcp_json(tmp_path):
    project = tmp_path / "project_lanterns"
    _write(project / ".mcp.json", {"mcpServers": {"pdf-viewer": {}}})
    names, problems = connectors.find_mcp_servers(str(project), str(tmp_path / "no-user-file.json"))
    assert names == ["pdf-viewer"]
    assert problems == []


def test_merges_user_and_project_servers_without_duplicates(tmp_path):
    project = tmp_path / "project_lanterns"
    user_json = tmp_path / ".claude.json"
    _write(user_json, {"mcpServers": {"github": {}}})
    _write(project / ".mcp.json", {"mcpServers": {"github": {}, "pdf-viewer": {}}})
    names, _ = connectors.find_mcp_servers(str(project), str(user_json))
    assert names == ["github", "pdf-viewer"]


def test_an_unreadable_file_is_reported_not_silently_skipped(tmp_path):
    user_json = tmp_path / ".claude.json"
    user_json.write_text("{ not json", encoding="utf-8")
    names, problems = connectors.find_mcp_servers(str(tmp_path / "project"), str(user_json))
    assert names == []
    assert problems == [f"could not read {user_json}"]


def test_no_files_at_all_is_not_an_error(tmp_path):
    names, problems = connectors.find_mcp_servers(
        str(tmp_path / "project"), str(tmp_path / "nope.json")
    )
    assert names == [] and problems == []


def test_report_includes_the_fixed_sentence_and_the_server_list(tmp_path):
    user_json = tmp_path / ".claude.json"
    _write(user_json, {"mcpServers": {"github": {}}})
    text = connectors.report(str(tmp_path / "project"), str(user_json))
    assert "MCP servers the CLI sees: github" in text
    assert "claude.ai connectors" in text
    assert "never set ANTHROPIC_API_KEY" in text
    assert "claude mcp add" in text


def test_report_says_none_found_rather_than_an_empty_line(tmp_path):
    text = connectors.report(str(tmp_path / "project"), str(tmp_path / "nope.json"))
    assert "MCP servers the CLI sees: none found" in text
