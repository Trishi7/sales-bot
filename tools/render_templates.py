"""EVERY R1-R14 POST THE FIXTURES RENDER, AS ONE FILE — the golden file of NFT2-1090 (T66).

    python tools/render_templates.py OUT.txt          render, write OUT.txt
    python tools/render_templates.py --diff GOLDEN    render, diff against GOLDEN; exit 1 on any difference

WHY. NFT2-1090 rewrote Saley's voice and must not move one byte of a daily post. The posts are
rendered in code (`drip.compose_fallback` for R1-R14, `news.render` for R1's stories), and the
offline checks (tests/*_checks.py) already drive every rule over canned sheets on pinned days. So
this runs those checks as they are, records every text the two renderers return, and writes the
distinct texts SORTED — order-free, so a check that renders in a different order is not a diff,
while any changed character is.

OFFLINE: the checks' own fakes (canned sheet, fake Discord, throwaway databases, no model).
"""
import asyncio
import importlib
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
os.chdir(ROOT)
os.environ.setdefault("DISCORD_TOKEN", "x")
os.environ["ANTHROPIC_API_KEY"] = "x"
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import offline_guard  # noqa: E402

GUARD = offline_guard.install_script()

import logging  # noqa: E402

logging.disable(logging.CRITICAL)

import drip  # noqa: E402
import news  # noqa: E402

# The checks that render the fourteen posts (R1 news, R2 screen, R3 events, R4 deliverables, R5
# prospects, R6 LI no DM, R7 DM no meeting, R8 meeting prep, R10 closure, R11/R13 next steps and the
# 8 Oct shapes, R12 packages, R14 reconnect, and the day's order that plans them all).
MODULES = ("news_checks", "news_screen_checks", "events_checks", "deliverables_checks",
           "prospects_checks", "li_no_dm_checks", "dm_no_meeting_checks", "meeting_prep_checks",
           "closure_support_checks", "next_steps_checks", "oct8_checks", "packages_checks",
           "reconnect_checks", "day_order_checks")

# R11 (new companies cross-checked against Outreach PoCs) is rendered by a
# verify SCRIPT, not a checks module: it is run as it is, its exit ignored.
SCRIPTS = ("verify_poc_crosscheck.py",)

SEEN: set = set()


def _record(fn, label):
    def wrapped(*a, **k):
        out = fn(*a, **k)
        if isinstance(out, str) and out.strip():
            msg = a[0] if a and isinstance(a[0], dict) else {}
            kind = msg.get("type") or msg.get("rule_id") or label
            SEEN.add(f"[{kind}]\n{out}")
        return out
    return wrapped


drip.compose_fallback = _record(drip.compose_fallback, "compose_fallback")
news.render = _record(news.render, "news.render")


def _noop(*_a, **_k):
    return None


def render() -> str:
    for name in MODULES:
        try:
            mod = importlib.import_module(name)
        except Exception as e:                      # a module that cannot load is a diff, said so
            SEEN.add(f"[import error] {name}: {type(e).__name__}")
            continue
        run = getattr(mod, "run", None)
        if run is None:
            continue
        try:
            out = run(_noop) if run.__code__.co_argcount <= 1 else run(_noop, _noop)
            if asyncio.iscoroutine(out):
                asyncio.run(out)
        except Exception as e:
            SEEN.add(f"[run error] {name}: {type(e).__name__}: {str(e)[:120]}")
    import runpy

    for script in SCRIPTS:
        argv = sys.argv[:]
        try:
            sys.argv = [script]
            runpy.run_path(os.path.join(ROOT, script), run_name="__main__")
        except SystemExit:
            pass
        except Exception as e:
            SEEN.add(f"[run error] {script}: {type(e).__name__}: {str(e)[:120]}")
        finally:
            sys.argv = argv
    sep = "\n" + "=" * 78 + "\n"
    return sep.join(sorted(SEEN)) + "\n"


def main() -> int:
    if len(sys.argv) >= 3 and sys.argv[1] == "--diff":
        want = open(sys.argv[2], encoding="utf-8").read()
        import contextlib
        import io

        with contextlib.redirect_stdout(io.StringIO()):
            got = render()
        if got == want:
            print(f"T66 templates: {got.count(chr(10) + '=' * 78)} + 1 rendered posts, byte-identical")
            return 0
        import difflib

        sys.stdout.writelines(list(difflib.unified_diff(
            want.splitlines(True), got.splitlines(True), "main", "branch", n=1))[:400])
        print("T66 templates: DIFFERENT")
        return 1
    import contextlib
    import io

    with contextlib.redirect_stdout(io.StringIO()):
        text = render()
    open(sys.argv[1], "w", encoding="utf-8", newline="\n").write(text)
    print(f"wrote {sys.argv[1]}: {len(SEEN)} distinct rendered posts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
