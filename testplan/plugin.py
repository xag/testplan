"""Which tests a run runs, in which order, and for how long; every run's cost kept.

Every run is timed: each test's outcome and duration go to runs.jsonl, one line per run, and what
each test used goes to deps.json, both kept in the repository's git directory so every worktree
shares them. A run then:

- selects the tests whose dependencies changed since they last passed, Ekstazi-style (Gligoric et
  al., ISSTA 2015): the files a test executed or opened, in the repo and in the packages installed
  from git, the versions of the other packages it executed, and the directories it listed. A Python
  file is compared by its syntax tree without docstrings, so comments and layout change nothing
  (Liu et al., ISSTA 2023). A change to a file's module-level code (an import, a constant, a
  decorated definition) selects every test that had the file loaded, since no test executes it.
  A test never run, or that did not pass last time, is selected.
- orders them: failed last time first, then those that failed in recent runs, then the fastest
  (Cheng et al., ISSTA 2024: faster tests that recently failed is the best order).
- stops starting tests once the budget is spent (--budget, seconds): by default the time by which
  four failing runs in five had failed, from runs.jsonl, else 30 s.

`pytest --all` runs every test with no budget, and is meant to run once a day (testplan.daily).
Tests named on the command line run as named, with no selection and no budget.

What it cannot see: a value a test reads from a cache another test filled (the code that filled it
does not run again), and work done in another process or thread. The daily run covers those.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import subprocess
import sys
import time
from importlib import metadata

import pytest

TOOL = "testplan"
RECENT = 10  # runs within which a past failure still raises a test's priority
DEFAULT_BUDGET = 30.0
EXCLUDED_DIRS = {".git", ".venv", "__pycache__", ".pytest_cache", "node_modules"}


def pytest_addoption(parser):
    g = parser.getgroup(TOOL)
    g.addoption("--all", action="store_true", help="run every test, no budget (once a day)")
    g.addoption("--budget", type=float, default=None,
                help="stop starting tests after this many seconds (default: from past runs)")


# --- what a key stands for, and its value now ---------------------------------------------------

class World:
    """Classifies a path into a dependency key and tells a key's current value."""

    def __init__(self, root: str):
        self.root = os.path.normcase(os.path.abspath(root))
        self.sites = sorted({os.path.normcase(os.path.abspath(p)) for p in sys.path
                             if p.endswith("site-packages") and os.path.isdir(p)}, key=len, reverse=True)
        self.dist_of: dict[str, str] = {}      # top-level entry in site-packages -> distribution
        self.version: dict[str, str] = {}
        self.from_git: set[str] = set()
        for dist in metadata.distributions():
            name = dist.metadata["Name"]
            self.version[name] = dist.version
            raw = dist.read_text("direct_url.json")
            if raw and ("vcs_info" in (info := json.loads(raw)) or "dir_info" in info):
                self.from_git.add(name)
            for f in dist.files or ():
                if f.parts and not f.parts[0].endswith((".dist-info", ".data")) and f.parts[0] != "..":
                    self.dist_of.setdefault(os.path.normcase(f.parts[0]), name)
        self._cache: dict[tuple[str, str], object] = {}

    def key(self, path) -> str | None:
        try:
            p = os.path.normcase(os.path.abspath(os.fspath(path)))
        except TypeError:
            return None
        for site in self.sites:
            if p.startswith(site + os.sep):
                rel = os.path.relpath(p, site)
                dist = self.dist_of.get(rel.split(os.sep)[0])
                if dist is None:
                    return None
                return "site/" + rel.replace(os.sep, "/") if dist in self.from_git else "dist:" + dist
        if p.startswith(self.root + os.sep):
            rel = os.path.relpath(p, self.root)
            if EXCLUDED_DIRS & set(rel.split(os.sep)):
                return None
            return rel.replace(os.sep, "/")
        return None

    def path(self, key: str) -> str:
        if key.startswith("site/"):
            rel = key[5:].split("/")
            for site in self.sites:
                if os.path.normcase(rel[0]) in self.dist_of:
                    candidate = os.path.join(site, *rel)
                    if os.path.exists(candidate):
                        return candidate
            return os.path.join(self.sites[0] if self.sites else self.root, *rel)
        return os.path.join(self.root, *key.split("/"))

    def value(self, key: str, kind: str = "full"):
        """kind: 'full' (a file's whole content), 'top' (its module-level code), 'dir' (a listing)."""
        if (key, kind) in self._cache:
            return self._cache[key, kind]
        if key.startswith("dist:"):
            v = self.version.get(key[5:])
        else:
            path = self.path(key)
            if kind == "dir":
                v = _digest("\n".join(sorted(os.listdir(path))).encode()) if os.path.isdir(path) else None
            elif not os.path.isfile(path):
                v = None
            elif path.endswith(".py"):
                v = _py_digest(open(path, "rb").read(), top=(kind == "top"))
            else:
                v = _digest(open(path, "rb").read())
        self._cache[key, kind] = v
        return v


def _digest(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()[:16]


def _py_digest(source: bytes, top: bool) -> str:
    """A Python file's syntax tree without docstrings; with top, only what runs when it is imported."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return _digest(source)

    def strip(body: list, outer: bool) -> list:
        kept = []
        for i, node in enumerate(body):
            if i == 0 and isinstance(node, ast.Expr) and isinstance(getattr(node, "value", None), ast.Constant) \
                    and isinstance(node.value.value, str):
                continue
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if top and outer and not node.decorator_list:
                    continue
                node.body = [ast.Pass()] if top else strip(node.body, False) or [ast.Pass()]
            elif isinstance(node, ast.ClassDef):
                node.body = strip(node.body, outer) or [ast.Pass()]
            kept.append(node)
        return kept

    tree.body = strip(tree.body, True)
    return _digest(ast.dump(tree, annotate_fields=False).encode())


# --- recording what a test uses ------------------------------------------------------------------

class Recorder:
    """Collects, while armed, the files whose functions start and the paths opened or listed."""

    def __init__(self):
        self.active: list[dict[str, set]] = []
        mon = sys.monitoring
        self.disable = mon.DISABLE
        self.tool = next(i for i in (3, 4, 2) if mon.get_tool(i) is None)
        mon.use_tool_id(self.tool, TOOL)
        mon.register_callback(self.tool, mon.events.PY_START, self._start)
        mon.set_events(self.tool, mon.events.PY_START)
        sys.addaudithook(self._audit)

    def _start(self, code, offset):
        for rec in self.active:
            rec["run"].add(code.co_filename)
        return self.disable

    def _audit(self, event, args):
        if not self.active:
            return
        if event == "open" and args and isinstance(args[0], (str, os.PathLike)) \
                and not (isinstance(args[1], str) and any(c in args[1] for c in "wax+")):
            for rec in self.active:
                rec["opened"].add(os.fspath(args[0]))
        elif event in ("os.listdir", "os.scandir") and args and isinstance(args[0], (str, os.PathLike)):
            for rec in self.active:
                rec["listed"].add(os.fspath(args[0]))

    def arm(self) -> dict[str, set]:
        rec = {"run": set(), "opened": set(), "listed": set()}
        self.active.append(rec)
        sys.monitoring.restart_events()
        return rec

    def disarm(self, rec):
        self.active.remove(rec)

    def close(self):
        self.active = []
        mon = sys.monitoring
        mon.set_events(self.tool, 0)
        mon.register_callback(self.tool, mon.events.PY_START, None)
        mon.free_tool_id(self.tool)


# --- the plugin ----------------------------------------------------------------------------------

class Plan:
    def __init__(self, config):
        self.config = config
        root = str(config.rootpath)
        self.world = World(root)
        try:
            common = subprocess.run(["git", "rev-parse", "--git-common-dir"], cwd=root,
                                    capture_output=True, text=True, check=True).stdout.strip()
            self.store = os.path.join(root, common, TOOL)
        except (OSError, subprocess.CalledProcessError):
            self.store = os.path.join(root, ".pytest_cache", TOOL)
        os.makedirs(self.store, exist_ok=True)
        self.deps_file = os.path.join(self.store, "deps.json")
        self.runs_file = os.path.join(self.store, "runs.jsonl")
        self.deps = _load_json(self.deps_file)
        self.runs = _load_runs(self.runs_file)
        self.recorder = Recorder()
        self.fixture_deps: dict[tuple, dict[str, set]] = {}
        self.results: dict[str, dict] = {}
        self.pending: dict[str, dict] = {}
        self.order: list[str] = []
        self.everything = config.getoption("all")
        self.named = config.args_source == pytest.Config.ArgsSource.ARGS and not self.everything
        self.budget = None if self.everything or self.named else (config.getoption("budget") or self._budget())
        self.started = None
        self.left_by_budget = 0
        self.unchanged = 0

    def _budget(self) -> float:
        """The time by which four failing runs in five had failed, once ten have failed."""
        firsts = []
        for run in self.runs:
            spent = 0.0
            for t in run.get("tests", []):
                spent += t["s"]
                if t["o"] == "failed":
                    firsts.append(spent)
                    break
        if len(firsts) < 10:
            return DEFAULT_BUDGET
        firsts.sort()
        return round(firsts[int(0.8 * (len(firsts) - 1))] + 0.5, 1)

    # selection and order

    def changed(self, nodeid: str) -> bool:
        rec = self.deps.get(nodeid)
        if rec is None or rec.get("outcome") != "passed":
            return True
        w = self.world
        return (any(w.value(k) != v for k, v in rec["files"].items())
                or any(w.value(k, "top") != v for k, v in rec["loaded"].items())
                or any(w.value(k, "dir") != v for k, v in rec["dirs"].items())
                or rec.get("python") != sys.version)

    def priority(self, nodeid: str):
        return (self.last.get(nodeid) != "failed", nodeid not in self.failed_recently,
                self.deps.get(nodeid, {}).get("seconds", 0.0))

    @pytest.hookimpl(trylast=True)
    def pytest_collection_modifyitems(self, session, config, items):
        if self.named:
            return
        if self.everything:
            chosen = list(items)
        else:
            chosen = [i for i in items if self.changed(i.nodeid)]
            dropped = [i for i in items if i not in chosen]
            self.unchanged = len(dropped)
            if dropped:
                config.hook.pytest_deselected(items=dropped)
        self.last = {t["id"]: t["o"] for run in self.runs for t in run.get("tests", [])}
        self.failed_recently = {t["id"] for run in self.runs[-RECENT:] for t in run.get("tests", [])
                                if t["o"] == "failed"}
        chosen.sort(key=lambda i: self.priority(i.nodeid))
        items[:] = chosen

    # recording

    @pytest.hookimpl(hookwrapper=True)
    def pytest_fixture_setup(self, fixturedef, request):
        rec = self.recorder.arm()
        try:
            yield
        finally:
            self.recorder.disarm(rec)
            key = (fixturedef.baseid, fixturedef.argname, getattr(request, "param_index", 0))
            self.fixture_deps[key] = rec

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_protocol(self, item, nextitem):
        if self.started is None:
            self.started = time.monotonic()
        rec = self.recorder.arm()
        self.results[item.nodeid] = {"o": "passed", "s": 0.0}
        try:
            yield
        finally:
            self.recorder.disarm(rec)
            for (baseid, name, _), frec in self.fixture_deps.items():
                if name in getattr(item, "fixturenames", ()) and item.nodeid.startswith(baseid):
                    for part in rec:
                        rec[part] |= frec[part]
            self._remember(item.nodeid, rec)
            self.order.append(item.nodeid)
            if self.budget is not None and nextitem is not None \
                    and time.monotonic() - self.started >= self.budget:
                self.left_by_budget = len(item.session.items) - len(self.order)
                item.session.shouldstop = f"the {self.budget:g} s budget is spent"

    def pytest_runtest_logreport(self, report):
        r = self.results.setdefault(report.nodeid, {"o": "passed", "s": 0.0})
        r["s"] += report.duration
        if report.failed:
            r["o"] = "failed"
        elif report.skipped and r["o"] == "passed" and report.when == "call":
            r["o"] = "skipped"

    def _remember(self, nodeid: str, rec):
        w = self.world
        files = {}
        for path in rec["run"] | rec["opened"]:
            k = w.key(path)
            if k is not None:
                files[k] = w.value(k)
        dirs = {k: w.value(k, "dir") for p in rec["listed"] if (k := w.key(p)) is not None}
        loaded = {}
        for mod in list(sys.modules.values()):
            f = getattr(mod, "__file__", None)
            if f and f.endswith(".py") and (k := w.key(f)) is not None and not k.startswith("dist:"):
                loaded[k] = w.value(k, "top")
        self.pending[nodeid] = {"files": files, "dirs": dirs, "loaded": loaded}

    def pytest_sessionfinish(self, session, exitstatus):
        tests = [{"id": n, "o": self.results[n]["o"], "s": round(self.results[n]["s"], 3)}
                 for n in self.order if n in self.results]
        run = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "mode": "all" if self.everything else
               "named" if self.named else "selected", "budget": self.budget,
               "seconds": round(sum(t["s"] for t in tests), 3), "tests": tests}
        with open(self.runs_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(run) + "\n")
        deps = _load_json(self.deps_file)  # another worktree may have written since
        for nodeid, rec in self.pending.items():
            r = self.results[nodeid]
            deps[nodeid] = {**rec, "outcome": r["o"], "seconds": round(r["s"], 3), "python": sys.version}
        tmp = self.deps_file + f".{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(deps, f)
        os.replace(tmp, self.deps_file)

    def pytest_unconfigure(self, config):
        self.recorder.close()

    def pytest_terminal_summary(self, terminalreporter):
        ran = len(self.order)
        parts = [f"{ran} run"]
        if self.unchanged:
            parts.append(f"{self.unchanged} unchanged since they passed")
        if self.left_by_budget:
            parts.append(f"{self.left_by_budget} left by the {self.budget:g} s budget")
        terminalreporter.write_line(f"{TOOL}: " + ", ".join(parts) + f"; times kept in {self.runs_file}")


def _load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _load_runs(path):
    runs = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    runs.append(json.loads(line))
                except ValueError:
                    pass
    except OSError:
        pass
    return runs


def pytest_configure(config):
    if not hasattr(sys, "monitoring"):
        return
    config.pluginmanager.register(Plan(config), TOOL + "-run")
