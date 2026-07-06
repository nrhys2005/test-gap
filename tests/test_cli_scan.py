"""CLI tests for ``testgap scan``.

``scan_project`` is monkeypatched in :mod:`testgap.cli_scan` so no coverage
subprocess is spawned. The tests focus on option parsing / rendering.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from testgap.cli import app
from testgap.scan import (
    FileCoverage,
    FunctionCoverage,
    ScanReport,
)

runner = CliRunner()


def _write_config(root: Path) -> None:
    (root / ".testgap.yml").write_text(
        "version: 1\n"
        "project:\n"
        "  source_paths: [src/]\n"
        "  test_paths: [tests/]\n"
        "coverage:\n"
        "  exclude: []\n"
        "llm:\n"
        "  model: fake/model\n"
        "  max_cost_per_run: 0\n",
        encoding="utf-8",
    )


def _fake_report(project_root: Path) -> ScanReport:
    from testgap.coverage import UncoveredFunction

    file_path = project_root / "src" / "demo" / "mod.py"
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text("def x():\n    return 1\n", encoding="utf-8")
    uf = UncoveredFunction(
        file=file_path.resolve(),
        qualname="x",
        start_line=1,
        end_line=2,
        source="def x():\n    return 1\n",
        uncovered_lines=[1, 2],
    )
    func = FunctionCoverage(
        qualname="x",
        start_line=1,
        end_line=2,
        uncovered_lines=[1, 2],
        total_lines=2,
        covered_lines=0,
        _underlying=uf,
    )
    fc = FileCoverage(
        path=file_path.resolve(),
        rel_path="src/demo/mod.py",
        total_lines=2,
        covered_lines=0,
        uncovered_functions=[func],
    )
    return ScanReport(
        project_root=project_root,
        files=[fc],
        total_lines=2,
        covered_lines=0,
        generated_at="2026-07-03T00:00:00+00:00",
    )


@pytest.fixture
def project(tmp_path: Path, monkeypatch) -> Path:
    _write_config(tmp_path)
    monkeypatch.chdir(tmp_path)
    return tmp_path


# ---------------------------------------------------------------------------
# help / options surface
# ---------------------------------------------------------------------------


def test_scan_help_lists_options():
    result = runner.invoke(app, ["scan", "--help"])
    assert result.exit_code == 0
    for opt in ["--below", "--json", "--sort-by"]:
        assert opt in result.output


def test_top_level_help_shows_scan():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "scan" in result.output


# ---------------------------------------------------------------------------
# happy paths
# ---------------------------------------------------------------------------


def test_scan_json_output_is_valid(project: Path, monkeypatch):
    report = _fake_report(project)
    monkeypatch.setattr("testgap.cli_scan.scan_project", lambda root, cfg, **kw: report)

    result = runner.invoke(app, ["scan", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["schema_version"] == 1
    assert payload["files"][0]["rel_path"] == "src/demo/mod.py"


def test_scan_json_output_no_underlying_field(project: Path, monkeypatch):
    """P2: recursively walk JSON output — no _underlying / source keys allowed."""
    report = _fake_report(project)
    monkeypatch.setattr("testgap.cli_scan.scan_project", lambda root, cfg, **kw: report)

    result = runner.invoke(app, ["scan", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    banned = {"_underlying", "source"}

    def walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                assert k not in banned, f"leaked field: {k}"
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(payload)


def test_scan_table_output_lists_files(project: Path, monkeypatch):
    report = _fake_report(project)
    monkeypatch.setattr("testgap.cli_scan.scan_project", lambda root, cfg, **kw: report)

    result = runner.invoke(app, ["scan"])
    assert result.exit_code == 0
    assert "src/demo/mod.py" in result.output


def test_scan_below_filters_correctly(project: Path, monkeypatch):
    """--below is forwarded to scan_project as below_pct."""
    captured: dict = {}

    def spy(root, cfg, **kwargs):
        captured.update(kwargs)
        return _fake_report(project)

    monkeypatch.setattr("testgap.cli_scan.scan_project", spy)
    result = runner.invoke(app, ["scan", "--below", "80"])
    assert result.exit_code == 0
    assert captured.get("below_pct") == 80.0


# ---------------------------------------------------------------------------
# error paths
# ---------------------------------------------------------------------------


def test_scan_missing_config_exits_1(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["scan"])
    assert result.exit_code == 1


def test_scan_invalid_config_exits_1(tmp_path: Path, monkeypatch):
    (tmp_path / ".testgap.yml").write_text("version: 999\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["scan"])
    assert result.exit_code == 1


def test_scan_invalid_sort_exits_1(project: Path):
    result = runner.invoke(app, ["scan", "--sort-by", "wat"])
    assert result.exit_code == 1
