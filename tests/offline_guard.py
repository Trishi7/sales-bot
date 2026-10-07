"""THE OFFLINE GUARD — one helper every test and verify script that builds a prompt or drives the
answer path installs, so nothing in a test can read the real Sheets or Drive.

WHY. Building a system prompt calls `sources.describe_for_prompt()`, which asks every source for
its status; on a laptop with credentials that is a live, read-only call to the real GTM sheet, the
mapping sheet and the Drive to-do file. A test that does that is not offline, and a test that
quietly depends on the answer is worse. Two parts:

  1. `sources.status_report` returns CANNED rows (so a prompt builds the same everywhere).
  2. Every route to a real Google call is patched at the lowest practical point and RAISES
     `LiveCallBlocked` (an AssertionError) after counting it:
       google.oauth2.service_account.Credentials.from_service_account_file
           (gtm_sheet, mapping_sheet and drive all authorise through this one class)
       gspread.authorize                                  (the Sheets client)
       google.auth.transport.requests.AuthorizedSession.request   (every Drive / Sheets REST call)
     A bot that catches the exception and carries on (check_access does) still leaves a count,
     so the script asserts `guard.calls == []` at the end and prints the list if it is not.

USE
    # a verify script, after `import config` and before the first prompt is built
    sys.path.insert(0, os.path.join(HERE, "tests"))
    import offline_guard
    GUARD = offline_guard.install()
    ...
    check("no live Sheets / Drive call", GUARD.calls, [])

    # pytest: tests/conftest.py installs it for every test (autouse) and fails the test at teardown
    # if a call was attempted. A test that exercises the real classes with their own fakes is
    # unaffected: only the real network / credential entry points are blocked.

NOT COVERED, on purpose: search, feeds, Discord and the model (each script fakes those itself);
the rclone sync command (a subprocess, faked by the scripts that run a sync).
Idempotent; `uninstall()` restores everything.
"""
import contextlib

CANNED_STATUS = [
    {"key": key, "label": label, "purpose": "canned for the test", "status": "connected",
     "detail": "canned, not probed"}
    for key, label in (("sales_spreadsheet", "Sales spreadsheet"), ("researcher_mapping", "Researcher mapping"),
                       ("strategy_doc", "Strategy doc"), ("sales_meeting_notes", "Sales meeting notes"),
                       ("todo_sheet", "To-do sheet"))
]


class LiveCallBlocked(AssertionError):
    """A test tried to reach the real Google Sheets or Drive."""


RealCallInTest = LiveCallBlocked          # the name the plan (D13) uses


class Guard:
    def __init__(self, statuses=None):
        self.statuses = statuses
        self.calls = []                  # one string per blocked call
        self._undo = []
        self.installed = False

    def _statuses(self):
        return [dict(r) for r in (self.statuses if self.statuses is not None else CANNED_STATUS)]

    @property
    def count(self):
        return len(self.calls)

    def _block(self, what):
        def boom(*a, **k):
            self.calls.append(what)
            raise LiveCallBlocked(f"{what}: a test tried to reach the real Sheets/Drive")
        return boom

    def _patch(self, obj, name, value):
        old = getattr(obj, name)
        setattr(obj, name, value)
        self._undo.append((obj, name, old))

    def install(self):
        if self.installed:
            return self
        with contextlib.suppress(ImportError):
            from google.oauth2.service_account import Credentials

            self._patch(Credentials, "from_service_account_file",
                        classmethod(lambda cls, *a, **k: self._block("Credentials.from_service_account_file")()))
        with contextlib.suppress(ImportError):
            import gspread

            self._patch(gspread, "authorize", self._block("gspread.authorize"))
        with contextlib.suppress(ImportError):
            from google.auth.transport.requests import AuthorizedSession

            self._patch(AuthorizedSession, "request",
                        lambda s, *a, **k: self._block("AuthorizedSession.request (Drive/Sheets REST)")())
        self._canned_sources()
        self.installed = True
        return self

    def _canned_sources(self):
        """Canned statuses, without forcing `sources` to import early (a script sets its config first)."""
        import sys

        if "sources" in sys.modules:
            self._patch(sys.modules["sources"], "status_report", self._statuses)
            return
        import importlib.abc
        import importlib.machinery

        guard = self

        class _Finder(importlib.abc.MetaPathFinder):
            def find_spec(self, name, path, target=None):
                if name != "sources":
                    return None
                sys.meta_path.remove(self)
                spec = importlib.machinery.PathFinder.find_spec(name, path)
                if spec is None or spec.loader is None:
                    return spec
                real_exec = spec.loader.exec_module

                def exec_module(module):
                    real_exec(module)
                    guard._patch(module, "status_report", guard._statuses)

                spec.loader.exec_module = exec_module
                return spec

        self._finder = _Finder()
        sys.meta_path.insert(0, self._finder)

    def uninstall(self):
        import sys

        if getattr(self, "_finder", None) in sys.meta_path:
            sys.meta_path.remove(self._finder)
        while self._undo:
            obj, name, old = self._undo.pop()
            setattr(obj, name, old)
        self.installed = False

    def assert_clean(self):
        assert not self.calls, f"live Sheets/Drive calls attempted: {self.calls}"


_GUARD = Guard()


def install(*, statuses=None):
    """Install (once) and return the process-wide guard. `statuses` replaces the canned rows for a check
    that needs a particular state (awaiting access, degraded); it can be changed later via
    `guard.statuses = [...]`."""
    if statuses is not None:
        _GUARD.statuses = statuses
    return _GUARD.install()


VIOLATIONS = _GUARD.calls               # same list object as guard.calls (cleared in place, never rebound)


def assert_clean():
    """Raise RealCallInTest listing every real Sheets/Drive call attempted since the last reset()."""
    if _GUARD.calls:
        raise RealCallInTest(f"live Sheets/Drive calls attempted: {sorted(set(_GUARD.calls))}")


def uninstall():
    _GUARD.uninstall()


def reset():
    _GUARD.calls.clear()


def install_script():
    """For a verify script: install, and at exit say plainly how many real Sheets / Drive calls were
    blocked. If any were, print them and exit 3, so a script that quietly depended on a real call
    cannot end green. Returns the guard (assert `guard.calls == []` yourself where there is a check())."""
    import atexit
    import os
    import sys

    g = install()
    if getattr(g, "_exit_hook", False):
        return g
    g._exit_hook = True

    def _report():
        if g.calls:
            print(f"\nOFFLINE GUARD: {len(g.calls)} real Sheets/Drive call(s) were BLOCKED: "
                  f"{sorted(set(g.calls))}\n1 FAILED (offline guard)", file=sys.stderr)
            sys.stderr.flush()
            sys.stdout.flush()
            os._exit(3)
        print("\nOFFLINE GUARD: no real Sheets/Drive call was attempted.")

    atexit.register(_report)
    return g
