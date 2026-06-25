from pathlib import Path

from testgap.detect import LayoutKind, detect_layout, detect_source_paths


def test_src_layout(tmp_project: Path):
    pkg = tmp_project / "src" / "myapp"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")

    result = detect_layout(tmp_project)
    assert result.kind == LayoutKind.SRC
    assert detect_source_paths(tmp_project) == ["src/"]


def test_flat_layout(tmp_project: Path):
    pkg = tmp_project / "myapp"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")

    result = detect_layout(tmp_project)
    assert result.kind == LayoutKind.FLAT
    assert detect_source_paths(tmp_project) == ["myapp/"]


def test_flat_layout_excludes_common_dirs(tmp_project: Path):
    for excluded in ("tests", "docs", ".venv"):
        d = tmp_project / excluded
        d.mkdir()
        (d / "__init__.py").write_text("", encoding="utf-8")

    pkg = tmp_project / "myapp"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")

    result = detect_layout(tmp_project)
    assert result.kind == LayoutKind.FLAT
    assert [p.name for p in result.candidates] == ["myapp"]


def test_unknown_when_no_packages(tmp_project: Path):
    result = detect_layout(tmp_project)
    assert result.kind == LayoutKind.UNKNOWN
    assert detect_source_paths(tmp_project) == []


def test_multiple_flat_candidates(tmp_project: Path):
    for name in ("alpha", "beta"):
        d = tmp_project / name
        d.mkdir()
        (d / "__init__.py").write_text("", encoding="utf-8")

    result = detect_layout(tmp_project)
    assert result.kind == LayoutKind.FLAT
    assert sorted(p.name for p in result.candidates) == ["alpha", "beta"]
