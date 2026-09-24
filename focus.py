"""FOCUS COMMANDS — "prioritise only AI Voice Agents for the next two weeks".

Sid or Vaishnavi can redirect prospecting at any time. R5 then offers matching
contacts FIRST, and everything else waits behind them until the focus expires.

A FOCUS NARROWS, IT DOES NOT SILENCE. When nothing on the sheet matches the
filter, R5 SAYS SO and falls back to sheet order rather than going quiet. That
matters more than it sounds: a focus that accidentally matched nothing — a typo,
an industry spelled differently in the sheet — would otherwise read exactly like
a quiet week, and nobody would learn the filter was the reason. The fallback is
announced, not silent.

ONLY APPROVERS MAY SET ONE, for the same reason only they may approve a write: a
focus changes who the whole team is contacting for a fortnight. Anybody else
asking gets a polite no that says who can.

IT EXPIRES ON ITS OWN, and says so once. `FOCUS_DEFAULT_DAYS` (14) when no
duration is given, capped at `FOCUS_MAX_DAYS` (90) — a focus set for six months
is a strategy change wearing a command's clothes, and belongs in
sales_strategy.md instead.

THIS MODULE IS PURE. Text in, a parsed focus out; rows and a focus in, a filtered
ordering out. It reads no sheet, writes no database row and sends nothing.
"""
import logging
import re
from datetime import date, timedelta
from typing import Optional

import config
import deadlines as dl
import gtm_sheet

log = logging.getLogger(__name__)

# The command shapes people actually type. `prioritise|prioritize|focus (on)|
# only` followed by the thing, optionally followed by a duration.
_SET_RE = re.compile(
    r"\b(?:prioriti[sz]e|focus(?:\s+on)?|concentrate\s+on|only\s+do)\b\s*"
    r"(?:only\s+)?(?P<what>.+?)"
    r"(?:\s+(?:for|over)\s+(?:the\s+)?(?:next\s+)?(?P<dur>.+?))?\s*$",
    re.IGNORECASE,
)
_SHOW_RE = re.compile(r"\b(?:show|what(?:'?s| is)|current)\s+(?:the\s+|my\s+|our\s+)?focus\b", re.IGNORECASE)
_CLEAR_RE = re.compile(
    r"\b(?:clear|remove|drop|cancel|end|stop)\s+(?:the\s+|my\s+|our\s+)?focus\b", re.IGNORECASE
)

# "two weeks", "10 days", "a month", "3 wks".
_DUR_RE = re.compile(
    r"^\s*(?P<n>\d+|a|an|one|two|three|four|five|six|seven|eight|nine|ten)?\s*"
    r"(?P<unit>day|days|week|weeks|wk|wks|month|months|fortnight)\s*$",
    re.IGNORECASE,
)
_WORD_NUMBERS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}

SET = "set"
SHOW = "show"
CLEAR = "clear"


def parse_duration(text: str) -> Optional[int]:
    """"two weeks" -> 14. None when it is not a duration.

    CAPPED AT FOCUS_MAX_DAYS, and the cap is applied here rather than at the
    call site so that every path into a focus gets it — including a future one
    nobody has written yet.
    """
    m = _DUR_RE.match(str(text or ""))
    if not m:
        return None
    raw = (m.group("n") or "1").lower()
    n = _WORD_NUMBERS.get(raw, None)
    if n is None:
        try:
            n = int(raw)
        except (TypeError, ValueError):
            return None
    unit = m.group("unit").lower()
    if unit.startswith("day"):
        days = n
    elif unit == "fortnight":
        days = 14 * n
    elif unit.startswith(("week", "wk")):
        days = 7 * n
    else:
        days = 30 * n
    return max(1, min(int(days), max(1, int(config.FOCUS_MAX_DAYS))))


def parse(text: str) -> Optional[dict]:
    """A focus command, or None when the message is not one.

    Returns {action, value, days, raw} — `value` and `days` only for `set`.

    IT DOES NOT GUESS A FIELD. "AI Voice Agents" could be an industry or a
    company name, and deciding which before looking at the sheet would mean
    filtering the wrong column. `matches()` tries every role in
    FOCUS_MATCH_ROLES instead, which is both simpler and right more often.
    """
    body = " ".join(str(text or "").split()).strip()
    if not body:
        return None
    if _CLEAR_RE.search(body):
        return {"action": CLEAR, "value": "", "days": 0, "raw": body}
    if _SHOW_RE.search(body):
        return {"action": SHOW, "value": "", "days": 0, "raw": body}

    m = _SET_RE.search(body)
    if not m:
        return None
    what = (m.group("what") or "").strip(" .,:;!?\"'")
    if not what:
        return None
    days = parse_duration(m.group("dur") or "") or 0
    if not days:
        days = max(1, min(int(config.FOCUS_DEFAULT_DAYS),
                          max(1, int(config.FOCUS_MAX_DAYS))))
    return {"action": SET, "value": what, "days": days, "raw": body}


def expiry(*, on: date, days: int) -> date:
    return on + timedelta(days=max(1, int(days)))


def matches(row: dict, value: str) -> str:
    """Which role on this row matches the focus, or "".

    EVERY ROLE IN FOCUS_MATCH_ROLES IS TRIED — industry, company, designation,
    based. Somebody saying "only AI voice agents" means an industry; "only
    Wispr" means a company; "only founders" means a designation; "only London"
    means a location. One command shape, four columns, and the bot does not have
    to be told which.

    THE CELL MUST BE AT LEAST AS SPECIFIC AS THE FOCUS, never less. "AI Voice
    Agents" matches a cell reading "AI voice agents (conversational)" — a sheet
    cell is rarely typed the way a command is, and an exact match would fail on
    almost every real row.

    IT DOES NOT MATCH THE OTHER WAY ROUND, and that is a bug this had. Testing
    `cell in want` as well meant a focus on "Quantum Robotics" matched every row
    whose industry cell said "Robotics" — the focus was narrower than the cell
    and the match made it wider. A focus that silently selects the wrong rows is
    worse than one that selects none, because the second says so.

    Two ways to match, both of them "the cell contains the focus": the focus as
    a substring, or every word of the focus present in the cell. The second
    catches "founders" against "Co-Founder / CEO", where word order differs.
    """
    want = gtm_sheet.normalise_header(value)
    if not want:
        return ""
    want_words = [w for w in want.split() if w]
    for role in (config.FOCUS_MATCH_ROLES or []):
        cell = gtm_sheet.normalise_header(gtm_sheet.clean_cell(row.get(role)))
        if not cell:
            continue
        if want in cell:
            return role
        cell_words = set(cell.split())
        if want_words and all(
            any(w == c or (len(w) > 4 and w in c) for c in cell_words)
            for w in want_words
        ):
            return role
    return ""


def apply_to(rows: list, focus: Optional[dict]) -> tuple:
    """(ordered rows, note) — matching rows first, then the rest in sheet order.

    THE NON-MATCHING ROWS ARE KEPT, NOT DROPPED. A focus is a priority, not a
    filter that silences the sheet: R5 takes the first N of whatever this
    returns, so matching rows fill the post and the rest are simply behind them.
    If the focus matched nothing, the ordering is unchanged and `note` says so.

    `note` is the sentence R5 puts in its message. It is never empty when a
    focus is live — the team should always be able to see the filter working, or
    see that it did not.
    """
    rows = list(rows or [])
    if not focus or not focus.get("value"):
        return rows, ""

    value = str(focus.get("value") or "")
    hits, rest = [], []
    for row in rows:
        (hits if matches(row, value) else rest).append(row)

    if not hits:
        return rows, (
            f"Nothing on the sheet matches the current focus ({value}), so I am "
            "going in sheet order instead"
        )
    where = matches(hits[0], value)
    return hits + rest, (
        f"Focused on {value} — {len(hits)} matching contact(s), by {where}"
    )


def describe(focus: Optional[dict], *, today: Optional[date] = None) -> str:
    """The answer to "show focus"."""
    if not focus:
        return "No focus is set — I am going in sheet order."
    day = today or dl.today_ist()
    ends = dl.parse_date(str(focus.get("expires_on") or ""))
    left = (ends - day).days if ends else 0
    return (
        f"Focused on **{focus.get('value')}** until "
        f"{dl.format_date(ends) if ends else 'further notice'}"
        + (f" ({left} day(s) left)" if ends and left >= 0 else "")
        + f", set by {focus.get('set_by') or 'someone'}."
    )


def confirmation(focus_value: str, *, days: int, ends: date) -> str:
    return (
        f"Right — focusing on {focus_value} for the next {days} day(s), until "
        f"{dl.format_date(ends)}. I will put matching contacts first and say so "
        "when nothing matches. Say clear focus any time."
    )


def expiry_announcement(focus: dict) -> str:
    return (
        f"The focus on {focus.get('value')} has run out, so I am back to sheet "
        "order from now on."
    )


def not_allowed_reply(name: str) -> str:
    """The polite no. Says WHO can, because the person was trying to help."""
    import guardrails
    who = [guardrails.mention_for(uid) for uid in config.approver_ids()]
    who = " or ".join([w for w in who if w]) or "Sid or Vaishnavi"
    return (
        f"Thanks {name} — setting the focus is {who}'s call, so I have not "
        "changed anything. Ask one of them and I will pick it up straight away."
    )


def _self_test() -> int:
    """`python -m focus` — the parser and the ordering, no sheet."""
    logging.basicConfig(level=logging.WARNING, format="%(levelname)-7s %(message)s")
    failures = 0

    def check(name, got, want):
        nonlocal failures
        ok = got == want
        failures += 0 if ok else 1
        print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}, want {want!r}")

    print("parsing a set command")
    p = parse("prioritise only AI Voice Agents for the next two weeks")
    check("action", p["action"], SET)
    check("value", p["value"], "AI Voice Agents")
    check("duration", p["days"], 14)

    p = parse("focus on Wispr Flow for 10 days")
    check("company focus", (p["value"], p["days"]), ("Wispr Flow", 10))
    p = parse("prioritize founders")
    check("no duration -> the default",
          (p["value"], p["days"]), ("founders", config.FOCUS_DEFAULT_DAYS))
    p = parse("focus on London universities for a month")
    check("a month", p["days"], 30)
    check("capped at FOCUS_MAX_DAYS",
          parse("focus on X for 400 days")["days"], config.FOCUS_MAX_DAYS)

    print("\nshow and clear")
    check("show focus", parse("show focus")["action"], SHOW)
    check("what's the focus", parse("what's the focus?")["action"], SHOW)
    check("clear focus", parse("clear focus")["action"], CLEAR)
    check("drop the focus", parse("drop the focus")["action"], CLEAR)

    print("\nnot a focus command")
    for text in ("met Sahaj today", "yes", "", "what is the queue",
                 "how many rows can you see"):
        check(f"parse({text!r})", parse(text), None)

    print("\nmatching rows")
    config.FOCUS_MATCH_ROLES = ["industry", "company", "designation", "based"]
    rows = [
        {"company": "Wispr Flow", "industry": "AI Voice Agents", "designation": "CTO", "based": "SF"},
        {"company": "Acme", "industry": "Fintech", "designation": "Founder", "based": "London"},
        {"company": "PolyAI", "industry": "AI voice agents (conversational)", "designation": "Eng", "based": "London"},
    ]
    check("matches on industry", matches(rows[0], "AI Voice Agents"), "industry")
    check("matches loosely", matches(rows[2], "AI Voice Agents"), "industry")
    check("matches on company", matches(rows[0], "Wispr"), "company")
    check("matches on designation", matches(rows[1], "Founder"), "designation")
    check("matches on location", matches(rows[1], "London"), "based")
    check("no match", matches(rows[1], "AI Voice Agents"), "")

    print("\nordering")
    ordered, note = apply_to(rows, {"value": "AI Voice Agents"})
    check("matching rows come first",
          [r["company"] for r in ordered], ["Wispr Flow", "PolyAI", "Acme"])
    check("nothing is dropped", len(ordered), 3)
    check("the note names the focus", "AI Voice Agents" in note, True)
    check("...and the count", "2 matching" in note, True)
    print(f"    -> {note}")

    ordered, note = apply_to(rows, {"value": "Quantum Robotics"})
    check("no match -> sheet order unchanged",
          [r["company"] for r in ordered], ["Wispr Flow", "Acme", "PolyAI"])
    check("...and it SAYS so", "Nothing on the sheet matches" in note, True)
    check("...and says it is falling back", "sheet order" in note, True)
    print(f"    -> {note}")

    ordered, note = apply_to(rows, None)
    check("no focus -> unchanged and silent", (len(ordered), note), (3, ""))

    print("\nthe confirmation and the expiry")
    line = confirmation("AI Voice Agents", days=14, ends=date(2026, 10, 5))
    check("confirms the subject", "AI Voice Agents" in line, True)
    check("confirms the end date", "05 Oct 2026" in line, True)
    check("mentions clear focus", "clear focus" in line, True)
    print(f"    -> {line}")
    print(f"    -> {expiry_announcement({'value': 'AI Voice Agents'})}")

    print(f"\n{'ALL PASSED' if not failures else str(failures) + ' FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_self_test())
