"""R2 "NEW COMPANIES IN THE NEWS" (rebuilt 12 Oct 2026). OFFLINE.

    python verify_news_screen.py            # every check
    python verify_news_screen.py --show     # also print the messages as they would be posted

Fake Discord, temp database, a scripted model, made-up sheets through the real parser; no network, no real
sheet. The checks themselves are in tests/news_screen_checks.py (shared with tests/test_news_screen.py).

WHAT IT PROVES
  MATCHING  the full name decides: Nudge Security is not Nudge - AI Commerce Discovery, Google is not Google
            DeepMind, and a partial match is a question.
  WINDOW    from the last post to now by first-seen time; both ends included; never past the previous scheduled
            day; seven days on the first run.
  KNOWN     all 382 companies, both sheets, no cap; ElevenLabs (number 1) and Yellow.ai (number 368) are dropped
            in code; an unreadable, empty or stale sheet CANCELS the message at ERROR; a company added seconds
            before is caught by the forced read.
  OUTCOMES  Master Pipeline: dropped. Outreach PoCs only: the pipeline-row line. Neither: proposed.
  MESSAGE   the agreed shape; two verdict wordings; every link masked; the most important story linked; five at
            most with one held-back line; nothing surviving posts nothing.
  MARKER    a test day, a simulation and a pretend clock never move it; a live post does.
  REPEATS   unanswered comes back; answered-without-choosing is dropped; two weeks drops it.
  REPLY     read by company name; an unknown or ambiguous name writes nothing; one more question before a write.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "tests"))
os.environ.setdefault("DISCORD_TOKEN", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")

import news_screen_checks as nsc  # noqa: E402

FAILED = 0
TOTAL = 0


def check(name, got, want):
    global FAILED, TOTAL
    TOTAL += 1
    ok = got == want
    FAILED += 0 if ok else 1
    print(("  ok    " if ok else "  FAIL  ") + name)
    if not ok:
        print(f"        got   {got!r}\n        want  {want!r}")


def show():
    import news_screen as ns

    print("\n" + "=" * 100 + "\nThe post (the tags line goes under the heading at send)\n" + "-" * 100)
    print(nsc.EXPECTED)
    print("\n" + "=" * 100 + "\nSeven survivors\n" + "-" * 100)
    rows = [{"company": f"Fresh Co {i}", "url": f"https://news.example/story-{i}", "what": "did a thing",
             "verdict": ns.GOOD if i % 2 else ns.WEAK, "reason": "a reason", "outcome": ns.NEW}
            for i in range(1, 6)]
    print(ns.render(rows, held=2))
    print("\n" + "=" * 100 + "\nThe reply\n" + "-" * 100)
    print(ns.confirm_text(["Mirage Labs"], ambiguous=[("nudge", ["Nudge Security", "Nudge Labs"])],
                          unknown=["foo corp"]))


if __name__ == "__main__":
    nsc.run(check)
    if "--show" in sys.argv:
        show()
    print(f"\n{TOTAL} checks. " + ("ALL PASSED" if not FAILED else f"{FAILED} FAILED"))
    sys.exit(1 if FAILED else 0)
