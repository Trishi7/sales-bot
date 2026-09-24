"""bot_rules.yaml, loaded and validated once at startup.

THE RULES ARE A FILE, NOT CODE. `bot_rules.yaml` is the machine copy of the
"Bot Rules" tab of *Sales Bot_membrane*, and it decides WHICH rules exist, WHEN
each may run, HOW MUCH one post may carry, WHERE it goes, and whether it counts
against the day's cap. What stays in `nextaction.py` is only HOW a rule works
out that something is due.

That split is the whole design. A weekday, a cap or a destination is a product
decision Vaishnavi and Sid own; making one of those changes should be an edit
and a restart, not a developer's afternoon.

LOADED ONCE, AT STARTUP. Not per tick, and not per question. A rules file that
reloaded itself would mean two messages in the same day could run under two
different sets of rules, and the log line that explained the first would not
explain the second. `reload()` exists for the self-test and for an operator who
knows what they are asking for.

IT FAILS LOUD AND IT FAILS SAFE. A file that is missing, unparseable, or names
a trigger that does not exist is an ERROR with the reason and the fix — and the
engine then runs NO rules rather than a guessed subset. A bot that silently
dropped half its schedule would look exactly like a quiet week.
"""
import logging
import os
import re
import threading
from typing import Optional

import config

log = logging.getLogger(__name__)

# The weekday names the file uses, in the order Python's weekday() returns.
WEEKDAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_WEEKDAY_INDEX = {name: i for i, name in enumerate(WEEKDAY_NAMES)}

# Where a rule's output may go. The drip enforces it; `guardrails.py` still
# refuses a DM outright, so a rule asking for one is REPORTED, not sent — see
# the DM note at the foot of bot_rules.yaml.
DEST_CHANNEL = "channel"
DEST_DM = "dm"
DEST_ESCALATION = "escalation"
DESTINATIONS = (DEST_CHANNEL, DEST_DM, DEST_ESCALATION)

# Every trigger name `nextaction.py` implements. A rules file naming anything
# else is a startup error: the alternative is a rule that is configured, listed
# in the preview, and quietly computes nothing forever.
KNOWN_TRIGGERS = (
    "ai_news",
    "news_company_screen",
    "events",
    "deliverables",
    "prospects",
    "li_no_dm",
    "dm_no_meeting",
    "meeting_prep",
    "meeting_followup",
    "closure_support",
    "new_pipeline_company",
    "sales_packages",
)

# Triggers that cannot finish their item without web research. They still
# produce items — each carrying WEB_PENDING — so the schedule is real and
# visible before the research layer lands. See `needs_web`.
WEB_DEPENDENT = frozenset({
    "ai_news", "news_company_screen", "events", "li_no_dm",
    "meeting_prep", "closure_support", "new_pipeline_company",
})

# The placeholder a web-dependent item carries UNTIL ITS SEARCH HAS RUN.
#
# IT MEANS "NOT SEARCHED YET", NOT "NEEDS SEARCHING". The distinction became
# real the moment the rules started actually searching: an item whose search ran
# and came back empty is a fact about a quiet day, and leaving the marker on it
# would say the opposite — that the bot never looked. So the research layer
# clears it on BOTH outcomes (see `bot._mark_unresearched`), and the marker
# survives only where search genuinely did not happen: it is off, the budget is
# spent, or the call failed. In every one of those cases the item also carries a
# `research_note` saying which, because "web research pending" on its own is a
# status and not a reason.
WEB_PENDING = "web research pending"


# WHAT EACH RULE IS, IN WORDS SOMEBODY OUTSIDE THE TEAM WOULD FOLLOW.
#
# WHY THIS IS NOT `name`. The names are the Bot Rules tab's own headings and
# they are written for people who already know the schedule — "LinkedIn
# connected, no DM" is a column heading, not a sentence. These are the sentence.
#
# WHY IT EXISTS AT ALL. "R6" means nothing to anyone who has not read
# bot_rules.yaml, and the model repeats whatever the tools hand it — so the
# team was reading answers about "R6" and "R1" in a sales channel. An id is an
# internal key: it belongs in the log and in `cadence preview`, where somebody
# is deliberately looking at the machinery, and nowhere else. See
# `render_for_user`.
#
# KEYED ON THE TRIGGER, NOT THE ID, because the trigger is what the rule DOES —
# an id can be retired and replaced, and the replacement means the same thing.
# A rule may override its own wording with a `plain:` line in bot_rules.yaml.
PLAIN_BY_TRIGGER = {
    "ai_news": "today's AI news worth reading",
    "news_company_screen": "companies in the news that aren't in the pipeline yet",
    "events": "AI events and summits coming up",
    "deliverables": "deliverables due or overdue",
    "prospects": "people we haven't made first contact with",
    "li_no_dm": "people connected on LinkedIn with no DM yet",
    "dm_no_meeting": "people we DM'd who haven't booked a meeting",
    "meeting_prep": "meetings coming up that need prep",
    "meeting_followup": "meetings that happened with no next steps recorded",
    "closure_support": "deals close enough to push over the line",
    "new_pipeline_company": "companies that just appeared in the pipeline",
    "sales_packages": "sales packages that aren't ready yet",
}

# A rule id as it appears in text: R1 to R99. Two digits, because the schedule
# is twelve rules today and a hard limit of nine would be an odd thing to build
# in.
_RULE_ID_RE = re.compile(r"\bR(\d{1,2})\b")

# The pieces `render_for_user` assembles per rule. Separate constants because
# they are built into patterns at runtime and a literal backslash buried in an
# f-string is how a working regex quietly stops matching.
RULE_ID_BOUNDARY = r"\b"
SEPARATOR_THEN = r"[ \t]*[-:–—]?[ \t]*"

# Tidy-up after a removal: doubled spaces, a separator left with nothing in
# front of it, a space pushed up against punctuation, and blank edges.
_DOUBLE_SPACE_RE = re.compile(r"[ \t]{2,}")
_ORPHAN_SEP_RE = re.compile(r"(^|\n)[ \t]*[-:–—][ \t]*")
_SPACE_BEFORE_PUNCT_RE = re.compile(r"[ \t]+([,.;:!?)])")
_BLANK_LINE_EDGE_RE = re.compile(r"^[ \t]+|[ \t]+$", re.M)


class RulesError(Exception):
    """The rules file could not be used. Carries a `remedy` naming the fix,
    because "invalid YAML" on its own has never helped anybody."""

    def __init__(self, message: str, *, remedy: str = ""):
        super().__init__(message)
        self.remedy = remedy


class Rule:
    """One row of the Bot Rules tab.

    Immutable by convention: the engine reads these and never edits them, so a
    rule cannot mean one thing to the preview and another to the queue.
    """

    __slots__ = ("id", "name", "plain", "weekdays", "trigger",
                 "max_items_per_post", "destination", "counts_toward_cap",
                 "enabled", "_order")

    def __init__(self, *, id: str, name: str, weekdays: tuple, trigger: str,
                 max_items_per_post: int, destination: str,
                 counts_toward_cap: bool, enabled: bool, order: int = 0,
                 plain: str = ""):
        self.id = id
        self.name = name
        # What this rule is, in words somebody outside the team would follow.
        # See PLAIN_BY_TRIGGER for why it exists and why it is not `name`.
        self.plain = plain or PLAIN_BY_TRIGGER.get(trigger, "") or name
        self.weekdays = weekdays            # tuple of 0..6, Monday = 0
        self.trigger = trigger
        self.max_items_per_post = max_items_per_post
        self.destination = destination
        self.counts_toward_cap = counts_toward_cap
        self.enabled = enabled
        self._order = order

    @property
    def anchored(self) -> bool:
        """True when the rule has NO weekdays and is anchored to a date on the
        sheet instead — R8 to a meeting, R9 to a completed meeting.

        An empty weekday list is therefore not "never runs", it is "runs when
        its anchor says so". Reading it as never would silently switch off
        meeting prep, which is the one thing on this schedule with a hard
        external deadline.
        """
        return not self.weekdays

    def runs_on(self, day) -> bool:
        """Does this rule run on `day`? Anchored rules always get to look."""
        if not self.enabled:
            return False
        if self.anchored:
            return True
        return day.weekday() in self.weekdays

    @property
    def needs_web(self) -> bool:
        return self.trigger in WEB_DEPENDENT

    def weekday_label(self) -> str:
        if self.anchored:
            return "anchored to the meeting date"
        return ", ".join(WEEKDAY_NAMES[i].capitalize() for i in self.weekdays) or "never"

    def describe(self) -> str:
        return (
            f"{self.id} {self.name} — {self.weekday_label()}, "
            f"max {self.max_items_per_post}/post, {self.destination}, "
            f"{'counts toward' if self.counts_toward_cap else 'OUTSIDE'} the cap"
            + ("" if self.enabled else "  [DISABLED]")
        )

    def as_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "plain": self.plain,
            "trigger": self.trigger,
            "weekdays": self.weekday_label(),
            "max_items_per_post": self.max_items_per_post,
            "destination": self.destination,
            "counts_toward_cap": self.counts_toward_cap,
            "enabled": self.enabled,
            "needs_web": self.needs_web,
        }


_lock = threading.RLock()
_cache: dict = {"loaded": False, "rules": [], "source": {}, "error": "", "remedy": ""}


def _fail(message: str, remedy: str) -> None:
    raise RulesError(message, remedy=remedy)


def _parse_weekdays(raw, rule_id: str) -> tuple:
    if raw is None:
        return ()
    if not isinstance(raw, (list, tuple)):
        _fail(f"{rule_id}: 'weekdays' must be a list, got {type(raw).__name__}",
              "Write it as a YAML list, e.g. weekdays: [mon, tue].")
    out = []
    for item in raw:
        key = str(item or "").strip().lower()[:3]
        if key not in _WEEKDAY_INDEX:
            _fail(f"{rule_id}: {item!r} is not a weekday",
                  "Use three-letter names: mon tue wed thu fri sat sun.")
        index = _WEEKDAY_INDEX[key]
        if index not in out:
            out.append(index)
    return tuple(sorted(out))


def _parse_rule(raw: dict, *, order: int) -> Rule:
    if not isinstance(raw, dict):
        _fail(f"rule #{order + 1} is not a mapping",
              "Each entry under 'rules:' is a block of key: value lines.")

    rule_id = str(raw.get("id") or "").strip()
    if not rule_id:
        _fail(f"rule #{order + 1} has no 'id'",
              "Give it a stable id like R7. Ids are never renumbered or reused.")

    name = str(raw.get("name") or "").strip() or rule_id
    trigger = str(raw.get("trigger") or "").strip()
    if trigger not in KNOWN_TRIGGERS:
        _fail(
            f"{rule_id}: trigger {trigger!r} is not implemented",
            "Use one of: " + ", ".join(KNOWN_TRIGGERS) + ". A trigger nothing "
            "implements would leave the rule configured, listed in the preview, "
            "and computing nothing forever — so it is refused at startup.",
        )

    try:
        max_items = int(raw.get("max_items_per_post", 5))
    except (TypeError, ValueError):
        max_items = -1
    if max_items < 1:
        _fail(f"{rule_id}: 'max_items_per_post' must be 1 or more",
              "A cap of 0 is a disabled rule — set enabled: false instead, so "
              "the reason it existed stays readable.")

    destination = str(raw.get("destination") or DEST_CHANNEL).strip().lower()
    if destination not in DESTINATIONS:
        _fail(f"{rule_id}: destination {destination!r} is not one of "
              + "/".join(DESTINATIONS),
              "Use channel, dm or escalation.")

    return Rule(
        id=rule_id,
        name=name,
        plain=" ".join(str(raw.get("plain") or "").split()).strip(),
        weekdays=_parse_weekdays(raw.get("weekdays"), rule_id),
        trigger=trigger,
        max_items_per_post=max_items,
        destination=destination,
        counts_toward_cap=bool(raw.get("counts_toward_cap", True)),
        enabled=bool(raw.get("enabled", True)),
        order=order,
    )


def _read(path: str) -> dict:
    try:
        import yaml
    except ImportError:
        _fail("PyYAML is not installed, so bot_rules.yaml cannot be read",
              "pip install -r requirements.txt")

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except FileNotFoundError:
        _fail(f"the rules file {path!r} does not exist",
              "Create it at the repo root, or point BOT_RULES_FILE at it. It is "
              "the machine copy of the 'Bot Rules' tab.")
    except OSError as e:
        _fail(f"the rules file {path!r} could not be read ({type(e).__name__})",
              "Check the file's permissions.")
    except Exception as e:                     # yaml.YAMLError and friends
        _fail(f"the rules file {path!r} is not valid YAML: {e}",
              "Fix the syntax. Indentation is two spaces and lists start with '- '.")

    if not isinstance(data, dict):
        _fail(f"the rules file {path!r} does not hold a mapping",
              "The top level needs 'version:', 'source:' and 'rules:' keys.")
    return data


def load(path: Optional[str] = None, *, force: bool = False) -> list:
    """Every rule in the file, in file order. Cached after the first call.

    Raises RulesError when the file cannot be used. Callers that must not die
    on a bad file use `safe_load()` instead, which logs and returns [].
    """
    path = (path or config.BOT_RULES_FILE or "").strip()
    with _lock:
        if _cache["loaded"] and not force:
            if _cache["error"]:
                raise RulesError(_cache["error"], remedy=_cache["remedy"])
            return list(_cache["rules"])

    data = _read(path)
    raw_rules = data.get("rules")
    if not isinstance(raw_rules, list) or not raw_rules:
        _fail(f"the rules file {path!r} defines no rules",
              "Add entries under 'rules:'. An empty schedule means the bot says "
              "nothing on its own, ever.")

    rules: list = []
    seen: dict = {}
    for i, raw in enumerate(raw_rules):
        rule = _parse_rule(raw, order=i)
        if rule.id in seen:
            _fail(f"duplicate rule id {rule.id!r}",
                  "Ids are the key the dedup ledger and the SQLite state are "
                  "written against, so two rules cannot share one.")
        seen[rule.id] = rule
        rules.append(rule)

    source = data.get("source") if isinstance(data.get("source"), dict) else {}

    with _lock:
        _cache.update({
            "loaded": True, "rules": list(rules), "source": dict(source),
            "error": "", "remedy": "",
        })
    return list(rules)


def safe_load(path: Optional[str] = None, *, force: bool = False) -> list:
    """`load()`, but a broken file logs at ERROR and yields NO rules.

    NO RULES, NOT SOME RULES. A partially-applied schedule is the failure this
    avoids: the bot would run whatever parsed, look healthy, and be silently
    missing a rule nobody noticed. An empty list makes every downstream report
    say "no rules loaded", which is the true statement and an obvious one.
    """
    try:
        return load(path, force=force)
    except RulesError as e:
        with _lock:
            _cache.update({
                "loaded": True, "rules": [], "source": {},
                "error": str(e), "remedy": e.remedy,
            })
        log.error(
            "[rules] NO RULES LOADED: %s. %s The bot will not say anything on "
            "its own initiative until this is fixed.", e, e.remedy,
        )
        return []


def reload(path: Optional[str] = None) -> list:
    """Re-read the file. For the self-test and for an operator who asked."""
    return safe_load(path, force=True)


def status() -> dict:
    """{path, loaded, count, enabled, error, remedy, source} — what the bot is
    actually running on, for the boot report and for `cadence preview`."""
    with _lock:
        rules = list(_cache["rules"])
        error, remedy, source = _cache["error"], _cache["remedy"], dict(_cache["source"])
    return {
        "path": (config.BOT_RULES_FILE or "").strip(),
        "loaded": bool(rules),
        "count": len(rules),
        "enabled": sum(1 for r in rules if r.enabled),
        "error": error,
        "remedy": remedy,
        "source": source,
    }


def plain_description(rule_id: str) -> str:
    """"R6" -> "people connected on LinkedIn with no DM yet". "" when unknown.

    The unknown case is deliberately empty rather than the id itself: an id we
    cannot explain is an id the reader definitely cannot, and echoing it back
    is the behaviour this whole function exists to stop.
    """
    rule = by_id(str(rule_id or "").strip().upper())
    return rule.plain if rule is not None else ""


def render_for_user(text: str) -> str:
    """The USER-FACING rendering of any text that might carry rule ids.

    "R6 LinkedIn connected, no DM: 4" becomes "people connected on LinkedIn
    with no DM yet: 4". An id with no rule behind it is REMOVED rather than
    passed through — see `plain_description`.

    WHY A RENDERING MODE AND NOT A FIX AT THE SOURCE. The ids are real and they
    are load-bearing: they key the dedup ledger, the SQLite state, `cadence
    preview` and every log line, and a team member reading the boot log needs
    them. It is only the OUTWARD face of the bot that must not use them, so the
    translation belongs at the boundary — one function, called once on the way
    out, rather than a dozen call sites each remembering.

    TWO PASSES, AND THE ORDER MATTERS. The id usually arrives with its own name
    behind it ("R6 LinkedIn connected, no DM"), so the pair is replaced first
    and as a unit; replacing the id alone would leave the heading behind and
    read as two things where there is one. Whatever id survives that is a bare
    one, and is replaced on its own.
    """
    body = str(text or "")
    if not body or "R" not in body:
        return body

    try:
        loaded = safe_load()
    except Exception:                      # never break a send over a rules file
        log.debug("[rules] could not load rules to render user-facing text",
                  exc_info=True)
        loaded = []

    for rule in loaded:
        if not rule.name:
            continue
        pair = re.compile(
            RULE_ID_BOUNDARY + re.escape(rule.id) + RULE_ID_BOUNDARY
            + SEPARATOR_THEN + re.escape(rule.name)
            + RULE_ID_BOUNDARY,
            re.IGNORECASE,
        )
        body = pair.sub(lambda _m, r=rule: r.plain, body)

    known = {r.id.upper(): r for r in loaded}
    body = _RULE_ID_RE.sub(
        lambda m: getattr(known.get("R" + m.group(1)), "plain", ""), body
    )
    return _tidy(body)


def _tidy(text: str) -> str:
    """Close the gaps a removed id leaves behind.

    A stripped id leaves "  " or a dangling "— " that reads as a typo, and a
    message that looks mangled gets less trust than one that says nothing.
    """
    out = _DOUBLE_SPACE_RE.sub(" ", text)
    out = _ORPHAN_SEP_RE.sub(lambda m: m.group(1), out)
    out = _SPACE_BEFORE_PUNCT_RE.sub(lambda m: m.group(1), out)
    return _BLANK_LINE_EDGE_RE.sub("", out).strip()


def by_id(rule_id: str) -> Optional[Rule]:
    for rule in safe_load():
        if rule.id == str(rule_id):
            return rule
    return None


def for_day(day) -> list:
    """The enabled rules that may run on `day`, in file order.

    Anchored rules (empty weekday list) are always included — their own
    evaluator decides whether anything is due, because their anchor is a
    meeting date on the sheet and not the calendar.
    """
    return [r for r in safe_load() if r.runs_on(day)]


def log_startup() -> None:
    """One block at boot naming every rule and when it runs.

    This is the line somebody reads to answer "why did nothing go out on
    Tuesday" without opening the YAML or the code.

    IT LOADS BEFORE IT REPORTS. `status()` reads the cache and does not fill
    it, so asking it first — which this used to do — described the cache at the
    instant before anything had loaded: an empty rule list and an empty error
    string, reported as "could not be loaded (unknown error). NOTHING proactive
    will run", moments before the rules loaded fine and ran all day. The report
    was the only thing broken, which is the worst kind of broken: the boot log
    is where somebody goes to find out whether the schedule is alive, and it
    was lying to them.

    AND IT NEVER SAYS "unknown error". A failure with no reason attached is not
    a diagnosis, it is a shrug. `safe_load()` records a reason for every genuine
    failure, so if the rule list is empty and no reason came with it, the file
    parsed and simply held nothing — which is a different fault with a different
    fix, and this says so.
    """
    rules_now = safe_load()
    st = status()
    if not st["loaded"]:
        log.error(
            "[rules] %s could not be loaded: %s%s NOTHING proactive will run.",
            st["path"] or "bot_rules.yaml",
            st["error"] or (
                "the file was read but produced no rules — the cache is empty "
                "and no error was recorded, which means the 'rules:' list "
                "parsed as empty rather than failing"
            ),
            (" " + st["remedy"]) if st["remedy"] else
            " Check that 'rules:' in the file lists at least one rule.",
        )
        return
    src = st["source"] or {}
    log.info(
        "[rules] ===== %d rule(s) from %s (%s / %s, %s) =====",
        st["count"], st["path"],
        src.get("sheet") or "?", src.get("tab") or "?",
        src.get("amended") or "unamended",
    )
    for rule in rules_now:
        log.info("[rules]   %s%s", rule.describe(),
                 "   [needs web research]" if rule.needs_web else "")
    disabled = st["count"] - st["enabled"]
    if disabled:
        log.warning(
            "[rules] %d rule(s) are DISABLED in %s and will produce nothing.",
            disabled, st["path"],
        )
    log.info("[rules] ===== end of rules =====")


def _self_test() -> int:
    """`python -m rules` — the file parses and means what it says."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(name)s: %(message)s")
    from datetime import date
    failures = 0

    def check(name, got, want):
        nonlocal failures
        ok = got == want
        failures += 0 if ok else 1
        print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}, want {want!r}")

    rules = reload()
    print("loading")
    check("twelve rules load", len(rules), 12)
    check("ids are R1..R12", [r.id for r in rules],
          [f"R{i}" for i in range(1, 13)])
    check("every trigger is implemented",
          sorted({r.trigger for r in rules}) == sorted(set(KNOWN_TRIGGERS)), True)

    print("\nweekdays")
    mon, tue, wed, thu, fri = (date(2026, 9, 21), date(2026, 9, 22),
                               date(2026, 9, 23), date(2026, 9, 24),
                               date(2026, 9, 25))
    check("R4 runs Monday", by_id("R4").runs_on(mon), True)
    check("R4 does not run Tuesday", by_id("R4").runs_on(tue), False)
    check("R5 runs Tue and Thu",
          (by_id("R5").runs_on(tue), by_id("R5").runs_on(thu)), (True, True))
    check("R5 does not run Wednesday", by_id("R5").runs_on(wed), False)
    check("R12 runs Thursday only",
          [by_id("R12").runs_on(d) for d in (mon, tue, wed, thu, fri)],
          [False, False, False, True, False])
    check("R1 runs every weekday",
          all(by_id("R1").runs_on(d) for d in (mon, tue, wed, thu, fri)), True)
    check("R1 does not run Saturday", by_id("R1").runs_on(date(2026, 9, 26)), False)

    print("\nanchored rules")
    check("R8 is anchored", by_id("R8").anchored, True)
    check("R9 is anchored", by_id("R9").anchored, True)
    check("an anchored rule always gets to look",
          by_id("R8").runs_on(wed), True)
    check("R8 is outside the cap", by_id("R8").counts_toward_cap, False)
    check("R9 is outside the cap", by_id("R9").counts_toward_cap, False)

    print("\ncaps and destinations")
    check("R5 carries 5 per post", by_id("R5").max_items_per_post, 5)
    check("R11 carries 3 per post", by_id("R11").max_items_per_post, 3)
    check("every destination is valid",
          all(r.destination in DESTINATIONS for r in rules), True)

    print("\nweb dependence")
    check("R1 needs web", by_id("R1").needs_web, True)
    check("R4 does not", by_id("R4").needs_web, False)
    check("R12 does not", by_id("R12").needs_web, False)

    print("\nfor_day")
    check("Monday's rules", [r.id for r in for_day(mon)],
          ["R1", "R4", "R7", "R8", "R9", "R10", "R11"])
    check("Thursday's rules", [r.id for r in for_day(thu)],
          ["R1", "R5", "R8", "R9", "R11", "R12"])
    check("Saturday's rules are the anchored ones only",
          [r.id for r in for_day(date(2026, 9, 26))], ["R8", "R9"])

    print(f"\n{'ALL PASSED' if not failures else str(failures) + ' FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_self_test())
