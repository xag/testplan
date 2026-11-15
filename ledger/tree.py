"""The decisions testplan is bound by, as a ledger. Read it with `uv run quern brief .`, never whole.

Vocabulary: ledger@0.8.0 and grounding@1.2.0, pinned in quern.lock.
"""

from __future__ import annotations

import os
from pathlib import Path

import quern.grounding  # noqa: F401
from quern import Node, Quern
from quern.library import consume


def _alt(id_: str, name: str, why: str) -> Node:
    return Node(id=id_, kind="alternative", name=name, payload={"why": why})


def entries() -> list[Node]:
    return [
        Node(
            id="tests-run-by-what-they-use-under-a-budget", kind="decision",
            name="A test run selects the tests whose code, data or packages changed since they passed (the files "
                 "each executed or opened, our git packages' included), runs last failures first then the fastest, "
                 "stops at a time budget set from past runs, and keeps every run's times; the whole suite runs once "
                 "a day (testplan.plugin, testplan.daily)",
            payload={"decided_on": "2026-11-15",
                     "the_owner": "We don't need to run all tests more than once a day; \"when any installed package "
                                  "changes version, testmon throws away its data and reruns everything\" Then it "
                                  "doesn't do the job; the robust approach is to stop using testmon; make sure every "
                                  "run is timed, you keep track of its time when you actually run it, and use that later",
                     "why": "Per-test file dependencies are Ekstazi's (Gligoric et al., ISSTA 2015), which tracks "
                            "library files and opened files as it tracks the project's; files compared by syntax "
                            "tree, as Liu et al. (ISSTA 2023) found changes that alter nothing worth skipping; "
                            "faster tests that failed recently is the best order in Cheng et al. (ISSTA 2024)."},
            children=[
                _alt("testmon", "Select with pytest-testmon",
                     "It measures only the repo's files, stands for a package by its minor version, and throws "
                     "away all its data when one changes: a new pin of a git package reran the whole suite, "
                     "and a new commit at the same version selected nothing."),
                _alt("functions", "Select by the functions each test runs",
                     "Ekstazi found file-level selection saves more than method-level; functions only pay inside "
                     "changed files (HyRTS), worth adding only if files prove too coarse."),
            ],
        ),
        Node(
            id="the-whole-suite-is-the-daily-runs", kind="decision",
            name="A run the budget ended says it is complete and that what it left is the daily run's, in the "
                 "line pytest prints for the stop and in the summary; --all is refused outside the daily run "
                 "(testplan.daily sets TESTPLAN_ALL=1), saying whose the whole suite is and how to ask for it "
                 "all the same",
            payload={"decided_on": "2026-11-15",
                     "the_owner": "pourquoi la suite compl\u00e8te \u2014 tu n'as pas capt\u00e9 qu'on a chang\u00e9 de process de "
                                  "test? \u2014 qu'est-ce qui t'a rendu confu? Fixe le",
                     "why": "After two packages were re-pinned under an app, a run stopped at its budget with "
                            "'3 run, 117 left by the 30 s budget' under pytest's '!!! Interrupted' banner. An "
                            "assistant read that as a run cut short, and ran --all before a deploy, which the "
                            "flag allowed with nothing said."},
            links={"rests_on": ["tests-run-by-what-they-use-under-a-budget"]},
            children=[
                _alt("words-only", "Reword the two lines and leave --all open",
                     "The words are read after the fact; the flag is where the whole suite is asked for, and a "
                     "refusal there is read by whoever is about to run it."),
                _alt("no-flag", "Remove --all and let the daily run call an entry point of its own",
                     "A person sometimes means the whole suite; one variable says so."),
            ],
        ),
    ]


def build() -> Quern:
    root = Path(__file__).resolve().parents[1]
    lib, refs = consume(root, os.environ.get("QUERN_REGISTRY", root.parent / "quern-registry"))
    quern = Quern(packages=list(refs))
    quern = lib.effective(quern)
    quern.root.children = entries()
    return quern
