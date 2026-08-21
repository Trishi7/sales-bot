"""THE SOURCE LAYER — the three things this bot reasons over, and whether it can
actually reach them yet.

A sales Chief of Staff is only as good as what it can see. Three sources matter:

  sales_spreadsheet    the pipeline / targets / outreach log — who we're talking
                       to, at what stage, against what number.
  strategy_doc         the current sales & marketing strategy — the plan outreach
                       is supposed to be executing.
  sales_meeting_notes  the Drive-synced meeting notes (notes.py) — what was
                       actually said and committed to.

Right now only the third is wired up. The other two are STUBS whose real readers
land in the next prompt. That is deliberate and it is the point of this module:
each source SELF-REPORTS its status, so the bot can be honest about its own blind
spots instead of quietly answering from two sources out of three and sounding
just as confident.

    connected        the source is reachable and readable right now.
    awaiting-access  we know what it is, we don't have access/credentials/a path
                     for it yet. NOT an error — a known gap.
    error            it's configured and should work, but reading it failed.

`status_report()` is what the "what can you do" answer and state/summary.json are
both built from, so what the bot tells a human and what it tells its supervisor
can never drift apart.

Every reader here is READ-ONLY by construction. Nothing in this module writes to
a spreadsheet, a doc, or Drive — a source is something the bot looks at.
"""
import logging
from typing import Optional

import config
import notes

log = logging.getLogger(__name__)

CONNECTED = "connected"
AWAITING_ACCESS = "awaiting-access"
ERROR = "error"


class Source:
    """One source of truth, and whether the bot can reach it.

    Subclasses implement `_probe()` (cheap: is it reachable?) and, once they're
    real, whatever read methods they need. The base class owns the vocabulary so
    every source reports its status in the same shape.
    """

    #: Stable machine key. This appears in state/summary.json — don't rename it
    #: without updating the README's state contract.
    key = "source"
    #: How the bot refers to this source when talking to a human.
    label = "a source"
    #: What this source is FOR, in one line. Used in the capability answer.
    purpose = ""

    def _probe(self) -> tuple[str, str]:
        """Return (status, detail). `detail` is one plain sentence a human can
        act on — for awaiting-access, say exactly what is still needed."""
        raise NotImplementedError

    def status(self) -> dict:
        """{key, label, purpose, status, detail}. Never raises: a probe that
        blows up is reported as an `error` status, because a source that can't
        even be checked is exactly what the bot must not hide."""
        try:
            state, detail = self._probe()
        except Exception as e:
            log.exception("[sources] probe failed for %s", self.key)
            state, detail = ERROR, f"Checking this source failed ({type(e).__name__})."
        return {
            "key": self.key,
            "label": self.label,
            "purpose": self.purpose,
            "status": state,
            "detail": detail,
        }

    @property
    def connected(self) -> bool:
        return self.status()["status"] == CONNECTED


class SalesSpreadsheet(Source):
    """The pipeline / targets / outreach log.

    STUB. The next prompt gives this a real reader (Sheets API or a synced
    export). Until then it reports awaiting-access and names what's missing, so
    "how are we tracking against target?" is answered with "I can't see the
    sheet yet" rather than a guess.

    When it is implemented, it needs at minimum:
      - rows(): the pipeline rows as dicts,
      - targets(): the number(s) being tracked against,
      - freshness(): how current the data is.
    """

    key = "sales_spreadsheet"
    label = "the sales spreadsheet"
    purpose = "pipeline, targets and the outreach log"

    def _probe(self) -> tuple[str, str]:
        if config.SALES_SPREADSHEET_FILE or config.SALES_SPREADSHEET_ID:
            # Configured, but there is no reader yet — say so precisely rather
            # than claiming a connection this module cannot make.
            return (
                AWAITING_ACCESS,
                "The spreadsheet location is configured, but the reader isn't built yet "
                "— I can't read pipeline or target numbers from it.",
            )
        return (
            AWAITING_ACCESS,
            "I don't have access to the sales spreadsheet yet — no location is "
            "configured (SALES_SPREADSHEET_ID / SALES_SPREADSHEET_FILE) and the reader "
            "isn't built.",
        )

    def rows(self) -> list[dict]:
        """Pipeline rows. Empty until the reader lands — callers must check
        `connected` and say so rather than treating [] as 'no deals'."""
        return []


class StrategyDoc(Source):
    """The current sales & marketing strategy document.

    STUB. This is the source the bot checks outreach AGAINST — "is what we're
    doing still what we said we'd do", and "is the strategy current". Until the
    reader lands it reports awaiting-access.

    When implemented it needs at minimum:
      - text(): the document body,
      - last_updated(): when it was last revised (strategy CURRENCY is a thing
        this bot is meant to enforce, so this is not optional).
    """

    key = "strategy_doc"
    label = "the strategy doc"
    purpose = "the current sales & marketing strategy, and how current it is"

    def _probe(self) -> tuple[str, str]:
        if config.STRATEGY_DOC_FILE or config.STRATEGY_DOC_ID:
            return (
                AWAITING_ACCESS,
                "The strategy doc location is configured, but the reader isn't built yet "
                "— I can't check outreach against the plan or tell you how current it is.",
            )
        return (
            AWAITING_ACCESS,
            "I don't have access to the strategy doc yet — no location is configured "
            "(STRATEGY_DOC_ID / STRATEGY_DOC_FILE) and the reader isn't built.",
        )

    def text(self) -> Optional[str]:
        """The doc body. None until the reader lands."""
        return None

    def last_updated(self) -> Optional[str]:
        """ISO date the strategy was last revised. None until the reader lands."""
        return None


class SalesMeetingNotes(Source):
    """Drive-synced sales meeting notes. THIS ONE IS WIRED UP (notes.py).

    A separate sync process (rclone or equivalent) drops the notes into
    NOTES_DIR; this bot only ever reads local files and holds no Google
    credential of its own. That's why this source can be connected while the
    other two aren't: it needs a folder, not an API grant.
    """

    key = "sales_meeting_notes"
    label = "the sales meeting notes"
    purpose = "what was said, decided and committed to in sales meetings"

    def _probe(self) -> tuple[str, str]:
        if not config.NOTES_DIR:
            return (
                AWAITING_ACCESS,
                "No notes folder is configured (NOTES_DIR), so I can't read meeting notes.",
            )
        if not notes.is_configured():
            return (
                AWAITING_ACCESS,
                f"NOTES_DIR is set to {config.NOTES_DIR!r} but that folder doesn't exist "
                "— the Drive sync may not have run yet.",
            )
        found = notes.list_notes(days=3650)
        if not found:
            # Reachable but empty: connected, with the emptiness stated. An empty
            # folder is a different fact from no access, and conflating them is
            # how a bot ends up saying "nothing was discussed" about a meeting it
            # simply couldn't see.
            return (
                CONNECTED,
                f"I can read {config.NOTES_DIR}, but there are no meeting notes in it yet.",
            )
        latest_date, synced_at = notes.freshness()
        return (
            CONNECTED,
            f"I can read {len(found)} meeting note(s); the most recent is from "
            f"{latest_date}" + (f" (synced {synced_at})." if synced_at else "."),
        )

    def latest(self) -> Optional[dict]:
        """The most recent meeting note, parsed. None when unavailable."""
        return notes.read_note()


# Built once at import — these hold no connections, only configuration, so a
# module-level instance is cheap and every caller sees the same three.
SALES_SPREADSHEET = SalesSpreadsheet()
STRATEGY_DOC = StrategyDoc()
SALES_MEETING_NOTES = SalesMeetingNotes()

ALL: list[Source] = [SALES_SPREADSHEET, STRATEGY_DOC, SALES_MEETING_NOTES]


def status_report() -> list[dict]:
    """Every source's current status, in a fixed order.

    The single source of truth for BOTH the "what can you do" answer and
    state/summary.json, so what the bot tells a human and what it tells its
    supervisor are the same statement. Statuses are probed live, not cached: a
    folder that appears after startup should show up as connected without a
    restart.
    """
    return [s.status() for s in ALL]


def awaiting_access() -> list[dict]:
    """Just the sources the bot can't reach yet — the blind spots it is required
    to volunteer when asked what it can do."""
    return [s for s in status_report() if s["status"] != CONNECTED]


def describe_for_prompt() -> str:
    """The source statuses as a prompt block, phrased so the model states them
    honestly instead of implying it checked something it can't see."""
    lines = ["SOURCE STATUS (what you can and cannot actually see right now):"]
    for s in status_report():
        lines.append(f"- {s['label']} ({s['purpose']}): {s['status'].upper()} — {s['detail']}")
    lines.append(
        "You MUST NOT answer from a source marked AWAITING-ACCESS or ERROR, and you must "
        "SAY SO plainly when a question needs one of them. Never imply you checked "
        "something you cannot reach."
    )
    return "\n".join(lines)
