"""SHEET QUESTIONS, ANSWERED FROM THE SHEET'S OWN HEADERS (NFT2-1095). PURE:
parsed tabs in, dicts out. No sheet read, no Discord, no network, no model.

    python -m sheetquery          # the self-test

WHY THIS EXISTS. On 15 Oct somebody asked for the PoCs whose Next Steps is
"Research the PoC" and who are based in the UK or the USA. The answer was on
the sheet (20 rows, 8 of them in the UK or the USA). Saley said it could not
filter by that value. It had eight fixed questions (`query_tracker`'s
filters) and no way to ask a ninth, and the rows it saw were keyed by code
role names: `next_steps` is the Notes/Remarks column and `outreach_step` is
the Next Steps dropdown, so nobody reading a row could tell which was which.

THREE JOBS.

  WHAT IS ON A TAB (`columns`). Every header in column order, with its
    letter and its role, and for a column with 30 or fewer distinct values (a
    dropdown) the top 20 values with counts. "research the poc" becomes
    column "Next Steps", value "Research the PoC" by reading this, not by
    guessing.

  WHICH ROWS (`query`). A list of conditions that must all hold, each
    {column, op, value}. The column is named by its HEADER as the sheet
    shows it today, or by its role; it is looked up by text at call time,
    so a column the team moved is still found. A name that fits two columns
    is never guessed: both come back with a question.

  WHAT A ROW LOOKS LIKE (`row_out`). Keyed by the sheet header, with the
    role in brackets: "Next Steps (outreach_step)", "Notes/Remarks
    (next_steps)". Empty cells are listed under "empty". Every sheet tool
    returns rows in this one shape.

READ-ONLY, AND NOTHING HERE CAN WRITE. This module imports no write path and
calls none. An input that asks for a write (`refuse_write`) is refused by
name. The role-based column locks are not touched: they are about writing,
and this only reads.

MATCHING IGNORES CASE, ACCENTS, EXTRA SPACES AND PUNCTUATION, so "research
the poc" equals "Research the PoC" and "Zelasko" equals "Żelasko". A short
word (three letters or fewer, like UK, USA or US) is matched as a whole word,
so "US" does not match "Austin" and "UK" does not match "Ukraine"; a longer
one is matched anywhere in the cell ("univ" finds "University").

DATES ARE READ DAY-FIRST by R14's reader (`reconnect.read_date`), the one
reader for hand-typed dates on this tab. A cell it cannot read is skipped and
counted in `unreadable_dates`; it is never guessed into or out of a window.
"""
import difflib
import re
import unicodedata
from collections import Counter
from datetime import date
from typing import Optional

OPS = ("equals", "contains", "contains_any", "starts_with", "empty", "not_empty",
       "in", "before", "after", "greater", "less")
# Ops whose value is a list. A single string is taken as a list of one.
LIST_OPS = ("contains_any", "in")
# Ops that take no value at all.
NO_VALUE_OPS = ("empty", "not_empty")
DATE_OPS = ("before", "after")
NUMBER_OPS = ("greater", "less")

DEFAULT_LIMIT = 50
MAX_LIMIT = 200
# A column with this many distinct values or fewer is shown with its values.
DROPDOWN_MAX = 30
TOP_VALUES = 20
# How many close values are offered when a value matched nothing.
CLOSEST_MAX = 3

# WHAT A WRITE LOOKS LIKE IN AN INPUT. Neither tool has a write path; these
# are refused by name so the refusal says why, instead of the tool quietly
# ignoring a key and answering a read as if the write had happened.
WRITE_KEYS = ("set", "update", "write", "values", "append", "insert", "delete",
              "clear", "replace", "edit", "add", "put", "new_value", "range")
WRITE_OPS = ("set", "update", "write", "append", "insert", "delete", "clear",
             "replace", "edit", "add", "put", "assign")

READ_ONLY_NOTE = (
    "sheet_query and sheet_columns only read the sheet; neither has a write path. "
    "A change to the sheet goes through an approval proposal, and an approver's yes "
    "is what writes it.")

_SHORT_WORD = 3


# -- text ----------------------------------------------------------------------

def norm(text) -> str:
    """A cell or a name as a comparable key: accents off, case folded,
    punctuation and underscores to spaces, spaces squeezed."""
    s = unicodedata.normalize("NFKD", str(text or ""))
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.casefold().replace("ł", "l").replace("ø", "o").replace("đ", "d")
    s = re.sub(r"[\W_]+", " ", s)
    return " ".join(s.split())


def letter(index0: int) -> str:
    """0-based column index -> A1 letter (0 -> A, 26 -> AA)."""
    n, out = int(index0) + 1, ""
    while n > 0:
        n, rem = divmod(n - 1, 26)
        out = chr(65 + rem) + out
    return out


def _clean(value) -> str:
    """A cell as every reader sees it (gtm_sheet.clean_cell: stripped, and
    empty when it is a spreadsheet error)."""
    import gtm_sheet
    return gtm_sheet.clean_cell(value)


# -- columns -------------------------------------------------------------------

def _roles(tab) -> dict:
    """{column index: role} from the map WITHOUT the compatibility aliases, so
    each column reports the one role the sheet actually gave it."""
    mapping = getattr(tab, "canonical_role_to_col", None) or getattr(tab, "role_to_col", {}) or {}
    return {i: role for role, i in mapping.items()}


def key(tab, i: int) -> str:
    """The name a column goes by in every row Saley sees: the header, and the
    role in brackets when it has one."""
    header = str(tab.headers[i]).strip()
    role = _roles(tab).get(i)
    return f"{header} ({role})" if role else header


def _indexes(tab) -> list:
    """Every column with a header, in sheet order."""
    return [i for i, h in enumerate(tab.headers) if str(h).strip()]


def cell(tab, row: dict, i: int) -> str:
    """The value of column `i` in a parsed row: a mapped column under its
    role, any other under its header in `_extra`."""
    role = _roles(tab).get(i)
    if role:
        return _clean(row.get(role))
    return _clean((row.get("_extra") or {}).get(str(tab.headers[i]).strip()))


def _forms(header: str) -> set:
    """Every name a header answers to EXACTLY: itself; itself without a
    bracketed note ("Based (Sept 2026)" is "Based"); and each part of a
    slashed header, because "Next Steps/Notes" names two things and a person
    asking for either one means it. That last rule is what makes "Next
    Steps" on a tab that has both "Next Steps" and "Next Steps/Notes" a
    question, not a guess."""
    text = str(header or "")
    out = {norm(text)}
    bare = re.sub(r"\([^)]*\)", " ", text)
    out.add(norm(bare))
    for part in re.split(r"/", bare):
        out.add(norm(part))
    out.discard("")
    return out


def _brief(tab, i: int) -> dict:
    return {"letter": letter(i), "header": str(tab.headers[i]).strip(),
            "role": _roles(tab).get(i) or ""}


def resolve(tab, name) -> tuple:
    """(column index, None) or (None, error dict).

    In order, and the first step that finds anything decides:
      1. "Header (role)" as the rows show it, or a role name on its own
         ("outreach_step", "based");
      2. an exact header, ignoring case and punctuation, with the forms in
         `_forms`;
      3. a header that contains every word asked for.
    ONE MATCH IS THE ANSWER. TWO OR MORE IS A QUESTION BACK, never a guess.
    NONE NAMES THE COLUMNS THAT DO EXIST.
    """
    raw = str(name or "").strip()
    if not raw:
        return None, {"error": "a condition needs a 'column'.", "columns": headers(tab)}
    roles = _roles(tab)
    by_role = {role: i for i, role in roles.items()}

    m = re.match(r"^(.*?)\s*\(([a-z0-9_]+)\)\s*$", raw)
    if m and m.group(2) in by_role:
        return by_role[m.group(2)], None
    # A ROLE ONLY WHEN IT IS WRITTEN AS ONE. "next steps" is a header (the
    # dropdown); "next_steps" is the role (the notes). Reading the first as
    # the second is the very confusion this module exists to end.
    as_role = raw.strip().lower()
    if as_role in by_role and "_" in as_role:
        return by_role[as_role], None

    want = norm(m.group(1) if m else raw)
    exact = [i for i in _indexes(tab) if want in _forms(tab.headers[i])]
    if not exact and as_role in by_role:
        exact = [by_role[as_role]]
    if len(exact) == 1:
        return exact[0], None
    if len(exact) > 1:
        return None, _ambiguous(tab, raw, exact)

    words = want.split()
    loose = [i for i in _indexes(tab)
             if words and all(w in norm(tab.headers[i]).split() for w in words)]
    if len(loose) == 1:
        return loose[0], None
    if len(loose) > 1:
        return None, _ambiguous(tab, raw, loose)
    return None, {
        "error": f"{tab.title!r} has no column called {raw!r}.",
        "columns": headers(tab),
        "note": ("Say which columns the tab does have (listed above) and ask which one "
                 "they meant. Never answer as if the column existed."),
    }


def _ambiguous(tab, raw: str, idx: list) -> dict:
    cols = [_brief(tab, i) for i in idx]
    named = " and ".join(f"{c['header']!r} ({c['letter']})" for c in cols)
    return {
        "error": f"{len(cols)} columns on {tab.title!r} match {raw!r}.",
        "ambiguous": cols,
        "question": f"{named} both match {raw!r}. Which one did you mean?"
        if len(cols) == 2 else f"These columns all match {raw!r}: {named}. Which one did you mean?",
        "note": "Ask the question above. Do not pick one.",
    }


def headers(tab) -> list:
    """The tab's headers in column order, as the sheet shows them."""
    return [str(h).strip() for h in tab.headers if str(h).strip()]


# -- values ---------------------------------------------------------------------

def _contains(hay: str, needle: str) -> bool:
    """`needle` in `hay`, both normalised. A short needle (UK, USA, US) must be
    a whole word; a longer one may sit anywhere."""
    if not needle:
        return False
    if len(needle.replace(" ", "")) <= _SHORT_WORD:
        return f" {needle} " in f" {hay} "
    return needle in hay


def _number(text) -> Optional[float]:
    """The first number in a cell: "$1,200" is 1200, "60%" is 60, ">50%" is
    50, "1.5k" is 1500, "2M" is 2000000. None when there is none."""
    s = str(text or "").replace(",", "")
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    if not m:
        return None
    value = float(m.group(0))
    tail = s[m.end():].strip().lower()
    if tail.startswith("k"):
        value *= 1_000
    elif tail.startswith(("m", "mn")):
        value *= 1_000_000
    return value


def _date(text, today: date) -> Optional[date]:
    import reconnect
    return reconnect.read_date(text, today=today)


def _date_empty(text) -> bool:
    """Blank, or the checkbox artefact FALSE: no date and not an unreadable one."""
    import reconnect
    return reconnect.is_empty(text)


def _values(cond: dict) -> list:
    v = cond.get("value")
    if isinstance(v, (list, tuple)):
        return [str(x) for x in v if str(x).strip()]
    return [str(v)] if v is not None and str(v).strip() else []


def _check_condition(tab, cond, today: date) -> tuple:
    """(compiled condition, None) or (None, error dict)."""
    if not isinstance(cond, dict):
        return None, {"error": f"each condition is {{column, op, value}}; got {cond!r}."}
    op = str(cond.get("op") or "equals").strip().lower()
    if op in WRITE_OPS:
        return None, {"error": f"{op!r} is a write. {READ_ONLY_NOTE}", "read_only": True}
    if op not in OPS:
        return None, {"error": f"unknown op {op!r}.", "ops": list(OPS)}
    i, err = resolve(tab, cond.get("column"))
    if err:
        return None, err
    values = _values(cond)
    out = {"index": i, "op": op, "values": values, "key": key(tab, i)}
    if op in NO_VALUE_OPS:
        return out, None
    if not values:
        return None, {"error": f"{op!r} on {key(tab, i)!r} needs a value."}
    if op in DATE_OPS:
        when = _date(values[0], today)
        if when is None:
            return None, {"error": f"{values[0]!r} is not a date I can read (day first: "
                                   "13/10/2026, 13 Oct 2026, 2026-10-13)."}
        out["date"] = when
    if op in NUMBER_OPS:
        num = _number(values[0])
        if num is None:
            return None, {"error": f"{values[0]!r} is not a number."}
        out["number"] = num
    out["needles"] = [norm(v) for v in values]
    return out, None


def _holds(tab, row: dict, c: dict, today: date, tally: Counter) -> bool:
    """Does `row` meet condition `c`? Unreadable dates and numbers are
    skipped and counted in `tally`, never guessed."""
    text = cell(tab, row, c["index"])
    op = c["op"]
    if op == "empty":
        return not text
    if op == "not_empty":
        return bool(text)
    if op in DATE_OPS:
        if _date_empty(text):
            return False
        when = _date(text, today)
        if when is None:
            tally["unreadable_dates"] += 1
            return False
        return when < c["date"] if op == "before" else when > c["date"]
    if op in NUMBER_OPS:
        if not text:
            return False
        num = _number(text)
        if num is None:
            tally["unreadable_numbers"] += 1
            return False
        return num > c["number"] if op == "greater" else num < c["number"]
    hay = norm(text)
    needles = c["needles"]
    if op in ("equals", "in"):
        return hay in needles if op == "in" else hay == needles[0]
    if op == "starts_with":
        return bool(needles[0]) and hay.startswith(needles[0])
    if op == "contains":
        return _contains(hay, needles[0])
    return any(_contains(hay, n) for n in needles)             # contains_any


def closest(tab, i: int, wanted, *, n: int = CLOSEST_MAX) -> list:
    """The values in column `i` nearest to `wanted`, best first — for "did you
    mean Research the PoC?" when "researching" matched nothing. A value that
    shares the first five letters of a word with what was asked is always
    offered; otherwise it must be at least half alike."""
    want = norm(wanted)
    if not want:
        return []
    seen: dict = {}
    for row in tab.rows:
        v = cell(tab, row, i)
        if v and norm(v) not in seen:
            seen[norm(v)] = v
    stems = {w[:5] for w in want.split() if len(w) >= 5}
    scored = []
    for k, v in seen.items():
        ratio = difflib.SequenceMatcher(None, want, k).ratio()
        stem = any(w[:5] in stems for w in k.split() if len(w) >= 5)
        if ratio >= 0.5 or stem:
            scored.append((-(ratio + (0.5 if stem else 0)), v))
    return [v for _s, v in sorted(scored)[:n]]


# -- rows ---------------------------------------------------------------------

def row_out(tab, row: dict) -> dict:
    """One row as every sheet tool returns it: the sheet row number, every
    filled cell keyed by its header (role in brackets), and the blank ones
    listed under "empty" — the reply must say when a cell is blank, and it can
    only do that if it knows which ones are."""
    out: dict = {"sheet_row": row.get("_row")}
    empty = []
    for i in _indexes(tab):
        value = cell(tab, row, i)
        if value:
            out[key(tab, i)] = value
        else:
            empty.append(key(tab, i))
    out["empty"] = empty
    return out


def default_columns(tab) -> list:
    """Column indexes returned when none are asked for. Outreach PoCs: Name,
    Company/Uni, Designation. Any other tab: Company and the first three other
    mapped roles in column order (the serial number is not one of them)."""
    import gtm_sheet
    by_role = {role: i for i, role in _roles(tab).items()}
    if getattr(tab, "kind", "") == gtm_sheet.POCS:
        picked = [by_role[r] for r in ("name", "company", "designation") if r in by_role]
        if picked:
            return picked
    picked = [by_role["company"]] if "company" in by_role else []
    cap = len(picked) + 3
    for i in sorted(by_role.values()):
        if len(picked) >= cap:
            break
        if i not in picked and _roles(tab)[i] != "sr_no":
            picked.append(i)
    return picked or _indexes(tab)[:3]


def select(tab, where, *, today: Optional[date] = None) -> tuple:
    """(matching rows, tally, compiled conditions, None) or (None, None, None,
    error). Every condition must hold. The rows are the parsed rows, so a line
    the parser already dropped (a spacer, a total, the FALSE-only checkbox
    rows with no company) is never matched."""
    today = today or date.today()
    if where is None:
        where = []
    if isinstance(where, dict):
        where = [where]
    if not isinstance(where, (list, tuple)):
        return None, None, None, {"error": "'where' is a list of {column, op, value}."}
    conds = []
    for raw in where:
        c, err = _check_condition(tab, raw, today)
        if err:
            return None, None, None, err
        conds.append(c)
    tally: Counter = Counter()
    rows = []
    for row in tab.rows:
        ok = True
        for c in conds:
            if not _holds(tab, row, c, today, tally):
                ok = False
                break
        if ok:
            rows.append(row)
    return rows, tally, conds, None


def refuse_write(inp) -> Optional[dict]:
    """The refusal for an input that asks for a write, or None."""
    if not isinstance(inp, dict):
        return None
    asked = sorted(k for k in inp if str(k).strip().lower() in WRITE_KEYS)
    if asked:
        return {"error": f"{', '.join(asked)} would be a write. {READ_ONLY_NOTE}",
                "read_only": True}
    for cond in inp.get("where") or []:
        if isinstance(cond, dict) and str(cond.get("op") or "").strip().lower() in WRITE_OPS:
            return {"error": f"{cond.get('op')!r} would be a write. {READ_ONLY_NOTE}",
                    "read_only": True}
    return None


def _limit(value) -> tuple:
    """(limit, asked-for limit was over the cap)."""
    try:
        n = int(value) if value not in (None, "") else DEFAULT_LIMIT
    except (TypeError, ValueError):
        n = DEFAULT_LIMIT
    return max(1, min(MAX_LIMIT, n)), n > MAX_LIMIT


def query(tab, *, where=None, columns=None, limit=None, today: Optional[date] = None) -> dict:
    """sheet_query on one parsed tab. See the module docstring."""
    rows, tally, conds, err = select(tab, where, today=today)
    if err:
        return dict(err, tab=tab.title)
    if columns:
        names = columns if isinstance(columns, (list, tuple)) else [columns]
        idx = []
        for name in names:
            i, cerr = resolve(tab, name)
            if cerr:
                return dict(cerr, tab=tab.title)
            if i not in idx:
                idx.append(i)
    else:
        idx = default_columns(tab)
    n, capped = _limit(limit)
    keys = [key(tab, i) for i in idx]
    out = {
        "tab": tab.title,
        "where": [{"column": c["key"], "op": c["op"], "value": c["values"]} for c in conds],
        "columns": keys,
        "match_count": len(rows),
        "returned": min(len(rows), n),
        "truncated": len(rows) > n,
        "limit": n,
        "unreadable_dates": tally.get("unreadable_dates", 0),
        "unreadable_numbers": tally.get("unreadable_numbers", 0),
        # EXACTLY THE COLUMNS ASKED FOR, nothing else: the answer is rendered
        # from these, and a key nobody asked for is a field somebody renders.
        "rows": [{key(tab, i): cell(tab, r, i) for i in idx} for r in rows[:n]],
    }
    if capped:
        out["limit_capped"] = (f"{limit} is over the cap; the first {MAX_LIMIT} rows are "
                               f"returned and match_count ({len(rows)}) is the total.")
    if out["truncated"]:
        out["note"] = (f"{len(rows)} rows match and {n} are shown. Give the total "
                       f"({len(rows)}) in the answer.")
    if tally.get("unreadable_dates"):
        out.setdefault("note", "")
        out["note"] = (out["note"] + " " if out["note"] else "") + (
            f"{tally['unreadable_dates']} date cell(s) could not be read and were left out; "
            "say so.")
    if not rows:
        hints = {}
        for c in conds:
            if c["op"] in ("equals", "in", "starts_with") and c["values"]:
                alone, _t, _c, _e = select(tab, [{"column": c["key"], "op": c["op"],
                                                  "value": c["values"]}], today=today)
                if not alone:
                    near = []
                    for v in c["values"]:
                        near += [x for x in closest(tab, c["index"], v) if x not in near]
                    if near:
                        hints[c["key"]] = near[:CLOSEST_MAX]
        if hints:
            out["closest"] = hints
            first = next(iter(hints.values()))
            out["did_you_mean"] = "Did you mean " + " or ".join(first) + "?"
            out["note"] = ("No row matches. Ask the did_you_mean question; never answer as if "
                           "the value existed.")
    return out


def columns(tab) -> dict:
    """sheet_columns on one parsed tab: every header in column order, its
    letter and role, and the values of every dropdown-sized column."""
    import gtm_sheet
    roles = _roles(tab)
    cols = []
    for i in _indexes(tab):
        counts = Counter(cell(tab, r, i) for r in tab.rows)
        counts.pop("", None)
        entry = {"letter": letter(i), "header": str(tab.headers[i]).strip(),
                 "role": roles.get(i) or "", "key": key(tab, i),
                 "filled": sum(counts.values()), "distinct": len(counts)}
        if 0 < len(counts) <= DROPDOWN_MAX:
            entry["values"] = [{"value": v, "count": c}
                               for v, c in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
                               [:TOP_VALUES]]
        cols.append(entry)
    expected = list((gtm_sheet.ROLES.get(getattr(tab, "kind", "")) or {}).keys())
    missing = [r for r in expected if r not in set(roles.values())]
    out = {"tab": tab.title, "kind": getattr(tab, "kind", ""), "rows": len(tab.rows),
           "column_count": len(cols), "columns": cols}
    if missing:
        out["roles_not_found"] = missing
        out["roles_note"] = ("These roles map to no column on this tab today. Report it if a "
                             "question needs one; never guess which column it is.")
    return out


# -- tabs ---------------------------------------------------------------------

# What people call each kind of tab. The title on the sheet always wins; these
# only help when the name asked for is not a title.
TAB_WORDS = {
    "outreach pocs": "outreach_pocs", "pocs": "outreach_pocs", "outreach": "outreach_pocs",
    "tracker": "outreach_pocs",
    "master pipeline": "researcher_lines", "pipeline": "researcher_lines",
    "deliverables": "deliverables_checklist", "deliverables checklist": "deliverables_checklist",
    "ai events": "events_summits", "events": "events_summits", "summits": "events_summits",
    "ai events summits": "events_summits",
    "sales packages": "sales_packages", "packages": "sales_packages",
    "goals": "goal_setting", "goal setting": "goal_setting",
}


def pick_tab(tabs: list, wanted, *, unreadable=()) -> tuple:
    """(tab, None) or (None, error dict). `tabs` are the parsed (readable)
    tabs; `unreadable` the titles of tabs the sheet has but the bot does not
    read. A title wins over a kind; two loose matches are a question."""
    want = norm(wanted)
    if not want:
        return None, {"error": "say which tab ('tab').",
                      "tabs": [t.title for t in tabs]}
    exact = [t for t in tabs if norm(t.title) == want]
    if len(exact) == 1:
        return exact[0], None
    kind = TAB_WORDS.get(want) or (want.replace(" ", "_")
                                   if any(t.kind == want.replace(" ", "_") for t in tabs) else "")
    if kind:
        of_kind = [t for t in tabs if t.kind == kind]
        if of_kind:
            return of_kind[0], None
    blocked = [u for u in unreadable if norm(u) == want]
    if blocked:
        return None, {"available": False,
                      "error": f"the GTM Playbook has no recognised {blocked[0]} tab",
                      "fix": "Check the tab still exists, or set GTM_COLUMN_MAP if its "
                             "headers were renamed.",
                      "tabs": [t.title for t in tabs]}
    loose = [t for t in tabs if want in norm(t.title)]
    if len(loose) == 1:
        return loose[0], None
    if len(loose) > 1:
        return None, {"error": f"{len(loose)} tabs match {wanted!r}.",
                      "ambiguous": [t.title for t in loose],
                      "question": "Which tab did you mean: "
                                  + ", ".join(repr(t.title) for t in loose) + "?"}
    return None, {"available": False,
                  "error": f"the GTM Playbook has no recognised {wanted} tab",
                  "fix": "Check the tab still exists, or set GTM_COLUMN_MAP if its headers "
                         "were renamed.",
                  "tabs": [t.title for t in tabs]}


# -- self-test ----------------------------------------------------------------

def _self_test() -> int:
    """`python -m sheetquery` — on a small made-up tab parsed by the real parser."""
    import gtm_sheet

    failures = 0

    def check(name, got, want):
        nonlocal failures
        ok = got == want
        failures += 0 if ok else 1
        print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}" + ("" if ok else f", want {want!r}"))

    hdr = ["Sr No", "Company/Uni", "Name", "Industry", "Designation", "Email id",
           "Based (Sept 2026)", "First Contact", "Next Steps", "Meeting Date",
           "Notes/Remarks", "Estd. Deal Size (USD)", "Deal Status", "Priority"]

    def row(n, company, name, title, based, step="", meet="", notes="", size="", deal="", pri=""):
        return [str(n), company, name, "AI", title, "", based, "TRUE" if company else "FALSE",
                step, meet, notes, size, deal, pri]

    grid = [hdr,
            row(1, "Acme AI", "Ada Lovelace", "CTO", "London, UK", "Research the PoC",
                "02-10-2026", "Send the deck", "$1,200", "In progress", "P1"),
            row(2, "Globex", "Grace Hopper", "Co-Founder", "Austin, USA", "Send email 1",
                "TBD", "", "60%", "", "P2"),
            row(3, "Initech", "Alan Turing", "VP Eng", "Bengaluru, India", "research the poc",
                "8/9/2026"),
            row(4, "Umbrella", "Żofia Kraków", "Lead", "Kyiv, Ukraine", "Research the PoC"),
            row(5, "", "", "", "")]
    tab = gtm_sheet.SHEETS._parse_values("Outreach PoCs", grid, read_at=0.0)
    today = date(2026, 10, 15)

    print("text")
    check("norm drops case, accents and punctuation", norm("  Research the PoC! "), "research the poc")
    check("...accents", norm("Żelasko"), "zelasko")
    check("letters", (letter(0), letter(15), letter(28)), ("A", "P", "AC"))

    print("\ncolumns")
    i, err = resolve(tab, "next steps")
    check("'next steps' -> the dropdown", (tab.headers[i], _roles(tab)[i], err),
          ("Next Steps", "outreach_step", None))
    i, err = resolve(tab, "Based")
    check("'Based' -> 'Based (Sept 2026)'", tab.headers[i], "Based (Sept 2026)")
    i, err = resolve(tab, "next_steps")
    check("the role 'next_steps' -> Notes/Remarks", tab.headers[i], "Notes/Remarks")
    i, err = resolve(tab, "Next Steps (outreach_step)")
    check("a row key resolves", tab.headers[i], "Next Steps")
    i, err = resolve(tab, "Company")
    check("'Company' -> 'Company/Uni'", tab.headers[i], "Company/Uni")
    i, err = resolve(tab, "Nonsense")
    check("a missing column names the real ones", (i, err["columns"][:3]),
          (None, ["Sr No", "Company/Uni", "Name"]))
    two = gtm_sheet.SHEETS._parse_values(
        "Outreach PoCs", [["Company", "Name", "Next Steps", "Next Steps/Notes"], ["Acme", "Ada", "x", "y"]],
        read_at=0.0)
    i, err = resolve(two, "Next Steps")
    check("two columns match: a question, no guess",
          (i, [c["header"] for c in err["ambiguous"]], "Which one" in err["question"]),
          (None, ["Next Steps", "Next Steps/Notes"], True))

    print("\nrows")
    res = query(tab, where=[{"column": "Next Steps", "op": "equals", "value": "research the poc"}],
                today=today)
    check("equals ignores case: 3 rows", res["match_count"], 3)
    check("the FALSE-only row with no company is never a row",
          [r["_row"] for r in select(tab, [{"column": "Next Steps", "op": "equals",
                                            "value": "research the poc"}], today=today)[0]],
          [2, 4, 5])
    res = query(tab, where=[{"column": "Next Steps", "op": "equals", "value": "research the poc"},
                            {"column": "Based", "op": "contains_any", "value": ["UK", "USA"]}],
                columns=["Name", "Company/Uni", "Designation"], today=today)
    check("and Based contains_any UK/USA: Acme only ('UK' is not 'Ukraine')",
          [r["Name (name)"] for r in res["rows"]], ["Ada Lovelace"])
    check("only the three columns asked for",
          sorted(res["rows"][0]), sorted(["Name (name)", "Company/Uni (company)",
                                          "Designation (designation)"]))
    res = query(tab, where=[{"column": "Next Steps", "op": "equals", "value": "researching"}], today=today)
    check("a value not in the dropdown: 0 rows and the closest value",
          (res["match_count"], res.get("did_you_mean")), (0, "Did you mean Research the PoC?"))
    res = query(tab, where=[{"column": "Meeting Date", "op": "after", "value": "2026-10-01"}], today=today)
    check("after 1 Oct: one row, 'TBD' counted as unreadable",
          (res["match_count"], res["unreadable_dates"]), (1, 1))
    res = query(tab, where=[{"column": "Meeting Date", "op": "before", "value": "1/10/2026"}], today=today)
    check("before 1 Oct, day first: 8/9/2026 is 8 September", res["match_count"], 1)
    res = query(tab, where=[{"column": "Estd. Deal Size (USD)", "op": "greater", "value": "100"}],
                today=today)
    check("greater: $1,200 > 100, 60% is not", res["match_count"], 1)
    res = query(tab, where=[{"column": "Deal Status", "op": "not_empty"}], today=today)
    check("not_empty", res["match_count"], 1)
    res = query(tab, where=[{"column": "Name", "op": "starts_with", "value": "zofia"}], today=today)
    check("starts_with, accents ignored", res["match_count"], 1)
    res = query(tab, where=[{"column": "Priority", "op": "in", "value": ["p1", "P2"]}], limit=1,
                today=today)
    check("in, limit 1: truncated, total kept", (res["match_count"], res["returned"], res["truncated"]),
          (2, 1, True))
    res = query(tab, limit=300, today=today)
    check("limit 300 is capped at 200", (res["limit"], "limit_capped" in res), (200, True))
    check("no columns -> Name, Company/Uni, Designation", res["columns"],
          ["Name (name)", "Company/Uni (company)", "Designation (designation)"])

    print("\nthe row shape")
    full = row_out(tab, tab.rows[0])
    check("keys are headers with the role in brackets",
          (full["Next Steps (outreach_step)"], full["Notes/Remarks (next_steps)"]),
          ("Research the PoC", "Send the deck"))
    check("blank cells are listed under 'empty'", "Email id (email)" in full["empty"], True)

    print("\nread-only")
    check("a write key is refused", "read_only" in (refuse_write({"tab": "x", "set": {"A2": "y"}}) or {}),
          True)
    check("a write op is refused", (query(tab, where=[{"column": "Name", "op": "update", "value": "x"}])
                                    .get("read_only")), True)
    check("a read is not refused", refuse_write({"tab": "x", "where": []}), None)

    print("\ndropdowns")
    cols = columns(tab)
    step = next(c for c in cols["columns"] if c["header"] == "Next Steps")
    check("Next Steps shows its values with counts", step["values"],
          [{"value": "Research the PoC", "count": 2}, {"value": "Send email 1", "count": 1},
           {"value": "research the poc", "count": 1}])
    check("a role with no column is reported", "reconnect_date" in cols.get("roles_not_found", []), True)

    print("\ntabs")
    check("a title", pick_tab([tab], "outreach pocs")[0] is tab, True)
    check("a kind word", pick_tab([tab], "PoCs")[0] is tab, True)
    _t, err = pick_tab([tab], "Activation Score", unreadable=["Activation Score"])
    check("an unreadable tab: the same error as today, naming it", err["error"],
          "the GTM Playbook has no recognised Activation Score tab")

    print(f"\n{'ALL PASSED' if not failures else str(failures) + ' FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    import sys
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    raise SystemExit(_self_test())
