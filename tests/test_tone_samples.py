"""NFT2-1064 — tools/tone_samples.py, tested WITHOUT the model (plan 2.8, 5.3).

Two kinds of check:

  STATIC   read the script's source and parse it. Never executes it. Always run.
           The key is read from the environment before any project import, `.env`
           and dotenv are never touched, the cap is a constant of 25, the SDK's own
           retries are off, and the key never reaches a print, a log call or an
           f-string.

  RUNNING  execute the script in a SANDBOX COPY of the repo (python files, markdown,
           tools/, fixtures; no .env, no database), with `anthropic.Anthropic`
           replaced by a counting fake and an unroutable proxy set, so a stray real
           call fails loudly. Gated behind `pytest --run-tone-samples`: the script is
           the human's, so these only run when the lead has said they may.

Nothing here ever passes --live-model to a real client.
"""
import ast
import glob
import os
import re
import runpy
import shutil
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "tools", "tone_samples.py")
MESSAGE = "Set ANTHROPIC_API_KEY in your shell first. I do not read it from .env."
gated = pytest.mark.tone_samples_run   # skipped unless `pytest --run-tone-samples` (see conftest)


def source():
    with open(SCRIPT, encoding="utf-8") as f:
        return f.read()


def project_modules():
    names = {os.path.splitext(os.path.basename(p))[0] for p in glob.glob(os.path.join(ROOT, "*.py"))}
    return names | {"tools", "tests"}


@pytest.fixture(autouse=True)
def _gate(request):
    if request.node.get_closest_marker("tone_samples_run") and not request.config.getoption("--run-tone-samples"):
        pytest.skip("runs tools/tone_samples.py (fake model, sandbox): use `pytest --run-tone-samples`")


class TestStatic:
    def test_the_key_is_read_from_the_environment_before_any_project_import(self):
        tree = ast.parse(source())
        projects = project_modules()
        key_line = first_project_import = None
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                mods = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
                if any(m.split(".")[0] in projects for m in mods):
                    if first_project_import is None or node.lineno < first_project_import:
                        first_project_import = node.lineno
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "KEY" for t in node.targets):
                seg = ast.get_source_segment(source(), node.value) or ""
                if "environ" in seg and "ANTHROPIC_API_KEY" in seg:
                    key_line = node.lineno if key_line is None else min(key_line, node.lineno)
        assert key_line, "no module-level KEY = os.environ.get('ANTHROPIC_API_KEY', ...)"
        assert first_project_import is None or key_line < first_project_import, \
            "config.py calls load_dotenv() at import: the key must be read BEFORE any project import"

    def test_it_never_touches_dotenv_or_the_config_key(self):
        src = source()
        code = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))
        assert "dotenv" not in code
        assert "config.ANTHROPIC_API_KEY" not in code
        assert not re.search(r"open\([^)]*\.env", code)
        assert not re.search(r"""['"]\.env['"]""", code.replace(MESSAGE, "")), "the script must not name .env as a file"

    def test_dotenv_never_overrides_the_environment(self):
        """The script trusts that a name already set in the shell (even empty) beats the .env file."""
        for p in glob.glob(os.path.join(ROOT, "*.py")) + glob.glob(os.path.join(ROOT, "tools", "*.py")):
            with open(p, encoding="utf-8") as f:
                for line in f:
                    if "load_dotenv(" in line and not line.strip().startswith(("#", "from")):
                        assert "override" not in line, (p, line)

    def test_the_cap_is_a_constant_of_25_and_retries_are_off(self):
        src = source()
        assert re.search(r"^HARD_CAP\s*=\s*25\s*$", src, re.M)
        assert "max_retries=0" in src
        assert "CapReached" in src
        assert "--live-model" in src and "--max-calls" in src and "--record" in src and "--no-voice" in src and "--voice-db" in src

    def test_the_refusal_message_is_exact(self):
        assert MESSAGE in source()

    def test_the_key_never_reaches_output(self):
        tree = ast.parse(source())
        bad = []

        def names_in(node):
            return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)} | \
                   {n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)}

        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                is_out = (isinstance(fn, ast.Name) and fn.id == "print") or \
                         (isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name)
                          and fn.value.id in ("log", "logging", "logger"))
                if is_out:
                    for a in list(node.args) + [k.value for k in node.keywords]:
                        if {"KEY", "api_key"} & names_in(a):
                            bad.append(node.lineno)
            if isinstance(node, ast.JoinedStr):
                inner = {n.id for v in node.values if isinstance(v, ast.FormattedValue)
                         for n in ast.walk(v) if isinstance(n, ast.Name)}
                if {"KEY", "api_key"} & inner:
                    bad.append(node.lineno)
        assert not bad, f"the key appears in a print / log / f-string at lines {bad}"

    def test_the_prompts_come_from_the_baseline_and_the_engine(self):
        src = source()
        assert "tone_baseline" in src and "_system_blocks" in src
        assert "replyguard" in src

    def test_it_is_not_imported_by_the_bot(self):
        for p in glob.glob(os.path.join(ROOT, "*.py")):
            with open(p, encoding="utf-8") as f:
                body = f.read()
            assert "tone_samples" not in body or os.path.basename(p).startswith("verify_"), p

    def test_the_page_does_not_exist_until_the_human_says_go(self):
        assert not os.path.exists(os.path.join(ROOT, "docs", "tone-samples.md"))


# -- RUNNING (gated) ------------------------------------------------------------------------

@pytest.fixture
def sandbox(tmp_path):
    """A copy of the repo's code with no .env, no database and no notes."""
    dst = tmp_path / "repo"
    dst.mkdir()
    for pat in ("*.py", "*.md", "*.json", "*.txt"):
        for p in glob.glob(os.path.join(ROOT, pat)):
            if not os.path.basename(p).startswith(".env"):
                shutil.copy2(p, dst)
    shutil.copytree(os.path.join(ROOT, "tools"), dst / "tools", ignore=shutil.ignore_patterns("__pycache__"))
    (dst / "tests").mkdir()
    shutil.copytree(os.path.join(ROOT, "tests", "fixtures"), dst / "tests" / "fixtures")
    shutil.copy2(os.path.join(ROOT, "tests", "offline_guard.py"), dst / "tests" / "offline_guard.py")
    (dst / "docs").mkdir()
    return dst


def OUT_ARGS(sandbox):
    """Where the sandboxed script writes, so nothing ever lands in the real docs/ or tests/fixtures."""
    return ["--out", str(sandbox / "out.md"), "--fixtures", str(sandbox / "fx.json")]


def tree_state(path):
    out = {}
    for base in ("docs", "tests"):
        for dp, dn, fn in os.walk(os.path.join(path, base)):
            dn[:] = [d for d in dn if d != "__pycache__"]
            for f in fn:
                p = os.path.join(dp, f)
                out[os.path.relpath(p, path)] = (os.path.getsize(p), os.path.getmtime(p))
    return out


def env_for(extra=None, drop=()):
    env = {k: v for k, v in os.environ.items() if k not in drop and not k.startswith("ANTHROPIC")}
    env.update({"HTTPS_PROXY": "http://127.0.0.1:9", "HTTP_PROXY": "http://127.0.0.1:9", "ALL_PROXY": "http://127.0.0.1:9",
                "NO_PROXY": "", "DISCORD_TOKEN": "x"})
    env.update(extra or {})
    return env


REAL_REQUESTS = []


class CountingClient:
    """Stands in for anthropic.Anthropic. Counts every messages.create and remembers how it was built."""

    instances = []

    def __init__(self, *a, **kw):
        self.kw = kw
        self.calls = 0
        self.fail = False
        CountingClient.instances.append(self)
        outer = self

        class _M:
            def create(self_inner, **k):
                outer.calls += 1
                CountingClient.total += 1
                if CountingClient.fail_all:
                    raise RuntimeError("scripted failure")
                if CountingClient.behavior:
                    CountingClient.behavior(k, CountingClient.total)    # may raise; counted either way
                CountingClient.requests.append(k)
                from types import SimpleNamespace
                return SimpleNamespace(
                    content=[SimpleNamespace(type="text", text="Acme replied on 12 Aug.", citations=[])],
                    stop_reason="end_turn",
                    usage=SimpleNamespace(input_tokens=10, output_tokens=5, cache_creation_input_tokens=0,
                                          cache_read_input_tokens=0,
                                          server_tool_use=SimpleNamespace(web_search_requests=0)))

        self.messages = _M()

    total = 0
    fail_all = False
    behavior = None                  # callable(request_kwargs, call_number) that may raise
    requests = []                    # the request of every call that did not raise


def run_script(sandbox_dir, argv, monkeypatch, *, key="sk-ant-SENTINEL", fail=False, capsys=None, behavior=None):
    import anthropic

    import httpx

    def no_real_request(self, *a, **k):
        REAL_REQUESTS.append(1)
        raise AssertionError("a real HTTP request was attempted")

    REAL_REQUESTS.clear()
    monkeypatch.setattr(httpx.Client, "send", no_real_request)
    for var in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY"):
        monkeypatch.setenv(var, "http://127.0.0.1:9")
    CountingClient.instances.clear()
    CountingClient.total = 0
    CountingClient.fail_all = fail
    CountingClient.behavior = behavior
    CountingClient.requests = []
    monkeypatch.setattr(anthropic, "Anthropic", CountingClient)
    monkeypatch.setenv("ANTHROPIC_API_KEY", key)
    monkeypatch.setenv("DISCORD_TOKEN", "x")
    monkeypatch.chdir(sandbox_dir)
    monkeypatch.setattr(sys, "argv", [str(sandbox_dir / "tools" / "tone_samples.py")] + argv)
    saved_path = list(sys.path)
    saved_mods = set(sys.modules)
    try:
        sys.path.insert(0, str(sandbox_dir))
        try:
            runpy.run_path(str(sandbox_dir / "tools" / "tone_samples.py"), run_name="__main__")
            code = 0
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else (0 if e.code in (None, 0) else 1)
    finally:
        sys.path[:] = saved_path
        for m in set(sys.modules) - saved_mods:           # do not leak the sandbox's modules into other tests
            sys.modules.pop(m, None)
    return code


@gated
class TestRunning:
    def test_the_dry_run_makes_no_model_call_and_writes_nothing(self, sandbox):
        before = tree_state(sandbox)
        p = subprocess.run([sys.executable, str(sandbox / "tools" / "tone_samples.py")], cwd=str(sandbox),
                           env=env_for({"ANTHROPIC_API_KEY": "sk-ant-SENTINEL"}), capture_output=True, text=True,
                           timeout=240)
        assert p.returncode == 0, p.stderr[-800:]
        assert "SENTINEL" not in p.stdout + p.stderr
        assert tree_state(sandbox) == before, "a dry run must not write under docs/ or tests/"
        assert "Evidence is canned test data" in p.stdout

    def test_live_model_without_a_key_exits_2_and_never_uses_the_decoy(self, sandbox):
        (sandbox / ".env").write_text("ANTHROPIC_API_KEY=sk-ant-DECOY\n")
        before = tree_state(sandbox)
        p = subprocess.run([sys.executable, str(sandbox / "tools" / "tone_samples.py"), "--live-model", *OUT_ARGS(sandbox)],
                           cwd=str(sandbox), env=env_for(), capture_output=True, text=True, timeout=120)
        assert p.returncode == 2, (p.returncode, p.stdout[-400:], p.stderr[-400:])
        assert MESSAGE in p.stdout + p.stderr
        assert "DECOY" not in p.stdout + p.stderr
        assert tree_state(sandbox) == before and not (sandbox / "out.md").exists()

    def test_twenty_calls_for_ten_questions_and_the_key_is_not_printed(self, sandbox, monkeypatch, capsys):
        code = run_script(sandbox, ["--live-model", "--no-voice", *OUT_ARGS(sandbox)], monkeypatch)
        out = capsys.readouterr()
        assert code == 0
        assert CountingClient.total == 20
        assert all(c.kw.get("max_retries") == 0 for c in CountingClient.instances)
        assert all(c.kw.get("api_key") == "sk-ant-SENTINEL" for c in CountingClient.instances)
        page = (sandbox / "out.md").read_text(encoding="utf-8")
        assert "SENTINEL" not in page + out.out + out.err
        assert REAL_REQUESTS == []
        assert page.splitlines()[0].startswith("Evidence is canned test data")
        assert page.count("BEFORE") >= 10 and page.count("AFTER") >= 10

    def test_every_failing_call_stops_at_the_cap_with_exit_3(self, sandbox, monkeypatch):
        code = run_script(sandbox, ["--live-model", "--no-voice", *OUT_ARGS(sandbox)], monkeypatch, fail=True)
        assert code == 3 and CountingClient.total <= 25
        page = sandbox / "out.md"
        assert not page.exists() or "INCOMPLETE" in page.read_text(encoding="utf-8")

    def test_max_calls_is_clamped_to_25(self, sandbox, monkeypatch):
        run_script(sandbox, ["--live-model", "--no-voice", *OUT_ARGS(sandbox), "--max-calls", "30"], monkeypatch, fail=True)
        assert CountingClient.total <= 25

    def test_max_calls_4_stops_after_4_and_says_incomplete(self, sandbox, monkeypatch):
        code = run_script(sandbox, ["--live-model", "--no-voice", *OUT_ARGS(sandbox), "--max-calls", "4"], monkeypatch)
        assert CountingClient.total == 4 and code == 3
        assert "INCOMPLETE" in (sandbox / "out.md").read_text(encoding="utf-8")

    def test_before_and_after_get_identical_messages(self, sandbox, monkeypatch):
        seen = []

        orig = CountingClient.__init__

        def spy(self, *a, **kw):
            orig(self, *a, **kw)
            create = self.messages.create

            def rec(**k):
                seen.append(k)
                return create(**k)
            self.messages.create = rec

        monkeypatch.setattr(CountingClient, "__init__", spy)
        run_script(sandbox, ["--live-model", "--no-voice", *OUT_ARGS(sandbox), "--max-calls", "4"], monkeypatch)
        assert len(seen) == 4
        for before, after in zip(seen[0::2], seen[1::2]):
            assert before["messages"] == after["messages"]
            assert before["system"] != after["system"]
            assert not before.get("tools") and not after.get("tools")


    def test_the_page_labels_its_facts_canned_and_says_the_guard_ran(self, sandbox, monkeypatch):
        run_script(sandbox, ["--live-model", "--no-voice", *OUT_ARGS(sandbox), "--max-calls", "4"], monkeypatch)
        page = (sandbox / "out.md").read_text(encoding="utf-8")
        assert page.splitlines()[0].startswith("Evidence is canned test data")
        assert "canned" in page.lower()
        assert "Offline guard: loaded" in page and "no real Sheets or Drive call" in page

    def test_a_live_run_without_the_guard_module_exits_2_before_any_call(self, sandbox):
        os.remove(sandbox / "tests" / "offline_guard.py")
        p = subprocess.run([sys.executable, str(sandbox / "tools" / "tone_samples.py"), "--live-model", "--no-voice",
                            *OUT_ARGS(sandbox)], cwd=str(sandbox), env=env_for({"ANTHROPIC_API_KEY": "sk-ant-SENTINEL"}),
                           capture_output=True, text=True, timeout=120)
        assert p.returncode == 2, (p.returncode, p.stderr[-300:])
        assert "offline_guard.py is missing" in p.stderr + p.stdout
        assert "SENTINEL" not in p.stdout + p.stderr and not (sandbox / "out.md").exists()

    def test_a_dry_run_without_the_guard_module_says_so(self, sandbox):
        os.remove(sandbox / "tests" / "offline_guard.py")
        p = subprocess.run([sys.executable, str(sandbox / "tools" / "tone_samples.py"), *OUT_ARGS(sandbox)],
                           cwd=str(sandbox), env=env_for(), capture_output=True, text=True, timeout=240)
        assert p.returncode == 0, p.stderr[-300:]
        assert "offline guard NOT loaded" in p.stderr and "offline guard NOT loaded" in p.stdout
