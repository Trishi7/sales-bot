"""THE MEETING-PREP BRIEF — one per meeting, attached to that day's digest.

WHAT IT IS. A meeting inside MEETING_PREP_DAYS earns one brief: who we are
meeting, what we already know about them, which pitch fits, and what came out of
the last conversation. It rides on the daily digest — it is NOT a message of its
own, and there is no send path in this file.

WHERE EVERY LINE COMES FROM, and nowhere else:
    the outreach tracker      company, PoC, designation, vertical, industry,
                              history (contacted / connected / intro / follow-ups
                              / response / assets / next steps). The tracker, not
                              the master tab: it is the one that has the dates
                              a "where we are" line is made of.
    Researcher Buyer Mapping  the PoC's mapped row: role, evidence, pitch hook,
                              watch-outs — WITH ITS CAVEATS. A name from that
                              sheet is only usable alongside its staleness
                              verdict, its departure check and its org flags, so
                              `MAPPING.enrich()` is what gets read here, never a
                              raw row.
    positioning matrix        the use case, the problem statement and the
                              matching pitch, chosen by the row's own use-case
                              or industry wording.
    meeting notes             anything on file about this company.

    ONLINE RESEARCH IS NOT ONE OF THEM. This bot has no web access. Rather than
    leave that as a silent gap that reads like "there was nothing to find", every
    brief carries a section saying so in as many words and marked "pending web
    access decision". Nothing external is inferred, guessed or filled in from
    the model's own knowledge — see `_no_web_section`.

WHAT IT WILL NOT DO. It will not assert anything the sources did not say. A
blank cell renders as "not recorded", never as a confident sentence. A brief
that invents a funding round or a headcount is worse than no brief at all,
because the person reading it is about to repeat it out loud in the meeting.
"""
import logging
from datetime import date
from typing import Optional

import config
import deadlines as dl
import gtm_sheet

log = logging.getLogger(__name__)

# How much of a free-text cell survives into the brief. Long enough to be useful,
# short enough that three briefs still fit in a digest.
_CELL_CLIP = 220
_NOTE_CLIP = 300


def _clip(text, limit: int = _CELL_CLIP) -> str:
    s = " ".join(str(text or "").split()).strip()
    if len(s) <= limit:
        return s
    return s[: limit - 1] + "…"


def _or_not_recorded(value) -> str:
    """A cell, or the words "not recorded". NEVER a guess, and never silence:
    "not recorded" is itself information a minute before a meeting."""
    return _clip(value) or "not recorded"


def _history_line(row: dict, *, today: date) -> str:
    """The relationship so far, from the tracker row's own date columns."""
    bits: list[str] = []
    for role, label in (
        ("first_contacted", "first contacted"),
        ("intro_date", "intro"),
        ("last_followed_up", "last followed up"),
    ):
        raw = str(row.get(role) or "").strip()
        if not raw:
            continue
        parsed = gtm_sheet.sheet_date(raw)
        if parsed:
            days = (today - parsed).days
            bits.append(f"{label} {dl.format_date(parsed)} ({days}d ago)")
        else:
            bits.append(f"{label} {raw}")
    count = str(row.get("followups_count") or "").strip()
    if count:
        bits.append(f"{count} follow-up(s)")
    connected = str(row.get("connected") or "").strip()
    if connected:
        bits.append(f"connected: {connected}")
    response = str(row.get("response") or "").strip()
    if response:
        bits.append(f"response: {response}")
    return "; ".join(bits) or "nothing recorded on the row yet"


def _positioning_match(row: dict, positioning_rows: list[dict]) -> Optional[dict]:
    """The positioning-matrix row that best fits this company.

    Matched on the tracker row's own words — its Use Case cell first, then its
    Industry — against the matrix's use case, ICP and company-type columns. No
    match returns None, and the brief then says so rather than attaching an
    arbitrary pitch.
    """
    if not positioning_rows:
        return None
    probes = [
        gtm_sheet.normalise_header(str(row.get("use_case") or "")),
        gtm_sheet.normalise_header(str(row.get("industry") or "")),
        gtm_sheet.normalise_header(str(row.get("poc_vertical") or "")),
    ]
    probes = [p for p in probes if len(p) >= 3]
    if not probes:
        return None

    best, best_score = None, 0
    for prow in positioning_rows:
        haystack = " ".join(
            gtm_sheet.normalise_header(str(prow.get(role) or ""))
            for role in ("use_case", "icp", "company_type", "label", "problem")
        )
        if not haystack.strip():
            continue
        score = 0
        for probe in probes:
            # Whole-token overlap, so "data" doesn't match "metadata" and a
            # two-word industry has to actually appear.
            tokens = [t for t in probe.split() if len(t) > 3]
            score += sum(1 for t in tokens if t in haystack)
            if probe and probe in haystack:
                score += 3
        if score > best_score:
            best, best_score = prow, score
    return best if best_score > 0 else None


def _mapping_section(company: str, poc: str, mapping) -> list[str]:
    """The Researcher Buyer Mapping's view of this PoC — WITH ITS CAVEATS.

    `MAPPING.enrich()` is the only shape read here. It carries the departure
    check, the staleness verdict, the org flags and the row's watch-outs
    alongside the pitch hook, which is the whole point: a mapped name quoted
    without them is exactly the mistake that sheet's legend warns about.
    """
    lines: list[str] = []
    if mapping is None:
        return ["_Researcher Buyer Mapping: not available._"]

    try:
        rows = mapping.for_org(company) or []
    except Exception:
        log.info("[prep] mapping lookup failed for %r", company, exc_info=True)
        return ["_Researcher Buyer Mapping: could not be read just now._"]

    if not rows:
        note = ""
        try:
            note = mapping.org_note(company) or ""
        except Exception:
            note = ""
        lines.append(f"_{company} is not mapped in the Researcher Buyer Mapping._")
        if note:
            lines.append(f"Org coverage note: {_clip(note)}")
        return lines

    # The PoC's own row first when the mapping knows them; otherwise everyone
    # mapped at the org, because "who else is there" is the useful question.
    want = gtm_sheet.normalise_header(poc)
    ordered = sorted(
        rows,
        key=lambda r: 0 if want and want in gtm_sheet.normalise_header(str(r.get("researcher") or "")) else 1,
    )
    for raw in ordered[:3]:
        try:
            person = mapping.enrich(raw)
        except Exception:
            log.debug("[prep] enrich failed", exc_info=True)
            continue
        name = _clip(person.get("researcher") or raw.get("researcher") or "(unnamed)", 80)
        role = _clip(person.get("role") or raw.get("role") or "", 80)
        lines.append(f"**{name}**{f' — {role}' if role else ''}")
        for key, label in (
            ("evidence", "evidence"),
            ("pitch_hook", "pitch hook"),
            ("watch_outs", "watch-outs"),
        ):
            value = _clip(person.get(key) or raw.get(key) or "")
            if value:
                lines.append(f"  · {label}: {value}")
        # THE CAVEATS. Never optional, never below the fold.
        caveats: list[str] = []
        staleness = person.get("staleness") or {}
        if isinstance(staleness, dict) and staleness.get("note"):
            caveats.append(str(staleness["note"]))
        if person.get("departed"):
            departed = person["departed"]
            where = departed.get("note") if isinstance(departed, dict) else ""
            caveats.append(f"MAY HAVE LEFT — {_clip(where, 120) or 'listed under departures'}")
        for flag in person.get("flags") or []:
            label = flag.get("label") if isinstance(flag, dict) else str(flag)
            if label:
                caveats.append(str(label))
        if caveats:
            lines.append(f"  · ⚠ {('; '.join(caveats))}")
    return lines


def _notes_section(company: str, *, notes_module, today: date) -> list[str]:
    """Anything on file from past meetings about this company.

    A title/summary match only — this reads the notes the team already wrote, it
    does not summarise them with a model, so nothing here can drift from what
    the note says.
    """
    if notes_module is None:
        return []
    try:
        found = notes_module.list_notes(days=max(1, config.CADENCE_PREP_NOTES_DAYS))
    except Exception:
        log.info("[prep] notes unavailable", exc_info=True)
        return ["_Meeting notes: could not be read just now._"]
    if not found:
        return []

    want = gtm_sheet.normalise_header(company)
    if not want:
        return []
    hits = [
        n for n in found
        if want in gtm_sheet.normalise_header(str(n.get("title") or ""))
        or want in gtm_sheet.normalise_header(str(n.get("label") or ""))
    ]
    if not hits:
        return [f"_No meeting note on file mentions {company} in the last "
                f"{config.CADENCE_PREP_NOTES_DAYS} days._"]

    lines: list[str] = []
    for meta in hits[:2]:
        try:
            note = notes_module.read_note(date=meta.get("date"), label=meta.get("label"))
        except Exception:
            log.debug("[prep] could not read note %r", meta.get("path"), exc_info=True)
            continue
        if not note:
            continue
        when = meta.get("date") or "?"
        label = _clip(note.get("label") or note.get("title") or "note", 60)
        lines.append(f"**{when} — {label}**")
        summary = _clip(note.get("summary") or "", _NOTE_CLIP)
        if summary:
            lines.append(f"  · {summary}")
        for step in (note.get("next_steps") or [])[:3]:
            task = _clip(step.get("task") if isinstance(step, dict) else step, 120)
            owner = (step.get("owner_name") if isinstance(step, dict) else "") or ""
            if task:
                lines.append(f"  · next step{f' ({owner})' if owner else ''}: {task}")
    return lines


def _no_web_section() -> list[str]:
    """The honest gap. Phase 1 has no web access, and a brief that quietly
    omitted external research would read as "there was nothing to find"."""
    if not config.CADENCE_PREP_NOTE_NO_WEB:
        return []
    return [
        "**External / online research — pending web access decision**",
        "  · Not included. This bot has no web access, so nothing here comes from "
        "outside the sheets and the meeting notes above. Recent news, funding, "
        "headcount and product launches have NOT been checked — look them up "
        "yourself before the call.",
    ]


def build(
    row: dict, *, meeting_date: str, today: Optional[date] = None,
    mapping=None, positioning_rows: Optional[list[dict]] = None,
    notes_module=None,
) -> str:
    """ONE prep brief, as Discord-ready text.

    Pure assembly: every caller-supplied source is optional and a missing one
    degrades to an explicit "not available" line rather than to a shorter brief
    that looks complete. Nothing in here reaches the network.
    """
    today = today or dl.today_ist()
    company = str(row.get("company") or "").strip() or "(unnamed company)"
    poc = str(row.get("poc") or "").strip()
    parsed = gtm_sheet.sheet_date(meeting_date)
    when = dl.format_date(parsed) if parsed else str(meeting_date or "")
    days_away = (parsed - today).days if parsed else None
    when_note = (
        "today" if days_away == 0 else
        "tomorrow" if days_away == 1 else
        f"in {days_away}d" if days_away is not None else ""
    )

    lines = [
        f"**MEETING PREP — {company}"
        + (f" / {poc}" if poc else "")
        + f" — {when}{f' ({when_note})' if when_note else ''}**",
    ]

    # Who
    who_bits = []
    if poc:
        designation = _clip(row.get("poc_designation"), 80)
        vertical = _clip(row.get("poc_vertical"), 80)
        who_bits.append(f"{poc}{f', {designation}' if designation else ''}")
        if vertical:
            who_bits.append(f"vertical: {vertical}")
    who_bits.append(f"industry: {_or_not_recorded(row.get('industry'))}")
    lines.append("**Who** — " + " · ".join(who_bits))

    # Where we are
    lines.append(f"**Where we are** — {_history_line(row, today=today)}")
    lines.append(f"**Assets shared** — {_or_not_recorded(row.get('assets_shared'))}")
    lines.append(f"**Next steps on file** — {_or_not_recorded(row.get('next_steps'))}")

    # The pitch
    match = _positioning_match(row, positioning_rows or [])
    if match:
        label = _clip(match.get("label") or "", 40)
        lines.append(
            f"**Use case / pitch**{f' — {label}' if label else ''}"
        )
        for role, title in (
            ("use_case", "use case"),
            ("problem", "problem statement"),
            ("offering", "what we offer"),
            ("business_impact", "business impact"),
        ):
            value = _clip(match.get(role))
            if value:
                lines.append(f"  · {title}: {value}")
    else:
        own = _clip(row.get("use_case"))
        lines.append(
            "**Use case / pitch** — "
            + (f"the row says {own!r}; no positioning-matrix row matched it."
               if own else
               "no use case on the row and no positioning-matrix row matched — "
               "pick the pitch yourself.")
        )

    # The mapping, with its caveats
    lines.append("**Researcher Buyer Mapping**")
    lines.extend(f"  {ln}" if not ln.startswith("  ") else ln
                 for ln in _mapping_section(company, poc, mapping))

    # Past notes
    note_lines = _notes_section(company, notes_module=notes_module, today=today)
    if note_lines:
        lines.append("**From past meeting notes**")
        lines.extend(f"  {ln}" if not ln.startswith("  ") else ln for ln in note_lines)

    lines.extend(_no_web_section())
    return "\n".join(lines)
