"""R5 — WHO TO CONTACT NEXT. One ordering, and every path that names a prospect uses it.

`python -m prospects` runs the self-test. PURE: rows in, lists and text out.
No sheet, no Discord, no database, no clock (the caller passes `today`).

WHY THIS IS ITS OWN MODULE. Until 11 Oct the Tuesday post, the "who's left at
X?" answer and the tracker tool each ordered people their own way, so the same
question had three answers. Everything that ranks a company or a person is
here, and the post (`nextaction._r_prospects`), the replies to it and the
ad-hoc answers (`bot._answer_prospects`) all call it.

THE DESIGN, IN FOUR PARTS.

  1. ONE COMPANY IS OPEN AT A TIME. It is chosen by score and HELD: it leaves
     the open slot only when nobody there is left to contact, or when a person
     says to move on (`resolve`, `move_on`). Nothing else retires a company.
     A company moved on from is PAUSED, not dropped; it keeps its score and
     comes back when the company that replaced it is finished.

  2. THE SCORE IS SIX RULES THAT STACK (`RULES`). Each rule's weight is larger
     than every lower rule's weight added together, so an earlier rule always
     outweighs any combination of later ones, and among companies that share
     their earliest rule the later rules still separate them. It is NOT
     first-match-wins. Ties go to the company with FEWER people left (it can
     be finished sooner), then to sheet order. A company matching no rule is
     queued LAST, never left out (`unmatched`).

  3. WITHIN A COMPANY: seniority (`nextaction._role_rank`), then, between
     people at the same rank, how many of the three person signals they carry
     (UK-based, a keyword in the title, a research paper link), then sheet
     order (`order_people`). The top five are named, and the SAME five are
     named every time until somebody is contacted. There is no rotation.

  4. THE WORDING IS FIXED TEXT (`post_parts`, `rest_text`): no model writes it.
"""

from __future__ import annotations

import calendar
import logging
import re
from datetime import date
from typing import Optional

import config
import gtm_sheet

log = logging.getLogger("sales-bot.prospects")

# THE SIX RULES, most important first. The weights are powers of two, which is
# the smallest set where each exceeds the sum of all the ones after it.
RULES = ("warm", "voice", "lab", "uk", "keyword", "paper")
WEIGHTS = {name: 1 << (len(RULES) - 1 - i) for i, name in enumerate(RULES)}

# "Warm": at least this many people at the company already have a first
# contact recorded. One could be a stray; two is a relationship being built.
WARM_MIN_REACHED = 2

HEADING = "**PoCs to contact — {company}**"
ASK_REST = "Would you like me to list the rest?"
NEXT_LINE = "{company} is next on our list, let me know if you'd like to move on to {company}"

# WHAT THE REPLIES SAY WHEN THERE IS NOTHING TO LIST OR NOWHERE TO MOVE.
CANNOT_READ = "I'm sorry, I couldn't read Outreach PoCs just now. Could you ask me again in a minute?"
NOBODY_WAITING = "Nobody on Outreach PoCs is waiting for a first contact."
NOBODY_LEFT = "Everyone at {company} now has a first contact recorded, so there is nobody left to list."
THAT_IS_EVERYONE = "That is everyone left at {company}."
NOT_IN_QUEUE = ("I can't find {company} among the companies with people left to contact. "
                "Could you give me the name as it is on Outreach PoCs?")
WHICH_COMPANY = "That could be {names}. Which one would you like to move on to?"
NO_OTHER_COMPANY = "{company} is the only company with people left to contact."
ALREADY_OPEN = "{company} is already the company we're working through."

_HEADING_RE = re.compile(r"\*\*PoCs to contact — (.+?)\*\*")
_NEXT_RE = re.compile(r"^(.+?) is next on our list, let me know", re.MULTILINE)
_NUMBERED_RE = re.compile(r"^\s*\d+\.\s+(.+?)\s*$", re.MULTILINE)


def norm(text) -> str:
    """Lower-case, punctuation to spaces, whitespace squeezed."""
    return gtm_sheet.normalise_header(str(text if text is not None else "")).replace("?", " ").strip()


def company_key(name) -> str:
    """One company, however its cell is cased or spaced."""
    return norm(name)


def _cell(row: dict, role: str) -> str:
    return gtm_sheet.clean_cell((row or {}).get(role))


def _is_blank(value) -> bool:
    key = norm(value)
    return not key or key in gtm_sheet._UNKNOWN_WORDS


def has_word(text, phrases) -> bool:
    """Does `text` carry one of `phrases` as WHOLE WORDS?

    WHOLE WORDS, because the lists are short tokens: "UK" inside "Ukraine" and
    "STT" inside "Stuttgart" are not matches.
    """
    hay = f" {norm(text)} "
    return any(f" {key} " in hay for key in (norm(p) for p in phrases or ()) if key)


def industry_is(industry, phrases) -> bool:
    """LOOSE on purpose: normalised, then substring. People type "AI labs ",
    "AI Labs (frontier)" and "AI Voice Agents / TTS" into the Industry cell, and
    each of those is the industry it starts with."""
    hay = norm(industry)
    return bool(hay) and any(key in hay for key in (norm(p) for p in phrases or ()) if key)


def is_uk(row: dict) -> bool:
    """The free-text Based cell names the UK or one of its cities."""
    return has_word(_cell(row, "based"), config.PROSPECT_UK_MARKERS)


def has_keyword(row: dict) -> bool:
    """The title carries one of the subjects the team sells into."""
    return has_word(_cell(row, "designation"), config.PROSPECT_KEYWORDS)


def has_paper(row: dict) -> bool:
    """The row has a research paper link (anything that is not "no value")."""
    return not _is_blank(_cell(row, "paper_links"))


def signals(row: dict) -> int:
    """How many of the three person signals this row carries, 0 to 3."""
    return int(is_uk(row)) + int(has_keyword(row)) + int(has_paper(row))


def role_rank(designation) -> int:
    import nextaction
    return nextaction._role_rank(str(designation or ""))


def order_people(rows) -> list:
    """THE ORDER PEOPLE ARE NAMED IN, everywhere.

    Seniority first. Between people at the SAME rank, more of the three person
    signals first. Then sheet order. A title the role list does not recognise
    ranks last, in sheet order: an unrecognised title says nothing about
    seniority either way, so it waits rather than jumping the queue.
    """
    return sorted(rows or [], key=lambda r: (role_rank(_cell(r, "designation")),
                                             -signals(r), int(r.get("_row") or 0)))


def designation_label(text) -> str:
    """The title as it may be printed in brackets, or "" for no bracket.

    Whitespace and line breaks are collapsed. A title longer than
    PROSPECT_DESIGNATION_MAX_CHARS prints NO BRACKET AT ALL: the cell is a
    person's own words, so it is never cut short and never replaced by a
    shorter title the sheet does not carry.
    """
    raw = str(text or "")
    label = " ".join(raw.split())
    if not label or _is_blank(label):
        return ""
    if "\n" in raw.strip() or "\r" in raw.strip():
        return ""
    return label if len(label) <= max(1, int(config.PROSPECT_DESIGNATION_MAX_CHARS)) else ""


def person_line(n: int, name, designation="") -> str:
    """`3. Ray (Ruizhe) Huang (Member of Technical Staff)`."""
    who = " ".join(str(name or "").split()) or "(no name on the row)"
    role = designation_label(designation)
    return f"{n}. {who}" + (f" ({role})" if role else "")


# -- the company queue --------------------------------------------------------

def build_queue(rows, *, today: date, snoozes: Optional[dict] = None) -> list:
    """Every company that still has somebody to contact, best first.

    One dict a company:

        company    the sheet's spelling (the first row's)
        key        `company_key`
        score      the sum of the weights of the rules it matches
        flags      which rules, in RULES order
        left       the people still to contact, in `order_people` order
        reached    how many rows there have a first contact recorded
        moved      True when a First Contact Date at the company falls in
                   `today`'s calendar month
        first_row  where the company first appears on the tab
        industries the Industry cells seen there, normalised

    WHO IS "LEFT": a row the stop and snooze rules allow, with no first
    contact recorded, at a named company. WHO IS "REACHED": every row at the
    company with a first contact recorded, whatever else is true of it.
    """
    import nextaction

    groups: dict = {}
    for row in rows or ():
        company = _cell(row, "company")
        key = company_key(company)
        if not key:
            continue
        g = groups.setdefault(key, {
            "company": company, "key": key, "left": [], "reached": 0, "moved": False,
            "first_row": int(row.get("_row") or 0), "industries": []})
        g["first_row"] = min(g["first_row"], int(row.get("_row") or 0))
        g["industries"].append(_cell(row, "industry"))
        if nextaction.first_contact_done(row):
            g["reached"] += 1
            when = nextaction._date(row, "first_contact_date")
            if when is not None and (when.year, when.month) == (today.year, today.month):
                g["moved"] = True
            continue
        ok, _why, _until = nextaction.row_gate(row, today=today, snoozes=snoozes or {})
        if ok:
            g["left"].append(row)

    queue: list = []
    for g in groups.values():
        if not g["left"]:
            continue
        left = order_people(g["left"])
        found = {
            "warm": g["reached"] >= WARM_MIN_REACHED,
            "voice": any(industry_is(i, config.PROSPECT_VOICE_INDUSTRIES)
                         for i in g["industries"]),
            "lab": any(industry_is(i, config.PROSPECT_LAB_INDUSTRIES)
                       for i in g["industries"]),
            "uk": any(is_uk(r) for r in left),
            "keyword": any(has_keyword(r) for r in left),
            "paper": any(has_paper(r) for r in left),
        }
        flags = [name for name in RULES if found[name]]
        queue.append({
            "company": g["company"], "key": g["key"], "left": left,
            "reached": g["reached"], "moved": g["moved"], "first_row": g["first_row"],
            "industries": sorted({norm(i) for i in g["industries"] if norm(i)}),
            "flags": flags, "score": sum(WEIGHTS[name] for name in flags),
        })
    # FEWER LEFT WINS A TIE (it can be finished sooner), then sheet order.
    queue.sort(key=lambda e: (-e["score"], len(e["left"]), e["first_row"]))
    return queue


def unmatched(queue) -> tuple:
    """(companies, people) that match none of the six rules. They are queued
    last, never left out, and the count is what the log and the "what is
    left?" answer report."""
    rest = [e for e in queue or [] if not e["score"]]
    return len(rest), sum(len(e["left"]) for e in rest)


def resolve(queue, state: Optional[dict]) -> dict:
    """Which company is open, which is next, and the state to remember.

    `state` is what the database holds: {"open": key, "paused": [keys, oldest
    first]}. Returns {"open", "next", "paused", "state"}; "open" and "next"
    are queue entries or None.

    A COMPANY IS NEVER DROPPED. THE OPEN COMPANY LEAVES THE OPEN SLOT IN
    EXACTLY TWO WAYS: nobody there is left to contact (it is no longer in the
    queue), or a person said to move on (`move_on`, which pauses it). A higher
    score elsewhere, a quiet month, a repeat of the same five names: none of
    those moves it. There is deliberately no other branch here.

    WHEN THE OPEN COMPANY IS FINISHED, every paused company comes back and the
    best score opens, which is how "paused, not dropped" is kept.
    """
    state = state or {}
    by_key = {e["key"]: e for e in queue or []}
    held = str(state.get("open") or "")
    paused = [k for k in (state.get("paused") or []) if k in by_key and k != held]
    current = by_key.get(held)
    if current is None:
        if held:
            paused = []                 # the open company is finished: the paused return
        fresh = [e for e in queue or [] if e["key"] not in paused]
        current = fresh[0] if fresh else ((queue or [None])[0])
        if current is not None and current["key"] in paused:
            paused.remove(current["key"])
    nxt = None
    if current is not None:
        others = [e for e in queue if e["key"] != current["key"]]
        nxt = next((e for e in others if e["key"] not in paused),
                   others[0] if others else None)
    return {"open": current, "next": nxt, "paused": [by_key[k] for k in paused],
            "state": {"open": current["key"] if current else "", "paused": list(paused)}}


def move_on(queue, state: Optional[dict], target_key: str = "") -> dict:
    """A PERSON SAID TO MOVE ON. The open company is paused and `target_key`
    (or, bare, the company that was next) becomes the open one. `resolve`'s
    shape back. The paused company keeps its score, because the score is read
    off the sheet every time and nothing here touches it."""
    now = resolve(queue, state)
    current, target = now["open"], None
    by_key = {e["key"]: e for e in queue or []}
    if target_key:
        target = by_key.get(target_key)
    elif now["next"] is not None:
        target = now["next"]
    if current is None or target is None or target["key"] == current["key"]:
        return now
    paused = [k for k in now["state"]["paused"] if k != target["key"]]
    paused.append(current["key"])
    return resolve(queue, {"open": target["key"], "paused": paused})


def find_company(queue, said: str):
    """The queue entries a person's words name: exact key, else one key inside
    the other ("Cartesia" is "Cartesia.ai"). A list, so the caller can tell
    "none" from "more than one" and never guesses."""
    want = company_key(said)
    if not want:
        return []
    exact = [e for e in queue or [] if e["key"] == want]
    if exact:
        return exact
    squeezed = want.replace(" ", "")
    return [e for e in queue or []
            if want in e["key"] or e["key"] in want
            or squeezed in e["key"].replace(" ", "")]


def in_industry(queue, said: str) -> list:
    """The queue entries whose Industry carries every word of `said`, in queue
    order: "voice AI" finds "AI Voice Agents". [] when the words name none."""
    want = set(norm(said).split())
    if not want:
        return []
    return [e for e in queue or []
            if any(want <= set(i.split()) for i in e.get("industries") or [])]


# -- the month's last slot ----------------------------------------------------

def is_last_slot_of_month(today: date, weekdays) -> bool:
    """Is `today` the LAST day this month the rule runs on (the later of the
    last Tuesday and the last Thursday, for R5)?"""
    days = {int(d) for d in weekdays or ()}
    if today.weekday() not in days:
        return False
    last = calendar.monthrange(today.year, today.month)[1]
    return not any(date(today.year, today.month, d).weekday() in days
                   for d in range(today.day + 1, last + 1))


def _names(names: list) -> str:
    names = [n for n in names if n]
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


def _people(n: int) -> str:
    return "1 person" if n == 1 else f"{n} people"


def stalled_lines(view: dict) -> list:
    """The two lines at the foot of the month's last post, or [].

    "HASN'T MOVED" IS READ OFF THE SHEET: no row at the company gained a First
    Contact Date in this calendar month (`build_queue`'s `moved`). No state is
    kept for it. Only when the OPEN company is stalled is anything said; the
    paused companies that are stalled too are then named with it, oldest
    first, with ONE number for all of them. THIS CLOSES NOTHING: it is a
    sentence, and the open company stays open.
    """
    current = (view or {}).get("open")
    if current is None or current.get("moved"):
        return []
    stuck = [e for e in (view.get("paused") or []) if not e.get("moved")] + [current]
    total = sum(len(e["left"]) for e in stuck)
    if len(stuck) == 1:
        return [f"{current['company']} hasn't moved this month — {_people(total)} "
                "still to contact there.",
                "It's blocking other PoCs to contact."]
    return [f"{_names([e['company'] for e in stuck])} haven't moved — {_people(total)} "
            "still to contact between them.",
            "They're blocking other PoCs to contact."]


# -- the wording --------------------------------------------------------------

def post_parts(company: str, people: list, *, reached: int, left_total: int,
               next_company: str = "", foot=(), ask: bool = True) -> dict:
    """The Tuesday and Thursday message, in the three pieces the drip sends.

        **PoCs to contact — Wisprlow.ai**

        1. Tanay Kothari (Co-Founder / CEO)
        ...
        5. Nathan Klapoetke (Computational Neuroscientist)

        We've already reached 2 from Wisprlow.ai, there are 7 people left to contact.
        Would you like me to list the rest?

        Cartesia.ai is next on our list, let me know if you'd like to move on to Cartesia.ai

    `people` is [(name, designation)] for the people SHOWN, already in order.
    `left_total` is everybody at the company with no first contact, the people
    shown included; the message says how many are left BEYOND those shown.
    ONE QUESTION ONLY: moving on is an offer answered in words. With nobody
    beyond the people shown, the question is dropped. `ask=False` drops it
    too, for an answer to a question somebody asked (nothing is behind a
    "yes" there, and an offer the bot cannot honour is not made).
    """
    company = " ".join(str(company or "").split())
    lines = [person_line(i, name, role) for i, (name, role) in enumerate(people, 1)]
    more = max(0, int(left_total) - len(lines))
    tail: list = []
    if reached > 0 and more:
        tail.append(f"We've already reached {reached} from {company}, there "
                    f"{'is' if more == 1 else 'are'} {_people(more)} left to contact.")
    elif reached > 0:
        tail.append(f"We've already reached {reached} from {company}.")
    elif more:
        tail.append(f"We haven't reached anyone from {company} yet, there "
                    f"{'is' if more == 1 else 'are'} {_people(more)} left to contact.")
    if more and ask:
        tail.append(ASK_REST)
    if next_company:
        if tail:
            tail.append("")
        tail.append(NEXT_LINE.format(company=" ".join(str(next_company).split())))
    foot = [str(x) for x in (foot or ()) if str(x).strip()]
    if foot:
        if tail:
            tail.append("")
        tail.extend(foot)
    return {"header": HEADING.format(company=company), "lines": lines, "tail": tail,
            "more": more}


def post_text(company: str, people: list, **kw) -> str:
    """`post_parts` as the one message it is posted as."""
    p = post_parts(company, people, **kw)
    body = [p["header"], ""] + p["lines"]
    if p["tail"]:
        body += [""] + p["tail"]
    return "\n".join(body)


def rest_text(company: str, people: list, *, start: int) -> str:
    """The answer to a yes: everybody else, numbered on from the post. No new
    question, and the company stays open."""
    company = " ".join(str(company or "").split())
    n = len(people)
    head = (f"Of course — here is the other one at {company}:" if n == 1
            else f"Of course — here are the other {n} at {company}:")
    return "\n".join([head, ""] + [person_line(i, name, role)
                                   for i, (name, role) in enumerate(people, start)])


def pairs(rows) -> list:
    """[(name, designation)] for rows, in the order given."""
    return [(_cell(r, "name"), _cell(r, "designation")) for r in rows or []]


def read_post(text: str) -> dict:
    """What one of the bot's own R5 messages said: {"company", "next",
    "names"}. Used to answer a reply to it. Each value is "" or [] when the
    text is not such a message."""
    body = str(text or "")
    head = _HEADING_RE.search(body)
    nxt = _NEXT_RE.search(body)
    names = []
    for line in _NUMBERED_RE.findall(body):
        names.append(line)
    return {"company": head.group(1).strip() if head else "",
            "next": nxt.group(1).strip() if nxt else "", "names": names}


def queue_summary(queue, view: dict, *, limit: int = 10) -> str:
    """What is left, for somebody who asks: the open company, the next few in
    order, and how many companies match none of the six rules."""
    if not queue:
        return "Nobody on Outreach PoCs is waiting for a first contact."
    current = (view or {}).get("open")
    total = sum(len(e["left"]) for e in queue)
    out = [f"{_people(total)} at {len(queue)} "
           f"{'company' if len(queue) == 1 else 'companies'} still have no first contact."]
    if current is not None:
        out += ["", f"Open now: {current['company']} ({len(current['left'])} left)."]
    rest = [e for e in queue if current is None or e["key"] != current["key"]]
    paused = {e["key"] for e in (view or {}).get("paused") or []}
    if rest:
        out += ["", "Next, in order:"]
        out += [f"{i}. {e['company']} ({len(e['left'])} left)"
                + (" — paused" if e["key"] in paused else "")
                for i, e in enumerate(rest[:max(1, int(limit))], 1)]
    companies, people = unmatched(queue)
    if companies:
        out += ["", f"{companies} {'company' if companies == 1 else 'companies'} "
                    f"({_people(people)}) match none of the six priority rules and "
                    "are queued last."]
    return "\n".join(out)


def log_unmatched(queue, *, where: str) -> None:
    """Say how many companies match no rule. They are queued, not excluded; the
    number is logged so nobody has to wonder where they went."""
    companies, people = unmatched(queue)
    log.info("[prospects] %s: %d company(ies) with people left to contact; %d of them "
             "(%d people) match none of the six priority rules and are queued last",
             where, len(queue or []), companies, people)


if __name__ == "__main__":
    import sys

    FAILED = 0

    def check(name, got, want):
        global FAILED
        ok = got == want
        FAILED += 0 if ok else 1
        print(("  ok    " if ok else "  FAIL  ") + name + ("" if ok else f"\n        got  {got!r}\n        want {want!r}"))

    print("weights")
    for i, name in enumerate(RULES):
        check(f"{name} outweighs every later rule together",
              WEIGHTS[name] > sum(WEIGHTS[n] for n in RULES[i + 1:]), True)
    print("\nmatching")
    check("loose industry: trailing space", industry_is("AI labs ", ["AI Labs"]), True)
    check("loose industry: a suffix", industry_is("AI Labs (frontier)", ["AI Labs"]), True)
    check("UK is a whole word", (has_word("Kyiv, Ukraine", ["UK"]), has_word("London, UK", ["UK"])),
          (False, True))
    print("\nthe month's last slot")
    check("Thu 29 Oct 2026 is the last Tue-or-Thu", is_last_slot_of_month(date(2026, 10, 29), (1, 3)), True)
    check("Tue 27 Oct 2026 is not", is_last_slot_of_month(date(2026, 10, 27), (1, 3)), False)
    check("Tue 30 Jun 2026 is (the Tuesday falls later)", is_last_slot_of_month(date(2026, 6, 30), (1, 3)), True)
    print("\nwording")
    check("a long title prints no bracket",
          person_line(1, "A B", "x" * 61), "1. A B")
    check("a title with a line break prints no bracket", person_line(2, "A B", "CEO\nBoard"), "2. A B")
    print("\nALL PASSED" if not FAILED else f"\n{FAILED} FAILED")
    sys.exit(1 if FAILED else 0)
