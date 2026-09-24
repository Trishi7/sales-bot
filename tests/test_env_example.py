"""`.env.example` must list EVERY variable the code reads, uncommented.

THE BUG THIS PREVENTS is the quiet one: somebody adds `os.getenv("NEW_THING")`
to `config.py`, the default is sensible, nothing breaks in development, and the
variable is never written down. Six months later an operator copies
`.env.example` to `.env`, starts the bot, and gets behaviour nobody can explain
from the file in front of them. The variable exists, it is live, and it is
invisible.

So the file is a CONTRACT, not documentation, and this test enforces it:

    every variable the code reads appears in `.env.example`
    as a real `KEY=value` line, not a commented-out suggestion.

**Uncommented matters.** A commented `# FOO=bar` is a hint; the goal is that
`cp .env.example .env` produces a file that already runs the bot with its
current defaults. If the line is commented, copying the file silently changes
nothing and the default stays hidden in the code.

WHAT COUNTS AS "READ BY THE CODE" is decided by reading the AST rather than by
grepping, so a name inside a docstring or a log message cannot fool it:

  * `os.getenv("X")`, `os.environ.get("X")`, `os.environ["X"]` in a LOAD
    context — an assignment to `os.environ["X"]` in a self-test is a write and
    proves nothing about what the bot reads;
  * the `config.py` helpers, which are all `helper("NAME", default)`;
  * a dict literal whose name contains `ENV`, because `tone.py` reads its five
    dials indirectly through `_ENV[key]` and the call site has no literal to
    find.

Retired variables are the other half of the contract. They live in a RETIRED
block at the bottom, commented out, one line each naming what replaced them —
and this test checks that a retired name has not quietly come back to life in
the code while still being listed as dead.
"""
import ast
import glob
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_EXAMPLE = os.path.join(ROOT, ".env.example")

# The `config.py` readers. Every one takes the variable name as its first
# argument, which is what makes this list enough.
HELPERS = {
    "_int", "_float", "_bool", "_int_list", "_int_set", "_str_list",
    "_lower_str_set", "_weekday", "_weekdays", "_json_object", "_dotenv_value",
}

# Names that are set by a test or a verify script and never read by the bot.
# They are environment variables in the strict sense and not settings, so the
# file does not have to carry them.
NOT_SETTINGS = {"PYTHONPATH", "PYTEST_CURRENT_TEST", "TZ"}

_VAR_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,}$")
# A live line: KEY=..., no leading "#", indentation tolerated.
_LIVE_RE = re.compile(r"^\s*([A-Z][A-Z0-9_]{2,})\s*=", re.M)
# A dead one: "# KEY=..." or "#KEY=...".
_DEAD_RE = re.compile(r"^\s*#\s*([A-Z][A-Z0-9_]{2,})\s*=", re.M)


def _attr(node):
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return ""


def _from_file(path):
    """Every variable name this file READS from the environment."""
    found = set()
    tree = ast.parse(open(path, encoding="utf-8").read(), path)
    for node in ast.walk(tree):
        name = None
        if isinstance(node, ast.Call):
            fn = _attr(node.func)
            base = _attr(node.func.value) if isinstance(node.func, ast.Attribute) else ""
            reads_env = (
                (fn == "getenv" and base in ("os", "environ"))
                or (fn == "get" and base == "environ")
                or fn in HELPERS
            )
            if reads_env and node.args and isinstance(node.args[0], ast.Constant) \
                    and isinstance(node.args[0].value, str):
                name = node.args[0].value
        elif isinstance(node, ast.Subscript):
            if _attr(node.value) == "environ" and isinstance(node.ctx, ast.Load) \
                    and isinstance(node.slice, ast.Constant) \
                    and isinstance(node.slice.value, str):
                name = node.slice.value
        elif isinstance(node, ast.Assign):
            # `_ENV = {"warmth": "SALEY_WARMTH", ...}` — an indirection table.
            targets = [_attr(t) for t in node.targets]
            if any("ENV" in (t or "") for t in targets) \
                    and isinstance(node.value, ast.Dict):
                for value in node.value.values:
                    if isinstance(value, ast.Constant) \
                            and isinstance(value.value, str) \
                            and _VAR_RE.match(value.value):
                        found.add(value.value)
        if name and _VAR_RE.match(name):
            found.add(name)
    return found


def variables_read_by_the_code():
    """Every environment variable any module reads. Sorted, deduplicated."""
    found = set()
    for path in sorted(glob.glob(os.path.join(ROOT, "*.py"))) + \
            sorted(glob.glob(os.path.join(ROOT, "tests", "*.py"))):
        found |= _from_file(path)
    return sorted(found - NOT_SETTINGS)


def _example_text():
    assert os.path.exists(ENV_EXAMPLE), ".env.example is missing entirely"
    return open(ENV_EXAMPLE, encoding="utf-8").read()


def live_keys():
    """Keys set as real `KEY=value` lines — the ones a copied file would use."""
    return set(_LIVE_RE.findall(_example_text()))


def commented_keys():
    """Keys that appear only as `# KEY=value`."""
    return set(_DEAD_RE.findall(_example_text()))


# The banner, matched as a whole line. The word "RETIRED" appears in ordinary
# prose all over this file — several live settings are explained by saying what
# they replaced — so searching for the word alone would scoop up half the file.
_BANNER_RE = re.compile(r"^#\s*RETIRED\b.*$", re.M)


def retired_keys():
    """Keys listed under the RETIRED banner at the bottom of the file."""
    text = _example_text()
    found = list(_BANNER_RE.finditer(text))
    if not found:
        return set()
    return set(_DEAD_RE.findall(text[found[-1].start():]))


class TestEveryVariableIsListed:
    """A variable the code reads but the file does not name is invisible."""

    def test_nothing_the_code_reads_is_missing(self):
        missing = [v for v in variables_read_by_the_code()
                   if v not in live_keys() and v not in commented_keys()]
        assert not missing, (
            "read by the code but absent from .env.example: "
            + ", ".join(missing)
            + ". Add each one as an uncommented KEY=value with its current "
              "default and a comment saying what it does."
        )

    def test_nothing_the_code_reads_is_only_commented_out(self):
        """`cp .env.example .env` must produce a file that runs the bot.

        A commented line is a hint, not a setting: copying the file would leave
        the value in the code where nobody reading `.env` can see it.
        """
        dead = [v for v in variables_read_by_the_code()
                if v not in live_keys() and v in commented_keys()]
        assert not dead, (
            "commented out in .env.example but still read by the code: "
            + ", ".join(dead)
            + ". Uncomment each one and give it its current default."
        )

    def test_secrets_are_present_but_empty(self):
        """The two credentials ship blank, so a copied file cannot leak one."""
        text = _example_text()
        for secret in ("DISCORD_TOKEN", "ANTHROPIC_API_KEY"):
            assert re.search(rf"^{secret}=\s*$", text, re.M), (
                f"{secret} must appear as a bare `{secret}=` with no value"
            )


class TestRetiredVariablesStayRetired:
    """The RETIRED block is a record, and a record can go stale too."""

    def test_a_retired_variable_is_not_read_anywhere(self):
        live = set(variables_read_by_the_code())
        zombies = sorted(retired_keys() & live)
        assert not zombies, (
            "listed as RETIRED but still read by the code: "
            + ", ".join(zombies)
            + ". Either the code should stop reading it, or it is not retired."
        )

    def test_the_retired_block_exists(self):
        assert "RETIRED" in _example_text(), (
            "the RETIRED section is gone — retired variables belong at the "
            "bottom of .env.example, commented, one line each saying what "
            "replaced them"
        )


class TestTheFileIsSane:
    """Cheap checks that catch a mangled edit."""

    def test_no_key_is_defined_twice(self):
        keys = _LIVE_RE.findall(_example_text())
        dupes = sorted({k for k in keys if keys.count(k) > 1})
        assert not dupes, "defined more than once in .env.example: " + ", ".join(dupes)

    def test_every_live_key_is_one_the_code_reads(self):
        """A live key nothing reads is either a typo or a retired setting.

        Retired ones belong in the RETIRED block, commented; a typo belongs
        nowhere. Either way an uncommented line that no module reads is a lie
        about what the bot does.
        """
        stray = sorted(live_keys() - set(variables_read_by_the_code()))
        assert not stray, (
            "set in .env.example but read by nothing: " + ", ".join(stray)
            + ". Move it to the RETIRED block or delete it."
        )
