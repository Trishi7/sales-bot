"""R11's OUTREACH PoCs CROSS-CHECK — who is already on the tab, and what the bot says about it.

WHAT WAS MISSING (9 Oct 2026). R11 named every company new to the Master
Pipeline and offered to look for people, without ever looking at Outreach PoCs:
a company with five contacts already on the tab was treated exactly like one
with none. And when an approver said yes, the people it found were only shown;
nothing could add them.

THREE BRANCHES, decided from a small summary of the PoCs tab (`build_index`):

    A         no row for the company. Find people and offer to ADD ROWS.
    B         rows exist and at least one lacks a MANDATORY field
              (POC_MANDATORY_FIELDS: name, designation, li_url). Show what a
              search finds for the blank cells and ASK A PERSON TO PASTE IT IN.
    complete  rows exist and none lacks a mandatory field. Named in no message.

WHY BRANCH B ONLY REMINDS. Company, Industry, Name, Designation, Email id,
Based, Research Paper Link and LI Url are columns B to I, and A:I is READ-ONLY
on an existing row (RESTRICTED_COLUMN_RANGES): that band is the team's own
record of who a person is. A new row may be written there
(NEW_ROW_WRITABLE_RANGES), an existing one may not. The one exception is the
Email cell, through an `email_write` proposal, only while it is still blank and
only when EMAIL_WRITE_ALLOWED is true. So Branch B shows, and a person writes.

A BLANK OPTIONAL FIELD IS NOT A GAP. Most people on the tab legitimately have
no email and no paper; counting those would put nearly every company in Branch
B for ever.

THIS MODULE IS PURE. It is handed rows and found values and returns a summary
or text. It reads no sheet, runs no search and writes nothing; `bot.py` does
those, behind the two approvals. The message shapes were agreed with Vaishnavi
and Kushal on 8 Oct: plain text, no bold, no emoji, a blank line between
blocks, asking and never instructing.
"""
import logging
import re
from datetime import date
from typing import Optional

import config
import gtm_sheet

log = logging.getLogger(__name__)

BRANCH_A = "a"
BRANCH_B = "b"
COMPLETE = "complete"

# The seven fields this feature is about, by the sheet's role names, in the
# order the tab holds them (columns C to I, with Name at D).
FIELD_ROLES = ("industry", "name", "designation", "email", "based", "paper_links", "li_url")
# Other ways to say a role in POC_MANDATORY_FIELDS. The role map itself lives in
# gtm_sheet; these are only spellings a person might type in .env.
_ROLE_ALIASES = {
    "research_paper_link": "paper_links", "research_paper": "paper_links",
    "paper": "paper_links", "paper_link": "paper_links",
    "linkedin": "li_url", "linkedin_url": "li_url", "li": "li_url",
    "title": "designation", "location": "based", "email_id": "email",
}
DEFAULT_MANDATORY = ("name", "designation", "li_url")

# -- the words ----------------------------------------------------------------
M1_HEADER = "New companies in the Master Pipeline — since {since}"
M1_SUFFIX_B = " — already on Outreach PoCs, missing some fields"
M1_ASK = "Would you like me to look these up and suggest prospective PoCs we could contact?"

M2_HEADER = "Suggested PoCs"
M2B_HEADER = "Missing fields"
NOTE_OMITTED = "Some fields are missing because I could not find them on the web."
M2B_REQUEST = "Those columns are ones I'm not able to write to, so could you add them please?"
M2B_EMAIL_OFFER = "I can fill the Email cell for you if you'd like — shall I?"
M4_BODY = ("I'm sorry, I searched and could not find named people I would be confident "
           "suggesting.\nHappy to try again with a starting point — a careers page or the "
           "LinkedIn company URL would be enough.")
M3_ADDED = "added to Outreach PoCs"

# M2's labels, in order. The value column starts 12 characters after the indent.
SUGGEST_FIELDS = (("industry", "Industry"), ("email", "Email"), ("based", "Based"),
                  ("linkedin_url", "LinkedIn"), ("paper", "Paper"), ("source", "Source"))
# M2b's order: the sheet's own column order, then where it was found.
FILL_ROLES = ("industry", "designation", "email", "based", "paper_links", "li_url")
_INDENT = "   "
_LABEL_WIDTH = 12


def company_key(name) -> str:
    """One company, one key: the same normalisation `db._norm_key` uses for
    `pipeline_companies.company_key`, so the Master Pipeline's three spellings
    of one company and the PoCs tab's fourth all meet here."""
    return gtm_sheet.normalise_header(gtm_sheet.clean_cell(name))


def mandatory_roles() -> tuple:
    """POC_MANDATORY_FIELDS as sheet roles. An entry that is not one of the
    seven fields is dropped and logged; an empty or unusable setting falls
    back to the default rather than making nothing mandatory (which would
    call every company complete)."""
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


def _blank(row: dict, role: str) -> bool:
    return not gtm_sheet.clean_cell((row or {}).get(role))


def row_gaps(row: dict, mandatory=None) -> list:
    """The mandatory roles this row leaves blank."""
    return [r for r in (mandatory or mandatory_roles()) if _blank(row, r)]


def build_index(rows, mandatory=None) -> dict:
    """{company_key: {"rows": n, "gaps": [role, ...], "gap_rows": k}} for the
    whole Outreach PoCs tab. THE ONLY THING R11 IS GIVEN ABOUT THAT TAB: a
    count and which mandatory fields are blank somewhere, never a row, a name
    or a cell. Built by the caller, so the engine stays pure and a preview of
    the queue reads nothing it was not handed."""
    mandatory = tuple(mandatory or mandatory_roles())
    index: dict = {}
    for row in rows or ():
        key = company_key((row or {}).get("company"))
        if not key:
            continue
        entry = index.setdefault(key, {"rows": 0, "gaps": [], "gap_rows": 0})
        entry["rows"] += 1
        gaps = row_gaps(row, mandatory)
        if gaps:
            entry["gap_rows"] += 1
            for role in gaps:
                if role not in entry["gaps"]:
                    entry["gaps"].append(role)
    return index


def classify(company, index) -> str:
    """BRANCH_A, BRANCH_B or COMPLETE for one company against the index.

    NO INDEX AT ALL (None: the tab could not be summarised) IS BRANCH A, said
    in the log by the caller. The write path re-reads the tab and refuses a
    duplicate, so the worst case is an offer the sheet then declines; calling
    everything complete would silently drop the week's companies instead.
    """
    entry = (index or {}).get(company_key(company))
    if not entry or not int(entry.get("rows") or 0):
        return BRANCH_A
    return BRANCH_B if entry.get("gaps") else COMPLETE


def collapse(entries) -> list:
    """The new-company entries with duplicates folded: several Master Pipeline
    rows naming one company become one, keeping the first spelling and the
    EARLIEST first-seen date (so a re-typed duplicate cannot restart a
    company's week)."""
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


# -- showing a link the way the messages do -----------------------------------

def bare(url) -> str:
    """"linkedin.com/in/sigil" from "https://www.linkedin.com/in/sigil/": the
    address as a person would read it out. Nothing is added or completed."""
    text = str(url or "").strip()
    text = re.sub(r"^[a-z]+://", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^www\.", "", text, flags=re.IGNORECASE)
    return text.rstrip("/")


def _day(d: date) -> str:
    return f"{d.strftime('%a')} {d.day} {d.strftime('%b')}"


def _field(label: str, value: str) -> str:
    return f"{_INDENT}{label:<{_LABEL_WIDTH}}{value}"


def _and(names: list) -> str:
    names = [n for n in names if n]
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


# -- M1 -------------------------------------------------------------------------

def m1_lines(companies, *, since: date) -> list:
    """M1 as lines. `companies` is [(name, branch)] in the order they go.

        New companies in the Master Pipeline — since Wed 1 Oct

        1. Underdog AI (Conway Research)
        2. Limbic AI — already on Outreach PoCs, missing some fields

        Would you like me to look these up and suggest prospective PoCs we could contact?

    Company names only, numbered. Branch B says THAT fields are missing, never
    which. [] when there is nobody to name: nothing in scope posts nothing.
    """
    named = [(str(n).strip(), b) for n, b in (companies or ()) if str(n).strip()
             and b in (BRANCH_A, BRANCH_B)]
    if not named:
        return []
    lines = [M1_HEADER.format(since=_day(since)), ""]
    for i, (name, branch) in enumerate(named, 1):
        lines.append(f"{i}. {name}" + (M1_SUFFIX_B if branch == BRANCH_B else ""))
    return lines + ["", M1_ASK]


# -- M2 / M2b / M4, one message -------------------------------------------------

def _person_head(person: dict) -> str:
    name = str(person.get("name") or "").strip()
    title = str(person.get("title") or person.get("designation") or "").strip()
    return name + (f" — {title}" if title else "")


def suggestion_block(people) -> tuple:
    """(lines, omitted) for one company's suggested people, numbered. A field
    with no value prints NOTHING; `omitted` says whether any was left out."""
    lines, omitted = [], False
    for i, person in enumerate(people or (), 1):
        if lines:
            lines.append("")
        lines.append(f"{i}. {_person_head(person)}")
        for key, label in SUGGEST_FIELDS:
            value = str(person.get(key) or "").strip()
            if key in ("linkedin_url", "paper", "source"):
                value = bare(value)
            if value:
                lines.append(_field(label, value))
            elif key != "source":
                omitted = True
    return lines, omitted


def fill_block(rows) -> tuple:
    """(lines, omitted) for one Branch B company. `rows` is
    [{"name", "title", "found": [(label, value)], "source", "unfound": n}]:
    existing rows, so NOT numbered, each showing only cells that are blank on
    the sheet and for which something was found, under the sheet's own column
    names."""
    lines, omitted = [], False
    for row in rows or ():
        found = [(str(l), str(v).strip()) for l, v in (row.get("found") or ()) if str(v).strip()]
        if int(row.get("unfound") or 0):
            omitted = True
        if not found:
            continue
        if lines:
            lines.append("")
        lines.append(_person_head(row))
        for label, value in found:
            lines.append(_field(label, bare(value) if "://" in value else value))
        source = bare(row.get("source"))
        if source:
            lines.append(_field("Source", source))
    return lines, omitted


def render_found(*, suggest=(), fill=(), failed=(), email_offer: bool = False) -> dict:
    """The one message after the first yes.

    `suggest` is [(company, [person, ...])] (Branch A, people found),
    `fill` is [(company, [row, ...])] (Branch B), `failed` the companies the
    search found nobody for. Returns {"text", "rows", "companies", "kind"}:
    `rows` is how many rows a yes would add, `kind` is "none" when nothing at
    all was found (M4), else "found".

    ORDER: M2's sections, then M2b's, then the ONE note covering every
    omission, then a line for each company the search failed on, then the
    closing lines. One message, never two.
    """
    suggest = [(c, list(p)) for c, p in (suggest or ()) if p]
    fill_blocks = []
    omitted = False
    for company, rows in (fill or ()):
        lines, miss = fill_block(rows)
        omitted = omitted or miss
        if lines:
            fill_blocks.append((company, lines))
    failed = [str(c).strip() for c in (failed or ()) if str(c).strip()]

    if not suggest and not fill_blocks:
        names = failed or [c for c, _r in (fill or ())]
        return {"kind": "none", "rows": 0, "companies": [],
                "text": f"No PoCs found — {_and(names)}\n\n{M4_BODY}"}

    parts: list = []
    if suggest:
        if len(suggest) == 1:
            company, people = suggest[0]
            block, miss = suggestion_block(people)
            omitted = omitted or miss
            parts.append("\n".join([f"{M2_HEADER} — {company}", ""] + block))
        else:
            parts.append(M2_HEADER)
            for company, people in suggest:
                block, miss = suggestion_block(people)
                omitted = omitted or miss
                parts.append("\n".join([company, ""] + block))
    if fill_blocks:
        parts.append(M2B_HEADER)
        for company, lines in fill_blocks:
            parts.append("\n".join([company, ""] + lines))
    if omitted:
        parts.append(NOTE_OMITTED)
    for company in failed:
        parts.append(f"I could not find PoCs for {company}. Happy to try again with a "
                     "careers page\nor the LinkedIn company URL.")
    rows = sum(len(p) for _c, p in suggest)
    if suggest:
        parts.append(f"Shall I go ahead? This adds {rows} row{'s' if rows != 1 else ''} "
                     f"for {_and([c for c, _p in suggest])}.")
    if fill_blocks:
        parts.append(M2B_EMAIL_OFFER if email_offer else M2B_REQUEST)
    return {"kind": "found", "rows": rows, "companies": [c for c, _p in suggest],
            "text": "\n\n".join(parts)}


# -- M3 -------------------------------------------------------------------------

def render_written(results) -> str:
    """M3. `results` is [{"company", "added": n, "skipped": [text, ...]}], one
    per company, in one message. No row numbers, no links, no recap of fields:
    only what a person needs to know that is NOT what they approved."""
    parts = []
    for res in results or ():
        company = str(res.get("company") or "").strip()
        added = int(res.get("added") or 0)
        skipped = [str(s).strip() for s in (res.get("skipped") or ()) if str(s).strip()]
        if added:
            block = [f"Done — {company}", M3_ADDED]
        else:
            block = [f"Not added — {company}"]
        block += skipped
        parts.append("\n".join(block))
    return "\n\n".join(parts)


def _self_test() -> int:
    """`python -m poc_crosscheck` — the index, the branches and the shapes."""
    failures = 0

    def check(name, got, want=True):
        nonlocal failures
        ok = got == want
        failures += 0 if ok else 1
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f": got {got!r}, want {want!r}"))

    config.POC_MANDATORY_FIELDS = ["name", "designation", "li_url"]
    full = {"company": "Acme AI", "name": "Ann Lee", "designation": "CTO",
            "li_url": "https://www.linkedin.com/in/ann"}
    rows = [full, dict(full, name="Bo Chen", li_url=""), {"company": " acme  ai ", "name": "Cy", "designation": "x",
                                                         "li_url": "y"},
            {"company": "Borealis", "name": "Dee", "designation": "CEO", "li_url": "z", "email": "", "paper_links": ""}]
    index = build_index(rows)
    print("the index and the branches")
    check("one key a company, whatever the spelling", sorted(index), ["acme ai", "borealis"])
    check("it carries counts and roles, never a row", index["acme ai"], {"rows": 3, "gaps": ["li_url"], "gap_rows": 1})
    check("no row at all: Branch A", classify("Cinder Labs", index), BRANCH_A)
    check("a mandatory field blank on one row: Branch B", classify("ACME AI", index), BRANCH_B)
    check("only optional fields blank: complete", classify("Borealis", index), COMPLETE)
    check("no index at all: Branch A", classify("Acme AI", None), BRANCH_A)
    config.POC_MANDATORY_FIELDS = ["name", "research_paper_link", "nonsense"]
    check("the setting takes aliases and drops what is not a field", mandatory_roles(), ("name", "paper_links"))
    config.POC_MANDATORY_FIELDS = []
    check("an empty setting falls back to the default", mandatory_roles(), DEFAULT_MANDATORY)
    check("duplicates collapse, earliest date kept",
          collapse([{"company": "Acme AI", "first_seen": "2026-10-06"}, {"company": "acme ai ", "first_seen": "2026-10-02"},
                    {"company": "Borealis", "first_seen": "2026-10-05"}]),
          [{"company": "Acme AI", "first_seen": "2026-10-02"}, {"company": "Borealis", "first_seen": "2026-10-05"}])

    print("\nM1")
    check("the agreed shape", "\n".join(m1_lines(
        [("Underdog AI (Conway Research)", BRANCH_A), ("Limbic AI", BRANCH_B)], since=date(2026, 10, 1))),
        "New companies in the Master Pipeline — since Thu 1 Oct\n\n"
        "1. Underdog AI (Conway Research)\n"
        "2. Limbic AI — already on Outreach PoCs, missing some fields\n\n"
        "Would you like me to look these up and suggest prospective PoCs we could contact?")
    check("nothing in scope: no lines at all", m1_lines([("X", COMPLETE)], since=date(2026, 10, 1)), [])

    print("\nM2, M2b, M4")
    sigil = {"name": "Sigil Wen", "title": "Founder & CEO", "industry": "AI Labs", "email": "sigil@underdog.ai",
             "based": "San Francisco, USA", "linkedin_url": "https://www.linkedin.com/in/sigil/",
             "source": "https://underdog.ai/team"}
    daniel = {"name": "Daniel Hong", "title": "Founding Team", "industry": "AI Labs", "based": "Seoul, South Korea",
              "linkedin_url": "https://linkedin.com/in/unifiedh", "paper": "https://unifiedh.com",
              "source": "https://linkedin.com/company/underdog-ai/people"}
    one = render_found(suggest=[("Underdog AI", [dict(sigil, paper="https://x.example/p")])])
    check("one company, everything found: one-line header, no note",
          (one["text"].splitlines()[0], NOTE_OMITTED in one["text"], one["rows"]),
          ("Suggested PoCs — Underdog AI", False, 1))
    two = render_found(suggest=[("Underdog AI (Conway Research)", [sigil, daniel]),
                                ("Limbic AI", [{"name": "Ross Harper", "title": "Co-founder & CEO",
                                                "source": "https://limbic.ai/team"}])])
    text = two["text"]
    check("two companies: stacked, the note once, the count in the closing line",
          (text.splitlines()[0], text.count(NOTE_OMITTED),
           text.endswith("Shall I go ahead? This adds 3 rows for Underdog AI (Conway Research) and Limbic AI.")),
          ("Suggested PoCs", 1, True))
    check("an omitted field prints nothing", [l for l in text.splitlines() if re.search(r"not found|n/a|\s{2}$", l)], [])
    check("labels in order, values aligned, links bare",
          text.split("\n\n")[2].splitlines(),
          ["1. Sigil Wen — Founder & CEO", "   Industry    AI Labs", "   Email       sigil@underdog.ai",
           "   Based       San Francisco, USA", "   LinkedIn    linkedin.com/in/sigil", "   Source      underdog.ai/team"])
    fill = [("VoiceCare AI", [{"name": "Anil Keshav", "title": "Head of Clinical AI",
                               "found": [("Email id", "anil@voicecare.ai"), ("LI Url", "https://linkedin.com/in/anilkeshav")],
                               "source": "https://voicecare.ai/about", "unfound": 0},
                              {"name": "Mara Ellis", "title": "Research Lead", "found": [], "unfound": 1}])]
    b = render_found(fill=fill)["text"]
    check("Branch B: not numbered, the sheet's own labels, a request and no offer",
          b, "Missing fields\n\nVoiceCare AI\n\nAnil Keshav — Head of Clinical AI\n   Email id    anil@voicecare.ai\n"
             "   LI Url      linkedin.com/in/anilkeshav\n   Source      voicecare.ai/about\n\n"
             + NOTE_OMITTED + "\n\n" + M2B_REQUEST)
    both = render_found(suggest=[("Underdog AI", [sigil])], fill=fill, failed=["Limbic AI"])["text"]
    order = [both.index(x) for x in ("Suggested PoCs — Underdog AI", "Missing fields", NOTE_OMITTED,
                                    "I could not find PoCs for Limbic AI", "Shall I go ahead?", M2B_REQUEST)]
    check("both in one message, in the agreed order", order, sorted(order))
    check("the email offer replaces the request only when asked for",
          render_found(fill=fill, email_offer=True)["text"].endswith(M2B_EMAIL_OFFER), True)
    none = render_found(failed=["Limbic AI"])
    check("nothing found for anybody: M4", (none["kind"], none["text"]),
          ("none", "No PoCs found — Limbic AI\n\n" + M4_BODY))
    check("no bold, no emoji, no 'Hey team'",
          [x for x in ("**", "Hey team", "•") if x in text + b + both + none["text"]], [])

    print("\nM3")
    check("one per company, nothing else",
          render_written([{"company": "Underdog AI (Conway Research)", "added": 2},
                          {"company": "Limbic AI", "added": 1, "skipped": ["Skipped Ross Harper: already on the tab."]}]),
          "Done — Underdog AI (Conway Research)\nadded to Outreach PoCs\n\n"
          "Done — Limbic AI\nadded to Outreach PoCs\nSkipped Ross Harper: already on the tab.")

    print(f"\n{'ALL PASSED' if not failures else str(failures) + ' FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_self_test())
