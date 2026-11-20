"""The daily run of the whole suite, for each repo named: origin/main, in a worktree of its own
(<repo>-daily beside it), every test, no budget.

Meant for a scheduled task whose action is the base interpreter's pythonw.exe running the hidden launcher (the poietic
plugin's tools/run-hidden/run_hidden.py -- python.exe daily.py <repo> ...), never a virtual environment's pythonw.exe, which is a
console program and opens a window. Every process it starts is made with CREATE_NO_WINDOW; it needs only the standard library. Each
repo's times and outcomes go where every run's go (testplan/runs.jsonl in its git directory), so a
test that fails here comes first in the next run; the output goes to testplan/daily.log.

    python -m testplan.daily <repo> [<repo> ...]
    python path/to/testplan/daily.py <repo> [<repo> ...]      (from the task: through the hidden launcher)
"""

import os
import subprocess
import sys
import time
from pathlib import Path

QUIET = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def run(log, *cmd, cwd) -> int:
    log.write(f"\n$ {' '.join(cmd)}\n")
    log.flush()
    return subprocess.run(cmd, cwd=cwd, stdout=log, stderr=subprocess.STDOUT, creationflags=QUIET).returncode


def daily(repo: Path) -> int:
    """The whole suite of origin/main of one repo; its exit code."""
    tree = repo.with_name(repo.name + "-daily")
    common = subprocess.run(["git", "rev-parse", "--git-common-dir"], cwd=repo, capture_output=True,
                            text=True, creationflags=QUIET).stdout.strip()
    store = (repo / common / "testplan").resolve()
    store.mkdir(parents=True, exist_ok=True)
    with open(store / "daily.log", "w", encoding="utf-8") as log:
        log.write(time.strftime("%Y-%m-%d %H:%M:%S") + "\n")
        if run(log, "git", "fetch", "-q", "origin", cwd=repo):
            return 1
        if not tree.exists():
            if run(log, "git", "worktree", "add", "--detach", str(tree), "origin/main", cwd=repo):
                return 1
        elif run(log, "git", "checkout", "-q", "--detach", "origin/main", cwd=tree):
            return 1
        if run(log, "uv", "sync", "-q", cwd=tree):
            return 1
        os.environ["TESTPLAN_ALL"] = "1"  # the one place the whole suite is meant (plugin: --all)
        return run(log, "uv", "run", "--no-sync", "pytest", "--all", "-q", cwd=tree)


def main(repos: list[str]) -> int:
    return max((daily(Path(r).resolve()) for r in repos), default=0)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
