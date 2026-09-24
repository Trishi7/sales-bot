"""COMPOSE THE SAME R7 MESSAGE UNDER TWO TONE SETTINGS. `python verify_tone.py`

  A. high warmth / casual
  B. medium warmth / formal

Same rule, same row, same facts — only the dials move. Both are printed so the
difference is legible rather than asserted.

It makes REAL compose calls when a usable ANTHROPIC_API_KEY is present, because
the whole point is whether the dials actually change what the model writes; a
mocked composer would prove the mock. Without a key it falls back to the
deterministic template and says so. Pass --offline to skip the live call.

NOTHING IS SENT. `drip.compose_fallback` and `llm.proactive_message` both return
text; no Discord client is constructed.
"""
import asyncio
import logging
import os
import sys
import tempfile
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
import db as dbmod
import deadlines as dl
import drip
import nextaction
import tone

OFFLINE = "--offline" in sys.argv
TODAY = date(2026, 9, 21)
SID, VAISHNAVI, KUSHAL = 1001, 1002, 1003
config.TEAM_ROSTER_IDS = [SID, VAISHNAVI, KUSHAL]
config.SALES_ALWAYS_TAG_IDS = [VAISHNAVI, SID]
config.ROSTER_DISPLAY_NAMES = {str(SID): "Sid", str(VAISHNAVI): "Vaishnavi",
                               str(KUSHAL): "Kushal"}

SETTINGS = {
    "A — high warmth, casual": {
        "SALEY_WARMTH": "high", "SALEY_FORMALITY": "casual",
        "SALEY_EMOJI": "light", "SALEY_LENGTH": "short", "SALEY_HUMOUR": "off",
    },
    "B — medium warmth, formal": {
        "SALEY_WARMTH": "medium", "SALEY_FORMALITY": "formal",
        "SALEY_EMOJI": "none", "SALEY_LENGTH": "short", "SALEY_HUMOUR": "off",
    },
}

failures = 0


def check(name, got, want):
    global failures
    ok = got == want
    failures += 0 if ok else 1
    print(f"    {'PASS' if ok else 'FAIL'}  {name}: got {got!r}, want {want!r}")


def have_key() -> bool:
    key = (os.getenv("ANTHROPIC_API_KEY") or "").strip()
    return len(key) > 20 and key.startswith("sk-")


def r7_message():
    """The R7 item, as the engine would produce it, grouped as the drip would."""
    trigger = nextaction.R_DM_NO_MEETING
    band = nextaction.RULE_BANDS[trigger]
    item = {
        "rule": trigger, "rule_id": "R7", "type": trigger,
        "rule_name": nextaction.TYPE_LABELS[trigger],
        "label": nextaction.TYPE_LABELS[trigger],
        "owner": "Kushal", "priority": band,
        "priority_label": nextaction.BAND_LABELS[band],
        "due_date": TODAY, "due_iso": dl.iso(TODAY), "overdue_days": 12,
        "company": "London Metropolitan University", "poc": "Karim Ouazzane",
        "poc_designation": "Senior Professor of Computing", "sheet_row": 7,
        "row_key": "london met|karim", "contact_key": "london met|karim",
        "max_items_per_post": config.DRIP_MAX_ITEMS_PER_POST,
        "counts_toward_cap": True, "destination": "channel", "web_pending": False,
        "why": ("R7 (Mondays): DM sent Wed 02 Sep 2026, 19d ago, more than "
                "DM_NO_MEETING_DAYS (7), no meeting date"),
        "text": ("London Metropolitan University · Karim Ouazzane — 19 day(s) since "
                 "the DM, no meeting booked. Last note: interested but tied up "
                 "until October"),
        "days_since_dm": 19,
        "last_note": "interested but tied up until October",
        "key": "R7:london met",
    }
    planned = drip.plan([item], day=TODAY)
    return planned["messages"][0]


async def compose(message, live, openers):
    """One composition under whatever the env currently says."""
    fallback = drip.compose_fallback(message, address="")
    if not live:
        return fallback, False
    import llm as llmmod
    engine = llmmod.LLM(os.environ["ANTHROPIC_API_KEY"], config.MODEL)
    return await engine.proactive_message(
        prompt=drip.compose_prompt(message, address=""),
        fallback=fallback, recent_openers=openers,
    )


async def main() -> int:
    global failures
    logging.basicConfig(level=logging.ERROR, format="%(levelname)-7s %(message)s")

    live = have_key() and not OFFLINE
    f = os.path.join(tempfile.gettempdir(), "verify_tone.db")
    if os.path.exists(f):
        os.remove(f)
    d = dbmod.DB(f)
    # Seed one opening so the no-repeat rule has something to avoid.
    d.record_opener("worth a look at", rule_id="R7", sent_at=dl.iso(TODAY))

    msg = r7_message()

    print("=" * 78)
    print("SAME R7 MESSAGE, TWO TONE SETTINGS")
    print("=" * 78)
    print(f"  mode       {'LIVE — real compose calls' if live else 'OFFLINE — template only'}")
    if not live and not OFFLINE:
        print("             (no usable ANTHROPIC_API_KEY found)")
    print(f"  rule       R7 · {msg['rule_name']}")
    print(f"  row        {', '.join(msg.get('companies') or [])} · Karim Ouazzane")
    print(f"  facts      19 days since the DM, no meeting, last note on file")
    print(f"  openers to avoid  {d.recent_openers(5)}")
    print()

    composed = {}
    for label, dials in SETTINGS.items():
        for k, v in dials.items():
            os.environ[k] = v
        print("-" * 78)
        print(f"  {label}")
        print(f"  {tone.describe()}")
        print("-" * 78)
        body, used_model = await compose(msg, live, d.recent_openers(5))
        full = drip.with_tags(body, owner_id=KUSHAL, owner_name="Kushal")
        for line in full.splitlines():
            print("  | " + line)
        print()
        print(f"    composed by       {'the model' if used_model else 'the template'}")
        print(f"    sentences         {tone.count_sentences(body)} "
              f"(cap {tone.SENTENCE_BUDGET[tone.settings()['length']]})")
        print(f"    emoji             {tone.count_emoji(body)} "
              f"(cap {tone.EMOJI_BUDGET[tone.settings()['emoji']]})")
        print(f"    opener key        {tone.opener_of(full)!r}")
        print(f"    tone check        {tone.check(body) or 'passes'}")
        print()
        composed[label] = (body, full, used_model)

    for k in ("SALEY_WARMTH", "SALEY_FORMALITY", "SALEY_EMOJI",
              "SALEY_LENGTH", "SALEY_HUMOUR"):
        os.environ.pop(k, None)

    print("=" * 78)
    print("  CHECKS")
    print("=" * 78)

    a_body, a_full, a_model = composed["A — high warmth, casual"]
    b_body, b_full, b_model = composed["B — medium warmth, formal"]

    check("both produced a message", bool(a_body) and bool(b_body), True)
    check("both tag Vaishnavi",
          f"<@{VAISHNAVI}>" in a_full and f"<@{VAISHNAVI}>" in b_full, True)
    check("both tag Sid", f"<@{SID}>" in a_full and f"<@{SID}>" in b_full, True)
    check("both tag the owner too",
          f"<@{KUSHAL}>" in a_full and f"<@{KUSHAL}>" in b_full, True)
    check("the tags come first", a_full.splitlines()[0].startswith("<@"), True)

    # The dials must have been APPLIED, whichever composer ran.
    os.environ["SALEY_LENGTH"] = "short"
    check("A is within its sentence cap", tone.count_sentences(a_body) <= 3, True)
    check("B is within its sentence cap", tone.count_sentences(b_body) <= 3, True)
    os.environ["SALEY_EMOJI"] = "none"
    check("B (emoji=none) carries no emoji", tone.count_emoji(b_body), 0)
    for k in ("SALEY_LENGTH", "SALEY_EMOJI"):
        os.environ.pop(k, None)

    if live and a_model and b_model:
        check("the two settings produced DIFFERENT text", a_body != b_body, True)
    else:
        print("    NOTE  both fell back to the template, so the wording is identical")
        print("          by construction — the tone block still reached the prompt,")
        print("          and the dials are asserted above.")

    # The prompt actually carries the dials.
    import persona
    os.environ["SALEY_FORMALITY"] = "formal"
    p_formal = persona.proactive_voice_prompt(recent_openers=["worth a look at"])
    os.environ["SALEY_FORMALITY"] = "casual"
    p_casual = persona.proactive_voice_prompt(recent_openers=["worth a look at"])
    os.environ.pop("SALEY_FORMALITY", None)
    check("the prompt changes with the dial", p_formal != p_casual, True)
    check("...and names the formal setting", "FORMALITY: FORMAL" in p_formal, True)
    check("...and the casual one", "FORMALITY: CASUAL" in p_casual, True)
    check("the prompt lists the openers to avoid",
          "worth a look at" in p_casual, True)
    check("the prompt carries the human touches",
          "ALWAYS OFFER AN EASY OUT" in p_casual, True)
    check("the exemplars are the new per-rule ones",
          "R7 — DM sent, no meeting" in p_casual, True)

    print()
    print("  " + ("ALL PASSED" if not failures else f"{failures} FAILED"))
    print("=" * 78)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
