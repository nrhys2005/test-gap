from pathlib import Path

from testgap.generator import find_few_shot_examples


def test_prefers_mirrored_test_file(tmp_project: Path):
    src = tmp_project / "src" / "myapp"
    src.mkdir(parents=True)
    (src / "calc.py").write_text("", encoding="utf-8")

    tests = tmp_project / "tests"
    tests.mkdir()
    (tests / "test_calc.py").write_text(
        "def test_calc_a():\n    assert True\n", encoding="utf-8"
    )
    (tests / "test_other.py").write_text(
        "def test_other_x():\n    assert True\n", encoding="utf-8"
    )

    examples = find_few_shot_examples(
        test_dirs=[tests],
        target_module_path=src / "calc.py",
        project_root=tmp_project,
        max_examples=1,
    )
    assert len(examples) == 1
    assert "test_calc_a" in examples[0]


def test_returns_empty_when_no_tests(tmp_project: Path):
    tests = tmp_project / "tests"
    tests.mkdir()
    examples = find_few_shot_examples(
        test_dirs=[tests],
        target_module_path=tmp_project / "src" / "x.py",
        project_root=tmp_project,
    )
    assert examples == []


def test_truncates_large_files(tmp_project: Path):
    tests = tmp_project / "tests"
    tests.mkdir()
    big = "x = 1\n" * 1000
    (tests / "test_big.py").write_text(big, encoding="utf-8")

    examples = find_few_shot_examples(
        test_dirs=[tests],
        target_module_path=tmp_project / "src" / "big.py",
        project_root=tmp_project,
        max_examples=1,
        max_chars_per_example=200,
    )
    assert len(examples[0]) <= 200
