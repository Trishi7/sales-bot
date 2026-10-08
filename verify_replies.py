"""NFT2-1063 — REPLIES AND ON-DEMAND REQUESTS. Real `on_message` path, offline.

    python verify_replies.py            every case
    python verify_replies.py d1,d7,e3   some of them (a prefix of the case name)

Each case below runs TWICE, SALES_TEST_MODE=false then true, through the REAL gate -> `_handle_query` -> reply
context -> acknowledge / vote / offer / engine path with a fake Discord (replies, reactions, fetch_message), a fake
model, a throwaway SQLite file and a throwaway STATE_DIR. It prints what a person would see: the message, what it
replied to, and what Saley did. Then it asserts live == test mode (same replies apart from the [TEST] tag, same
reactions, votes, reminders, model calls).

The cases are tests/replies_cases.py: the decision table D1-D24 (plan 5.5), the ticket's four edge cases, the 7 Oct
case, the proposal of every kind (P1), the objectives answer (8.2) at 13:58 and 14:05 and for two askers at once,
and the interim wording (6). tests/test_replies.py runs the same cases under pytest. The replay of the 6 and 7 Oct
exchanges is verify_replay_oct6.py.

OFFLINE. Nothing reaches Discord, the real sheet, Drive, the web or the model. Every setting the cases lean on is
PINNED to its .env.example default (tests/replies_world.py PINS), so the laptop's real .env (SIMULATION_PREFIX
`[TEST-live]`, other approver ids, a 150-minute sweep) cannot change the outcome. The offline guard fails the run on
any real Sheets/Drive call.

THE DECISIONS THE HUMAN HAS NOT MADE are built as the plan's section-0 defaults and asserted against ONE constant
each, at the top of tests/replies_cases.py (Q1 AI news excluded, Q3 offer lines left out, Q5 the thumbs-up, Q6 a
non-reply 'thanks' reacts).
"""
import asyncio
import logging
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.chdir(HERE)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

TMP = tempfile.mkdtemp(prefix="saley-replies-")
os.environ["STATE_DIR"] = os.path.join(TMP, "state0")
os.environ["DB_PATH"] = os.path.join(TMP, "sales_bot_test.db")
os.environ.setdefault("DISCORD_TOKEN", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")

sys.path.insert(0, os.path.join(HERE, "tests"))
import offline_guard  # noqa: E402

GUARD = offline_guard.install_script()      # any real Sheets/Drive call is counted and fails the run

import config  # noqa: E402

config.DB_PATH = os.environ["DB_PATH"]
for _noisy in ("discord", "asyncio", "httpx", "httpcore", "anthropic", "googleapiclient", "google", "urllib3"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)
logging.getLogger().setLevel(logging.WARNING)

import replies_cases as rc  # noqa: E402
import replies_world as rw  # noqa: E402

failures = 0
ONLY = [a.strip().lower() for a in (sys.argv[1].split(",") if len(sys.argv) > 1 else []) if a.strip()]


def check(name, got, want=True):
    global failures
    ok = (got == want)
    failures += 0 if ok else 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f": got {got!r}, want {want!r}"))


def check_test_mode(name, got, want=True):
    """Test mode must satisfy the same checks. Only a failure is printed: the PASS lines would double the output."""
    global failures
    if got != want:
        failures += 1
        print(f"  FAIL  [test mode] {name}: got {got!r}, want {want!r}")


def narrate(snap):
    if not isinstance(snap, dict) or "replies" not in snap:
        return
    said = [t.replace("\n", " / ")[:110] for t in snap["replies"]]
    print(f"      Saley posted: {said}   reactions: {[e for _t, e in snap['reactions']]}   "
          f"votes: {len(snap['votes'])}   reminders: {snap['reminders']}   model calls: {snap['counts']['model']}")


async def main():
    print("pinned to .env.example defaults: SIMULATION_PREFIX=%r approvers=%s PROPOSAL_BARE_YES_MINUTES=%s "
          "(this laptop's real values differ and are never read)" % (rw.PINS["SIMULATION_PREFIX"],
                                                                      rw.PINS["SALES_APPROVER_IDS"],
                                                                      rw.PINS["PROPOSAL_BARE_YES_MINUTES"]))
    for fn in rc.CASES:
        name = fn.__name__
        if ONLY and not any(name.lower().startswith(o) for o in ONLY):
            continue
        doc = (fn.__doc__ or "").strip().splitlines()[0] if fn.__doc__ else ""
        print(f"\n{name}" + (f" — {doc}" if doc else ""))
        try:
            live = await fn(check, False)
            test = await fn(check_test_mode, True)
        except Exception as e:                       # a case that cannot even run is a failure, with the cause
            import traceback
            global_fail()
            print(f"  FAIL  the case raised {type(e).__name__}: {e}")
            print("      " + traceback.format_exc().strip().splitlines()[-3].strip())
            continue
        narrate(live)
        if isinstance(live, dict) and isinstance(test, dict):
            check("test mode == live (same replies apart from the tag, reactions, votes, reminders, model calls)",
                  rw.fresh_snapshot_equal(live, test), True)
            if "tagged" in live:
                check("the tag is the only difference: nothing live is tagged; in test mode only a drip post is",
                      (not any(live["tagged"]),
                       all(t.startswith("**") or test["tagged"][i - 1]
                           for i, (t, tg) in enumerate(zip(test["replies"], test["tagged"])) if tg)), (True, True))
            if not rw.fresh_snapshot_equal(live, test):
                print(f"      live: {live}\n      test: {test}")
    check("no real Sheets/Drive call was attempted", GUARD.calls, [])


def global_fail():
    global failures
    failures += 1


try:
    asyncio.run(main())
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\n{'ALL PASSED' if not failures else str(failures) + ' FAILED'}")
sys.exit(1 if failures else 0)
