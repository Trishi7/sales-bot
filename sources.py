"""THE SOURCE LAYER — the three things this bot reasons over, and whether it can
actually reach them yet.

A sales Chief of Staff is only as good as what it can see. Five sources matter:

  sales_spreadsheet    the GTM Playbook — the outreach tracker, the positioning
                       matrix and the prospect priority list. LIVE via the Google
                       Sheets API (gtm_sheet.py).
  researcher_mapping   the researcher/buyer mapping — which PEOPLE to pitch
                       inside those orgs, in which ICP lane, with what hook, and
                       who must not be pitched at all. LIVE and STRICTLY
                       READ-ONLY (mapping_sheet.py).
  strategy_doc         the current sales & marketing strategy — the plan outreach
                       is supposed to be executing. LIVE, read-only over the
                       Drive API (strategy.py). Reports DEGRADED rather than
                       connected when the plan is stale, because an answer built
                       on a plan nobody has revised in a month has to say so.
  sales_meeting_notes  the Drive-synced meeting notes (notes.py) — what was
                       actually said and committed to. LIVE, and filtered: only
                       notes that are SALES meetings are ever loaded. Everything
                       derived from them carries a CITATION — see meetings.py.
  todo_sheet           "Membrane Sales To-Dos" (todos.py) — the sheet the bot
                       creates, SHARES with the team and appends action items
                       to. A source because its failure mode is silent: an
                       unshared sheet is perfectly readable by the bot and
                       invisible to every human.

All five are wired up.

The playbook and the mapping sheet are two sources rather than one on purpose:
they answer different questions (which ACCOUNT vs which PERSON), they can fail
independently, and only one of them is ever writable. Collapsing them would let
an outage in one be reported as health in the other.

Which is the point of this module. Each source SELF-REPORTS its status, so the
bot can be honest about its own blind spots instead of quietly answering from two
sources out of three and sounding just as confident.

    connected        the source is reachable and readable right now.
    degraded         readable, but something behind it is broken and the data is
                     going stale — e.g. the notes folder is readable while the
                     rclone sync that fills it is failing. Usable, WITH a caveat
                     the answer must carry.
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
from datetime import date
from typing import Optional

import config
import drive
import gtm_sheet
import mapping_sheet
import notes
import strategy
import todos

log = logging.getLogger(__name__)

CONNECTED = "connected"
DEGRADED = "degraded"
AWAITING_ACCESS = "awaiting-access"
ERROR = "error"

#: Statuses the bot may actually answer FROM. `degraded` is in here on purpose:
#: stale data with the staleness stated is useful, silence is not.
USABLE = (CONNECTED, DEGRADED)


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

    @property
    def usable(self) -> bool:
        """Readable at all — connected, or degraded and going stale. Callers that
        can carry a staleness caveat should check this; callers that need fresh
        data should check `connected`."""
        return self.status()["status"] in USABLE


class SalesSpreadsheet(Source):
    """The GTM Playbook — the pipeline, the positioning matrix and the priority
    list. WIRED UP, live through the Sheets API (see gtm_sheet.py).

    Two spreadsheets sit behind this one source: the ORIGINAL (read-only source
    of truth) and the sandbox COPY (the bot's writable mirror). The source is
    CONNECTED when the original is readable, because that is what answers
    questions; the copy being unreachable degrades writing only, and is reported
    in the detail rather than by pretending the whole source is down.

    The probe is cheap by design — it reuses the last startup access check rather
    than hitting the API on every status call, since `status_report()` runs on
    every capability answer and every state write.
    """

    key = "sales_spreadsheet"
    label = "the GTM Playbook spreadsheet"
    purpose = "the outreach tracker, the positioning matrix and the prospect priority list"

    def _probe(self) -> tuple[str, str]:
        if not config.GOOGLE_SERVICE_ACCOUNT_JSON:
            return (
                AWAITING_ACCESS,
                "No Google service-account key is configured "
                "(GOOGLE_SERVICE_ACCOUNT_JSON), so I can't open the GTM Playbook.",
            )

        access = gtm_sheet.SHEETS.last_access()
        if not access:
            # Nothing probed yet (a status call before startup finished). Do it
            # now rather than reporting a state we haven't checked.
            access = gtm_sheet.SHEETS.check_access()

        original = access.get(gtm_sheet.ORIGINAL, {})
        copy = access.get(gtm_sheet.COPY, {})

        if not original.get("ok"):
            remedy = original.get("remedy") or ""
            return (
                AWAITING_ACCESS,
                f"I can't open the GTM Playbook: {original.get('error', 'unknown error')}. "
                + (f"Fix: {remedy}" if remedy else ""),
            )

        # Readable. Say what's in it, and be explicit when the sandbox — and so
        # writing — is the part that's unavailable.
        try:
            tabs = gtm_sheet.SHEETS.read(gtm_sheet.ORIGINAL)
        except gtm_sheet.SheetAccessError as e:
            return (AWAITING_ACCESS, f"I can open the GTM Playbook but couldn't read it: {e}")

        found = ", ".join(f"{kind} ({len(tab.rows)} rows)" for kind, tab in tabs.items())
        detail = f"Reading {original.get('title') or 'the GTM Playbook'}: {found or 'no recognised tabs'}."

        if config.SHEET_WRITE_TARGET == "off":
            detail += " Sheet writing is off, so deadlines are stored locally only."
        elif not copy.get("ok"):
            detail += (
                f" The sandbox copy is NOT writable ({copy.get('error', 'unknown')}). "
                f"{copy.get('remedy', '')} Deadlines still work — they're stored locally "
                "and announced — they just aren't mirrored to the sheet."
            )
        return (CONNECTED, detail)

    def tabs(self) -> dict:
        """{kind: Tab} from the original. Raises SheetAccessError when the sheet
        has never been readable; callers check `connected` first."""
        return gtm_sheet.SHEETS.read(gtm_sheet.ORIGINAL)

    def rows(self) -> list[dict]:
        """Outreach tracker rows. Callers must check `connected` and say so
        rather than treating [] as "no deals"."""
        try:
            tab = gtm_sheet.SHEETS.tab(gtm_sheet.TRACKER)
        except gtm_sheet.SheetAccessError:
            return []
        return tab.rows if tab else []


class ResearcherMapping(Source):
    """The researcher/buyer mapping sheet. WIRED UP, live, and READ-ONLY.

    This is the source that turns "we're talking to Acme" into "and the person to
    pitch there is <researcher>, T1, lane a, here's the hook" — and, just as
    often, into "nobody there: the sheet flags them as a competitor".

    Its status detail deliberately reports more than reachability, because each
    of these changes how an answer built from it must be phrased:
      - whether the legend parsed. The legend IS the rule set; without it the
        staleness threshold and the exclusion lists fall back to last-known.
      - whether the DEPARTURES list was readable. If it wasn't, the do-not-pitch
        check CANNOT run, and "nobody is flagged" would be a fabrication.
      - how old the sheet's research pass is against its own refresh rule.
      - that it is read-only, so nobody expects the bot to update it.
    """

    key = "researcher_mapping"
    label = "the researcher/buyer mapping sheet"
    purpose = (
        "which researcher to pitch at each org, in which ICP lane, with what hook "
        "— plus who must not be pitched (read-only)"
    )

    def _probe(self) -> tuple[str, str]:
        if not config.GOOGLE_SERVICE_ACCOUNT_JSON:
            return (
                AWAITING_ACCESS,
                "No Google service-account key is configured "
                "(GOOGLE_SERVICE_ACCOUNT_JSON), so I can't open the researcher mapping.",
            )
        if not config.GTM_MAPPING_SHEET_ID:
            return (
                AWAITING_ACCESS,
                "No mapping spreadsheet id is configured (GTM_MAPPING_SHEET_ID), so I "
                "can't tell you who to pitch at an org.",
            )

        access = mapping_sheet.MAPPING.last_access()
        if not access:
            access = mapping_sheet.MAPPING.check_access()
        if not access.get("ok"):
            remedy = access.get("remedy") or ""
            return (
                AWAITING_ACCESS,
                "I can't open the researcher mapping sheet: "
                f"{access.get('error', 'unknown error')}. "
                + (f"Fix: {remedy}" if remedy else ""),
            )

        try:
            tabs = mapping_sheet.MAPPING.read()
        except gtm_sheet.SheetAccessError as e:
            return (AWAITING_ACCESS,
                    f"I can open the researcher mapping but couldn't read it: {e}")

        found = ", ".join(f"{kind} ({len(tab.rows)} rows)" for kind, tab in tabs.items())
        detail = (
            f"Reading {access.get('title') or 'the researcher mapping'} (READ-ONLY — I "
            f"never write to this sheet): {found or 'no recognised tabs'}."
        )

        legend = mapping_sheet.MAPPING.legend()
        # A missing legend or a missing DEPARTURES row is DEGRADED, not connected:
        # the rows are still readable, but rules every recommendation has to obey
        # can't be applied, and the answer must carry that caveat.
        gaps = []
        if not legend.built:
            gaps.append(
                "the legend's build date didn't parse, so the staleness caveat runs on "
                f"the default ~{legend.refresh_weeks}-week clock"
            )
        if not mapping_sheet.MAPPING.departures_known():
            gaps.append(
                "the Edge Map's DEPARTURES row wasn't readable, so I CANNOT check "
                "whether someone has left before recommending them"
            )
        if gaps:
            return (DEGRADED, detail + " Caveat: " + "; and ".join(gaps) + ".")

        days = (date.today() - legend.built).days
        limit = legend.stale_after_days()
        age = (
            f" The research pass is from {legend.built.isoformat()} ({days} days ago); "
            + (
                f"that is past its own ~{legend.refresh_weeks}-week refresh rule, so "
                "EVERY row needs a re-verify-role caveat."
                if days > limit else
                f"the sheet's own ~{legend.refresh_weeks}-week refresh rule bites in "
                f"{limit - days} day(s)."
            )
        )
        return (
            CONNECTED,
            detail + age
            + f" {len(mapping_sheet.MAPPING.departures().people)} person(s) are on the "
              "DEPARTURES do-not-pitch list.",
        )

    def researchers(self) -> list[dict]:
        """Raw researcher rows. Callers should use `mapping_sheet.MAPPING.enrich`
        before showing one to the model — an un-enriched row carries no departure
        check, no staleness verdict and no org flags."""
        try:
            return mapping_sheet.MAPPING.researchers()
        except gtm_sheet.SheetAccessError:
            return []


class StrategyDoc(Source):
    """The current sales & marketing strategy document. WIRED UP (strategy.py).

    NO LONGER A STUB. STRATEGY_DOC_ID points at the human-owned strategy doc,
    and the bot reads it read-only over the Drive API — see drive.py's scopes.
    It is the source three things run against:

      the DEADLINE CADENCE   a cadence stated in the doc outranks the
                             working-day defaults (deadlines.resolve_rule).
      CURRENCY               Drive's modifiedTime against STRATEGY_STALE_DAYS.
      OUTREACH-vs-PLAN       the targets it names against where outreach went.

    DEGRADED, not connected, when the doc is readable but STALE: the answers
    built on it are still the best available, and every one of them has to say
    the plan they are quoting hasn't been revised in a month.
    """

    key = "strategy_doc"
    label = "the strategy doc"
    purpose = "the current sales & marketing strategy, and how current it is"

    def _probe(self) -> tuple[str, str]:
        try:
            readable, detail = strategy.status_detail()
        except Exception as e:
            log.debug("[sources] strategy probe raised", exc_info=True)
            return (
                AWAITING_ACCESS,
                f"The strategy doc could not be probed ({type(e).__name__}). "
                "Check STRATEGY_DOC_ID and the service-account key.",
            )
        if not readable:
            return AWAITING_ACCESS, detail
        try:
            stale = strategy.is_stale()
        except Exception:
            stale = False
        return (DEGRADED if stale else CONNECTED), detail

    def text(self) -> Optional[str]:
        """The doc body, or None when it can't be read."""
        return strategy.text()

    def last_updated(self) -> Optional[str]:
        """ISO date the strategy was last revised, or None."""
        return strategy.last_updated()


class TodoSheet(Source):
    """"Membrane Sales To-Dos" — the sheet the bot creates, shares and appends to.

    IT IS A SOURCE BECAUSE ITS FAILURE MODE IS SILENT. A sheet that exists but
    was never shared looks, from inside the bot, exactly like a healthy one: the
    bot can read and write it perfectly well and no human can open it. So the
    status line reports WHO CAN ACTUALLY SEE IT, not merely whether the API call
    worked.
    """

    key = "todo_sheet"
    label = "the team to-do sheet"
    purpose = "the shared action-item list, appended from the meeting notes"

    def _probe(self) -> tuple[str, str]:
        if not config.TODO_SHEET_ENABLED:
            return AWAITING_ACCESS, "TODO_SHEET_ENABLED=false — there is no to-do sheet."
        ok, why = drive.available()
        if not ok:
            return AWAITING_ACCESS, f"{why}, so I can't create or read the to-do sheet."
        if not config.TEAM_SHARE_EMAILS:
            return (
                DEGRADED,
                "TEAM_SHARE_EMAILS is empty. A sheet the service account creates is "
                "INVISIBLE to every human until it is shared, so I would be keeping a "
                "to-do list nobody can open.",
            )
        sid = todos.sheet_id(self._db)
        if not sid:
            return (
                AWAITING_ACCESS,
                f"The to-do sheet hasn't been created yet. It is created on the next "
                f"boot and shared with {', '.join(config.TEAM_SHARE_EMAILS)}.",
            )

        url = drive.sheet_url(sid)
        try:
            who = [
                p.get("emailAddress") for p in drive.permissions(sid)
                if p.get("emailAddress")
                and p.get("emailAddress") != drive.service_account_email()
            ]
        except Exception:
            who = []
        missing = [e for e in config.TEAM_SHARE_EMAILS if e not in who]
        detail = f"{config.TODO_SHEET_TITLE} — {url}."
        if who:
            detail += f" Shared with {', '.join(str(w) for w in who)}."
        if missing:
            return (
                DEGRADED,
                detail + f" NOT shared with {', '.join(missing)} — they cannot open it.",
            )
        detail += (
            f" Refreshed from the meeting notes every {config.TODO_REFRESH_DAY} "
            "(append-only; Status and Notes belong to the team)."
        )
        return CONNECTED, detail

    # The database is injected by bot.py at startup: this source's status
    # depends on a stored id, and a module-level source object has no other way
    # to reach it.
    _db = None

    def bind(self, db) -> None:
        self._db = db


class SalesMeetingNotes(Source):
    """Drive-synced sales meeting notes. WIRED UP, including the sync (notes.py).

    The bot runs NOTES_SYNC_CMD — an rclone command — to pull the Drive docs into
    NOTES_DIR at startup, every NOTES_SYNC_MINUTES and before answering a notes
    question. It still holds no Google credential of its own: rclone owns that.

    TWO different failures have to stay distinguishable here, because they have
    different fixes and only one of them is the bot's fault:

      the SYNC is broken   → DEGRADED. The folder is readable, it's just going
                             stale. The detail names the fix (usually rclone not
                             being on PATH, or an expired token).
      nothing SALES came    → CONNECTED with zero notes loaded. The sync works;
      down                   the sales calls simply aren't being recorded, or the
                             sync account wasn't invited to them. No amount of
                             config fixes that, so the detail asks the question.
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

        st = notes.sync_status()
        if not st["dir_exists"]:
            return (
                AWAITING_ACCESS,
                f"NOTES_DIR is set to {config.NOTES_DIR!r} but that folder doesn't exist "
                "and I couldn't create it — check the path is writable.",
            )

        # 1. THE SYNC: when it last ran, and whether it is working.
        if not st["cmd_configured"]:
            sync_line = (
                "No sync command is configured (NOTES_SYNC_CMD), so nothing refreshes this "
                "folder — what's in it is whatever was last copied there by hand."
            )
        elif st["ok"] is None:
            sync_line = f"The sync (every {st['interval_minutes']} min) hasn't run yet this session."
        elif st["ok"]:
            sync_line = f"Last synced {st['last_success']} (every {st['interval_minutes']} min)."
        else:
            stale = (
                f" The newest thing I have is whatever the last good sync ({st['last_success']}) "
                "left behind, so it may be out of date."
                if st["last_success"]
                else " I have never completed a sync, so I'm reading whatever was already on disk."
            )
            sync_line = (
                f"The sync is FAILING (last tried {st['last_attempt']}): {st['error']} "
                f"Fix: {st['remedy']}{stale}"
            )

        # 2. THE FILTER: how much came down, how much went into context, and how
        #    much was held back. The filter is EXCLUDE-based — everything loads
        #    except the product standups — so the honest phrasing is "loaded /
        #    excluded", not "qualified / didn't qualify".
        counts = (
            f"{st['docs_seen']} doc(s) on disk, {st['docs_loaded']} loaded, "
            f"{st['docs_excluded']} excluded (product standups)"
        )

        # 3. Nothing loaded is NOT "no notes" and NOT "no access". Under an
        #    exclude-based filter it means either everything that came down was a
        #    standup, or nothing that came down parsed as a dated note — and those
        #    have different fixes, so say which one it is.
        if not st["docs_loaded"]:
            undated = max(0, st["docs_seen"] - st["notes_seen"])
            why = []
            if st["docs_excluded"]:
                why.append(
                    f"{st['docs_excluded']} were excluded as standups by "
                    f"{st['exclude_patterns']}"
                )
            if undated:
                why.append(f"{undated} had no parseable date, so they aren't meeting notes")
            detail = (
                f"{sync_line} {counts} — nothing is loaded into context"
                + (": " + " and ".join(why) if why else "")
                + ". That is not the same as no meetings happening. Every synced doc is "
                "loaded EXCEPT titles matching "
                f"{st['exclude_patterns']}; widen or clear NOTES_EXCLUDE_TITLE_PATTERNS "
                "if a real meeting is being caught by it."
            )
        else:
            latest_date, _ = notes.freshness()
            detail = (
                f"{sync_line} {counts}; the most recent loaded note is from {latest_date}. "
                "Everything the sync pulls is read EXCEPT titles matching "
                f"{st['exclude_patterns']} — those are the product standups and they stay "
                "on disk, unread."
            )

        # DEGRADED, not CONNECTED, while the sync is broken: the notes are readable
        # but going stale, and an answer built on them has to say so.
        return (DEGRADED if st["degraded"] else CONNECTED, detail)

    def sync(self, question: str = "") -> dict:
        """Pull the freshest notes before answering. Blocking — call it via
        asyncio.to_thread. Returns the sync outcome; never raises."""
        return notes.sync_for_question(question)

    def latest(self) -> Optional[dict]:
        """The most recent LOADED meeting note, parsed — the newest note that isn't
        an excluded standup. None when unavailable."""
        return notes.read_note()


# Built once at import — these hold no connections, only configuration, so a
# module-level instance is cheap and every caller sees the same three.
SALES_SPREADSHEET = SalesSpreadsheet()
RESEARCHER_MAPPING = ResearcherMapping()
STRATEGY_DOC = StrategyDoc()
SALES_MEETING_NOTES = SalesMeetingNotes()
TODO_SHEET = TodoSheet()

ALL: list[Source] = [
    SALES_SPREADSHEET, RESEARCHER_MAPPING, STRATEGY_DOC, SALES_MEETING_NOTES,
    TODO_SHEET,
]


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
    """Just the sources the bot CANNOT reach — the blind spots it is required to
    volunteer when asked what it can do. A degraded source is deliberately not in
    here: it can be read, it just has to be read with a caveat (see
    `degraded()`)."""
    return [s for s in status_report() if s["status"] not in USABLE]


def degraded() -> list[dict]:
    """Sources that are readable but going stale — a failing notes sync, say. The
    bot may answer from these, but must say what is wrong with them."""
    return [s for s in status_report() if s["status"] == DEGRADED]


def describe_for_prompt() -> str:
    """The source statuses as a prompt block, phrased so the model states them
    honestly instead of implying it checked something it can't see."""
    lines = ["SOURCE STATUS (what you can and cannot actually see right now):"]
    for s in status_report():
        lines.append(f"- {s['label']} ({s['purpose']}): {s['status'].upper()} — {s['detail']}")
    lines.append(
        "You MUST NOT answer from a source marked AWAITING-ACCESS or ERROR, and you must "
        "SAY SO plainly when a question needs one of them. Never imply you checked "
        "something you cannot reach. A source marked DEGRADED may be used, but you MUST "
        "state the caveat in its detail line — that data is going stale — in the same "
        "breath as the answer you build from it."
    )
    return "\n".join(lines)
