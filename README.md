# testplan

A pytest plugin that runs the tests most likely to fail, for as long as you allow, and keeps every run's cost.

- **Every run is timed.** Each test's outcome and duration go to `testplan/runs.jsonl` in the repository's git directory (shared by its worktrees), and what each test used to `testplan/deps.json`.
- **Selection** (Ekstazi, Gligoric et al., ISSTA 2015): a test runs when something it used changed since it last passed — a file it executed or opened, in the repo or in a package installed from git; the version of another package it executed; a directory it listed. Python files are compared by syntax tree, so comments and layout change nothing (Liu et al., ISSTA 2023). A change to a file's module-level code selects every test that loaded it.
- **Order** (Cheng et al., ISSTA 2024): last failures first, then recent failures, then the fastest.
- **Budget**: no test starts once `--budget` seconds are spent; by default the time by which four failing runs in five had failed, else 30 s.
- `pytest --all` runs everything with no budget, once a day: `pythonw -m testplan.daily <repo> ...` from a scheduled task. Tests named on the command line run as named.

Not seen: a value a test reads from a cache another test filled, and work in another process or thread. The daily run covers those.

Install as a dev dependency pinned by commit:

```toml
[dependency-groups]
dev = ["pytest>=8.0.0", "testplan"]

[tool.uv.sources]
testplan = { git = "https://github.com/xag/testplan", rev = "<commit>" }
```
