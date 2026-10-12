"""R2 — COMPANIES IN THE NEWS THAT WE DO NOT TRACK YET. The pure half.

`python -m news_screen` runs the self-test. PURE: names, stories and rows in,
lists and text out. No sheet, no Discord, no database, no model, no clock (the
caller passes `now`). `bot._screen_companies` does the reading and the calls.

THE BUG THIS EXISTS FOR (11 Oct 2026). The post named three companies as "not
in the pipeline". ElevenLabs was number 1 of the 382 on the tracked list: the
model saw it, said "already tracked, so probably skip", and it went out under
the opposite heading because nothing in code checked the answer. Yellow.ai was
number 368: the list handed to the model was cut at 200, so it was never seen.
Two independent faults, so two fixes:

  1. THE LIST IS NEVER CUT. Every company on the Master Pipeline and on
     Outreach PoCs is known, however many there are (`build_index`).
  2. THE SCREEN IS IN CODE, NOT IN THE PROMPT. The model is asked only which
     companies the stories are about (`names_prompt`). Each name is then
     matched against both sheets HERE (`classify`), and only what survives is
     sent back for a verdict (`verdict_prompt`). A tracked company the model
     names anyway cannot reach the post.

MATCHING IS ON THE FULL NAME. "Nudge Security" and "Nudge - AI Commerce
Discovery" are different companies and both are real; "Google" is not "Google
DeepMind". A name that only PARTLY matches a tracked one is a QUESTION put to
the team (`similar`), never an assumption in either direction.

THREE OUTCOMES: on the Master Pipeline -> dropped, never mentioned. On Outreach
PoCs only -> its own line, asking for a pipeline row. On neither -> proposed.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Optional

import gtm_sheet
import links

KNOWN, POCS_ONLY, NEW = "known", "pocs_only", "new"
GOOD, WEAK = "Good fit", "Weak fit"
MAX_SHOWN = 5

HEADING = "**New companies in the news**"
POCS_ONLY_LINE = ("{company} isn't on the Master Pipeline, but it is on Outreach PoCs. "
                  "Shall I add a pipeline row?")
SIMILAR_LINE = ("  {company} may be the same company as {others}, which we already track. "
                "Could you confirm before I add it?")

# A LEGAL OR HOUSE SUFFIX IS NOT PART OF THE NAME: "Cartesia" in a headline is
# "Cartesia.ai" on the sheet, and "Acme Inc" is "Acme". Only a TRAILING word,
# and only while something is left. Nothing else is ever dropped: "Labs",
# "Security", "DeepMind" are what tell two companies apart.
_SUFFIXES = frozenset({"ai", "inc", "incorporated", "ltd", "limited", "llc", "corp",
                       "corporation", "co", "company", "plc", "gmbh"})
_NUMBER_WORDS = ("Zero", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight",
                 "Nine", "Ten")


def _words(name) -> list:
    return gtm_sheet.normalise_header(str(name or "")).replace("?", " ").split()


def name_key(name) -> str:
    """One company, however it is cased, spaced or punctuated: lower-case, the
    trailing legal suffix off, the spaces out. "Eleven Labs", "ElevenLabs" and
    "ElevenLabs Inc." are one key; "Nudge Security" and "Nudge - AI Commerce
    Discovery" are two."""
    words = _words(name)
    while len(words) > 1 and words[-1] in _SUFFIXES:
        words.pop()
    return "".join(words)


def _core(name) -> list:
    words = _words(name)
    while len(words) > 1 and words[-1] in _SUFFIXES:
        words.pop()
    return words


def build_index(pocs_names, pipeline_names) -> dict:
    """The known companies, by `name_key`: {"pipeline": {key: name}, "pocs":
    {key: name}}. EVERY NAME, NO CAP. `count` is how many distinct companies
    that is across both sheets."""
    def table(names) -> dict:
        out: dict = {}
        for n in names or ():
            label = gtm_sheet.clean_cell(n)
            key = name_key(label)
            if key and key not in out:
                out[key] = label
        return out
    pipeline, pocs = table(pipeline_names), table(pocs_names)
    return {"pipeline": pipeline, "pocs": pocs, "count": len(set(pipeline) | set(pocs))}


def all_names(index: dict) -> list:
    """Every known company once, Master Pipeline spelling first."""
    out = dict(index.get("pocs") or {})
    out.update(index.get("pipeline") or {})
    return list(out.values())


def _contains(long_: list, short: list) -> bool:
    n = len(short)
    return 0 < n < len(long_) and any(long_[i:i + n] == short for i in range(len(long_) - n + 1))


def classify(name, index: dict) -> dict:
    """Where one company stands: {"outcome", "sheet_name", "similar"}.

    THE FULL NAME DECIDES. `known` (on the Master Pipeline) and `pocs_only`
    need the whole normalised name to be equal. `similar` lists tracked
    companies whose name CONTAINS this one as whole words or is contained in
    it ("Google" and "Google DeepMind"): that is a question for a person, and
    the company is still treated as not tracked until somebody answers it.
    """
    key = name_key(name)
    pipeline, pocs = index.get("pipeline") or {}, index.get("pocs") or {}
    if key and key in pipeline:
        return {"outcome": KNOWN, "sheet_name": pipeline[key], "similar": []}
    if key and key in pocs:
        return {"outcome": POCS_ONLY, "sheet_name": pocs[key], "similar": []}
    mine = _core(name)
    similar: list = []
    if mine and any(len(w) >= 3 for w in mine):
        for known_name in all_names(index):
            theirs = _core(known_name)
            if _contains(theirs, mine) or _contains(mine, theirs):
                if known_name not in similar:
                    similar.append(known_name)
    return {"outcome": NEW, "sheet_name": "", "similar": similar}


# -- the news window ----------------------------------------------------------

def previous_run_day(today: date, weekdays) -> date:
    """The last day BEFORE `today` the rule is scheduled on."""
    days = {int(d) for d in weekdays or ()}
    for back in range(1, 8):
        day = today - timedelta(days=back)
        if not days or day.weekday() in days:
            return day
    return today - timedelta(days=7)


def window(*, now: datetime, marker: Optional[datetime], floor: datetime,
           first_days: int = 7) -> tuple:
    """(since, until) for one run. Both ends are INCLUSIVE.

    since   the moment R2 last POSTED (`marker`), but never earlier than
            `floor`, the previous scheduled R2 slot. Friday looks back to
            Tuesday even if Tuesday posted nothing, so a run of quiet weeks or
            a fortnight's outage cannot turn into a fortnight-wide sweep.
    until   now.
    With no marker at all (the first run ever): `first_days` back from now.
    """
    if marker is None:
        return now - timedelta(days=max(1, int(first_days))), now
    return min(max(marker, floor), now), now


# -- step (a): which companies are the stories about --------------------------

def names_prompt(stories: list) -> str:
    """NAMES ONLY. No verdict and no reason: those are paid for later, and
    only for the companies that survive the code-side filter."""
    lines = [
        "Below are numbered news stories. For each one, name the COMPANY or "
        "companies the story is specifically about.",
        "",
        "Names only. No description, no opinion, no explanation.",
        "A story about regulation, a government, a city, a person with no company, "
        "or an industry-wide trend is about no company: write NONE for it.",
        "Give the company's own name as the story writes it, not a product's name.",
        "",
        "FORMAT, one line per story and nothing else:",
        "  NAMES | <story number> | <company>; <company>",
        "  NAMES | <story number> | NONE",
        "",
        "STORIES:",
    ]
    for i, s in enumerate(stories, 1):
        head = " ".join(str(s.get("title") or s.get("headline") or "").split())[:200]
        what = " ".join(str(s.get("what") or s.get("summary") or "").split())[:300]
        lines.append(f"  {i}. {head}" + (f": {what}" if what else ""))
    return "\n".join(lines)


_NAMES_RE = re.compile(r"^\s*[-*•]?\s*NAMES\s*\|\s*(\d+)\s*\|\s*(.*?)\s*$", re.IGNORECASE)


def parse_names(text: str, stories: list) -> list:
    """[{"company", "stories": [story, ...]}] in the order first named. A line
    for a story number that was not shown is ignored: the model is data."""
    found: dict = {}
    for line in str(text or "").splitlines():
        m = _NAMES_RE.match(line)
        if not m:
            continue
        n = int(m.group(1))
        if not 1 <= n <= len(stories):
            continue
        for raw in re.split(r"[;|]", m.group(2)):
            name = " ".join(raw.split()).strip(" .,-–—")
            if not name or name.upper() == "NONE" or len(name) > 80:
                continue
            entry = found.setdefault(name_key(name), {"company": name, "stories": []})
            if stories[n - 1] not in entry["stories"]:
                entry["stories"].append(stories[n - 1])
    return [e for k, e in found.items() if k]


def best_story(stories: list) -> dict:
    """The story a company's name links to: the most important one, and on a
    tie the newest."""
    def rank(s):
        return (int(s.get("importance") or 0),
                str(s.get("published_at") or s.get("seen_at") or ""))
    return max(stories, key=rank) if stories else {}


# -- step (c): the verdict, for the survivors only ----------------------------

def verdict_prompt(candidates: list, *, use_cases: str = "", allow_lookup: bool = False) -> str:
    """One verdict and one clause of reason per company. EXACTLY TWO VERDICTS."""
    lines = [
        "For each company below, judge whether it could be a customer for membrane, "
        "using only the story given for it and what membrane sells (below).",
        "",
        "For each company write ONE line and nothing else:",
        "  VERDICT | <company, exactly as given> | <what happened, in a few words> | "
        "GOOD or WEAK | <the reason, one short clause>",
        "",
        "GOOD or WEAK only. There is no third answer: something borderline is WEAK, "
        "with the reason. Never refer to a use case by a letter or a number. Do not "
        "propose adding anything to any sheet.",
    ]
    if allow_lookup:
        lines += [
            "",
            "If the story does NOT say what the company does, do not guess. Instead of "
            "its VERDICT line write:",
            "  LOOKUP | <company>",
        ]
    if str(use_cases or "").strip():
        lines += ["", "WHAT MEMBRANE SELLS (from the sales strategy):", use_cases.strip()]
    lines += ["", "COMPANIES:"]
    for c in candidates:
        head = " ".join(str(c.get("title") or "").split())[:200]
        what = " ".join(str(c.get("summary") or "").split())[:300]
        extra = " ".join(str(c.get("lookup") or "").split())[:600]
        lines.append(f"  - {c['company']} — story: {head}" + (f": {what}" if what else "")
                     + (f" — also found: {extra}" if extra else ""))
    return "\n".join(lines)


_VERDICT_RE = re.compile(r"^\s*[-*•]?\s*VERDICT\s*\|", re.IGNORECASE)
_LOOKUP_RE = re.compile(r"^\s*[-*•]?\s*LOOKUP\s*\|\s*(.+?)\s*$", re.IGNORECASE)


def _clause(text: str, limit: int = 160) -> str:
    """One clause: whitespace squeezed, no trailing full stop, no ping."""
    body = " ".join(str(text or "").split()).strip(" .;—–-")
    body = body.replace("@", "@​")
    return body if len(body) <= limit else body[:limit].rsplit(" ", 1)[0].rstrip(" ,;:")


def parse_verdicts(text: str) -> dict:
    """{name_key: {"what", "verdict", "reason"}}. ANYTHING THAT IS NOT "GOOD" IS
    A WEAK FIT: the message has two verdicts and no third wording can get in."""
    out: dict = {}
    for line in str(text or "").splitlines():
        if not _VERDICT_RE.match(line):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 5 or not parts[1]:
            continue
        word = parts[3].strip(" *.").upper()
        out[name_key(parts[1])] = {
            "what": _clause(parts[2], 90),
            "verdict": GOOD if word.startswith("GOOD") else WEAK,
            "reason": _clause(" ".join(parts[4:])),
        }
    return out


def parse_lookups(text: str) -> list:
    out: list = []
    for line in str(text or "").splitlines():
        m = _LOOKUP_RE.match(line)
        if m:
            name = m.group(1).split("|")[0].strip()
            if name and name not in out:
                out.append(name)
    return out


# -- the repeats: ask again until answered or stale ---------------------------

def merge(fresh: list, ledger: list, *, now: datetime, stale_days: int = 14) -> dict:
    """Today's candidates and what the ledger must record.

    `fresh` is this window's survivors; `ledger` is every row the database
    holds ({"company_key", "status", "first_at", ...}). Returns {"candidates",
    "stale", "suppressed"}:

      - a row still OPEN (asked, not answered) or WAITING (held back last
        time) COMES BACK, with what was already known about it, until it is
        `stale_days` old. Then it is `stale` and is dropped;
      - a fresh company somebody DECLINED, or that went stale, inside the last
        `stale_days` is `suppressed`: it was answered. After that it may be
        proposed again if it is in the news again;
      - everything else fresh is a candidate.
    """
    limit = now - timedelta(days=max(1, int(stale_days)))
    by_key = {str(r.get("company_key") or ""): r for r in ledger or []}

    def at(row) -> Optional[datetime]:
        try:
            return datetime.fromisoformat(str(row.get("first_at") or ""))
        except ValueError:
            return None

    candidates, stale, suppressed, seen = [], [], [], set()
    for row in ledger or []:
        if row.get("status") not in ("open", "waiting"):
            continue
        when = at(row)
        if when is not None and when < limit:
            stale.append(row)
            continue
        candidates.append({**row, "carried": True})
        seen.add(str(row.get("company_key") or ""))
    for c in fresh or []:
        key = c["company_key"]
        if key in seen:
            continue
        old = by_key.get(key)
        if old and old.get("status") in ("dropped", "stale"):
            when = at(old)
            if when is not None and when >= limit:
                suppressed.append(c)
                continue
        candidates.append({**c, "carried": False})
        seen.add(key)
    candidates.sort(key=lambda c: (c.get("outcome") == POCS_ONLY,
                                   -int(c.get("importance") or 0),
                                   str(c.get("first_at") or "9999")))
    return {"candidates": candidates, "stale": stale, "suppressed": suppressed}


# -- the message ---------------------------------------------------------------

def _count_word(n: int) -> str:
    return _NUMBER_WORDS[n] if 0 <= n < len(_NUMBER_WORDS) else str(n)


def company_link(name: str, url: str) -> str:
    """The company's name as a masked link to its story. A url with no scheme
    is not a link (it would go out as dead text), so the name goes out bold
    and plain rather than half-linked."""
    name = " ".join(str(name or "").split()).replace("@", "@​")
    url = str(url or "").strip().strip("<>")
    if not re.match(r"^https?://\S+$", url):
        return f"**{name}**"
    return f"**[{name}](<{url}>)**" if "]" not in name else f"**{links.link(name, url)}**"


def render(shown: list, *, held: int = 0) -> str:
    """The message body, or "" with nothing to say (and then NOTHING IS POSTED).

        Two companies in today's news aren't on the Master Pipeline yet:

        - **[Nudge Security](<url>)** — raised $25M. **Weak fit:** sells to
          enterprise security teams, not AI teams.

        Would you like me to add either of them to the Master Pipeline?

    A PoCs-only company gets its own sentence after the list, and asks its own
    question. `held` companies that did not fit are counted in one line.
    """
    new = [c for c in shown if c.get("outcome") != POCS_ONLY]
    pocs = [c for c in shown if c.get("outcome") == POCS_ONLY]
    if not new and not pocs:
        return ""
    out: list = []
    if new:
        n = len(new)
        out.append(f"{_count_word(n)} {'company' if n == 1 else 'companies'} in today's news "
                   f"{'isn’t' if n == 1 else 'aren’t'} on the Master Pipeline yet:".replace("’", "'"))
        for c in new:
            what = _clause(c.get("what") or "", 90)
            reason = _clause(c.get("reason") or "")
            verdict = GOOD if c.get("verdict") == GOOD else WEAK
            line = f"- {company_link(c['company'], c.get('url') or '')}"
            if what:
                line += f" — {what}."
            line += f" **{verdict}:** {reason}." if reason else f" **{verdict}.**"
            out += ["", line]
            if c.get("similar"):
                out.append(SIMILAR_LINE.format(
                    company=c["company"], others=_and(c["similar"][:3])))
        out += ["", "Would you like me to add "
                    + ("it" if n == 1 else "either of them" if n == 2 else "any of them")
                    + " to the Master Pipeline?"]
    for c in pocs:
        out += ["", POCS_ONLY_LINE.format(company=c.get("sheet_name") or c["company"])]
    if held > 0:
        out += ["", (f"{_count_word(held)} more "
                     f"{'company is' if held == 1 else 'companies are'} waiting — I'll bring "
                     f"{'it' if held == 1 else 'them'} next time.")]
    return "\n".join(out).strip()


def _and(names: list) -> str:
    names = [str(n) for n in names if str(n).strip()]
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


# -- the reply -------------------------------------------------------------------

_FILLER = frozenset("""
add adding yes yeah yep ok okay sure please pls thanks thank you the to a an it them that this
those these one ones master pipeline row rows sheet go ahead do can could would want like lets
let s us me i we for on in of our into as well also just too both all either and or but not
no nope dont don t skip leave out except only company companies new first second
""".split())
_ALL_RE = re.compile(r"\b(both|all( of them| \w+)?|either|every one|everything)\b", re.IGNORECASE)
_NONE_RE = re.compile(r"^\s*(no|nope|none|neither|not now|no thanks|no thank you|skip( them| it)?|"
                      r"leave (them|it)|not (any|either)( of them)?)[\s.!]*$", re.IGNORECASE)
_BARE_YES_RE = re.compile(r"^\s*(yes|yeah|yep|ok|okay|sure|go ahead|please do|add (it|them))"
                          r"([\s,]+(please|thanks))?[\s.!]*$", re.IGNORECASE)


def read_reply(text: str, companies: list) -> dict:
    """What a reply to the message asks for, read by COMPANY NAME.

    Returns {"chosen", "ambiguous", "unknown", "none", "bare_yes"}:

      chosen     companies from the message the reply names. A full name, or
                 words that fit exactly ONE company in the message ("add
                 Mirage" when only Mirage Labs was listed).
      ambiguous  [(what was said, [the companies it could mean])]: "add Nudge"
                 with two Nudges. NOTHING IS EVER WRITTEN ON THESE.
      unknown    names the reply gave that were not in the message.
      none       the reply declines everything ("no", "none of them").
      bare_yes   a yes that names nobody. With one company in the message the
                 caller may take it as that one; with more it must ask which.

    NOT `approvals.read_vote`: that reads a yes or a no out of a sentence, and
    "yes to Mirage but not Nudge" is neither.
    """
    body = " ".join(str(text or "").split())
    out = {"chosen": [], "ambiguous": [], "unknown": [], "none": False, "bare_yes": False}
    if not body or not companies:
        return out
    if _NONE_RE.match(body):
        out["none"] = True
        return out
    if _BARE_YES_RE.match(body):
        out["bare_yes"] = True
        return out
    keyed = [(c, _core(c)) for c in companies]
    hay = f" {' '.join(_words(body))} "
    proper = {w for tok in body.split()[1:] if tok[:1].isupper() for w in _words(tok)}
    taken: list = []
    for c, core in sorted(keyed, key=lambda kc: -len(kc[1])):
        phrase = " ".join(core)
        if phrase and f" {phrase} " in hay:
            out["chosen"].append(c)
            hay = hay.replace(f" {phrase} ", " | ", 1)
            taken.append(c)
    # "but not Nudge": a company named after a refusal is NOT chosen.
    refused = re.search(r"\b(?:but not|not|except|without|skip|leave out)\b(.*)$", body,
                        re.IGNORECASE)
    if refused:
        tail = f" {' '.join(_words(refused.group(1)))} "
        out["chosen"] = [c for c in out["chosen"]
                         if f" {' '.join(_core(c))} " not in tail]
        hay = hay.split(" not ")[0] + " " if " not " in hay else hay
    if not out["chosen"] and _ALL_RE.search(body) and not refused:
        out["chosen"] = list(companies)
        return out
    # WHAT IS LEFT: words that are not filler. Each run of them is a name the
    # reply gave that did not match a whole company.
    for chunk in hay.split("|"):
        run: list = []
        for w in chunk.split() + [""]:
            if w and w not in _FILLER and not w.isdigit():
                run.append(w)
                continue
            if run:
                said = " ".join(run)
                fits = [c for c, core in keyed if c not in taken
                        and any(x in core for x in run)]
                if len(fits) == 1 and fits[0] not in out["chosen"]:
                    out["chosen"].append(fits[0])
                elif len(fits) > 1:
                    out["ambiguous"].append((said, fits))
                elif not fits and any(x in proper for x in run)                         and not any(x in core for c, core in keyed for x in run):
                    # ONLY A CAPITALISED RUN IS A NAME. "looks great" is not a
                    # company the reply asked for; "Foo Corp" is.
                    out["unknown"].append(said)
                run = []
    order = {c: i for i, c in enumerate(companies)}
    out["chosen"].sort(key=lambda c: order.get(c, 99))
    return out


def confirm_text(chosen: list, *, ambiguous=(), unknown=()) -> str:
    """What was understood and what was not, then the one question that must
    be answered before anything is written."""
    lines: list = []
    for said, fits in ambiguous or ():
        lines.append(f"\"{said}\" could be {_and(list(fits))}. Which one did you mean? "
                     "I haven't added anything for it.")
    for said in unknown or ():
        lines.append(f"I didn't recognise \"{said}\": it wasn't one of the companies in "
                     "my message, so I've left it.")
    if chosen:
        lines.append(f"I understood: add {_and(list(chosen))} to the Master Pipeline. "
                     "Shall I go ahead?")
    return "\n".join(lines)


WHICH_ONE = "Which would you like me to add: {names}? Please reply with the company's name."
NOTHING_UNDERSTOOD = ("I couldn't tell which company you meant. The ones in my message were "
                      "{names}. Please reply with the company's name.")


if __name__ == "__main__":
    import sys

    FAILED = 0

    def check(name, got, want):
        global FAILED
        ok = got == want
        FAILED += 0 if ok else 1
        print(("  ok    " if ok else "  FAIL  ") + name
              + ("" if ok else f"\n        got  {got!r}\n        want {want!r}"))

    idx = build_index(["Vapi.ai", "ElevenLabs"],
                      ["ElevenLabs", "Nudge - AI Commerce Discovery", "Google DeepMind", "Yellow.ai"])
    print("matching")
    check("the full name decides", classify("Eleven Labs Inc.", idx)["outcome"], KNOWN)
    check("Nudge Security is not Nudge - AI Commerce Discovery",
          (classify("Nudge Security", idx)["outcome"], classify("Nudge Security", idx)["similar"]),
          (NEW, []))
    check("Google is not Google DeepMind, and it is a question",
          (classify("Google", idx)["outcome"], classify("Google", idx)["similar"]),
          (NEW, ["Google DeepMind"]))
    check("on Outreach PoCs only", classify("Vapi", idx)["outcome"], POCS_ONLY)
    print("\nthe reply")
    two = ["Nudge Security", "Nudge Labs", "Mirage Labs"]
    check("a full name", read_reply("add Mirage Labs", two)["chosen"], ["Mirage Labs"])
    check("an ambiguous name is a question",
          read_reply("add Nudge", two)["ambiguous"], [("nudge", ["Nudge Security", "Nudge Labs"])])
    check("a name that was not there", read_reply("add Foo Corp", two)["unknown"], ["foo corp"])
    print("\nALL PASSED" if not FAILED else f"\n{FAILED} FAILED")
    sys.exit(1 if FAILED else 0)
