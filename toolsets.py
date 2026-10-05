"""WHICH TOOLS A QUESTION NEEDS, and what each is told about itself.

THE ENGINE USED TO CARRY EVERY TOOL ON EVERY QUESTION — thirty-odd definitions,
several of them a paragraph long, re-sent on each iteration of the loop. A
question about today's AI news read the full contract of the mapping sheet
three times over to answer it. Two changes, both here:

    select()   hands the engine the tools the question's own words call for —
               news/web gets web_search, fetch_page and strategy_doc; a sheet
               question gets the sheet and mapping tools; notes get the notes
               tools; reminders the reminder tools. Usually under ten. THE FULL
               SET IS SENT ONLY WHEN THE QUESTION IS UNCLEAR — nothing matched,
               which is what a bare follow-up ("and Globex?") looks like.

    slim()     gives every tool a ONE-SENTENCE description. The long versions
               stay in bot.py beside the handlers, as the documentation they
               are; the rules that matter at answer time are already in the
               engine's system prompt and in each tool's own result.

ROUTING IS BY THE QUESTION'S WORDS, DETERMINISTICALLY — no model call decides
it, so it costs nothing and the same question always gets the same tools. The
patterns are generous and the groups overlap on purpose: sending two tools too
many costs a few hundred tokens, and sending one too few costs the answer. The
log line names the groups chosen, so a misroute is one grep away.
"""
import logging
import re

log = logging.getLogger(__name__)

# ONE SENTENCE EACH. What the tool is for, and the one rule that cannot wait
# for the result. A tool not listed here keeps the description it came with.
ONE_LINE = {
    "recent_sales_activity": (
        "Recent sales-channel posts by ONE named person, newest first — for "
        "'what has X been doing' or 'did X follow up'; if the name is "
        "ambiguous, ask which person rather than picking."),
    "recent_channel_activity": (
        "A digest of what happened across the sales channels over the last N "
        "days, not scoped to a person — for 'what's been going on' or "
        "'anything I missed'; quote the window it reports."),
    "search_channel_history": (
        "Keyword-search the sales channels' history, each match returned with "
        "the messages around it — use whenever a question asks whether "
        "something happened, was sent, was agreed or was discussed."),
    "open_chases": (
        "The commitments people made in the sales channels that I am still "
        "waiting on — my own tracking, not a pipeline or a task list."),
    "list_meeting_notes": (
        "List the recent meeting notes on file (date, label, title) with sync "
        "and freshness — for when it is unclear which meeting is meant or "
        "what notes exist."),
    "read_meeting_note": (
        "Read ONE meeting note (summary, decisions, next steps) by date "
        "(YYYY-MM-DD) and optional label; omit the date for the most recent."),
    "meeting_facts": (
        "Holds, decisions and commitments from the meeting notes, each with a "
        "citation you must print — for 'is X on hold' or 'what did we decide "
        "about X'."),
    "show_todos": (
        "The team to-do sheet's link and its open items — always print both."),
    "todo_candidates": (
        "Action items from this week's meeting notes that could go on the "
        "to-do sheet; read-only."),
    "strategy_doc": (
        "The sales & marketing strategy doc: what it says, when it was last "
        "revised, and the targets it names."),
    "outreach_vs_plan": (
        "Compare where outreach actually went against the strategy doc's "
        "targets, in both directions — for 'are we on plan'."),
    "lookup_company": (
        "Everything the outreach tracker holds about ONE company plus its "
        "deadlines — use first for 'where are we with X' or 'who's the PoC at "
        "X', cite the tab, and never fill a blank cell from elsewhere."),
    "query_tracker": (
        "Filtered slices of the outreach tracker for questions about many rows "
        "(filter: all, open, no_response, responded, never_contacted, "
        "no_next_step, no_poc, meetings) — quote match_count and cite the tab."),
    "prospect_priority": (
        "The prospect-priority tab (P1/P2/P3 with a rationale), optionally "
        "cross-checked against who has been contacted — quote the rationale as "
        "written."),
    "positioning_matrix": (
        "The positioning matrix (use case, problem, offering, company type, "
        "ICP, impact) — for 'what do we pitch to X'; quote its own words."),
    "sheet_status": (
        "What I am actually reading: every tab found, row counts, active rows "
        "and what I may write — for 'which sheet or tab are you on' or 'why "
        "aren't you chasing X'."),
    "activate_rows": (
        "Activate rows at an org that have no first-contact or connection date "
        "so the proactive rules can see them — pass 'org', optionally 'names', "
        "and report exactly which rows changed."),
    "research_brief": (
        "A research brief on ONE person already on the tab — who they are, how "
        "much the role weighs, lane fit, an angle and a draft message — as copy "
        "material that is never sent and never written to the sheet."),
    "cadence_preview": (
        "Today's due items from the twelve rules, grouped by rule, read-only — "
        "for 'what needs attention' or 'what's the queue'; answer grouped by "
        "rule."),
    "next_action": (
        "The one next action for a named company or PoC, or the specific "
        "reason there is none (stopped, snoozed, no readable date, nothing due "
        "yet)."),
    "snooze_row": (
        "Snooze a row until a date ('follow up in 5 days') — pass 'days' or "
        "'date'; it writes to my own records only, so report the date back."),
    "schedule_reminder": (
        "Set a one-off reminder at an exact time for anything, company or not "
        "— reply with the 'confirm' line it returns."),
    "list_reminders": (
        "The open one-off reminders — the asker's by default, everyone=true "
        "for the whole team's."),
    "cancel_reminder": (
        "Cancel one open reminder by its id; call list_reminders first if you "
        "do not know the id."),
    "set_deadline": (
        "Set a deadline for a company when none exists and announce it — use "
        "when asked about a due date and there isn't one."),
    "list_deadlines": (
        "The deadlines I am holding, all open ones or one company's, with who "
        "set each."),
    "who_to_pitch": (
        "The researcher/buyer mapping — who to pitch at an org, in a lane or "
        "at a tier — and always quote person, org, tier AND confidence as "
        "separate facts."),
    "mapping_rules": (
        "The mapping sheet's legend and rules: the lanes, the tier "
        "definitions, how stale it is, the departures list and the excluded "
        "orgs."),
    "mapping_coverage": (
        "Which orgs the mapping covers and how many researchers each has — "
        "set only_gaps for orgs with none, and always quote the account note."),
    "mapping_edges": (
        "Warm-intro paths between orgs and people (shared labs, investors, "
        "co-authors) — routes, not targets, so check anyone named with "
        "who_to_pitch."),
    "cross_check_outreach": (
        "Join the outreach tracker with the researcher mapping for one org — "
        "who we are talking to versus who we should be, including the "
        "departures check."),
    "find_people": (
        "Find named people (PoCs) at a company, optionally in one team, from "
        "search results, each with the page that names them; never guesses a "
        "name, title or email."),
    "web_search": (
        "Search the web and get back up to 8 titles and snippets with their "
        "urls — for news, funding, launches and anything about the outside "
        "world."),
    "fetch_page": (
        "Read one public web page (never LinkedIn) by its url, when a snippet "
        "is not enough."),
}

# THE GROUPS. Small on purpose; a tool may sit in several.
GROUPS = {
    "web": ("web_search", "fetch_page", "strategy_doc"),
    "people": ("find_people", "lookup_company", "who_to_pitch",
               "cross_check_outreach"),
    "sheet": ("lookup_company", "query_tracker", "prospect_priority",
              "positioning_matrix", "next_action", "sheet_status",
              "who_to_pitch", "cross_check_outreach"),
    "mapping": ("who_to_pitch", "mapping_rules", "mapping_coverage",
                "mapping_edges", "cross_check_outreach", "lookup_company"),
    "notes": ("list_meeting_notes", "read_meeting_note", "meeting_facts",
              "todo_candidates", "show_todos"),
    "reminders": ("schedule_reminder", "list_reminders", "cancel_reminder",
                  "snooze_row", "set_deadline", "list_deadlines"),
    "channel": ("recent_sales_activity", "recent_channel_activity",
                "search_channel_history", "open_chases"),
    "plan": ("strategy_doc", "outreach_vs_plan", "positioning_matrix"),
    "ops": ("cadence_preview", "next_action", "sheet_status", "activate_rows",
            "snooze_row"),
    "brief": ("research_brief", "lookup_company", "who_to_pitch"),
}

_I = re.IGNORECASE
_ROUTES = (
    ("web", re.compile(
        r"\b(news|latest|announce\w*|funding|funded|rais(e|ed|es|ing)|acqui\w+|"
        r"launch\w*|hiring|conference\w*|summit\w*|papers?|published|web|online|"
        r"google|search|look\s+(it\s+)?up|what'?s\s+new|in\s+the\s+news|"
        r"industry|market|competitors?)\b", _I)),
    ("people", re.compile(
        r"\b(find|get|look\s+for)\b.{0,40}\b(pocs?|people|contacts?|someone)\b|"
        r"who\s+should\s+(we|i)\s+(contact|reach|talk\s+to)|\bpeople\s+(at|in)\b|"
        r"\bpocs?\s+(at|in|for)\b", _I)),
    ("reminders", re.compile(
        r"\bremind\w*|\bping\s+me\b|\bsnooze\b|follow\s+up\s+(in|on)\b|"
        r"come\s+back\s+to\b|\bdeadlines?\b|\bdue\s+date\b|what'?s\s+due\b|"
        r"leave\s+(them|it|him|her)\s+alone", _I)),
    ("notes", re.compile(
        r"\bmeetings?\b|\bnotes?\b|\bstandup\b|\bsync\b|\bcall\s+(with|notes)\b|"
        r"\bdecid\w+|\bdecisions?\b|\bon\s+hold\b|\baction\s+items?\b|"
        r"\bto-?\s?dos?\b|what\s+do\s+i\s+owe|came\s+out\s+of", _I)),
    ("channel", re.compile(
        r"anything\s+i\s+missed|what'?s\s+been\s+(going\s+on|happening)|"
        r"\bdid\s+(we|anyone|anybody|\w+)\s+(ever\s+)?(send|sent|follow|say|said|"
        r"reply|agree)|has\s+(anyone|anybody)\b|who\s+said\b|in\s+(the\s+)?channel|"
        r"\bpromised?\b|\bchasing\b|\boutstanding\b|what\s+has\s+\w+\s+been|"
        r"what\s+did\s+we\s+agree|working\s+on\s+this\s+week", _I)),
    ("mapping", re.compile(
        r"who\s+(do|should)\s+we\s+pitch|pitch\s+hook|\bmapping\b|\bmapped\b|"
        r"\bresearchers?\b|\bT[123]\b|\btiers?\b|\bchampions?\b|\blanes?\b|"
        r"warm\s+intro|\bway\s+in\b|how\s+do\s+we\s+get\s+to|\bdepart\w+|"
        r"who\s+covers|\bleft\s+(the\s+)?company", _I)),
    ("sheet", re.compile(
        r"where\s+are\s+we\s+with|\bstatus\s+of\b|\bpipeline\b|\btracker\b|"
        r"\bsheet\b|\bpocs?\b|\breplied\b|\bno\s+response\b|\bnext\s+steps?\b|"
        r"\bprospects?\b|\bP[123]s?\b|\bpriorit\w+|\bcontacted\b|\bdeals?\b|"
        r"\bclosure\b|what\s+do\s+we\s+pitch|\buse\s+cases?\b|\boffering\b|"
        r"company\s+type|\bICP\b|\boutreach\b|what\s+did\s+we\s+send", _I)),
    ("plan", re.compile(
        r"\bstrategy\b|\bthe\s+plan\b|\bon\s+plan\b|\btargets?\b|\bsegments?\b|"
        r"\bdrift\w*|\btargeting\b", _I)),
    ("ops", re.compile(
        r"\bcadence\b|rules?\s+preview|\bqueue\b|what\s+needs\b|\bslipping\b|"
        r"\burgent\b|what\s+would\s+you\s+chase|\bactivate\b|start\s+chasing|"
        r"what'?s\s+next\s+for|why\s+aren'?t\s+you\s+chasing|what\s+can\s+you\s+"
        r"write|which\s+tabs?\b|sheet\s+status|when\s+is\s+\w+\s+due", _I)),
    ("brief", re.compile(
        r"\bbrief\s+me\b|what\s+should\s+i\s+say\s+to|what\s+do\s+we\s+open\s+with|"
        r"\bbrief\s+on\b", _I)),
)


def route(text: str) -> list:
    """The groups this question's words call for, in a stable order. [] when
    nothing matched — which is "unclear", and gets the full set."""
    body = str(text or "")
    return [name for name, pattern in _ROUTES if pattern.search(body)]


def select(tools: list, text: str, *, previous: str = "") -> tuple:
    """(tools to send, groups, why). `tools` are the engine's entries
    ({"schema", "handler"}); the subset keeps their order.

    A FOLLOW-UP INHERITS ITS QUESTION. "and Globex?" matches nothing by itself;
    `previous` — the question before it in this channel — is routed with it, so
    the follow-up gets the tools the conversation was already using.

    FULL SET WHEN UNCLEAR: nothing matched even with `previous`, or the groups
    named no tool that is actually available this turn.
    """
    groups = route(text)
    why = "the question's words"
    if not groups and (previous or "").strip():
        groups = route(previous)
        why = "the previous question (a follow-up)"
    if not groups:
        return list(tools or []), [], "unclear — the full set"
    wanted: set = set()
    for name in groups:
        wanted.update(GROUPS.get(name, ()))
    picked = [t for t in (tools or []) if t["schema"]["name"] in wanted]
    if not picked:
        return list(tools or []), [], "no routed tool is available — the full set"
    return picked, groups, why


def slim(tools: list) -> list:
    """The same tools with ONE-SENTENCE descriptions. Handlers and parameter
    schemas are untouched; the caller's own list is not modified."""
    out: list = []
    for t in tools or []:
        schema = dict(t["schema"])
        short = ONE_LINE.get(schema.get("name") or "")
        if short and "description" in schema:
            schema["description"] = short
        out.append({**t, "schema": schema})
    return out


def _self_test() -> int:
    """`python -m toolsets` — routing and the one-line descriptions."""
    failures = 0

    def check(name, got, want):
        nonlocal failures
        ok = got == want
        failures += 0 if ok else 1
        print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}, want {want!r}")

    every = sorted({n for names in GROUPS.values() for n in names})
    tools = [{"schema": {"name": n, "description": "long " * 60,
                         "input_schema": {"type": "object"}}, "handler": None}
             for n in every]

    def names(text, previous=""):
        picked, groups, _ = select(tools, text, previous=previous)
        return [t["schema"]["name"] for t in picked], groups

    print("routing")
    got, groups = names("what's the latest AI news today?")
    check("news -> the web group", groups, ["web"])
    check("...web_search, fetch_page, strategy_doc",
          sorted(got), ["fetch_page", "strategy_doc", "web_search"])
    got, groups = names("where are we with Acme?")
    check("a sheet question -> sheet", groups, ["sheet"])
    check("...under ten tools", len(got) < 10, True)
    check("...with the mapping join", "cross_check_outreach" in got, True)
    got, groups = names("remind me tomorrow at 2pm about the deck")
    check("a reminder -> the reminder tools", groups, ["reminders"])
    check("...schedule_reminder is there", "schedule_reminder" in got, True)
    got, groups = names("what did we decide in yesterday's meeting?")
    check("notes -> the notes tools", "read_meeting_note" in got and "notes" in groups,
          True)
    got, groups = names("find PoCs at Shunya Labs")
    check("find PoCs -> people", "find_people" in got, True)
    got, groups = names("who do we pitch at Anthropic for evals?")
    check("mapping", "who_to_pitch" in got and "mapping" in groups, True)

    print("\nunclear, and follow-ups")
    got, groups = names("and Globex?")
    check("nothing matched -> the full set", (len(got), groups), (len(every), []))
    got, groups = names("and Globex?", previous="where are we with Acme?")
    check("a follow-up inherits its question's group", groups, ["sheet"])
    picked, groups, why = select(tools[:0], "latest news")
    check("no routed tool available -> whatever there is", picked, [])

    print("\nthe target")
    asks = ("what's the latest AI news today?", "where are we with Acme?",
            "remind me tomorrow at 2pm about the deck", "what did we decide "
            "in yesterday's meeting?", "find PoCs at Shunya Labs",
            "anything I missed this week?", "cadence preview")
    sizes = [len(names(a)[0]) for a in asks]
    check("every single-topic question is under ten tools",
          all(s < 10 for s in sizes), True)

    print("\none sentence each")
    check("every grouped tool has a one-liner",
          sorted(n for n in every if n not in ONE_LINE), [])
    slimmed = slim(tools)
    check("the description is replaced",
          slimmed[0]["schema"]["description"], ONE_LINE[every[0]])
    check("the caller's list is untouched",
          tools[0]["schema"]["description"].startswith("long"), True)
    check("the parameters survive", slimmed[0]["schema"]["input_schema"],
          {"type": "object"})
    longest = max(len(v) for v in ONE_LINE.values())
    check("no one-liner runs past 260 characters", longest <= 260, True)

    print(f"\n{'ALL PASSED' if not failures else str(failures) + ' FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_self_test())
