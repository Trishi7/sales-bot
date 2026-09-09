"""THE CADENCE LAYER — phase 1's rules are RETIRED; this is what is left.

WHAT HAPPENED. Phase 1 ran ten lettered rules (a–j) against the outreach TRACKER
tab. Phase 2 moved the bot's sheet world to the "Outreach PoCs" tab of the GTM
Playbook and retired both the tab and the rules. THE RULE EVALUATION IS GONE —
not disabled behind a flag, not left in place returning nothing: removed.

WHAT WAS REMOVED, IN FULL, so a reader of this file is never left wondering
whether something is still quietly firing somewhere:

    a  stale_followup     last-followed-up date older than n, no response
    b  intro_pending      Connected = Y but the intro date is blank
    c  start_interacting  first contacted, never connected, n days on
                          — and its COLD CEILING, cold_summary and cold_list
    d  alt_channel        follow-ups >= n with no response -> another channel
    e  unresponsive       follow-ups >= n with no response -> mark them so
    f  lock_meeting       positive response, Next Steps blank
    g  try_another_poc    one alternative-PoC suggestion per rejected company
    h  meeting_soon       a meeting inside MEETING_PREP_DAYS (this is what fed
                          the meeting-prep briefs, so those are unwired too)
    i  post_meeting       meeting past, assets or next steps missing
    j  nextstep_stall     Next Steps unchanged for n days

    …plus the UPDATE-TRACKER fill-in asks (`fill_in_gaps`) and the nightly
    master/tracker cross-check (`crosscheck`), both of which existed only to
    serve those rules, and every threshold that tuned them.

WHAT SURVIVES, AND WHY. The SHEET-HEALTH FLAGS: broken formulas, master rows
whose cells hold another column's vocabulary, response values nobody
standardised. They are a property of the SPREADSHEET rather than of any cadence
rule, they were never lettered, and they are the one thing here that still has
something true to say about a tab whose rules have not been written yet. Also the
plumbing every future rule set will need: the rejection test, the response
vocabulary, the item shape, the ranking and the caps.

A PHASE-2 RULE SET IS NOT DEFINED YET. Until one is, the five cadence sections of
the digest carry sheet-health lines and nothing else. That is a deliberate,
visible emptiness rather than a quiet one: `run()` logs what it considered and
`boot_report_text()` prints it at startup, so "the cadence said nothing today"
can be told apart from "the cadence is broken today".

    THERE IS NO PROACTIVE SEND PATH IN THIS FILE, and there must never be one.
    Everything here is handed to the EXISTING once-daily digest in bot.py, which
    is still the only unprompted message this bot writes.

ACTIVATION IS APPLIED BEFORE THIS MODULE IS CALLED. A row with no first-contact
date and no connection date is invisible to every proactive feature, and bot.py
filters through `activation.active_rows` before handing rows here — so `run()`
never sees an inactive row and cannot count one. See activation.py.

THIS MODULE IS PURE. It takes rows, a date and a few lookups, and returns dicts.
No sheet reads, no database writes, no Discord.
"""
import logging
from datetime import date
from typing import Callable, Optional

import config
import deadlines as dl
import gtm_sheet

log = logging.getLogger(__name__)

# -- rule ids -----------------------------------------------------------------
# ONE ID SURVIVES. The lettered rules (a-j), the cold summary, the fill-in asks
# and the cross-check were removed with the phase-1 rule set; see the header.
RULE_DATA_QUALITY = "data_quality"            # broken formulas, stray vocabulary

RULE_LETTERS = {
    RULE_DATA_QUALITY: "dq",
}

# -- priority buckets ---------------------------------------------------------
# Kept whole. A phase-2 rule set will need somewhere to put an urgent item, and
# a ranking invented twice is a ranking that disagrees with itself.
URGENT = "urgent"
WAITING = "waiting"
INTRO = "intro"
SUGGESTION = "suggestion"

_BUCKET_RANK = {URGENT: 0, WAITING: 1, INTRO: 2, SUGGESTION: 3}

RULE_PRIORITY = {
    RULE_DATA_QUALITY: WAITING,
}

# -- digest sections ----------------------------------------------------------
# The section keys are defined HERE so a rule and its section can never drift
# apart; digest.py re-exports them because it owns rendering. All five survive
# the retirement — they are the shape of the digest, not of the old rules, and
# they simply carry nothing until a phase-2 rule set fills them.
SECTION_FOLLOWUPS = "cadence_followups"
SECTION_INTROS = "cadence_intros"
SECTION_MEETINGS = "cadence_meetings"
SECTION_UPDATES = "cadence_updates"
SECTION_ASSETS = "cadence_assets"

RULE_SECTION = {
    # A sheet-health flag asks a human to WRITE something in the sheet, which is
    # what the update-tracker line is for.
    RULE_DATA_QUALITY: SECTION_UPDATES,
}

CADENCE_SECTIONS = (
    SECTION_FOLLOWUPS,
    SECTION_INTROS,
    SECTION_MEETINGS,
    SECTION_UPDATES,
    SECTION_ASSETS,
)

# The UPDATE-TRACKER kinds. Only the sheet-health flag is left of the three.
UPDATE_RULES = (RULE_DATA_QUALITY,)


# -- reading a row ------------------------------------------------------------


def _text(row: dict, role: str) -> str:
    """A row cell as trimmed text. Spreadsheet ERROR values were already turned
    into blanks by `gtm_sheet.clean_cell` at parse time; this re-applies it so a
    hand-built row (a test, a demo fixture) behaves the same way."""
    return gtm_sheet.clean_cell(row.get(role))


def _row_blob(row: dict) -> str:
    """Every readable cell of a row as one lower-cased string.

    Used only by the rejection test, which has to look everywhere: the team
    writes "rejected" in whichever column is nearest to hand, and a marker in
    Other Updates means exactly what a marker in Status means.
    """
    parts = [str(v) for k, v in row.items() if not str(k).startswith("_")]
    parts += [str(v) for v in (row.get("_extra") or {}).values()]
    return " | ".join(parts).lower()


def _marker_hit(row: dict) -> str:
    """The explicit written-off marker somebody typed into this row, or "".

    Deliberately a bounded phrase match, never an inference. A blank cell is not
    a rejection, and neither is a low score or an old date: rejection has to be
    written down.
    """
    blob = _row_blob(row)
    if not blob:
        return ""
    for marker in config.CADENCE_REJECTED_MARKERS or []:
        m = str(marker or "").strip().lower()
        if not m:
            continue
        # Bounded on both sides so "lost" doesn't match "lostrand Ltd".
        for boundary_l in ("", " ", "|", ":", "-", "(", ","):
            probe = f"{boundary_l}{m}"
            idx = blob.find(probe)
            while idx != -1:
                after = blob[idx + len(probe): idx + len(probe) + 1]
                if after in ("", " ", "|", ":", ".", ",", ")", "-", "/", ";"):
                    return m
                idx = blob.find(probe, idx + 1)
    return ""


def is_rejected(row: dict) -> tuple[bool, str]:
    """Has this row been written off? Returns (rejected, the reason found).

    EXCLUDED FROM EVERY PROACTIVE FEATURE when true — never chased, revisited
    offline by people. Two ways to be rejected, and both are things a human
    wrote:
      - the Response cell says so: "N", "N - Rejected", "No";
      - one of CADENCE_REJECTED_MARKERS appears anywhere in the row.

    THIS IS A SECOND, NARROWER GATE THAN ACTIVATION. Activation asks "has this
    ever started"; rejection asks "was it deliberately stopped". A row can fail
    either and be invisible for a completely different reason, and the two are
    reported separately because "we never contacted them" and "they said no"
    are not the same answer to any question.
    """
    if responded(row) == gtm_sheet.RESPONSE_NEGATIVE:
        return True, f"response {_text(row, 'response') or 'N'!r}"
    marker = _marker_hit(row)
    if marker:
        return True, f"marker {marker!r}"
    return False, ""


def responded(row: dict) -> str:
    """The row's response polarity, using the sheet's own vocabulary."""
    return gtm_sheet.response_status(row.get("response"))


def no_response(row: dict) -> bool:
    """Nothing has come back at all. Distinct from "they said no"."""
    return responded(row) == gtm_sheet.RESPONSE_NONE


def positive_response(row: dict) -> bool:
    """A reply we can act on: Y, P, "Did Respond", "P - Positive/In Progress".

    An UNCLASSIFIED reply counts too — somebody wrote something in the cell, so
    they DID come back, and the cost of asking about a real reply is far lower
    than the cost of missing one. The raw wording is reported separately by the
    data-quality flag so the sheet can standardise it.
    """
    return responded(row) in (gtm_sheet.RESPONSE_POSITIVE, gtm_sheet.RESPONSE_UNKNOWN)


def followups_count(row: dict) -> int:
    """Total follow-ups as a number. Blank or unreadable is 0."""
    raw = _text(row, "followups_count")
    digits = "".join(ch for ch in raw if ch.isdigit())
    return int(digits) if digits else 0


def days_since(value, today: date) -> Optional[int]:
    """Calendar days between a sheet date and today, or None if unreadable.

    CALENDAR days, not working days. The deadline machinery elsewhere in this
    bot uses working days, and that difference is deliberate.
    """
    parsed = gtm_sheet.sheet_date(value)
    if parsed is None:
        return None
    return (today - parsed).days


def row_identity(row: dict) -> tuple[str, str]:
    """(company, poc) as displayed. A row always has a company — `_parse_values`
    drops the ones that don't."""
    return _text(row, "company") or "(unnamed row)", _text(row, "poc")


def describe_row(row: dict) -> str:
    """The company · PoC · designation prefix every line opens with."""
    company, poc = row_identity(row)
    parts = [company]
    if poc:
        designation = _text(row, "poc_designation")
        parts.append(f"{poc}{f' ({designation})' if designation else ''}")
    return " · ".join(parts)


def row_key(row: dict) -> str:
    """The stable identity of a sheet row, for carry-forward and cross-checks.

    Built from the company and PoC, never from the sheet row NUMBER, which moves
    the moment somebody sorts the tab and would restart every item's age. The
    same shape `activation.row_key` uses, deliberately — an activated row and a
    cadence item are the same row and must key alike.
    """
    company, poc = row_identity(row)
    return f"{gtm_sheet.normalise_header(company)}|{gtm_sheet.normalise_header(poc)}"


# -- the item shape -----------------------------------------------------------


def _item(
    row: dict, *, rule: str, text: str, today: date, age_days: int = 0,
    detail: str = "", key: str = "",
) -> dict:
    """One finding. `key` is what carries an item's AGE across days, so it is
    built from the rule and the row identity — never from the sheet row number,
    which moves whenever somebody sorts the tab."""
    company, poc = row_identity(row)
    bucket = RULE_PRIORITY.get(rule, WAITING)
    return {
        "rule": rule,
        "letter": RULE_LETTERS.get(rule, "?"),
        "priority": bucket,
        "section": RULE_SECTION.get(rule, SECTION_UPDATES),
        "company": company,
        "poc": poc,
        "owner": _text(row, "owner"),
        "sheet_row": row.get("_row"),
        "text": text,
        "detail": detail,
        "age_days": max(0, int(age_days or 0)),
        "key": key or f"cadence:{rule}:{row_key(row)}",
        "_row": row,
    }


# -- sheet health -------------------------------------------------------------

_MASTER_VOCAB = {
    "connected": {"yes", "no"},
    "intro_sent": {"yes", "no"},
    "meeting_done": {"yes", "no"},
    "assets_shared": {"yes", "no"},
    "response_status": {"p positive in progress", "n rejected", "no response",
                        "p", "n", "y", ""},
}


def data_quality_flags(
    *, tracker_rows: list[dict], master_rows: Optional[list[dict]] = None,
    error_cells: Optional[list] = None, today: Optional[date] = None,
    tabs: Optional[dict] = None,
) -> list[dict]:
    """The three sheet-health findings, as (flag_key, signature, text) dicts.

    Each is ONE line, and each carries a SIGNATURE — a short string describing
    exactly what was found. The caller (bot.py) uses the signature to dedup the
    flag UNTIL IT IS FIXED: a flag whose signature has not changed since it was
    last reported is not repeated, because a daily reminder about a broken
    formula everybody already knows about is how a digest gets muted.

    `tracker_rows` are the ACTIVE rows of the canonical tab — bot.py filters
    before calling. A response value nobody standardised is only worth reporting
    on a row somebody is actually working; flagging the vocabulary of two
    hundred rows nobody has contacted would be sheet-tidying dressed up as work.

    `error_cells` is [(sheet_row, header, raw)] collected at parse time, from any
    tab — `tabs` maps a tab title to that list so the flag can name the tab.
    BROKEN FORMULAS ARE NOT FILTERED BY ACTIVATION: a #REF! is a property of the
    tab, not of a row's readiness, and the person who has to fix it needs the
    whole count.
    """
    today = today or dl.today_ist()
    out: list[dict] = []

    # 1. BROKEN FORMULAS. The values API returns "#REF!" as text; every one of
    #    them was normalised to empty when the row was parsed, which is right,
    #    and would otherwise be completely invisible.
    per_tab = dict(tabs or {})
    if error_cells:
        per_tab.setdefault("(canonical tab)", list(error_cells))
    for title, cells in sorted(per_tab.items()):
        if not cells:
            continue
        by_col: dict[str, int] = {}
        for _row, header, _raw in cells:
            by_col[str(header)] = by_col.get(str(header), 0) + 1
        cols = ", ".join(
            f"{name!r} ({n} cell{'s' if n != 1 else ''})"
            for name, n in sorted(by_col.items(), key=lambda kv: -kv[1])[:4]
        )
        signature = f"{title}:{sorted(by_col.items())}"
        out.append({
            "flag_key": f"broken_formula:{title}",
            "signature": signature,
            "text": (
                f"Tab {title!r} has {len(cells)} cell(s) holding a spreadsheet error "
                f"(#REF! / #N/A / #VALUE!) in {cols}. I read those as EMPTY — a broken "
                f"formula is not a value. Fix the formula or clear the cells."
            ),
        })

    # 2. MISALIGNED MASTER ROWS. A "Mar-2026" in Connected or a "No Response" in
    #    Meeting Done means that row's cells have shifted, and every aggregate
    #    computed from it is wrong.
    misaligned: list[str] = []
    for mrow in master_rows or []:
        wrong = []
        for role, allowed in _MASTER_VOCAB.items():
            value = gtm_sheet.normalise_header(_text(mrow, role))
            if value and value not in allowed:
                wrong.append(f"{role}={_text(mrow, role)!r}")
        if wrong:
            company, poc = row_identity(mrow)
            misaligned.append(
                f"row {mrow.get('_row')} ({company}{f' / {poc}' if poc else ''}): "
                + ", ".join(wrong[:3])
            )
    if misaligned:
        out.append({
            "flag_key": "master_misaligned",
            "signature": f"{len(misaligned)}:{misaligned[:5]}",
            "text": (
                f"{len(misaligned)} master row(s) appear misaligned — a cell holds a "
                f"value from the wrong column's vocabulary: "
                + "; ".join(misaligned[:3])
                + (f" (and {len(misaligned) - 3} more)" if len(misaligned) > 3 else "")
            ),
        })

    # 3. RESPONSE VALUES NOBODY STANDARDISED. Listed ONCE so the sheet can be
    #    tidied; the rows themselves are still classified, by the prefix rules.
    strays: dict[str, int] = {}
    for row in list(tracker_rows or []) + list(master_rows or []):
        for role in ("response", "response_status"):
            raw = _text(row, role)
            if raw and not gtm_sheet.is_known_response(raw):
                strays[raw] = strays.get(raw, 0) + 1
    if strays:
        listed = ", ".join(
            f"{value!r} ({n})"
            for value, n in sorted(strays.items(), key=lambda kv: -kv[1])[:6]
        )
        out.append({
            "flag_key": "response_vocabulary",
            "signature": str(sorted(strays.items())),
            "text": (
                f"{len(strays)} response value(s) outside the known vocabulary "
                f"(Y / P / N / Did Respond / Awaited / No Response): {listed}. I read "
                f"them as 'they replied, polarity unclear'. Worth standardising."
            ),
        })

    return out


def quality_items(flags: list[dict], *, today: date) -> list[dict]:
    """Data-quality flags -> UPDATE-TRACKER items, in the standard item shape."""
    out: list[dict] = []
    for flag in flags or []:
        fake_row = {"company": "(the sheet)", "poc": "", "_extra": {}}
        item = _item(
            fake_row, rule=RULE_DATA_QUALITY, today=today,
            key=f"cadence:{RULE_DATA_QUALITY}:{flag['flag_key']}",
            text=flag["text"],
            detail="Sheet health, reported once and not repeated until it changes.",
        )
        item["flag_key"] = flag["flag_key"]
        item["signature"] = flag.get("signature", "")
        item["company"] = ""
        out.append(item)
    return out


def evaluate_row(
    row: dict, *, today: date,
    stall_days: Optional[Callable[[dict], int]] = None,
) -> list[dict]:
    """Every rule that fires on ONE row.

    ALWAYS EMPTY TODAY. The phase-1 rules (a-j) that used to be evaluated here
    are retired — see the module header for the full list — and no phase-2 rule
    set has been defined against the "Outreach PoCs" tab yet.

    The function survives as the single place a rule set plugs in, and it keeps
    its signature so the day rules return, one call site changes rather than
    five. It is NOT a disabled rule set: there is nothing behind it to enable.
    """
    return []


# -- ranking ------------------------------------------------------------------


def sort_key(item: dict) -> tuple:
    """URGENT first, then WAITING, then INTRO, then SUGGESTION; within a bucket
    the oldest thing first, then alphabetically so the order is stable day to
    day."""
    return (
        _BUCKET_RANK.get(item.get("priority"), 9),
        -int(item.get("age_days") or 0),
        str(item.get("company") or "").lower(),
        str(item.get("rule") or ""),
    )


_UPDATE_KIND_RANK = {RULE_DATA_QUALITY: 0}


def _update_within_kind_key(item: dict) -> tuple:
    """Order INSIDE one update-tracker kind: oldest first, then alphabetical."""
    return (
        -int(item.get("age_days") or 0),
        str(item.get("company") or "").lower(),
        str(item.get("text") or ""),
    )


def order_updates(items: list[dict]) -> list[dict]:
    """UPDATE-TRACKER order: INTERLEAVED across the kinds, not blocked.

    Only one kind is left (sheet health), so today this is a plain sort. The
    interleave is kept rather than simplified away because it is the behaviour a
    second kind needs the moment one exists: ranked in blocks, one noisy kind
    takes the whole budget and the other is never seen.
    """
    buckets: dict[str, list[dict]] = {}
    for item in items:
        buckets.setdefault(item.get("rule"), []).append(item)
    ordered: list[tuple] = []
    for rule, group in buckets.items():
        group.sort(key=_update_within_kind_key)
        rank = _UPDATE_KIND_RANK.get(rule, 9)
        for turn, item in enumerate(group):
            ordered.append(((turn, rank), item))
    ordered.sort(key=lambda pair: pair[0])
    return [item for _k, item in ordered]


# -- the daily run ------------------------------------------------------------


def run(
    rows: list[dict], *, today: Optional[date] = None,
    mapping_lookup: Optional[Callable[[str], list]] = None,
    stall_days: Optional[Callable[[dict], int]] = None,
    limit: Optional[int] = None,
    urgent_limit: Optional[int] = None,
    update_limit: Optional[int] = None,
    master_rows: Optional[list[dict]] = None,
    error_cells: Optional[list] = None,
    error_cells_by_tab: Optional[dict] = None,
    quality_seen: Optional[Callable[[str, str], bool]] = None,
    inactive: int = 0,
) -> dict:
    """THE DAILY PASS over the canonical tab. Rows in, a ranked result out.

    `rows` ARE ALREADY THE ACTIVE ROWS. bot.py filters through
    `activation.active_rows` before calling, so nothing here can count, mention
    or chase a row with no first-contact and no connection date. `inactive` is
    passed in purely so the result can SAY how many were held back — a filter
    whose effect is invisible is a filter nobody can check.

    Returns:
        {"items": [...],        the capped, ranked items for the digest
         "held": [...],         what the caps left out, in full
         "all": [...],          every item, ranked
         "updates": [...],      the capped sheet-health lines
         "updates_held": [...], what the update cap left out
         "updates_all": [...],  every sheet-health line, ranked
         "quality": [...],      the raw data-quality flags (for dedup recording)
         "excluded": int,       active rows dropped as rejected
         "inactive": int,       rows never considered, for the same reason
         "counts": {rule: n},   how many items each rule produced
         "rows": int,           ACTIVE rows considered
         "today": date}

    THE ITEM LIST IS EMPTY UNTIL A PHASE-2 RULE SET EXISTS. `evaluate_row`
    returns nothing (see its docstring), so `items` carries only what the
    sheet-health scan produces. The caps below are kept whole for the day rules
    return; they cost nothing when there is nothing to cap.

    Passing 0 for any cap means NO cap, which is what an on-demand full list
    asks for.

    `quality_seen(flag_key, signature)` returns True when that exact flag has
    already been reported and nothing about it has changed — the "deduped until
    fixed" rule. Omit it and every flag is reported.
    """
    today = today or dl.today_ist()
    cap = config.DIGEST_MAX_ITEMS if limit is None else limit
    ucap = config.DIGEST_MAX_ITEMS if update_limit is None else update_limit
    if urgent_limit is not None:
        gcap = urgent_limit
    elif limit == 0:
        gcap = 0                      # "everything" includes the urgent overflow
    else:
        gcap = config.URGENT_MAX

    items: list[dict] = []
    updates: list[dict] = []
    live_rows: list[dict] = []
    excluded = 0

    for row in rows or []:
        rejected, _why = is_rejected(row)
        if rejected:
            excluded += 1
            continue
        live_rows.append(row)
        items.extend(evaluate_row(row, today=today, stall_days=stall_days))

    # Sheet health, deduped until it changes. The ONE thing this module still
    # computes, and it runs on the live rows only — see data_quality_flags.
    quality: list[dict] = []
    if config.CADENCE_DATA_QUALITY_ENABLED:
        try:
            quality = data_quality_flags(
                tracker_rows=live_rows, master_rows=master_rows,
                error_cells=error_cells, tabs=error_cells_by_tab, today=today,
            )
        except Exception:
            log.exception("[cadence] the data-quality scan failed; skipping the flags")
            quality = []
        fresh = []
        for flag in quality:
            try:
                if quality_seen is not None and quality_seen(
                    flag["flag_key"], flag.get("signature", "")
                ):
                    log.info(
                        "[cadence] data-quality flag %s unchanged since it was last "
                        "reported — not repeating it", flag["flag_key"],
                    )
                    continue
            except Exception:
                log.debug("[cadence] quality dedup check failed", exc_info=True)
            fresh.append(flag)
        updates.extend(quality_items(fresh, today=today))

    items.sort(key=sort_key)
    updates = order_updates(updates)

    counts: dict[str, int] = {}
    for item in items + updates:
        counts[item["rule"]] = counts.get(item["rule"], 0) + 1

    urgent = [i for i in items if i["priority"] == URGENT]
    rest = [i for i in items if i["priority"] != URGENT]
    gshown, gheld = (
        (urgent[:gcap], urgent[gcap:]) if (gcap and len(urgent) > gcap) else (urgent, [])
    )
    rshown, rheld = (
        (rest[:cap], rest[cap:]) if (cap and len(rest) > cap) else (rest, [])
    )
    shown = gshown + rshown
    held = gheld + rheld
    ushown, uheld = (
        (updates[:ucap], updates[ucap:]) if (ucap and len(updates) > ucap) else (updates, [])
    )

    log.info(
        "[cadence] %d ACTIVE row(s) considered (%d inactive row(s) never looked at — no "
        "first-contact or connection date), %d excluded as rejected; %d item(s) and %d "
        "sheet-health line(s); showing %d + %d, %d held. The phase-1 rules (a-j) are "
        "RETIRED, so an empty item list is expected until a phase-2 rule set exists.",
        len(rows or []), max(0, int(inactive or 0)), excluded,
        len(items), len(updates), len(shown), len(ushown), len(held) + len(uheld),
    )
    for rule in UPDATE_RULES:
        if counts.get(rule):
            log.info("[cadence]   %s (%s): %d item(s)",
                     RULE_LETTERS.get(rule, "?"), rule, counts[rule])

    return {
        "items": shown,
        "held": held,
        "all": items,
        "updates": ushown,
        "updates_held": uheld,
        "updates_all": updates,
        "quality": quality,
        "excluded": excluded,
        "inactive": max(0, int(inactive or 0)),
        "counts": counts,
        "rows": len(rows or []),
        "today": today,
    }


def overflow_line(held: int, updates_held: int = 0) -> str:
    """The one closing line that makes the cap honest. "" when nothing was cut."""
    total = max(0, int(held or 0)) + max(0, int(updates_held or 0))
    if total <= 0:
        return ""
    return f"{total} more held — ask 'sheet status' for what I am reading."


def sections(items: list[dict]) -> dict[str, list[dict]]:
    """Ranked items -> {digest section key: [items]}, order preserved."""
    out: dict[str, list[dict]] = {key: [] for key in CADENCE_SECTIONS}
    for item in items:
        out.setdefault(item.get("section") or SECTION_UPDATES, []).append(item)
    return out


# -- the startup report -------------------------------------------------------


def boot_report_text(
    result: Optional[dict], *, source: str = "", staleness: str = "",
    roles: Optional[list] = None, tab=None, activated: int = 0,
) -> str:
    """WHAT THE BOT IS READING TODAY, printed at boot. Sends nothing.

    This replaced the phase-1 cadence dry run, which printed which rows each
    lettered rule fired on. Those rules are gone, so the question this report
    answers is the one that matters now: WHICH TAB, WHICH COLUMNS, HOW MANY
    ROWS ARE ACTIVE, AND WHERE MAY THE BOT WRITE. Every one of those is a thing
    that fails silently, and all four are visible here on the first restart
    rather than in a week of empty digests.
    """
    lines = [
        "=" * 78,
        f"SHEET WORLD AT BOOT — {result.get('today') if result else 'n/a'} "
        f"(read-only; nothing is sent)",
        "=" * 78,
        "",
    ]

    if tab is not None:
        lines += [
            f"CANONICAL TAB: {tab.title!r}   (found by NAME, from GTM_POCS_TAB_TITLES)",
            f"  rows: {len(tab.rows)}   columns: {len(tab.headers)}   "
            f"header row: {tab.header_row}",
            "  discovered schema:",
        ]
        col_to_role = {i: r for r, i in tab.role_to_col.items()}
        for i, header in enumerate(tab.headers):
            if not str(header).strip():
                continue
            role = col_to_role.get(i)
            lines.append(
                f"    {config.column_label(i):>3}  {str(header)[:44]:<44} "
                + (f"-> {role}" if role else "   (no role — carried as _extra)")
                + ("   [RESTRICTED]" if config.is_restricted_column(i) else "")
            )
        lines.append("")
    else:
        lines += [
            "CANONICAL TAB: NOT FOUND.",
            f"  GTM_POCS_TAB_TITLES = "
            f"{', '.join(repr(t) for t in config.GTM_POCS_TAB_TITLES) or '(empty)'}",
            "  Nothing proactive has anything to run against until a tab with that "
            "name exists.",
            "",
        ]

    lines += [
        "WRITE LOCK",
        "-" * 78,
        f"  restricted (never written): {config.RESTRICTED_COLUMN_RANGES!r}"
        f"  -> {', '.join(config.column_label(lo) + ':' + config.column_label(hi) for lo, hi in config.RESTRICTED_COLUMN_BANDS) or '(none)'}",
        f"  writable window between the bands: "
        f"{config.writable_window_label() or '(none)'}",
        "  reading is UNRESTRICTED — this is a write lock only.",
        "",
    ]
    if tab is not None:
        named = [
            f"{c['column']}={c['header']!r}"
            + (f" [{c['role']}]" if c["role"] else "")
            for c in (_window_columns(tab))
        ]
        lines.append(f"  named columns inside the window ({len(named)}):")
        for entry in named:
            lines.append(f"    {entry}")
        if not named:
            lines.append("    (none — check the bands against the schema above)")
        lines.append("")

    if roles:
        lines.append("TAB ROLES — which real tab matched which signature today")
        lines.append("-" * 78)
        for kind, label, titles in roles:
            lines.append(f"  {kind:<18} {label}")
            lines.append(f"  {'':<18} -> {titles}")
        lines.append("")

    if result is not None:
        lines += [
            "ROW ACTIVATION — a row is ACTIVE only with a first-contact or "
            "connection date",
            "-" * 78,
            f"  active rows considered:   {result.get('rows', 0)}",
            f"  inactive (never looked at): {result.get('inactive', 0)}",
            f"  active but rejected:      {result.get('excluded', 0)}",
            f"  explicit activations held: {activated}",
            "  Inactive rows are invisible to every proactive feature. Ask about one "
            "by name and it still answers in full.",
            "",
            "CADENCE",
            "-" * 78,
            "  The phase-1 rules (a-j), the cold ceiling, the fill-in asks and the "
            "master cross-check are RETIRED.",
            "  No phase-2 rule set is defined yet, so the cadence sections carry "
            "sheet-health lines only.",
            f"  sheet-health lines today: {len(result.get('updates_all') or [])}",
        ]
        for item in (result.get("updates_all") or [])[:12]:
            lines.append(f"    · {item['text']}")
        if source:
            lines.append("")
            lines.append(f"  source: {source}" + (f"   [{staleness}]" if staleness else ""))
    lines.append("")
    return "\n".join(lines)


def _window_columns(tab) -> list[dict]:
    """The named columns inside the writable window, computed from a Tab.

    A local copy of `GTMSheets.writable_window_columns` so the boot report stays
    a pure function of the Tab it is handed and this module keeps its promise of
    touching no client.
    """
    out: list[dict] = []
    col_to_role = {i: r for r, i in tab.role_to_col.items()}
    for lo, hi in config.writable_windows():
        for idx in range(lo, min(hi, len(tab.headers) - 1) + 1):
            header = str(tab.headers[idx]).strip()
            if not header:
                continue
            out.append({
                "column": config.column_label(idx),
                "header": header,
                "role": col_to_role.get(idx, ""),
            })
    return out


def _self_test() -> int:
    """`python -m cadence` — the rules that are left, on fixtures, offline."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(name)s: %(message)s")
    today = dl.today_ist()

    rows = [
        {"_row": 2, "company": "Acme", "poc": "Ann", "response": "P",
         "first_contacted": "01-08-2026", "_extra": {}},
        {"_row": 3, "company": "Beta", "poc": "Bob", "response": "N - Rejected",
         "connected": "05-08-2026", "_extra": {}},
        {"_row": 4, "company": "Gamma", "poc": "Gil", "response": "shrugged",
         "first_contacted": "20-08-2026", "_extra": {}},
    ]

    failures = 0

    def check(name: str, got, want) -> None:
        nonlocal failures
        ok = got == want
        failures += 0 if ok else 1
        print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}, want {want!r}")

    print("rejection")
    check("positive row is not rejected", is_rejected(rows[0])[0], False)
    check("'N - Rejected' is rejected", is_rejected(rows[1])[0], True)

    print("\nthe retired rules")
    check("evaluate_row fires nothing", evaluate_row(rows[0], today=today), [])

    print("\nrun()")
    result = run(rows, today=today, inactive=7)
    check("active rows counted", result["rows"], 3)
    check("rejected excluded", result["excluded"], 1)
    check("inactive reported", result["inactive"], 7)
    check("no cadence items", result["all"], [])
    check(
        "stray response flagged",
        any(i["rule"] == RULE_DATA_QUALITY for i in result["updates_all"]),
        True,
    )

    print("\nrestricted bands (config)")
    check("A is restricted", config.is_restricted_column(0), True)
    check("J is writable", config.is_restricted_column(9), False)
    check("S is restricted", config.is_restricted_column(18), True)

    print(f"\n{'ALL PASSED' if not failures else str(failures) + ' FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_self_test())
