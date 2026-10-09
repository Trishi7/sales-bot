"""R11's OUTREACH PoCs CROSS-CHECK — who is already on the tab, and what the bot says about it.

THREE STATES for a company new to the Master Pipeline, decided from a small
summary of the PoCs tab (`build_index`):

    NEW       no row for it          look for people from scratch and offer to ADD ROWS
    GAPS      rows, and one lacks a  look up the blank cells of those rows and ASK A
              MANDATORY field        PERSON TO PASTE THEM IN. Gaps only: no new people
    MORE      rows, nothing          look for MORE people, leaving out everybody already
              mandatory blank        on the sheet, and offer to add them

WHY "GAPS" ONLY REMINDS. Company, Industry, Name, Designation, Email id, Based,
Research Paper Link and LI Url are columns B to I, and A:I is READ-ONLY on an
existing row (RESTRICTED_COLUMN_RANGES): the team's own record of who a person
is. A NEW row may be written there, an existing one may not.

THE SHEET CELL IS THE IDENTITY (9 Oct, the live run). The Master Pipeline said
"Underdog AI" and Outreach PoCs said "Underdog AI (Conway Research)"; compared
by equal keys they were two companies, so Sigil Wen, row 651, was offered as a
new row. Companies are now matched with `gtm_sheet`'s own `find_company`
(exact, then substring), people by `activation.row_key`, and the Master
Pipeline's spelling is carried unchanged through every message and every row.

WHAT A MESSAGE NEVER CARRIES: a field with no value ("->", "-", "n/a",
"unknown" are no value: `is_blank`), a link that is not masked, a Source that
repeats the LinkedIn link, a "designation" that is a fellowship or a seat
somewhere else, a row count.

THIS MODULE IS PURE: rows and found values in, a summary or text out.
"""
import logging
import re
from datetime import date

import activation
import config
import gtm_sheet
import links

log = logging.getLogger(__name__)

NEW = "a"            # no row on Outreach PoCs
GAPS = "b"           # rows with a blank mandatory field
MORE = "c"           # rows, nothing mandatory blank
BRANCH_A, BRANCH_B, COMPLETE = NEW, GAPS, MORE          # the names the first build used

FIELD_ROLES = ("industry", "name", "designation", "email", "based", "paper_links", "li_url")
_ROLE_ALIASES = {
    "research_paper_link": "paper_links", "research_paper": "paper_links",
    "paper": "paper_links", "paper_link": "paper_links",
    "linkedin": "li_url", "linkedin_url": "li_url", "li": "li_url",
    "title": "designation", "location": "based", "email_id": "email",
}
DEFAULT_MANDATORY = ("name", "designation", "li_url")

# -- the words ----------------------------------------------------------------
M1_HEADER = "New companies in the Master Pipeline — since {since}"
M1_SUFFIX = {NEW: "", GAPS: " — already on Outreach PoCs, missing some fields",
             MORE: " — already on Outreach PoCs"}
M1_ASK = "Would you like me to look these up and suggest prospective PoCs we could contact?"

ADD_HEADER = "Suggested PoCs"
MORE_HEADER = "More PoCs"
FILL_HEADER = "Missing fields"
NOTE_OMITTED = "Some fields are missing because I could not find them on the web."
ADD_CLOSE = "Shall I go ahead and add these new people to the Outreach PoCs sheet?"
FILL_CLOSE = "Those {companies} columns are ones I'm not able to write to, so could you add them please?"
FILL_EMAIL_OFFER = "I can fill the Email cell for you if you'd like — shall I?"
# ONE "try again" phrasing, in every failure case.
TRY_AGAIN = ("I can try again if you'd like — it would help if you could give me a starting point,\n"
             "a team page or their LinkedIn.")
NOBODY = "I'm sorry, I searched and could not find named people I would be confident suggesting."
M3_ADDED = "added to Outreach PoCs"

# Suggested PoCs: labels in order. Missing fields uses the sheet's own headers.
ADD_FIELDS = (("email", "Email"), ("based", "Based"), ("linkedin_url", "LinkedIn"),
              ("paper", "Paper"), ("source", "Source"))
FILL_ROLES = ("designation", "email", "based", "paper_links", "li_url")
_URL_FIELDS = ("linkedin_url", "paper", "source")
_INDENT = "   "
_LABEL_WIDTH = 12

# What is not a role AT THE COMPANY: a fellowship, an award, alumni status, a
# board or advisory seat. "Thiel Fellow" is where somebody has been, not what
# they do at Underdog AI. Omitting a title is always safe.
_NOT_A_ROLE_RE = re.compile(
    r"\b(fellow|fellowship|alumn\w*|award\w*|winner|laureate|scholar(ship)?|"
    r"board\s+(member|director|observer|advis\w+)|advis[oe]r\w*|advisory|investor|"
    r"angel|mentor|volunteer|student|graduate|ex|former(ly)?|previously)\b", re.IGNORECASE)
_ELSEWHERE_RE = re.compile(r"\s(?:at|@)\s+(.+)$", re.IGNORECASE)


def company_key(name) -> str:
    """The normalisation `db._norm_key` uses for `pipeline_companies.company_key`."""
    return gtm_sheet.normalise_header(gtm_sheet.clean_cell(name))


def is_blank(value) -> bool:
    """Is this "no value"? Empty, whitespace, None, a dash or an arrow ("-",
    "--", "->", "—"), or one of the words a sheet or a model writes for
    "nothing" (`gtm_sheet._UNKNOWN_WORDS`: n/a, unknown, not found, tbd …).
    ON 9 OCT "Based  ->" WAS POSTED: the value was the literal arrow from the
    prompt's own "or ->", and the omit rule only tested for empty."""
    norm = gtm_sheet.normalise_header(str(value if value is not None else ""))
    return not norm or norm in gtm_sheet._UNKNOWN_WORDS


def clean(value) -> str:
    """The value, or "" when it is no value."""
    return "" if is_blank(value) else " ".join(str(value).split())


def mandatory_roles() -> tuple:
    """POC_MANDATORY_FIELDS as sheet roles; the default when it is unusable."""
    out = []
    for raw in (getattr(config, "POC_MANDATORY_FIELDS", None) or ()):
        role = gtm_sheet.normalise_header(str(raw)).replace(" ", "_")
        role = _ROLE_ALIASES.get(role, role)
        if role in FIELD_ROLES:
            if role not in out:
                out.append(role)
        elif role:
            log.warning("[r11] POC_MANDATORY_FIELDS names %r, which is not one of the "
                        "Outreach PoCs fields (%s); ignored", raw, ", ".join(FIELD_ROLES))
    return tuple(out) or DEFAULT_MANDATORY


def row_gaps(row: dict, mandatory=None) -> list:
    """The mandatory roles this row leaves blank."""
    return [r for r in (mandatory or mandatory_roles()) if is_blank((row or {}).get(r))]


def finder_for(rows):
    """`tab.find_company` for a plain list of rows: the same matcher (exact
    normalised first, then substring), for callers and tests holding no Tab."""
    return gtm_sheet.Tab.find_company.__get__(type("_Rows", (), {"rows": list(rows or [])})())


def build_index(find_company, companies, mandatory=None) -> dict:
    """{company_key: {"rows", "gaps", "gap_rows"}} for the given Master
    Pipeline companies. `find_company` is the Outreach PoCs tab's own matcher
    (`tab.find_company`), so "Underdog AI" finds the rows filed under
    "Underdog AI (Conway Research)". THE ONLY THING R11 IS GIVEN ABOUT THAT
    TAB: counts and roles, never a row, a name or a cell."""
    mandatory = tuple(mandatory or mandatory_roles())
    index: dict = {}
    for company in companies or ():
        key = company_key(company)
        if not key or key in index:
            continue
        rows = list(find_company(gtm_sheet.clean_cell(company)) or [])
        gaps, gap_rows = [], 0
        for row in rows:
            missing = row_gaps(row, mandatory)
            gap_rows += 1 if missing else 0
            gaps += [r for r in missing if r not in gaps]
        index[key] = {"rows": len(rows), "gaps": gaps, "gap_rows": gap_rows}
    return index


def classify(company, index) -> str:
    """NEW, GAPS or MORE. No index, or no entry, is NEW: the write path
    re-reads the tab and drops anybody already on it."""
    entry = (index or {}).get(company_key(company))
    if not entry or not int(entry.get("rows") or 0):
        return NEW
    return GAPS if entry.get("gaps") else MORE


def collapse(entries) -> list:
    """New-company entries with duplicates folded, earliest first-seen kept."""
    order, seen = [], {}
    for entry in entries or ():
        name = gtm_sheet.clean_cell((entry or {}).get("company"))
        key = company_key(name)
        if not key:
            continue
        first = str((entry or {}).get("first_seen") or "")
        if key not in seen:
            seen[key] = {"company": name, "first_seen": first}
            order.append(key)
        elif first and (not seen[key]["first_seen"] or first < seen[key]["first_seen"]):
            seen[key]["first_seen"] = first
    return [seen[k] for k in order]


# -- people -------------------------------------------------------------------

def person_key(company, name) -> str:
    """`activation.row_key` for a person: normalised company | name. The
    company alone is never the identity (row_key's own docstring records the
    two contacts that mistake dropped)."""
    return activation.row_key({"company": company, "name": name})


def _name_key(name) -> str:
    return gtm_sheet.normalise_header(gtm_sheet.clean_cell(name))


def names_on_sheet(rows) -> list:
    """The people already on Outreach PoCs for one company, as the sheet
    spells them. NEVER CAPPED: it is a handful of names, and a slice here is
    how a known person would be offered again."""
    out = []
    for row in rows or ():
        name = gtm_sheet.clean_cell((row or {}).get("name"))
        if name and name not in out:
            out.append(name)
    return out


def is_excluded(name, excluded) -> bool:
    """Is this found person one of `excluded` (on the sheet, or suggested
    earlier)? By normalised name, and by one name's words all being in the
    other's, so "Sigil Wen" excludes "Sigil S. Wen". THE POST-FILTER: a search
    treats a negative term as a hint and a model sometimes returns an excluded
    person spelt differently, so this is what makes a duplicate impossible."""
    want = set(_name_key(name).split())
    if not want:
        return True
    for other in excluded or ():
        have = set(_name_key(other).split())
        if have and (want <= have or have <= want):
            return True
    return False


def role_at_company(title, company="") -> str:
    """The title, when it is a role at this company; "" otherwise."""
    title = clean(title)
    if not title or _NOT_A_ROLE_RE.search(title):
        return ""
    elsewhere = _ELSEWHERE_RE.search(title)
    if elsewhere:
        org, ours = company_key(elsewhere.group(1)), company_key(company)
        if org and ours and org not in ours and ours not in org \
                and not (set(org.split()) & (set(ours.split()) - {"ai", "labs", "inc", "the"})):
            return ""
        title = title[:elsewhere.start()].strip(" ,-—")
    return title


def url_key(url) -> str:
    """A url for comparing: no scheme, no www., no trailing slash, lowercase."""
    text = re.sub(r"^[a-z]+://", "", str(url or "").strip(), flags=re.IGNORECASE)
    return re.sub(r"^www\.", "", text, flags=re.IGNORECASE).rstrip("/").lower()


def bare(url) -> str:
    """"linkedin.com/in/sigil": the LABEL of a link, never the link itself."""
    text = re.sub(r"^[a-z]+://", "", str(url or "").strip(), flags=re.IGNORECASE)
    return re.sub(r"^www\.", "", text, flags=re.IGNORECASE).rstrip("/")


def masked(url) -> str:
    """`[linkedin.com/in/sigil](<https://www.linkedin.com/in/sigil>)`, through
    `links.link`: clickable, no embed. "" for anything that is not an absolute
    http(s) url — a host with no scheme is dead text and is never emitted."""
    url = str(url or "").strip()
    if not re.match(r"^https?://\S+$", url, re.IGNORECASE):
        return ""
    return links.link(bare(url), url)


def _day(d: date) -> str:
    return f"{d.strftime('%a')} {d.day} {d.strftime('%b')}"


def _field(label: str, value: str) -> str:
    """One aligned line. A label as long as the column ("Research Paper Link",
    "Based (Sept 2026)") still gets two spaces before its value."""
    pad = label.ljust(_LABEL_WIDTH) if len(label) < _LABEL_WIDTH - 1 else label + "  "
    return f"{_INDENT}{pad}{value}"


def _and(names) -> str:
    names = [n for n in names if n]
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


def _subheading(company: str, industry: str = "") -> str:
    """"Underdog AI (Conway Research) — AI Labs". Industry is the Master
    Pipeline's own cell and is the same for everybody at the company, so it
    sits here and not on each person; a blank cell drops it."""
    industry = clean(industry)
    return company + (f" — {industry}" if industry else "")


# -- M1 -----------------------------------------------------------------------

def m1_lines(companies, *, since: date) -> list:
    """M1 as lines, `companies` being [(name, state)]. All three states are
    listed and the one question covers them. [] when there is nobody to name."""
    named = [(str(n).strip(), s) for n, s in (companies or ()) if str(n).strip() and s in M1_SUFFIX]
    if not named:
        return []
    lines = [M1_HEADER.format(since=_day(since)), ""]
    for i, (name, state) in enumerate(named, 1):
        lines.append(f"{i}. {name}{M1_SUFFIX[state]}")
    return lines + ["", M1_ASK]


# -- the reply to a yes ---------------------------------------------------------

def person_lines(person: dict, number: int = 0) -> tuple:
    """(lines, omitted) for one suggested person."""
    title = clean(person.get("title"))
    head = clean(person.get("name")) + (f" — {title}" if title else "")
    lines = [f"{number}. {head}" if number else head]
    omitted = not title
    shown: set = set()
    for key, label in ADD_FIELDS:
        raw = clean(person.get(key))
        if key in _URL_FIELDS:
            value = masked(raw)
            if key == "source" and url_key(raw) in shown:
                continue                # a source that repeats a field says nothing
            if value:
                shown.add(url_key(raw))
        else:
            value = raw
        if value:
            lines.append(_field(label, value))
        elif key != "source":
            omitted = True
    return lines, omitted


def _already(names, found: int, *, left_out: bool = False) -> str:
    names = [n for n in names if n]
    if not names:
        return ""
    are = "is" if len(names) == 1 else "are"
    if not found:
        return f"{_and(names)} {are} already on the sheet, and I could not find anyone else."
    if left_out:
        return f"Already on the sheet, so I left them out: {', '.join(names)}."
    count = {1: "One other", 2: "Two others", 3: "Three others"}.get(found, f"{found} others")
    return f"{_and(names)} {are} already on the sheet. {count} I found:"


def render_found(*, add=(), fill=(), title: str = ADD_HEADER, email_offer: bool = False,
                 left_out: bool = False) -> dict:
    """The ONE message after a yes (or after "find someone else").

    `add`  [{"company", "industry", "people": [...], "already": [names]}] —
           companies to find people for: with no row yet, or complete and
           wanting more. `already` are the people left out as known.
    `fill` [{"company", "industry", "rows": [{"name", "title", "found":
           [(label, value)], "source", "unfound": n}]}] — existing rows.

    Two sections, each headed only when it has something in it; one note for
    every omitted field; then the closing lines. THE ADD LINE NEVER COUNTS ROWS
    OR NAMES A COMPANY. Returns {"text", "people": n, "kind"}; kind is "none"
    when nothing at all was found.
    """
    add = [dict(a) for a in (add or ())]
    fill = [dict(f) for f in (fill or ())]
    people_total = sum(len(a.get("people") or ()) for a in add)
    fill_found = any(r.get("found") for f in fill for r in (f.get("rows") or ()))

    # NOTHING FOUND FOR ANYBODY, and nothing else to say: the short form.
    if not people_total and not fill_found and not any(a.get("already") for a in add) and not fill:
        names = [a["company"] for a in add]
        return {"kind": "none", "people": 0,
                "text": f"No PoCs found — {_and(names)}\n\n{NOBODY}\n{TRY_AGAIN}"}

    omitted = False
    single = (len(add) + len(fill)) == 1
    parts: list = []
    if add:
        if single:
            parts.append(f"{title} — {add[0]['company']}")
        else:
            parts.append(title)
        for a in add:
            block: list = []
            if not single:
                block += [_subheading(a["company"], a.get("industry")), ""]
            people = list(a.get("people") or ())
            intro = _already(a.get("already") or (), len(people), left_out=left_out)
            if intro:
                block += [intro, ""] if people else [intro, TRY_AGAIN]
            elif not people:
                block += [NOBODY, TRY_AGAIN]
            for i, person in enumerate(people, 1):
                lines, miss = person_lines(person, i)
                omitted = omitted or miss
                block += lines + [""]
            parts.append("\n".join(block).strip("\n"))
    fill_companies = []
    if fill:
        if single:
            parts.append(f"{FILL_HEADER} — {fill[0]['company']}")
        else:
            parts.append(FILL_HEADER)
        for f in fill:
            block = [] if single else [_subheading(f["company"], f.get("industry")), ""]
            unfound_names = []
            for row in f.get("rows") or ():
                found = [(str(l), clean(v)) for l, v in (row.get("found") or ()) if clean(v)]
                if int(row.get("unfound") or 0):
                    omitted = omitted or bool(found)
                if not found:
                    unfound_names.append(clean(row.get("name")))
                    continue
                title_ = clean(row.get("title"))
                block.append(clean(row.get("name")) + (f" — {title_}" if title_ else ""))
                shown = set()
                for label, value in found:
                    is_url = bool(re.match(r"^https?://", value, re.IGNORECASE))
                    block.append(_field(label, masked(value) if is_url else value))
                    if is_url:
                        shown.add(url_key(value))
                source = clean(row.get("source"))
                if masked(source) and url_key(source) not in shown:
                    block.append(_field("Source", masked(source)))
                block.append("")
                if f["company"] not in fill_companies:
                    fill_companies.append(f["company"])
            if unfound_names:
                block += [f"I could not find the missing details for {_and(unfound_names)} on the web.",
                          TRY_AGAIN]
            parts.append("\n".join(block).strip("\n"))
    if omitted:
        parts.append(NOTE_OMITTED)
    closes = []
    if people_total:
        closes.append(ADD_CLOSE)
    if fill_companies:
        closes.append(FILL_EMAIL_OFFER if email_offer
                      else FILL_CLOSE.format(companies=_and(fill_companies)))
    if closes:
        parts.append("\n".join(closes))
    return {"kind": "found", "people": people_total, "text": "\n\n".join(p for p in parts if p)}


# -- M3 -----------------------------------------------------------------------

def render_written(results) -> str:
    """M3: one block a company; under it only what is NOT what was approved."""
    parts = []
    for res in results or ():
        company = str(res.get("company") or "").strip()
        skipped = [str(s).strip() for s in (res.get("skipped") or ()) if str(s).strip()]
        block = [f"Done — {company}", M3_ADDED] if int(res.get("added") or 0) \
            else [f"Not added — {company}"]
        parts.append("\n".join(block + skipped))
    return "\n\n".join(parts)


def _self_test() -> int:
    """`python -m poc_crosscheck` — the matcher, the states and the shapes."""
    failures = 0

    def check(name, got, want=True):
        nonlocal failures
        ok = got == want
        failures += 0 if ok else 1
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f": got {got!r}, want {want!r}"))

    config.POC_MANDATORY_FIELDS = ["name", "designation", "li_url"]
    rows = [{"company": "Underdog AI (Conway Research)", "name": "Sigil Wen", "designation": "CEO", "li_url": "x"},
            {"company": "Underdog AI (Conway Research)", "name": "Daniel Hong", "designation": "MTS", "li_url": "y"},
            {"company": "Limbic AI", "name": "Ross Harper", "designation": "CEO", "li_url": ""}]
    index = build_index(finder_for(rows), ["Underdog AI", "Limbic AI", "Oogam AI", "underdog ai "])
    print("matching and the three states")
    check("'Underdog AI' finds the rows filed under 'Underdog AI (Conway Research)'",
          index["underdog ai"], {"rows": 2, "gaps": [], "gap_rows": 0})
    check("no row: NEW; a mandatory blank: GAPS; complete: MORE",
          [classify(c, index) for c in ("Oogam AI", "Limbic AI", "Underdog AI")], [NEW, GAPS, MORE])
    check("the people on the sheet, never capped", names_on_sheet(rows[:2]), ["Sigil Wen", "Daniel Hong"])
    check("a known person is excluded, whatever the spelling",
          [is_excluded(n, ["Sigil Wen", "Daniel Hong"]) for n in ("sigil wen", "Sigil S. Wen", "Ana Pereira")],
          [True, True, False])
    check("a person is company|name, never the company alone",
          person_key("Underdog AI", "Sigil Wen") != person_key("Underdog AI", "Daniel Hong"), True)

    print("\nno value")
    for v in ("", "  ", None, "none", "NULL", "-", "--", "->", "—", "n/a", "N/A", "na", "nil", "unknown",
              "Not Found", "not available", "tbd", "TBA"):
        check(f"is_blank({v!r})", is_blank(v), True)
    check("a real value is not blank", [is_blank(v) for v in ("Pune", "AI Labs", "0")], [False] * 3)
    check("a fellowship, an award or a seat elsewhere is not a role here",
          [role_at_company(t, "Underdog AI") for t in ("Thiel Fellow", "Forbes 30 Under 30 winner", "Advisor",
                                                       "Board Member at OtherCo", "CTO at Globex", "->")], [""] * 6)
    check("a role here is kept",
          [role_at_company(t, "Underdog AI") for t in ("Founder & CEO", "Research Engineer at Underdog AI")],
          ["Founder & CEO", "Research Engineer"])

    print("\nM1")
    check("three states, one question", "\n".join(m1_lines(
        [("Oogam AI", NEW), ("Limbic AI", GAPS), ("Underdog AI (Conway Research)", MORE)], since=date(2026, 10, 7))),
        "New companies in the Master Pipeline — since Wed 7 Oct\n\n1. Oogam AI\n"
        "2. Limbic AI — already on Outreach PoCs, missing some fields\n"
        "3. Underdog AI (Conway Research) — already on Outreach PoCs\n\n" + M1_ASK)

    print("\nthe reply")
    priya = {"name": "Priya Raghavan", "title": "VP Research", "email": "priya@oogam.ai", "based": "Bengaluru, India",
             "linkedin_url": "https://www.linkedin.com/in/priyaraghavan", "source": "https://oogam.ai/team"}
    ana = {"name": "Ana Pereira", "title": "Research Engineer", "based": "->",
           "linkedin_url": "https://www.linkedin.com/in/anapereira", "source": "https://www.linkedin.com/in/anapereira/"}
    out = render_found(
        add=[{"company": "Oogam AI", "industry": "Voice AI", "people": [priya]},
             {"company": "Underdog AI (Conway Research)", "industry": "AI Labs", "people": [ana],
              "already": ["Sigil Wen", "Daniel Hong"]}],
        fill=[{"company": "Limbic AI", "industry": "Mental Health AI", "rows": [
            {"name": "Ross Harper", "title": "Co-founder & CEO", "unfound": 0, "source": "https://limbic.ai/team",
             "found": [("LI Url", "https://www.linkedin.com/in/rossgharper")]}]}])
    text = out["text"]
    check("two sections, industry on the subheadings, the two closing lines last", (
        [l for l in text.splitlines() if l in ("Suggested PoCs", "Missing fields", "Oogam AI — Voice AI",
                                                "Underdog AI (Conway Research) — AI Labs",
                                                "Limbic AI — Mental Health AI")],
        text.splitlines()[-2:]),
        (["Suggested PoCs", "Oogam AI — Voice AI", "Underdog AI (Conway Research) — AI Labs", "Missing fields",
          "Limbic AI — Mental Health AI"],
         [ADD_CLOSE, "Those Limbic AI columns are ones I'm not able to write to, so could you add them please?"]))
    check("the known people are named, then the new one",
          "Sigil Wen and Daniel Hong are already on the sheet. One other I found:" in text, True)
    check("'->' prints nothing; a Source equal to the LinkedIn link is dropped; links are masked",
          text.split("1. Ana Pereira — Research Engineer\n")[1].split("\n\n")[0],
          "   LinkedIn    [linkedin.com/in/anapereira](<https://www.linkedin.com/in/anapereira>)")
    check("no Industry line on a person, no row count, no dead link",
          [x for x in ("   Industry", "This adds", "rows for") if x in text]
          + re.findall(r"(?<![\[(<./\w])(?:linkedin\.com|oogam\.ai)/\S+", text), [])
    check("the note appears exactly once", text.count(NOTE_OMITTED), 1)
    full = dict(priya, paper="https://oogam.ai/paper")
    one = render_found(add=[{"company": "Oogam AI", "industry": "Voice AI", "people": [full]}])["text"]
    check("one company, one section: the one-line header, no subheading, no note when all was found",
          (one.splitlines()[0], "Oogam AI — Voice AI" in one, NOTE_OMITTED in one, one.splitlines()[-1]),
          ("Suggested PoCs — Oogam AI", False, False, ADD_CLOSE))
    nobody = render_found(add=[{"company": "Limbic AI", "people": []}])
    check("nothing found: the short form with the one try-again close",
          (nobody["kind"], nobody["text"]), ("none", f"No PoCs found — Limbic AI\n\n{NOBODY}\n{TRY_AGAIN}"))
    known = render_found(add=[{"company": "Underdog AI (Conway Research)", "industry": "AI Labs", "people": [],
                               "already": ["Sigil Wen", "Daniel Hong"]},
                              {"company": "Oogam AI", "people": [priya]}])["text"]
    check("everybody found is already known: said, with the same close",
          "Sigil Wen and Daniel Hong are already on the sheet, and I could not find anyone else.\n" + TRY_AGAIN
          in known, True)
    gaps = render_found(fill=[{"company": "Limbic AI", "industry": "Mental Health AI",
                               "rows": [{"name": "Ross Harper", "found": [], "unfound": 2}]}])["text"]
    check("no details found for an existing row: said, with the same close, and no closing request",
          gaps, "Missing fields — Limbic AI\n\nI could not find the missing details for Ross Harper on the web.\n"
                + TRY_AGAIN)
    more = render_found(add=[{"company": "Underdog AI (Conway Research)", "industry": "AI Labs", "people": [ana],
                              "already": ["Sigil Wen", "Daniel Hong"]}], title=MORE_HEADER, left_out=True)["text"]
    check("the follow-up shape", more.splitlines()[:3],
          ["More PoCs — Underdog AI (Conway Research)", "",
           "Already on the sheet, so I left them out: Sigil Wen, Daniel Hong."])

    print("\nM3")
    check("one block a company", render_written([{"company": "Oogam AI", "added": 2}]),
          "Done — Oogam AI\nadded to Outreach PoCs")

    print(f"\n{'ALL PASSED' if not failures else str(failures) + ' FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_self_test())
