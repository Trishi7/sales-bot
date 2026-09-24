"""PERMISSION BEFORE EVERY WRITE — the proposal, the vote, the tie-break.

THE GLOBAL RULE FROM THE WORKBOOK: the bot never writes a cell straight from a
reply. It says exactly what it proposes to change, in the sheet's own column
names, and waits for a yes from an approver.

    "Shall I set Meeting Date for Sahaj (Wispr Flow) to 24 Sep? Reply yes."

WHAT THIS COSTS, AND WHY IT IS WORTH IT. The old loop was one message: somebody
said "met Sahaj today" and the cell was written by the time they read the echo.
That is faster, and it is fine right up until the extractor is wrong about which
row, which column or which date — and then a person's data has been overwritten
by a machine nobody told to do it. The undo window catches that only if somebody
reads the echo. A proposal catches it before it happens.

WHO MAY SAY YES. Only `SALES_APPROVER_IDS` — Sid and Vaishnavi. ANYBODY may tell
the bot something, and it will propose the change and say who can approve it;
only an approver's answer applies it. Somebody else saying "yes" gets a polite
no and the proposal stays open.

WHEN TWO APPROVERS DISAGREE, SID WINS (`SALES_FINAL_SAY_ID`), and that is the
whole reason every vote is stored rather than the first one acted on. Vaishnavi
says yes at 14:02 and Sid says no at 14:09: the write must not have happened at
14:02. `decide()` is therefore computed from ALL the votes every time one lands,
and a decision that reverses an earlier one says so out loud.

NO REPLY IS ALSO AN ANSWER, eventually. One nudge the next working day, then the
proposal is dropped and logged. Two nudges is nagging; a proposal that never
expires is a queue of half-decisions with no visible end.

THIS MODULE IS PURE. Votes and proposals in, decisions out. It reads no sheet,
writes no database row and sends nothing — `bot.py` owns all three.
"""
import logging
import re
from typing import Optional

import config
import deadlines as dl

log = logging.getLogger(__name__)

# What counts as a yes and a no. Deliberately short lists of things people
# actually type, matched as WHOLE WORDS — "yes" must not fire on "yesterday",
# and "no" must not fire on "not yet" or "nothing came back".
_YES_WORDS = (
    "yes", "y", "yep", "yeah", "yup", "ok", "okay", "sure", "go ahead",
    "do it", "please do", "confirmed", "approved", "correct", "right",
)
_NO_WORDS = (
    "no", "n", "nope", "nah", "do not", "don't", "dont", "stop", "cancel",
    "wrong", "incorrect", "leave it", "hold off", "not yet", "wait",
)

VOTE_YES = "yes"
VOTE_NO = "no"
VOTE_NONE = ""

# The decisions `decide()` can reach.
APPLY = "apply"
DECLINE = "decline"
WAIT = "wait"


def read_vote(text: str) -> str:
    """"yes" / "no" / "" for one message.

    WHOLE-WORD MATCHING, AND NO IS CHECKED FIRST. "no, leave it" contains a
    "no"; it also contains nothing yes-shaped, but "not yet" contains no "yes"
    either and would be missed by a naive check. Checking the negatives first
    means an ambiguous message resolves to the SAFE answer — a refusal leaves
    the sheet as it is, and a wrong yes writes to it.

    Anything unrecognised is "" and the proposal stays open, because a person
    replying with a sentence the bot cannot read has not decided anything.
    """
    # APOSTROPHES ARE DELETED, NOT SPACED. Replacing them with a space turns
    # "don't" into "don t", which matches neither "dont" nor "don t" and let a
    # refusal read as no answer at all — the one direction this must not fail.
    raw = str(text or "").lower().replace("'", "").replace(chr(0x2019), "")
    low = f" {re.sub(r'[^a-z0-9 ]+', ' ', raw)} "
    low = re.sub(r"\s+", " ", low)
    for word in _NO_WORDS:
        if f" {word} " in low:
            return VOTE_NO
    for word in _YES_WORDS:
        if f" {word} " in low:
            return VOTE_YES
    return VOTE_NONE


def decide(votes: list) -> tuple:
    """(decision, why, decided_by) from EVERY vote on one proposal.

    THE TIE-BREAK IS A PERSON, NOT A TIMESTAMP. `SALES_FINAL_SAY_ID`'s answer
    wins outright whenever they have given one, whether it came first or last
    and whether it agrees or disagrees. Strategy section 8: "If Sid and anyone
    else give conflicting answers, Sid's answer wins."

    With no final-say configured the FIRST approver to answer decides, and the
    reason says so — an unconfigured tie-break should be visible in the
    explanation rather than silently resolved by arrival order.

    Only approvers' votes count at all; anybody else's is dropped before this.
    """
    real = [v for v in (votes or []) if v.get("vote") in (VOTE_YES, VOTE_NO)]
    if not real:
        return WAIT, "nobody who can approve this has answered yet", ""

    final_id = int(getattr(config, "SALES_FINAL_SAY_ID", 0) or 0)
    if final_id:
        theirs = [v for v in real if int(v.get("voter_id") or 0) == final_id]
        if theirs:
            vote = theirs[-1]
            others = [v for v in real if int(v.get("voter_id") or 0) != final_id]
            disagreed = [v for v in others if v.get("vote") != vote["vote"]]
            why = f"{vote.get('voter_label') or 'the final say'} said {vote['vote']}"
            if disagreed:
                names = ", ".join(
                    f"{d.get('voter_label') or d.get('voter_id')} said {d['vote']}"
                    for d in disagreed
                )
                why += f" — {names}, and the final say outranks that"
            return (APPLY if vote["vote"] == VOTE_YES else DECLINE), why, \
                str(vote.get("voter_label") or vote.get("voter_id") or "")

    first = real[0]
    why = f"{first.get('voter_label') or 'an approver'} said {first['vote']}"
    if not final_id and len(real) > 1:
        why += (" (SALES_FINAL_SAY_ID is unset, so the first answer decides)")
    return (APPLY if first["vote"] == VOTE_YES else DECLINE), why, \
        str(first.get("voter_label") or first.get("voter_id") or "")


def proposal_text(*, company: str, poc: str, applied: list, kind: str = "cell_update",
                  tab: str = "") -> str:
    """The exact change, as a question. What the approver is agreeing to.

    NAMES THE COLUMN AS THE SHEET NAMES IT, and the value as it will be written
    — not as the person phrased it. "Shall I set Meeting Date for Sahaj (Wispr
    Flow) to 24 Sep?" is checkable against the sheet in two seconds; "shall I
    record the meeting?" is not, and an approval given to a vague question is
    not really an approval.
    """
    who = f"{poc} ({company})" if poc and company else (poc or company or "that row")
    if kind == "row_add":
        return (
            f"Shall I add a new row for {who}"
            + (f" to {tab}" if tab else "")
            + "? Reply yes."
        )
    if not applied:
        return f"Shall I update {who}? Reply yes."
    changes = " and ".join(
        f"{a.get('label') or a.get('role')} to {a.get('new')}"
        + (f" (currently {a.get('old')})" if a.get("old") else "")
        for a in applied
    )
    return f"Shall I set {changes} for {who}? Reply yes."


def who_can_approve() -> str:
    """A phrase naming the approvers, for the proposal message."""
    import guardrails
    names = [guardrails.mention_for(uid) for uid in config.approver_ids()]
    names = [n for n in names if n]
    if not names:
        return "nobody is configured to approve this (SALES_APPROVER_IDS is empty)"
    if len(names) == 1:
        return f"{names[0]} can approve it"
    return f"{' or '.join([', '.join(names[:-1]), names[-1]])} can approve it"


def not_an_approver_reply(name: str) -> str:
    """The polite no for somebody who is not an approver.

    POLITE, AND IT SAYS WHY RATHER THAN JUST REFUSING. The person was trying to
    help; the answer is that this particular thing needs Sid or Vaishnavi, not
    that they did something wrong.
    """
    return (
        f"Thanks {name} — I need a yes from {who_can_approve().replace(' can approve it', '')} "
        "before I touch the sheet, so I will leave this one open."
    )


def pending_text(proposals: list) -> str:
    """ONE message for everything still waiting on a yes.

    ONE MESSAGE, NOT ONE PER PROPOSAL, and that is the whole reason this exists.
    Four separate "still waiting on this" posts in an afternoon is four
    interruptions about the same kind of thing; one list is a glance. It also
    means the sweep costs the day exactly one message however many proposals
    have piled up.

    IT DOES NOT COUNT AGAINST THE DAILY CAP. A day that used all its slots on
    rules and therefore never mentioned four pending approvals would be a day
    the approvals queue grew invisibly.
    """
    n = len(proposals or [])
    if not n:
        return ""
    head = (
        "A few things still waiting for a yes:" if n > 1
        else "One thing still waiting for a yes:"
    )
    lines = [head]
    for p in proposals[:8]:
        who = p.get("company") or "that row"
        if p.get("poc"):
            who = f"{p['poc']} ({p['company']})" if p.get("company") else p["poc"]
        asked = str(p.get("proposed_text") or "").rstrip()
        # The proposal text already ends "Reply yes." — the list has its own
        # instruction at the bottom, so the per-line repeat is noise.
        asked = asked.replace(" Reply yes.", "").rstrip()
        lines.append(f"  - {who}: {asked}")
    if n > 8:
        lines.append(f"  - ...and {n - 8} more")
    lines.append(
        "Reply yes to any of them and I will apply it. If one is wrong, say no and "
        "I will drop it \u2014 otherwise I will let them go after tomorrow."
    )
    return "\n".join(lines)


def nudge_text(proposal: dict) -> str:
    """The ONE follow-up, the next working day. Softer, and it says it is the
    last time — which is what makes it a courtesy rather than a second demand."""
    return (
        f"Still holding this one: {proposal.get('proposed_text') or 'the change I proposed'} "
        "If it is not right, just say no and I will drop it — otherwise I will "
        "let it go after today."
    )


def dropped_text(proposal: dict) -> str:
    return (
        f"Dropping this one, nobody got to it: "
        f"{proposal.get('proposed_text') or 'the change I proposed'} "
        "Nothing has changed in the sheet. Tell me again if it still needs doing."
    )


def _self_test() -> int:
    """`python -m approvals` — the vote reader and the tie-break."""
    logging.basicConfig(level=logging.WARNING, format="%(levelname)-7s %(message)s")
    failures = 0

    def check(name, got, want):
        nonlocal failures
        ok = got == want
        failures += 0 if ok else 1
        print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}, want {want!r}")

    print("reading a vote")
    for text, want in [
        ("yes", VOTE_YES), ("Yes", VOTE_YES), ("yes please", VOTE_YES),
        ("go ahead", VOTE_YES), ("sure", VOTE_YES), ("ok", VOTE_YES),
        ("no", VOTE_NO), ("No.", VOTE_NO), ("nope", VOTE_NO),
        ("no, leave it", VOTE_NO), ("not yet", VOTE_NO), ("hold off", VOTE_NO),
        ("don't", VOTE_NO), ("wait", VOTE_NO),
        ("yesterday", VOTE_NONE), ("nothing came back", VOTE_NONE),
        ("what do you mean?", VOTE_NONE), ("", VOTE_NONE),
        ("I met him yesterday", VOTE_NONE),
    ]:
        check(f"read_vote({text!r})", read_vote(text), want)

    print("\nthe tie-break")
    config.SALES_FINAL_SAY_ID = 111          # Sid
    V = {"voter_id": 222, "voter_label": "Vaishnavi"}
    S = {"voter_id": 111, "voter_label": "Sid"}

    check("nobody has answered", decide([])[0], WAIT)
    check("one yes applies", decide([{**V, "vote": "yes"}])[0], APPLY)
    check("one no declines", decide([{**V, "vote": "no"}])[0], DECLINE)
    check("Vaishnavi yes then Sid no -> DECLINE",
          decide([{**V, "vote": "yes"}, {**S, "vote": "no"}])[0], DECLINE)
    check("...and the reason names the override",
          "outranks" in decide([{**V, "vote": "yes"}, {**S, "vote": "no"}])[1], True)
    check("Sid no then Vaishnavi yes -> still DECLINE",
          decide([{**S, "vote": "no"}, {**V, "vote": "yes"}])[0], DECLINE)
    check("Sid yes then Vaishnavi no -> APPLY",
          decide([{**S, "vote": "yes"}, {**V, "vote": "no"}])[0], APPLY)
    check("both agree, no override language",
          "outranks" in decide([{**V, "vote": "yes"}, {**S, "vote": "yes"}])[1], False)
    check("the decider is named",
          decide([{**V, "vote": "yes"}, {**S, "vote": "no"}])[2], "Sid")

    config.SALES_FINAL_SAY_ID = 0
    check("no final say -> the first answer decides",
          decide([{**V, "vote": "yes"}, {**S, "vote": "no"}])[0], APPLY)
    check("...and the reason says the tie-break is unset",
          "unset" in decide([{**V, "vote": "yes"}, {**S, "vote": "no"}])[1], True)
    config.SALES_FINAL_SAY_ID = 111

    print("\nthe proposal text")
    text = proposal_text(
        company="Wispr Flow", poc="Sahaj",
        applied=[{"label": "Meeting Date", "new": "24 Sep", "old": ""}],
    )
    check("names the column", "Meeting Date" in text, True)
    check("names the value", "24 Sep" in text, True)
    check("names the person and company", "Sahaj (Wispr Flow)" in text, True)
    check("asks for a yes", "Reply yes." in text, True)
    print(f"    -> {text}")
    withold = proposal_text(
        company="Acme", poc="Ann",
        applied=[{"label": "Meeting Status", "new": "Completed", "old": "Booked"}],
    )
    check("says what is there now", "currently Booked" in withold, True)
    print(f"    -> {withold}")
    add = proposal_text(company="Nova Labs", poc="", applied=[],
                        kind="row_add", tab="Master Pipeline")
    check("a row addition reads as one", "add a new row" in add, True)
    print(f"    -> {add}")

    print(f"\n{'ALL PASSED' if not failures else str(failures) + ' FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_self_test())
