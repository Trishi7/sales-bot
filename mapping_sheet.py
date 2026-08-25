"""The Researcher/Buyer Mapping spreadsheet — STRICTLY READ-ONLY.

The third source. Where the GTM Playbook says which COMPANIES we are working,
this sheet says which PEOPLE inside them are worth pitching, what to open with,
and — just as importantly — who must not be pitched at all.

    GTM_MAPPING_SHEET_ID   "membrane.social - Researcher Buyer Mapping"

READ-ONLY IS ENFORCED IN CODE, NOT BY CONVENTION. Three independent things have
to fail before a byte could be written here:

  1. This module has NO write method. There is no update, no append, no
     ensure-column: the class simply does not expose one.
  2. Its client is built with the READ-ONLY Sheets scope
     (spreadsheets.readonly), not the read/write scope gtm_sheet.py uses. The
     token this module holds is rejected by Google on any write, whatever the
     code asks for.
  3. `config.is_read_only_sheet_id()` names this spreadsheet id, and every write
     path in gtm_sheet.py refuses a target that matches it — so even pointing
     GTM_SHEET_COPY_ID at this sheet cannot produce a write.

WHAT MAKES THIS SOURCE DIFFERENT: THE LEGEND IS BEHAVIOUR, NOT DATA.

The "Mapping _Legend" tab is not a glossary to quote back. It states the rules
every recommendation from this sheet must obey, and this module loads them as
enforcement:

    STALENESS   the sheet states its own refresh rule ("re-verify any row older
                than ~6 weeks"). The threshold is READ FROM THE LEGEND and
                measured against TODAY — never hardcoded — so the caveat starts
                appearing on its own as the sheet ages. Two clocks are checked:
                how old the sheet's research pass is, and how old the newest
                dated evidence in the row itself is. A row whose role was
                verified from 2025 press was already stale the day it was
                written.
    DEPARTURES  the Edge Map carries a DEPARTURES row. Anyone on it has left the
                org the sheet lists them under, and is NEVER recommended — with
                the move named, because "don't pitch them" without "they moved to
                Oracle Health in the meantime" is not an answer.
    FLAGS       non-buyers (competitors and channel partners) and budget-gate
                failures are mapped for completeness, NOT for pitching. Asked
                about one, the bot says what the sheet says and why they're out.
    TIER vs     Tier is buyer FIT. Confidence is EVIDENCE QUALITY. The legend
    CONFIDENCE  says in as many words that they are independent, so both are
                carried separately on every row and both must be quoted.
    WATCH-OUTS  a per-row column of caveats. Carried on the row and surfaced with
                it, because a hook quoted without its watch-out is how someone
                walks into a call with stale information.

HEADERS AND TABS ARE DISCOVERED, NOT ASSUMED. The known tabs are the legend, the
researcher mapping, the org coverage list and the edge map, but they are matched
by what their headers contain, so a rename or a new column does not blind this
module. Anything unrecognised is logged once and ignored rather than guessed at.

Failure is a first-class state, exactly as in gtm_sheet.py: a 60-second cache, a
last-good copy kept indefinitely for stale fallback, and an unreadable sheet
reported with the share-with-<service-account> line rather than a stack trace.
"""
import logging
import re
import threading
import time
from datetime import date, datetime
from typing import Optional

import config
from gtm_sheet import SheetAccessError, normalise_header

log = logging.getLogger(__name__)

# Same optional-import treatment as gtm_sheet: the bot must still boot and still
# answer non-sheet questions on a box without the Google libraries.
try:
    import gspread
    from google.oauth2.service_account import Credentials

    _IMPORT_ERROR: Optional[str] = None
except Exception as e:  # pragma: no cover - depends on the environment
    gspread = None
    Credentials = None
    _IMPORT_ERROR = f"{type(e).__name__}: {e}"

#: THE READ-ONLY SCOPE, and the second of the three locks on this sheet. This is
#: deliberately NOT the scope in gtm_sheet.SCOPES: a token minted for
#: spreadsheets.readonly is refused by Google on any write, so a future bug that
#: somehow reached a write call against this client still cannot change a cell.
SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]

# Tab kinds.
LEGEND = "mapping_legend"
RESEARCHERS = "researcher_mapping"
ORG_COVERAGE = "org_coverage"
EDGES = "edge_map"

KINDS = (LEGEND, RESEARCHERS, ORG_COVERAGE, EDGES)

# -- header roles -------------------------------------------------------------
# Same matching contract as gtm_sheet.ROLES: case- and punctuation-insensitive,
# exact-normalised first and substring second, longest alias first, one header to
# at most one role.
ROLES: dict[str, dict[str, tuple[str, ...]]] = {
    LEGEND: {
        # The legend is a two-column key/value sheet, not a table of records.
        "key": ("key", "field", "item", "legend"),
        "value": ("value", "meaning", "detail", "description"),
    },
    RESEARCHERS: {
        "sr_no": ("sr. no.", "sr no", "s.no", "sno", "serial", "#"),
        "researcher": ("researcher", "researcher name", "name", "person"),
        "research_link": ("research link", "paper", "paper link", "links"),
        "bucket": ("membrane bucket", "bucket", "segment"),
        "prioritization": ("outreach prioritization", "outreach priority",
                           "prioritization", "priority"),
        "company": ("current company", "company", "org", "organisation",
                    "organization", "employer"),
        "industry": ("industry", "sector", "vertical"),
        "role": ("verified current role", "current role", "role", "title",
                 "designation"),
        "role_verified_via": ("role verified via", "verified via", "verification",
                              "source"),
        "lanes": ("lanes", "lane", "icp lanes", "icp lane"),
        "key_evidence": ("key evidence", "evidence", "proof"),
        "network_edges": ("network edges", "edges", "network", "connections"),
        "profiles": ("public profiles", "profiles", "profile", "links (public)"),
        "why_them": ("why them (pitch hook)", "why them", "pitch hook", "hook",
                     "why"),
        "confidence": ("confidence", "confidence level"),
        "tier": ("tier", "tiering", "t1/t2/t3"),
        "watch_outs": ("watch-outs", "watch outs", "watchouts", "caveats",
                       "caveat", "watch out"),
    },
    ORG_COVERAGE: {
        "sr_no": ("sr. no.", "sr no", "s.no", "sno", "serial", "#"),
        "company": ("company", "org", "organisation", "organization", "account",
                    "company name"),
        "industry": ("industry", "sector", "vertical"),
        "researchers_mapped": ("researchers mapped", "researchers", "mapped",
                               "count"),
        "t1": ("t1",),
        "t2": ("t2",),
        "t3": ("t3",),
        "notes": ("account notes", "notes", "comments", "remarks"),
    },
    EDGES: {
        "edge": ("edge / cluster", "edge/cluster", "edge", "cluster"),
        "connects": ("who it connects", "connects", "who", "people"),
        "why": ("why it matters", "why", "matters", "so what"),
    },
}

# (kind, signature roles, how many must match, roles that are MANDATORY).
#
# The mandatory column is what keeps the other tabs in this workbook — an
# academic-outreach list, a research-topic list — from being read as one of
# these. The academic tab has "Researcher Name" and would otherwise score against
# RESEARCHERS; it has no Tier and no Confidence, which is exactly what makes it a
# different thing, so both are mandatory here.
_KIND_SIGNATURES: list[tuple[str, tuple[str, ...], int, tuple[str, ...]]] = [
    (RESEARCHERS,
     ("researcher", "company", "lanes", "tier", "confidence", "why_them", "watch_outs"),
     4, ("researcher", "tier", "confidence")),
    (ORG_COVERAGE,
     ("company", "researchers_mapped", "t1", "t2", "t3", "notes"),
     3, ("company", "researchers_mapped")),
    (EDGES, ("edge", "connects", "why"), 2, ("edge", "connects")),
    (LEGEND, ("key", "value"), 1, ()),
]

_KIND_NAME_HINTS: list[tuple[str, tuple[str, ...]]] = [
    (LEGEND, ("legend", "mapping legend", "key", "readme")),
    (RESEARCHERS, ("researcher mapping", "researcher", "mapping")),
    (ORG_COVERAGE, ("org coverage", "coverage", "org", "account coverage")),
    (EDGES, ("edge map", "edge", "network")),
]

# -- the legend's own vocabulary ----------------------------------------------

#: Fallback ICP lanes, used ONLY when the legend row can't be parsed. The live
#: legend is authoritative; this exists so a reworded legend degrades to "the
#: lanes we last knew" rather than to nothing.
_FALLBACK_LANES = {
    "a": "model evals/benchmarks",
    "b": "post-training / RLHF / preference data",
    "c": "red-teaming / safety",
    "d": "agent trajectories & personalization",
}
_FALLBACK_TIERS = {
    "T1": "verified role + direct buyer/champion fit, pitch-ready",
    "T2": "strong fit, re-verify role or secondary champion",
    "T3": "door-opener / fallback / watch",
}
#: The sheet's stated refresh rule, in weeks, used when the legend's Refresh row
#: can't be parsed. Never used to OVERRIDE the legend — only to stand in for it.
_FALLBACK_REFRESH_WEEKS = 6

# "T1" / "t-2" / "Tier 3" → T1 / T2 / T3.
_TIER_RE = re.compile(r"\bt(?:ier)?[\s\-]?([123])\b", re.IGNORECASE)
# Lane letters as the sheet writes them: "a", "a, b", "a,b,d".
_LANE_RE = re.compile(r"\b([a-d])\b", re.IGNORECASE)
# "~6 weeks" / "6 weeks" / "about six weeks" (digits only; a spelled-out number
# is rare enough that falling back is better than a brittle word parser).
_WEEKS_RE = re.compile(r"~?\s*(\d{1,2})\s*week", re.IGNORECASE)

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
# "Jul 2026", "July 2026", "Mar 2025".
_MONTH_YEAR_RE = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+((?:19|20)\d{2})\b",
    re.IGNORECASE,
)
# A bare 4-digit year. Deliberately anchored on 19xx/20xx so an arXiv id like
# 2503.16431 — which is a paper number, not a year — is never read as a date.
_YEAR_RE = re.compile(r"\b((?:19|20)\d{2})\b")
# "23 Jul 2026" / "23-24 Jul 2026" — how the legend states the build date.
_DAY_MONTH_YEAR_RE = re.compile(
    r"\b(\d{1,2})(?:\s*[-–]\s*\d{1,2})?\s+"
    r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+((?:19|20)\d{2})\b",
    re.IGNORECASE,
)

#: How the legend labels the two exclusion classes. Matched as substrings against
#: the legend's own text, so the org lists are read from the sheet rather than
#: pinned in code — if someone adds a fifth non-buyer, the bot knows.
_NON_BUYER_MARKERS = ("non-buyer", "non buyer", "not a buyer", "competitor", "channel")
_BUDGET_MARKERS = ("budget-gate", "budget gate", "fails budget", "pilot-budget",
                   "pilot budget")

FLAG_NON_BUYER = "non_buyer"
FLAG_BUDGET_GATE = "budget_gate"
FLAG_LABELS = {
    FLAG_NON_BUYER: "NOT a pitch target — competitor or channel partner",
    FLAG_BUDGET_GATE: "NOT a pitch target — fails the pilot-budget gate",
}


def _clean(text) -> str:
    """A cell as the sheet meant it. The Sheets API hands back the display value,
    but the workbook is full of escaped markdown (``\\-``, ``\\~``, ``\\#``) from
    how it was authored, and those backslashes would be quoted verbatim into a
    Discord answer."""
    s = str(text or "").strip()
    if not s:
        return ""
    return re.sub(r"\\([\\\-~#*_|\[\]()>])", r"\1", s)


def _split_outside_parens(text: str, sep: str = ",") -> list[str]:
    """Split on `sep`, but not inside brackets.

    The DEPARTURES cell is one long comma-separated list whose entries carry
    parenthesised moves that CONTAIN commas — "Barret Zoph + Luke Metz (Thinking
    Machines->OpenAI, Jan 2026)". A naive split turns one departure into two
    fragments, one of which is a date, and the bot starts reporting "Jan 2026" as
    a person who has left.
    """
    out, buf, depth = [], [], 0
    for ch in text:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth = max(0, depth - 1)
        if ch == sep and depth == 0:
            out.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    if buf:
        out.append("".join(buf).strip())
    return [p for p in out if p]


def parse_tier(value) -> str:
    """"T1"/"T2"/"T3", or "" when the cell doesn't hold a tier.

    Returns "" rather than guessing: the live sheet has a row whose Tier cell
    holds a confidence phrase (the columns slipped by one), and inventing a tier
    for it would put an unverified person in a pitch-ready answer.
    """
    m = _TIER_RE.search(_clean(value))
    return f"T{m.group(1)}" if m else ""


def parse_lanes(value) -> list[str]:
    """"a, b" → ["a", "b"]. Deduplicated, ordered, lowercase."""
    seen, out = set(), []
    for m in _LANE_RE.finditer(_clean(value)):
        lane = m.group(1).lower()
        if lane not in seen:
            seen.add(lane)
            out.append(lane)
    return out


def _latest_dated(text: str) -> tuple[Optional[date], str]:
    """The NEWEST date mentioned in a blob of text, with HOW PRECISE it is.

    Returns (date, precision) where precision is "day", "month", "year" or ""
    (nothing found). The precision is not decoration: a staleness caveat driven
    by a bare "2026" in a citation is a caveat driven by a guess, and the caller
    has to be able to tell the two apart. A bare year is returned as 1 January —
    a placeholder the caller is expected to resolve, not to measure from.

    Newest rather than oldest on purpose: an evidence cell lists several sources,
    and what matters is the most recent one, not the oldest one cited.

    Day- and month-precise dates are taken over year-precise ones even when the
    bare year is later, because "Mar 2025" plus a stray "2026" in a paper title
    is a March-2025 verification, not a 2026 one.
    """
    text = _clean(text)
    if not text:
        return None, ""
    best: Optional[date] = None

    def _keep(d: date) -> None:
        nonlocal best
        if best is None or d > best:
            best = d

    for m in _DAY_MONTH_YEAR_RE.finditer(text):
        try:
            _keep(date(int(m.group(3)), _MONTHS[m.group(2)[:3].lower()], int(m.group(1))))
        except ValueError:
            continue
    if best is not None:
        return best, "day"

    for m in _MONTH_YEAR_RE.finditer(text):
        try:
            _keep(date(int(m.group(2)), _MONTHS[m.group(1)[:3].lower()], 1))
        except ValueError:
            continue
    if best is not None:
        return best, "month"

    for m in _YEAR_RE.finditer(text):
        try:
            _keep(date(int(m.group(1)), 1, 1))
        except ValueError:
            continue
    return (best, "year") if best is not None else (None, "")


def _latest_date(text: str) -> Optional[date]:
    """`_latest_dated` without the precision, for callers that don't need it."""
    return _latest_dated(text)[0]


class Legend:
    """The "Mapping _Legend" tab, loaded as RULES the bot has to apply.

    Everything here is read from the sheet. The `_FALLBACK_*` constants stand in
    only for a row that could not be parsed, so a reworded legend degrades to the
    last-known wording rather than to silence — but a legend that parses always
    wins over them.
    """

    def __init__(self, pairs: list[tuple[str, str]]) -> None:
        #: Every (key, value) as read, in sheet order. Kept whole so a question
        #: about a legend row this code doesn't model ("what's the methodology?")
        #: is still answerable.
        self.pairs = pairs
        self._by_key = {normalise_header(k): v for k, v in pairs if k}
        #: Rows with no key are continuation lines under the previous key —
        #: that's how "Honesty rules" and "Known gaps" are laid out.
        self.loose = [v for k, v in pairs if not k and v]

        self.built: Optional[date] = self._parse_built()
        self.refresh_weeks: int = self._parse_refresh_weeks()
        self.lanes: dict[str, str] = self._parse_lanes()
        self.tiers: dict[str, str] = self._parse_tiers()
        self.confidence_rule: str = self._find("confidence") or (
            "Reflects evidence quality for the CURRENT role and lane fit; "
            "independent of Tier."
        )
        self.scope: str = self._find("scope")
        self.methodology: str = self._find("methodology")
        self.refresh_rule: str = self._find("refresh")
        self.honesty_rules: list[str] = self._section("honesty")
        self.known_gaps: list[str] = self._section("known gaps", "gaps")
        self.non_buyers: dict[str, str] = {}
        self.budget_gate: dict[str, str] = {}
        self._parse_flag_lists()

    # -- lookups ----------------------------------------------------------

    def _find(self, *keys: str) -> str:
        for key in keys:
            want = normalise_header(key)
            for k, v in self._by_key.items():
                if k == want or want in k:
                    return v
        return ""

    def _section(self, *keys: str) -> list[str]:
        """The bullet lines that follow a heading row.

        "Honesty rules" and "Known gaps" are a heading with an empty value,
        followed by rows whose key cell is blank and whose value is one bullet.
        So the section is "every keyless row until the next keyed one".
        """
        wanted = [normalise_header(k) for k in keys]
        out: list[str] = []
        collecting = False
        for k, v in self.pairs:
            key = normalise_header(k)
            if key:
                collecting = any(w == key or w in key for w in wanted)
                # A heading row can carry its first bullet in the value cell.
                if collecting and v:
                    out.append(v)
                continue
            if collecting and v:
                out.append(v)
        return out

    # -- parsing ----------------------------------------------------------

    def _parse_built(self) -> Optional[date]:
        raw = self._find("built", "build date", "created")
        found = _latest_date(raw)
        if found:
            return found
        # The build date also appears inside the methodology line ("21 parallel
        # research passes, 23-24 Jul 2026"), which is worth falling back to
        # because every staleness caveat is measured from it.
        return _latest_date(self._find("methodology"))

    def _parse_refresh_weeks(self) -> int:
        m = _WEEKS_RE.search(self._find("refresh", "refresh rule", "staleness"))
        if m:
            try:
                weeks = int(m.group(1))
                if 1 <= weeks <= 104:
                    return weeks
            except ValueError:
                pass
        log.info(
            "[mapping] the legend's refresh rule didn't parse; using the last known "
            "%d-week rule. Answers still carry the caveat, just on the default clock.",
            _FALLBACK_REFRESH_WEEKS,
        )
        return _FALLBACK_REFRESH_WEEKS

    def _parse_lanes(self) -> dict[str, str]:
        """"a = model evals/benchmarks  b = post-training…" → {a: …, b: …}.

        The lanes sit in ONE cell separated by whitespace, so the split is on the
        "<letter> =" markers themselves rather than on any delimiter.
        """
        raw = self._find("icp lanes", "lanes", "icp lane")
        if not raw:
            return dict(_FALLBACK_LANES)
        marks = list(re.finditer(r"\b([a-d])\s*[=:]\s*", raw, re.IGNORECASE))
        if not marks:
            return dict(_FALLBACK_LANES)
        out: dict[str, str] = {}
        for i, m in enumerate(marks):
            end = marks[i + 1].start() if i + 1 < len(marks) else len(raw)
            text = raw[m.end():end].strip(" \t;,.")
            if text:
                out[m.group(1).lower()] = text
        return out or dict(_FALLBACK_LANES)

    def _parse_tiers(self) -> dict[str, str]:
        raw = self._find("tiers", "tier")
        if not raw:
            return dict(_FALLBACK_TIERS)
        marks = list(re.finditer(r"\b(T[123])\s*[=:]\s*", raw, re.IGNORECASE))
        if not marks:
            return dict(_FALLBACK_TIERS)
        out: dict[str, str] = {}
        for i, m in enumerate(marks):
            end = marks[i + 1].start() if i + 1 < len(marks) else len(raw)
            text = raw[m.end():end].strip(" \t;,.")
            if text:
                out[m.group(1).upper()] = text
        return out or dict(_FALLBACK_TIERS)

    def _parse_flag_lists(self) -> None:
        """The legend's own exclusion lists → {org: why}.

        Read from the "Known gaps" bullets ("Non-buyers flagged: Neysa (sells
        red-teaming — competitor/channel), …"). Reading them from the sheet
        rather than pinning the names in code is the point: a fifth non-buyer
        added to the legend is enforced without a code change.
        """
        for line in self.known_gaps:
            low = line.lower()
            is_non_buyer = any(m in low for m in _NON_BUYER_MARKERS)
            is_budget = any(m in low for m in _BUDGET_MARKERS)
            if not (is_non_buyer or is_budget):
                continue
            body = line.split(":", 1)[1] if ":" in line else line
            target = self.non_buyers if is_non_buyer else self.budget_gate
            # The bullet itself is the reason for every org in it, and is used
            # when an entry carries no reason of its own. Stripped of its list
            # marker so the quoted reason doesn't start with a stray dash.
            shared = line.lstrip("-–— ").strip()
            for part in _split_outside_parens(body):
                org, why = self._org_from_list_entry(part)
                if org:
                    # Always lead with the org name. Several orgs share one
                    # bullet, and a reason string that didn't name which org it
                    # was about turned the exclusion list in a tool result into
                    # the same sentence repeated three times.
                    target[normalise_header(org)] = f"{org}: {why or shared}"

    @staticmethod
    def _org_from_list_entry(part: str) -> tuple[str, str]:
        """One entry of a legend list → (org name, the reason beside it).

        These bullets are prose, not data — "Nudge, ROAST, Studdy, and likely
        Indyx - mapped for completeness, not for pitching" — so the org name is
        the LEADING run of words, and the rest is commentary. Two rules keep
        commentary out of the org list:

          - the name ends at the first " - ", " — ", ":" or "(" ("likely Indyx -
            mapped for completeness" is the org Indyx, not a 5-word name), and
          - an entry that doesn't START like a proper noun is not an org. That is
            what stops the trailing fragment "not for pitching" from being
            recorded as a company nobody may pitch — which read as a real
            exclusion in the tool output.
        """
        text = _clean(part).strip()
        if not text:
            return "", ""
        # Strip list glue, but keep whatever follows: "and likely Indyx" → "Indyx".
        text = re.sub(r"^(?:and\s+|&\s+|likely\s+)+", "", text, flags=re.IGNORECASE)

        paren = ""
        m = re.search(r"\((.*)\)", text)
        if m:
            paren = m.group(1).strip()
            text = text[: m.start()].strip()

        # The name stops at the first commentary separator.
        name = re.split(r"\s+[-–—]\s+|:\s+", text, maxsplit=1)[0].strip(" .;,")
        if not name or len(name) > 40 or len(name.split()) > 4:
            return "", ""
        if not name[0].isupper() and not name[0].isdigit():
            return "", ""
        why = paren or (text[len(name):].strip(" -–—:;.,") if len(text) > len(name) else "")
        return name, why

    # -- the rules, as the bot must state them ----------------------------

    def stale_after_days(self) -> int:
        return max(1, self.refresh_weeks) * 7

    def describe(self) -> dict:
        """The legend as structured data for a tool result."""
        return {
            "built": self.built.isoformat() if self.built else "",
            "refresh_rule": self.refresh_rule,
            "refresh_weeks": self.refresh_weeks,
            "icp_lanes": self.lanes,
            "tiers": self.tiers,
            "confidence_rule": self.confidence_rule,
            "tier_and_confidence_are_independent": True,
            "scope": self.scope,
            "methodology": self.methodology,
            "honesty_rules": self.honesty_rules,
            "known_gaps": self.known_gaps,
            "non_buyers": sorted(self.non_buyers.values()),
            "budget_gate_failures": sorted(self.budget_gate.values()),
        }


class Departures:
    """The DEPARTURES record from the Edge Map — the do-not-pitch list.

    One cell holds every departure as "Name (Old->New)" entries. A person on this
    list has left the org the Researcher Mapping still lists them under, so
    recommending them sends someone to a company where they no longer work.
    """

    def __init__(self, raw: str = "", note: str = "") -> None:
        self.raw = _clean(raw)
        self.note = _clean(note)
        #: normalised name -> {"name", "move", "from", "to"}
        self.people: dict[str, dict] = {}
        self._parse()

    def _parse(self) -> None:
        for entry in _split_outside_parens(self.raw):
            m = re.match(r"^\s*([^(]+?)\s*(?:\((.*)\))?\s*$", entry)
            if not m:
                continue
            names_part, move = m.group(1).strip(), _clean(m.group(2) or "")
            if not names_part:
                continue
            frm, to = "", ""
            if move:
                # "Flipkart->Microsoft" / "left Cosmos" / "Thinking Machines->OpenAI, Jan 2026"
                arrow = re.split(r"\s*(?:->|→|>)\s*", move.split(",")[0], maxsplit=1)
                if len(arrow) == 2:
                    frm, to = arrow[0].strip(), arrow[1].strip()
                elif move.lower().startswith("left "):
                    frm = move[5:].strip()
            # "Barret Zoph + Luke Metz" is two people who left together.
            for name in re.split(r"\s+\+\s+|\s+and\s+", names_part):
                name = name.strip(" .;")
                if not name or len(name) > 60:
                    continue
                self.people[normalise_header(name)] = {
                    "name": name, "move": move, "from": frm, "to": to,
                }

    def lookup(self, name: str) -> Optional[dict]:
        """The departure record for `name`, or None.

        Matched on the normalised full name first, then on a both-ways substring
        so "Mayur Datar" is found from "Datar, Mayur" and from a row that carries
        a middle name. Anything shorter than a full name is not substring-matched
        — "Li" must not depose every Li in the sheet.
        """
        want = normalise_header(name)
        if not want:
            return None
        hit = self.people.get(want)
        if hit:
            return hit
        if len(want) < 6 or " " not in want:
            return None
        for key, rec in self.people.items():
            if want in key or key in want:
                return rec
        return None

    def all(self) -> list[dict]:
        return list(self.people.values())


class Tab:
    """One parsed worksheet of the mapping workbook.

    Deliberately narrower than gtm_sheet.Tab: there is no column_letter() and no
    header_row, because those exist to ADDRESS A CELL FOR WRITING and nothing in
    this module may write.
    """

    def __init__(self, *, title: str, kind: str, headers: list[str],
                 role_to_col: dict[str, int], rows: list[dict], read_at: float):
        self.title = title
        self.kind = kind
        self.headers = headers
        self.role_to_col = role_to_col
        self.rows = rows
        self.read_at = read_at

    @property
    def age_seconds(self) -> float:
        return max(0.0, time.time() - self.read_at)

    def schema_line(self) -> str:
        mapped = ", ".join(sorted(self.role_to_col)) or "(none)"
        return (
            f"tab {self.title!r} kind={self.kind} rows={len(self.rows)} "
            f"cols={len(self.headers)} mapped=[{mapped}]"
        )


class MappingSheet:
    """Live READ-ONLY access to the researcher/buyer mapping spreadsheet.

    There is no write method on this class, and there never should be. If a
    future feature needs to record something about a researcher, it belongs in
    SQLite or in the sandbox copy of the GTM Playbook — not here.
    """

    def __init__(self) -> None:
        self._client = None
        self._lock = threading.RLock()
        #: kind -> [Tab]. The LAST GOOD read, kept indefinitely so an outage is
        #: answered from cache with a staleness note rather than with silence.
        self._cache: dict[str, list[Tab]] = {}
        self._access: dict = {}
        self._schema_logged: set[str] = set()
        self._legend: Optional[Legend] = None
        self._departures: Optional[Departures] = None

    # -- identity ----------------------------------------------------------

    @property
    def service_account_email(self) -> str:
        path = config.GOOGLE_SERVICE_ACCOUNT_JSON
        if not path:
            return ""
        try:
            import json

            with open(path, "r", encoding="utf-8") as f:
                return str(json.load(f).get("client_email") or "")
        except Exception:
            log.debug("[mapping] could not read client_email from the key file", exc_info=True)
            return ""

    def share_instruction(self) -> str:
        """The exact fix for a permission error. VIEWER, never Editor: this sheet
        is read-only by design and asking for Editor on it would undo the whole
        guarantee at the sharing dialog."""
        email = self.service_account_email or "the bot's service account"
        # ASCII only — this string is logged, and a Windows console on cp1252
        # raises UnicodeEncodeError on arrows and en-dashes.
        return (
            f'Share "membrane.social - Researcher Buyer Mapping" with {email} as '
            f"VIEWER (open the sheet, click Share, paste the address, set Viewer, "
            f"Send). Viewer is correct and sufficient - the bot never writes to "
            f"this sheet."
        )

    @property
    def sheet_id(self) -> str:
        return config.GTM_MAPPING_SHEET_ID

    # -- auth --------------------------------------------------------------

    def _get_client(self):
        """The authorised gspread client, built once, with the READ-ONLY scope.

        Note this is a SEPARATE client from gtm_sheet's: same service-account
        key, narrower token. Sharing gtm_sheet's read/write client here would
        work perfectly well and would silently delete lock #2.
        """
        with self._lock:
            if self._client is not None:
                return self._client
            if gspread is None:
                raise SheetAccessError(
                    f"the Google Sheets libraries aren't installed ({_IMPORT_ERROR})",
                    remedy="Run: pip install gspread google-auth",
                )
            path = config.GOOGLE_SERVICE_ACCOUNT_JSON
            if not path:
                raise SheetAccessError(
                    "no service-account key is configured",
                    remedy="Set GOOGLE_SERVICE_ACCOUNT_JSON to the key file path.",
                )
            try:
                creds = Credentials.from_service_account_file(path, scopes=SCOPES)
                self._client = gspread.authorize(creds)
            except FileNotFoundError:
                raise SheetAccessError(
                    f"the service-account key file {path!r} does not exist",
                    remedy=f"Put the key at {path}, or fix GOOGLE_SERVICE_ACCOUNT_JSON.",
                )
            except Exception as e:
                raise SheetAccessError(
                    f"the service-account key could not be loaded ({type(e).__name__})",
                    remedy=f"Check that {path!r} is a valid service-account JSON key.",
                )
            return self._client

    def _open(self):
        client = self._get_client()
        sid = self.sheet_id
        if not sid:
            raise SheetAccessError(
                "no spreadsheet id is configured for the researcher mapping",
                remedy="Set GTM_MAPPING_SHEET_ID.",
            )
        try:
            return client.open_by_key(sid)
        except Exception as e:
            raise self._translate(e, sid)

    def _translate(self, e: Exception, sid: str) -> SheetAccessError:
        """API exception → an actionable SheetAccessError.

        Simpler than gtm_sheet's because there is no write side to distinguish:
        a 403 here can only ever mean "not shared".
        """
        name = type(e).__name__
        text = str(e) or ""
        status = getattr(getattr(e, "response", None), "status_code", None)

        if name == "PermissionError" or status == 403 or "PERMISSION_DENIED" in text:
            if ("has not been used" in text or "SERVICE_DISABLED" in text
                    or "accessNotConfigured" in text):
                return SheetAccessError(
                    "the Google Sheets API is not enabled for this service account's project",
                    remedy=(
                        "Enable the Google Sheets API in the Google Cloud console for the "
                        "key's project, then retry."
                    ),
                )
            return SheetAccessError(
                "the service account cannot open the researcher mapping sheet "
                "(permission denied)",
                remedy=self.share_instruction(),
            )
        if name == "SpreadsheetNotFound" or status == 404:
            return SheetAccessError(
                f"the researcher mapping spreadsheet id {sid!r} was not found",
                remedy=(
                    "Check GTM_MAPPING_SHEET_ID. It is the long id in the sheet's URL "
                    "between /d/ and /edit. " + self.share_instruction()
                ),
            )
        if status == 429 or "RESOURCE_EXHAUSTED" in text or "Quota exceeded" in text:
            return SheetAccessError(
                "the Sheets API quota is exhausted",
                remedy="Wait for the quota window to reset; reads are cached meanwhile.",
                transient=True,
            )
        if status and 500 <= int(status) < 600:
            return SheetAccessError(
                f"the Sheets API returned a server error ({status})",
                remedy="Transient on Google's side; the last cached read is used meanwhile.",
                transient=True,
            )
        if name in ("APIError", "ConnectionError", "Timeout", "ReadTimeout", "SSLError"):
            return SheetAccessError(
                f"the Sheets API call failed ({name})",
                remedy="Usually transient; the last cached read is used meanwhile.",
                transient=True,
            )
        return SheetAccessError(
            f"the researcher mapping sheet could not be opened ({name})",
            remedy=self.share_instruction(),
        )

    # -- access check (startup) -------------------------------------------

    def check_access(self) -> dict:
        """Probe the spreadsheet. Never raises.

        Returns {ok, title, tabs, error, remedy}. On failure the share
        instruction is logged at ERROR, exactly as the GTM sheets do, so a
        missing share is one line to fix rather than a debugging session.
        """
        try:
            sh = self._open()
            titles = [ws.title for ws in sh.worksheets()]
            out = {"ok": True, "title": sh.title, "tabs": titles, "error": "", "remedy": ""}
            log.info(
                "[mapping] sheet OK (READ-ONLY): %r with %d tab(s): %s",
                sh.title, len(titles), ", ".join(titles),
            )
        except SheetAccessError as e:
            out = {"ok": False, "title": "", "tabs": [], "error": str(e), "remedy": e.remedy}
            log.error("[mapping] sheet UNAVAILABLE: %s", e)
            if e.remedy:
                log.error("[mapping] FIX: %s", e.remedy)
        except Exception:
            out = {"ok": False, "title": "", "tabs": [],
                   "error": "unexpected error", "remedy": ""}
            log.exception("[mapping] access check raised unexpectedly")
        with self._lock:
            self._access = out
        return out

    def last_access(self) -> dict:
        with self._lock:
            return dict(self._access)

    # -- schema discovery --------------------------------------------------

    def _map_headers(self, kind: str, headers: list[str]) -> dict[str, int]:
        """Discovered headers → {role: column index}, same contract as
        gtm_sheet._map_headers: exact matches across all roles first, substrings
        second, longest alias first, one header to one role."""
        aliases = ROLES.get(kind, {})
        norm = [normalise_header(h) for h in headers]
        taken: set[int] = set()
        out: dict[str, int] = {}

        override = (config.GTM_MAPPING_COLUMN_MAP or {}).get(kind, {})
        for role, header_text in (override or {}).items():
            key = normalise_header(str(header_text))
            for i, h in enumerate(norm):
                if h == key and i not in taken:
                    out[role] = i
                    taken.add(i)
                    log.info("[mapping] GTM_MAPPING_COLUMN_MAP: %s.%s -> %r",
                             kind, role, headers[i])
                    break
            else:
                log.warning(
                    "[mapping] GTM_MAPPING_COLUMN_MAP names %r for %s.%s but no such "
                    "header exists in this tab; auto-detecting that role instead.",
                    header_text, kind, role,
                )

        for exact_pass in (True, False):
            for role, role_aliases in aliases.items():
                if role in out:
                    continue
                for alias in sorted(role_aliases, key=len, reverse=True):
                    key = normalise_header(alias)
                    if not key:
                        continue
                    for i, h in enumerate(norm):
                        if i in taken or not h:
                            continue
                        hit = (h == key) if exact_pass else (key in h or h in key)
                        if hit:
                            out[role] = i
                            taken.add(i)
                            break
                    if role in out:
                        break
        return out

    def _matches_kind(self, kind: str, headers: list[str]) -> int:
        spec = next((s for s in _KIND_SIGNATURES if s[0] == kind), None)
        if spec is None:
            return 0
        _k, needed, threshold, mandatory = spec
        mapped = self._map_headers(kind, headers)
        if any(role not in mapped for role in mandatory):
            return 0
        score = sum(1 for r in needed if r in mapped)
        return score if score >= threshold else 0

    def _detect_kind(self, title: str, headers: list[str]) -> Optional[str]:
        """What kind of tab this is. Name hints only win when the HEADERS agree,
        so a renamed tab is still found and a similarly-named one isn't
        mistaken."""
        low = normalise_header(title)
        for kind, hints in _KIND_NAME_HINTS:
            if any(h in low for h in hints) and self._matches_kind(kind, headers):
                return kind
        best, best_score = None, 0
        for kind, _needed, _threshold, _mandatory in _KIND_SIGNATURES:
            # The legend has no real headers, so it can only ever be identified
            # by name — letting it win a header-signature contest would make
            # every two-column tab in the workbook a legend.
            if kind == LEGEND:
                continue
            score = self._matches_kind(kind, headers)
            if score > best_score:
                best, best_score = kind, score
        return best

    @staticmethod
    def _header_row_index(values: list[list]) -> int:
        for i, row in enumerate(values[:10]):
            filled = [c for c in row if str(c).strip()]
            if len(filled) >= 2:
                return i
        return 0

    @staticmethod
    def _a1_sheet(title: str) -> str:
        """A worksheet title as an A1 range. Single quotes are doubled — the tab
        named "Mapping _Legend" is fine, but the workbook is edited by hand and
        an apostrophe in a future tab name would otherwise fail the whole batch
        request."""
        return "'" + str(title).replace("'", "''") + "'"

    def _batch_values(self, sh, titles: list[str]) -> dict[str, list[list]]:
        """Every worksheet in ONE API call. Same quota reasoning as
        gtm_sheet._batch_values: this workbook has six-plus tabs and the read
        quota is per-minute across ALL THREE sheets, so a per-tab read here
        would starve the playbook."""
        try:
            resp = sh.values_batch_get([self._a1_sheet(t) for t in titles])
        except Exception:
            log.warning(
                "[mapping] batched read failed; falling back to one request per tab "
                "(this burns quota)", exc_info=True,
            )
            out: dict[str, list[list]] = {}
            for ws in sh.worksheets():
                try:
                    out[ws.title] = ws.get_all_values()
                except Exception:
                    log.exception("[mapping] could not read tab %r", ws.title)
            return out
        ranges = resp.get("valueRanges") or []
        return {t: (vr.get("values") or []) for t, vr in zip(titles, ranges)}

    def _parse_legend_tab(self, title: str, values: list[list], *, read_at: float) -> Tab:
        """The legend is a key/value sheet, not a table, so it is parsed by
        POSITION (first non-empty cell is the key, the next is the value) rather
        than by header. Keyless rows are kept: they are the bullets under
        "Honesty rules" and "Known gaps", which is where the exclusion lists
        live."""
        rows: list[dict] = []
        for r, raw in enumerate(values, start=1):
            cells = [_clean(c) for c in raw]
            if not any(cells):
                continue
            key = cells[0] if cells else ""
            value = next((c for c in cells[1:] if c), "")
            rows.append({"_row": r, "key": key, "value": value, "_extra": {}})
        return Tab(title=title, kind=LEGEND, headers=["key", "value"],
                   role_to_col={"key": 0, "value": 1}, rows=rows, read_at=read_at)

    def _parse_values(self, title: str, values: list[list], *,
                      read_at: float) -> Optional[Tab]:
        if not values:
            return None

        low = normalise_header(title)
        if any(h in low for h in ("legend", "readme")):
            return self._parse_legend_tab(title, values, read_at=read_at)

        hidx = self._header_row_index(values)
        headers = [_clean(h) for h in values[hidx]]
        kind = self._detect_kind(title, headers)
        if kind is None:
            log.debug("[mapping] tab %r doesn't match a known kind; skipping", title)
            return None

        role_to_col = self._map_headers(kind, headers)
        col_to_role = {v: k for k, v in role_to_col.items()}

        rows: list[dict] = []
        for r, raw in enumerate(values[hidx + 1:], start=hidx + 2):
            cells = [_clean(c) for c in raw]
            if not any(cells):
                continue
            row: dict = {"_row": r, "_extra": {}}
            for i, header in enumerate(headers):
                value = cells[i] if i < len(cells) else ""
                role = col_to_role.get(i)
                if role:
                    row[role] = value
                elif header:
                    row["_extra"][header] = value
            # A researcher row with no person, or a coverage row with no org, is
            # a spacer or a totals line — counting it would inflate every answer.
            if kind == RESEARCHERS and not (row.get("researcher") or "").strip():
                continue
            if kind == ORG_COVERAGE and not (row.get("company") or "").strip():
                continue
            rows.append(row)

        return Tab(title=title, kind=kind, headers=headers,
                   role_to_col=role_to_col, rows=rows, read_at=read_at)

    # -- reads -------------------------------------------------------------

    def read(self, *, force: bool = False) -> dict[str, Tab]:
        """Every recognised tab as {kind: Tab}, cached for SHEET_CACHE_SECONDS.

        Same failure contract as gtm_sheet.read: on a failed read the last good
        copy is returned (check `Tab.age_seconds` and say so), and
        SheetAccessError is raised only when there is no cache to fall back on.
        """
        now = time.time()
        with self._lock:
            cached = {k: v[0] for k, v in self._cache.items() if v}
            if cached and not force:
                freshest = min(t.read_at for t in cached.values())
                if now - freshest < max(1, config.SHEET_CACHE_SECONDS):
                    return cached

        try:
            sh = self._open()
            titles = [ws.title for ws in sh.worksheets()]
            by_title = self._batch_values(sh, titles)
            read_at = time.time()
            groups: dict[str, list[Tab]] = {}
            unrecognised: list[str] = []
            for title in titles:
                try:
                    tab = self._parse_values(title, by_title.get(title) or [], read_at=read_at)
                except Exception:
                    log.exception("[mapping] tab %r failed to parse; skipping it", title)
                    continue
                if tab is None:
                    unrecognised.append(title)
                    continue
                groups.setdefault(tab.kind, []).append(tab)
                if tab.title not in self._schema_logged:
                    log.info("[mapping] %s", tab.schema_line())
                    self._schema_logged.add(tab.title)

            for kind, found in groups.items():
                found.sort(key=lambda t: len(t.rows), reverse=True)

            with self._lock:
                for kind, found in groups.items():
                    self._cache[kind] = found
                # The legend and the departures are DERIVED state — rebuild them
                # from the read that just happened, not from whatever was parsed
                # last time, or a corrected legend would never take effect.
                self._legend = None
                self._departures = None

            if unrecognised and "_unrecognised" not in self._schema_logged:
                log.info(
                    "[mapping] %d tab(s) matched no known kind and are ignored: %s. "
                    "Set GTM_MAPPING_COLUMN_MAP if one of these should be read.",
                    len(unrecognised), ", ".join(repr(t) for t in unrecognised[:25]),
                )
                self._schema_logged.add("_unrecognised")

            log.info(
                "[mapping] read: %s",
                ", ".join(f"{k}={sum(len(t.rows) for t in v)} rows" for k, v in groups.items())
                or "no known tabs",
            )
            return {k: v[0] for k, v in groups.items()}

        except SheetAccessError as e:
            with self._lock:
                cached = {k: v[0] for k, v in self._cache.items() if v}
            if cached:
                log.warning(
                    "[mapping] read failed (%s); answering from the cached copy (%.0fs old)",
                    e, max(t.age_seconds for t in cached.values()),
                )
                return cached
            log.error("[mapping] read failed with no cache to fall back to: %s", e)
            raise

    def tab(self, kind: str) -> Optional[Tab]:
        return self.read().get(kind)

    def staleness_note(self, tab: Tab) -> str:
        """The API-staleness note — "this came from cache after a failed read".

        Not to be confused with the sheet's OWN staleness rule (`row_staleness`),
        which is about how old the research is. Both can apply at once and they
        mean different things.
        """
        age = tab.age_seconds
        if age <= max(1, config.SHEET_CACHE_SECONDS) * 2:
            return ""
        mins = int(age // 60)
        when = f"{max(1, mins)} minute(s) ago" if mins < 60 else f"{mins // 60} hour(s) ago"
        return (
            f"(read from the mapping sheet {when} — I couldn't reach the Sheets API "
            "just now, so this may be out of date)"
        )

    # -- the legend, and the rules it carries ------------------------------

    def legend(self) -> Legend:
        """The parsed legend. Built once per read and cached alongside the tabs.

        Returns an EMPTY legend (fallback lanes/tiers, default refresh rule)
        rather than None when the tab can't be read, so every caller can apply
        the rules unconditionally instead of guarding each one.
        """
        with self._lock:
            if self._legend is not None:
                return self._legend
        tab = self.tab(LEGEND)
        pairs = [(r.get("key", ""), r.get("value", "")) for r in tab.rows] if tab else []
        legend = Legend(pairs)
        with self._lock:
            self._legend = legend
        if not pairs:
            log.warning(
                "[mapping] no legend tab was readable — falling back to the last known "
                "lanes, tiers and %d-week refresh rule. Recommendations still carry "
                "their caveats.", legend.refresh_weeks,
            )
        return legend

    def departures(self) -> Departures:
        """The do-not-pitch list from the Edge Map.

        Returns an EMPTY Departures rather than None when the Edge Map is
        unreadable — but callers must check `departures_known()` before implying
        someone is safe, because "nobody is on the list" and "I couldn't read the
        list" are opposite statements about a person.
        """
        with self._lock:
            if self._departures is not None:
                return self._departures
        tab = self.tab(EDGES)
        found = Departures()
        if tab:
            for row in tab.rows:
                if "departure" in normalise_header(row.get("edge", "")):
                    found = Departures(row.get("connects", ""), row.get("why", ""))
                    break
            else:
                log.warning(
                    "[mapping] the Edge Map has no DEPARTURES row — the departure check "
                    "cannot run, and answers must say so rather than implying everyone "
                    "is current."
                )
        with self._lock:
            self._departures = found
        return found

    def departures_known(self) -> bool:
        """True when the departures list was actually read. False means the check
        could NOT run, which an answer has to state — silence would read as "not
        departed"."""
        return bool(self.departures().people)

    # -- the sheet's rules, applied ----------------------------------------

    def row_staleness(self, row: dict, *, today: Optional[date] = None) -> dict:
        """Apply the sheet's OWN refresh rule to one researcher row.

        Two clocks, because they answer different questions and either alone is
        misleading:

          research_age    how long since the sheet's research pass (the legend's
                          Built date). This is the rule the legend literally
                          states, and it ticks for every row at once.
          verified_age    how long since the ROLE ITSELF was verified, read from
                          the row's own "Role Verified Via" cell. A role verified
                          from Mar-2025 press was already past the refresh window
                          on the day the sheet was built, and the whole-sheet
                          clock would never say so.

        Stale if EITHER exceeds the legend's threshold.

        WHY ONLY "Role Verified Via" FEEDS THE SECOND CLOCK. The Key Evidence
        column dates PAPERS, not employment. A researcher whose newest listed
        paper is from 2024 has not thereby gone stale — the legend's rule is
        about ROLES going stale in a fast-moving market. Measuring role currency
        off publication dates put a re-verify caveat on nearly every row and made
        the caveat meaningless, which is worse than not having it. The newest
        cited evidence is still reported, as information, without triggering.

        PRECISION MATTERS. A verification citing a bare year is only year-precise.
        If that year is the year the sheet was built, it says nothing about being
        older than the pass, so the research-pass date is used; an earlier year is
        read as its LAST day, which is the most generous reading that still
        catches a genuinely old citation. Only a month- or day-precise date is
        measured literally.

        The threshold is read from the legend and measured against `today`.
        Nothing here is pinned to a date, so the caveat starts appearing on its
        own as the sheet ages past its own refresh rule.
        """
        legend = self.legend()
        today = today or date.today()
        limit = legend.stale_after_days()

        research_age = (today - legend.built).days if legend.built else None

        verified, precision = _latest_dated(row.get("role_verified_via", ""))
        if verified is not None and precision == "year":
            if legend.built and verified.year >= legend.built.year:
                # Same year as the research pass: no evidence it predates it.
                verified = legend.built
            else:
                verified = date(verified.year, 12, 31)
        # A verification can't be newer than the pass that recorded it.
        if verified and legend.built and verified > legend.built:
            verified = legend.built
        verified_age = (today - verified).days if verified else None

        # Reported at the precision it was actually written at: a bare "2025" in
        # a citation is the year 2025, and printing it as "2025-01-01" would
        # invent a day the sheet never claimed.
        cited, cited_precision = _latest_dated(row.get("key_evidence", ""))
        cited_text = ""
        if cited:
            cited_text = (
                str(cited.year) if cited_precision == "year"
                else cited.strftime("%b %Y") if cited_precision == "month"
                else cited.isoformat()
            )

        reasons = []
        if research_age is not None and research_age > limit:
            reasons.append(
                f"the sheet's research pass is {research_age} days old "
                f"(built {legend.built.isoformat()}), past its own "
                f"~{legend.refresh_weeks}-week refresh rule"
            )
        if verified_age is not None and verified_age > limit:
            reasons.append(
                f"this row's role was last verified against evidence from "
                f"{verified.isoformat()} ({verified_age} days ago)"
            )
        stale = bool(reasons)
        return {
            "stale": stale,
            "as_of": today.isoformat(),
            "refresh_weeks": legend.refresh_weeks,
            "stale_after_days": limit,
            "sheet_built": legend.built.isoformat() if legend.built else "",
            "research_age_days": research_age,
            "role_verified_date": verified.isoformat() if verified else "",
            "role_verified_precision": precision,
            "role_verified_age_days": verified_age,
            # Reported, never a trigger — see the docstring.
            "newest_evidence_cited": cited_text,
            "reasons": reasons,
            "caveat": (
                "RE-VERIFY ROLE BEFORE OUTREACH — " + "; ".join(reasons) + "."
                if stale else ""
            ),
        }

    def org_flags(self, company: str) -> list[dict]:
        """The exclusion flags on an org: non-buyer, budget-gate, or neither.

        Read from the legend's own lists first (authoritative — the legend names
        them), then from the Org Coverage account note, which is where a flag
        added to one account but not to the legend would show up.
        """
        legend = self.legend()
        key = normalise_header(company)
        if not key:
            return []
        out: list[dict] = []

        def _match(table: dict[str, str]) -> str:
            for org_key, why in table.items():
                if org_key and (org_key == key or org_key in key or key in org_key):
                    return why
            return ""

        why = _match(legend.non_buyers)
        if why:
            out.append({"flag": FLAG_NON_BUYER, "label": FLAG_LABELS[FLAG_NON_BUYER],
                        "why": why, "source": "the legend's Known gaps"})
        why = _match(legend.budget_gate)
        if why:
            out.append({"flag": FLAG_BUDGET_GATE, "label": FLAG_LABELS[FLAG_BUDGET_GATE],
                        "why": why, "source": "the legend's Known gaps"})

        if not out:
            note = (self.org_note(company) or "")
            low = note.lower()
            if any(m in low for m in ("not a buyer", "non-buyer", "competitor",
                                      "partner track", "channel angle")):
                out.append({"flag": FLAG_NON_BUYER, "label": FLAG_LABELS[FLAG_NON_BUYER],
                            "why": note, "source": "the Org Coverage account note"})
            elif any(m in low for m in ("budget gate", "budget-gate", "fails budget",
                                        "pilot budget")):
                out.append({"flag": FLAG_BUDGET_GATE,
                            "label": FLAG_LABELS[FLAG_BUDGET_GATE],
                            "why": note, "source": "the Org Coverage account note"})
        return out

    def org_note(self, company: str) -> str:
        """The Org Coverage account note for one org, or ""."""
        row = self.org_row(company)
        return (row or {}).get("notes", "")

    # -- enrichment --------------------------------------------------------

    def enrich(self, row: dict, *, today: Optional[date] = None) -> dict:
        """One researcher row with EVERY RULE ALREADY APPLIED.

        This is the only shape a caller should ever hand to the model, because it
        is the shape that cannot be quoted without its caveats: the departure
        check, the staleness verdict, the org flags and the row's own watch-outs
        travel with the pitch hook rather than beside it.
        """
        company = row.get("company", "")
        name = row.get("researcher", "")
        legend = self.legend()
        tier = parse_tier(row.get("tier"))
        lanes = parse_lanes(row.get("lanes"))
        staleness = self.row_staleness(row, today=today)
        departed = self.departures().lookup(name)
        flags = self.org_flags(company)

        out = {
            "researcher": name,
            "company": company,
            "industry": row.get("industry", ""),
            "role": row.get("role", ""),
            "role_verified_via": row.get("role_verified_via", ""),
            "bucket": row.get("bucket", ""),
            "outreach_prioritization": row.get("prioritization", ""),
            # Tier and Confidence are carried SEPARATELY and both are required in
            # every answer: the legend states they are independent by design, and
            # collapsing them into one "score" is the mistake it warns against.
            "tier": tier or "",
            "tier_raw": row.get("tier", ""),
            "tier_meaning": legend.tiers.get(tier, "") if tier else "",
            "confidence": row.get("confidence", ""),
            "confidence_means": legend.confidence_rule,
            "lanes": lanes,
            "lane_meanings": {ln: legend.lanes.get(ln, "") for ln in lanes},
            "lanes_raw": row.get("lanes", ""),
            "why_them": row.get("why_them", ""),
            "key_evidence": row.get("key_evidence", ""),
            "network_edges": row.get("network_edges", ""),
            "public_profiles": row.get("profiles", ""),
            "research_link": row.get("research_link", ""),
            "watch_outs": row.get("watch_outs", ""),
            "sheet_row": row.get("_row"),
            "staleness": staleness,
            "org_flags": flags,
            "departed": bool(departed),
            "departure": departed or {},
            "recommendable": not departed and not flags,
        }
        if not tier and row.get("tier"):
            out["tier_note"] = (
                f"The Tier cell reads {row.get('tier')!r}, which is not T1/T2/T3 — the "
                "columns have slipped on this row. Report the tier as unknown; do not "
                "infer one."
            )
        if departed:
            move = departed.get("move") or "the Edge Map does not say where"
            out["do_not_recommend"] = (
                f"{name} is on the mapping sheet's DEPARTURES list ({move}) and has left "
                f"{company or 'the org listed here'}. NEVER recommend them for outreach "
                f"at that org. Say they have moved, and name the move."
            )
        if flags:
            out["do_not_pitch"] = (
                f"{company} is flagged in the sheet as: "
                + "; ".join(f"{f['label']} ({f['why']})" for f in flags)
                + ". This org is NOT a pitch target. Say what the sheet says and why "
                  "they're excluded, rather than recommending anyone there."
            )
        if row.get("watch_outs"):
            out["must_state"] = (
                f"This row's Watch-outs column says: {row['watch_outs']} — state it in "
                "the same breath as the hook."
            )
        # Anything the sheet gained since this code was written.
        extras = {k: v for k, v in (row.get("_extra") or {}).items() if str(v).strip()}
        if extras:
            out["other_columns"] = extras
        return out

    # -- queries -----------------------------------------------------------

    def researchers(self) -> list[dict]:
        """Raw researcher rows. Callers should almost always use `enrich`."""
        tab = self.tab(RESEARCHERS)
        return list(tab.rows) if tab else []

    def find_person(self, name: str) -> list[dict]:
        """Rows matching a person's name, exact-ish first then substring."""
        want = normalise_header(name)
        if not want:
            return []
        rows = self.researchers()
        exact = [r for r in rows if normalise_header(r.get("researcher", "")) == want]
        if exact:
            return exact
        return [r for r in rows if want and want in normalise_header(r.get("researcher", ""))]

    def for_org(self, company: str) -> list[dict]:
        """Every mapped researcher at one org."""
        want = normalise_header(company)
        if not want:
            return []
        rows = self.researchers()
        exact = [r for r in rows if normalise_header(r.get("company", "")) == want]
        if exact:
            return exact
        return [r for r in rows if want in normalise_header(r.get("company", ""))]

    def by_lane(self, lane: str) -> list[dict]:
        """Every researcher in an ICP lane.

        `lane` is a letter ("a") or a phrase ("evals", "red-teaming"). The phrase
        is resolved against the LEGEND'S OWN lane descriptions and against the
        membrane Bucket column, so the question can be asked in the sheet's
        vocabulary rather than in single letters.
        """
        want = _clean(lane).lower().strip()
        if not want:
            return []
        letters = set()
        if len(want) == 1 and want in "abcd":
            letters.add(want)
        else:
            tokens = [t for t in re.split(r"[^a-z0-9]+", want) if len(t) > 2]
            for letter, description in self.legend().lanes.items():
                desc = description.lower()
                if any(t in desc for t in tokens):
                    letters.add(letter)
        rows = self.researchers()
        if letters:
            hits = [r for r in rows if letters & set(parse_lanes(r.get("lanes")))]
            if hits:
                return hits
        # No lane letter resolved — fall back to the Bucket column, which is the
        # same taxonomy in words ("EVALS", "RED-TEAMING").
        tokens = [t for t in re.split(r"[^a-z0-9]+", want) if len(t) > 2]
        return [
            r for r in rows
            if any(t in (r.get("bucket") or "").lower() for t in tokens)
        ]

    def by_tier(self, tier: str) -> list[dict]:
        want = parse_tier(tier)
        if not want:
            return []
        return [r for r in self.researchers() if parse_tier(r.get("tier")) == want]

    def org_rows(self) -> list[dict]:
        tab = self.tab(ORG_COVERAGE)
        return list(tab.rows) if tab else []

    def org_row(self, company: str) -> Optional[dict]:
        want = normalise_header(company)
        if not want:
            return None
        rows = self.org_rows()
        for r in rows:
            if normalise_header(r.get("company", "")) == want:
                return r
        for r in rows:
            key = normalise_header(r.get("company", ""))
            if key and (want in key or key in want):
                return r
        return None

    def orgs_without_researchers(self) -> list[dict]:
        """Orgs the sheet lists but has mapped NOBODY at.

        Answered from the Org Coverage tab's own count when it has one, because
        that column is the sheet's own statement about coverage; the researcher
        rows are only used as a cross-check, and a disagreement between the two
        is reported rather than smoothed over.
        """
        mapped: dict[str, int] = {}
        for r in self.researchers():
            key = normalise_header(r.get("company", ""))
            if key:
                mapped[key] = mapped.get(key, 0) + 1

        out: list[dict] = []
        for r in self.org_rows():
            company = r.get("company", "")
            key = normalise_header(company)
            raw = (r.get("researchers_mapped") or "").strip()
            try:
                stated = int(re.sub(r"[^0-9-]", "", raw)) if raw else None
            except ValueError:
                stated = None
            actual = mapped.get(key, 0)
            if stated == 0 or (stated is None and actual == 0):
                entry = {
                    "company": company,
                    "industry": r.get("industry", ""),
                    "researchers_mapped": stated if stated is not None else actual,
                    "account_notes": r.get("notes", ""),
                    "org_flags": self.org_flags(company),
                    "sheet_row": r.get("_row"),
                }
                if stated == 0 and actual > 0:
                    entry["disagreement"] = (
                        f"Org Coverage says 0 researchers mapped, but the Researcher "
                        f"Mapping tab has {actual} row(s) for {company}. Report both "
                        f"numbers; don't pick one."
                    )
                out.append(entry)
        return out

    def edges(self, query: str = "") -> list[dict]:
        """Edge Map rows, optionally filtered. The DEPARTURES row is excluded —
        it is a do-not-pitch record, not a warm-intro path, and returning it
        among connection routes is how it gets used as one."""
        rows = []
        for r in self.tab(EDGES).rows if self.tab(EDGES) else []:
            if "departure" in normalise_header(r.get("edge", "")):
                continue
            rows.append({
                "edge": r.get("edge", ""),
                "connects": r.get("connects", ""),
                "why_it_matters": r.get("why", ""),
                "sheet_row": r.get("_row"),
            })
        want = _clean(query).lower()
        if want:
            tokens = [t for t in re.split(r"[^a-z0-9]+", want) if len(t) > 2]
            if tokens:
                rows = [
                    r for r in rows
                    if any(t in f"{r['edge']} {r['connects']} {r['why_it_matters']}".lower()
                           for t in tokens)
                ] or rows
        return rows


#: One instance, shared. Holds a lazily-built READ-ONLY client and a cache.
MAPPING = MappingSheet()


def _self_test() -> int:
    """`python -m mapping_sheet` — access check, schema dump and a rules dry-run,
    without booting Discord.

    There is no write test here, and that absence is the point: this module has
    no write path to verify.
    """
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Researcher mapping sheet self-test (read-only).")
    parser.add_argument("--person", help="Show the enriched row(s) for one researcher.")
    parser.add_argument("--org", help="Show every mapped researcher at one org.")
    parser.add_argument("--lane", help="Show researchers in one ICP lane (letter or phrase).")
    parser.add_argument("--tier", help="Show researchers in one tier (T1/T2/T3).")
    parser.add_argument("--gaps", action="store_true", help="Orgs with no mapped researchers.")
    parser.add_argument("--json", action="store_true", help="Dump results as JSON.")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    )

    print(f"service account: {MAPPING.service_account_email or '(none readable)'}")
    print(f"sheet id:        {MAPPING.sheet_id or '(unset)'}")
    print("write paths:     NONE (read-only scope, no write methods)")

    access = MAPPING.check_access()
    if not access.get("ok"):
        print(f"\nUNAVAILABLE: {access.get('error')}")
        if access.get("remedy"):
            print(f"FIX: {access['remedy']}")
        return 1
    print(f"\nOK: {access.get('title')!r} — {len(access.get('tabs') or [])} tab(s)")

    try:
        tabs = MAPPING.read(force=True)
    except SheetAccessError as e:
        print(f"read failed: {e}")
        return 1
    for kind, tab in tabs.items():
        print(f"  {tab.schema_line()}")

    legend = MAPPING.legend()
    print("\nLEGEND (loaded as behaviour):")
    print(f"  built:        {legend.built}")
    print(f"  refresh:      ~{legend.refresh_weeks} weeks "
          f"(stale after {legend.stale_after_days()} days, measured against today)")
    print(f"  ICP lanes:    {legend.lanes}")
    print(f"  tiers:        {legend.tiers}")
    print(f"  confidence:   {legend.confidence_rule}")
    print(f"  non-buyers:   {sorted(legend.non_buyers)}")
    print(f"  budget gate:  {sorted(legend.budget_gate)}")

    dep = MAPPING.departures()
    print(f"\nDEPARTURES: {len(dep.people)} person(s) on the do-not-pitch list")
    for rec in dep.all()[:8]:
        print(f"  - {rec['name']}: {rec['move'] or '(no move recorded)'}")

    rows: list[dict] = []
    if args.person:
        rows = MAPPING.find_person(args.person)
    elif args.org:
        rows = MAPPING.for_org(args.org)
    elif args.lane:
        rows = MAPPING.by_lane(args.lane)
    elif args.tier:
        rows = MAPPING.by_tier(args.tier)

    if args.gaps:
        gaps = MAPPING.orgs_without_researchers()
        print(f"\nORGS WITH NO MAPPED RESEARCHERS: {len(gaps)}")
        for g in gaps[:20]:
            print(f"  - {g['company']}: {g['account_notes'][:110]}")

    if rows:
        enriched = [MAPPING.enrich(r) for r in rows[:10]]
        if args.json:
            print(json.dumps(enriched, indent=2, default=str))
        else:
            print(f"\n{len(rows)} row(s):")
            for e in enriched:
                print(f"\n  {e['researcher']} — {e['company']}")
                print(f"    tier={e['tier'] or '?'} confidence={e['confidence'] or '?'} "
                      f"lanes={','.join(e['lanes']) or '?'}")
                print(f"    hook: {e['why_them'][:140]}")
                if e["staleness"]["caveat"]:
                    print(f"    {e['staleness']['caveat'][:200]}")
                if e.get("do_not_recommend"):
                    print(f"    DEPARTED: {e['do_not_recommend'][:200]}")
                if e.get("do_not_pitch"):
                    print(f"    FLAGGED: {e['do_not_pitch'][:200]}")
                if e["watch_outs"]:
                    print(f"    watch-outs: {e['watch_outs'][:160]}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_self_test())
