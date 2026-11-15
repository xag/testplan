"""The plugin, on a small project of its own: what it selects, in which order, and when it stops."""

import json
import re

import pytest

TESTS = '''
import time
from pathlib import Path
import shapes

def test_area():
    assert shapes.area(2) == 4

def test_label():
    assert Path(__file__).with_name("label.txt").read_text() == "box"

def test_slow():
    time.sleep(0.3)
    assert shapes.SIDES == 4
'''


@pytest.fixture
def project(pytester):
    pytester.makepyfile(shapes="SIDES = 4\n\ndef area(side):\n    return side * side\n")
    pytester.makepyfile(test_shapes=TESTS)
    (pytester.path / "label.txt").write_text("box")
    pytester.syspathinsert()
    return pytester


def ran(result) -> list[str]:
    """The tests a verbose run ran, in order."""
    return [m.group(1) for line in result.outlines if (m := re.match(r"\S+::(test_\w+) (PASSED|FAILED)", line))]


def runs(pytester) -> list[dict]:
    lines = (pytester.path / ".pytest_cache/testplan/runs.jsonl").read_text().splitlines()
    return [json.loads(line) for line in lines]


def test_every_run_is_timed_and_a_second_run_selects_nothing(project):
    first = project.runpytest_subprocess("-v")
    assert sorted(ran(first)) == ["test_area", "test_label", "test_slow"]
    slow = next(t for t in runs(project)[0]["tests"] if t["id"].endswith("test_slow"))
    assert slow["o"] == "passed" and slow["s"] >= 0.3
    second = project.runpytest_subprocess("-v")
    assert ran(second) == []
    assert "3 unchanged since they passed" in second.stdout.str()


def test_a_function_body_selects_the_tests_that_ran_it(project):
    project.runpytest_subprocess()
    project.makepyfile(shapes="SIDES = 4\n\ndef area(side):\n    return side ** 2\n")
    assert ran(project.runpytest_subprocess("-v")) == ["test_area"]


def test_a_comment_selects_nothing(project):
    project.runpytest_subprocess()
    project.makepyfile(shapes="SIDES = 4\n\n\ndef area(side):  # a square\n    return side * side\n")
    assert ran(project.runpytest_subprocess("-v")) == []


def test_a_file_a_test_opened_selects_it(project):
    project.runpytest_subprocess()
    (project.path / "label.txt").write_text("crate")
    result = project.runpytest_subprocess("-v")
    assert ran(result) == ["test_label"]
    assert result.ret == 1


def test_module_level_code_selects_every_test_that_loaded_it(project):
    project.runpytest_subprocess()
    project.makepyfile(shapes="SIDES = 5\n\ndef area(side):\n    return side * side\n")
    assert sorted(ran(project.runpytest_subprocess("-v"))) \
        == ["test_area", "test_label", "test_slow"]


def test_last_failure_first_then_fastest(project):
    (project.path / "label.txt").write_text("crate")
    project.runpytest_subprocess()
    project.makepyfile(shapes="SIDES = 4\n\ndef area(side):\n    return side * side + 0\n")
    order = ran(project.runpytest_subprocess("-v"))
    assert order[:2] == ["test_label", "test_area"]


def test_the_budget_stops_the_run_and_says_what_it_left(project):
    project.makepyfile(test_more="import time\n\ndef test_wait():\n    time.sleep(0.5)\n")
    result = project.runpytest_subprocess("-v", "--budget=0.2")
    assert len(ran(result)) < 4
    out = result.stdout.str()
    assert "left by the 0.2 s budget to the daily run (complete as it is: do not run them now)" in out
    assert "this run is complete, the daily run takes the rest" in out


def test_all_is_refused_outside_the_daily_run(project, monkeypatch):
    """A session that sees tests left by the budget does not run them all: --all says whose they are."""
    monkeypatch.delenv("TESTPLAN_ALL", raising=False)
    result = project.runpytest_subprocess("--all")
    assert result.ret == 4 and "--all is the daily run's" in result.stderr.str()


def test_all_runs_everything(project, monkeypatch):
    project.runpytest_subprocess()
    monkeypatch.setenv("TESTPLAN_ALL", "1")
    result = project.runpytest_subprocess("-v", "--all")
    assert len(ran(result)) == 3
    assert runs(project)[-1]["mode"] == "all"
