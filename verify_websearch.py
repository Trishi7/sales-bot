"""DRY-RUN R1 AND R11 AND PRINT THE COMPOSED MESSAGES. `python verify_websearch.py`

Runs the two rules end to end — item -> web search -> composed message with its
links — and prints what would go out. NOTHING IS SENT: `drip.plan` computes and
`compose_fallback` renders; no Discord client is constructed.

IT MAKES REAL SEARCH CALLS when a usable ANTHROPIC_API_KEY is present, because a
verification that mocks the API proves the mock works and not the tool string.
Without a key it says so and runs the composition path against a canned
response, so the message shape is still checked. Which mode ran is printed.

Cost: a handful of searches at $10/1000, plus tokens. Pass --offline to skip the
live call deliberately.
"""
import asyncio
import logging
import os
import sys
import tempfile
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
import db as dbmod
import deadlines as dl
import drip
import nextaction
import websearch

OFFLINE = "--offline" in sys.argv
TODAY = date(2026, 9, 21)

SID, VAISHNAVI = 1001, 1002
config.TEAM_ROSTER_IDS = [SID, VAISHNAVI]
config.SALES_ALWAYS_TAG_IDS = [SID, VAISHNAVI]
config.ROSTER_DISPLAY_NAMES = {str(SID): "Sid", str(VAISHNAVI): "Vaishnavi"}

COMPANY = "Wispr Flow"
failures = 0


def check(name, got, want):
    global failures
    ok = got == want
    failures += 0 if ok else 1
    print(f"    {'PASS' if ok else 'FAIL'}  {name}: got {got!r}, want {want!r}")


def have_key() -> bool:
    key = (os.getenv("ANTHROPIC_API_KEY") or "").strip()
    return len(key) > 20 and key.startswith("sk-")


def item(rule_id, trigger, company, *, text):
    band = nextaction.RULE_BANDS.get(trigger, nextaction.P_CHASE)
    return {
        "rule": trigger, "rule_id": rule_id, "type": trigger,
        "rule_name": nextaction.TYPE_LABELS.get(trigger, trigger),
        "label": nextaction.TYPE_LABELS.get(trigger, trigger),
        "owner": "Vaishnavi", "priority": band,
        "priority_label": nextaction.BAND_LABELS[band],
        "due_date": TODAY, "due_iso": dl.iso(TODAY), "overdue_days": 0,
        "company": company, "poc": "", "poc_designation": "", "sheet_row": 2,
        "row_key": "", "contact_key": "",
        "max_items_per_post": config.DRIP_MAX_ITEMS_PER_POST,
        "counts_toward_cap": True, "destination": "channel",
        "web_pending": True,
        "why": "seeded for the dry run",
        "text": f"{text} [{websearch.WEB_PENDING}]",
        "key": f"{rule_id}:{company.lower()}",
    }


CANNED = {
    "ok": True,
    "text": ("Wispr Flow raised a $56M Series B led by Menlo Ventures (announced "
             "3 Sep 2026). They are hiring for speech-data and evaluation roles."),
    "sources": [
        {"url": "https://techcrunch.com/wispr-flow-series-b",
         "title": "Wispr Flow raises $56M", "quote": "a $56 million Series B"},
        {"url": "https://wisprflow.ai/careers",
         "title": "Wispr Flow — Careers", "quote": "Speech Data Lead"},
    ],
    "searches": 2, "errors": [], "note": "",
}


async def research(rule_id, trigger, company, live):
    """One rule's web half. Returns the parsed result dict."""
    query = websearch.RULE_QUERIES[trigger]
    context = f"Company: {company}"
    if trigger == "ai_news":
        context += "\nCompanies and people already on our sheet:\nWispr Flow, PolyAI, ElevenLabs"
    if not live:
        return dict(CANNED)

    import llm as llmmod
    engine = llmmod.LLM(os.environ["ANTHROPIC_API_KEY"], config.MODEL)
    return await engine.web_research(
        rule=rule_id, prompt=query + "\n\n" + context,
        max_uses=min(3, config.WEB_SEARCH_MAX_USES),
    )


async def main() -> int:
    global failures
    logging.basicConfig(level=logging.WARNING, format="%(levelname)-7s %(message)s")

    live = have_key() and not OFFLINE
    f = os.path.join(tempfile.gettempdir(), "verify_websearch.db")
    if os.path.exists(f):
        os.remove(f)
    d = dbmod.DB(f)

    print("=" * 78)
    print("WEB SEARCH DRY RUN — R1 (AI news) and R11 (new company: %s)" % COMPANY)
    print("=" * 78)
    print(f"  mode              {'LIVE — real search calls' if live else 'OFFLINE — canned response'}")
    if not live and not OFFLINE:
        print("                    (no usable ANTHROPIC_API_KEY found)")
    print(f"  tool type         {config.WEB_SEARCH_TOOL_TYPE}")
    print(f"  tool name         {websearch.TOOL_NAME}")
    print(f"  max_uses per call {config.WEB_SEARCH_MAX_USES}")
    print(f"  daily budget      {config.WEB_SEARCH_DAILY_BUDGET}")
    print()
    print("  the tool definition that goes on the request:")
    for k, v in websearch.tool_definition().items():
        print(f"    {k:<22} {v!r}")
    print()

    results = {}
    for rule_id, trigger, text in (
        ("R1", nextaction.R_AI_NEWS,
         "AI news: funding, hires, papers by our PoCs, competitor and regulation news"),
        ("R11", nextaction.R_NEW_COMPANY,
         f"{COMPANY} is new in the Master Pipeline. I can fill in funding, location "
         "and industry and suggest PoCs — shall I?"),
    ):
        it = item(rule_id, trigger, COMPANY, text=text)
        res = await research(rule_id, trigger, COMPANY, live)
        d.record_web_search(on_date=dl.iso(TODAY), rule_id=rule_id,
                            searches=int(res.get("searches") or 0),
                            errors=len(res.get("errors") or []))
        if res.get("ok"):
            it["research"] = res["text"]
            it["sources"] = res["sources"]
            it["web_pending"] = False
            it["text"] = it["text"].replace(f" [{websearch.WEB_PENDING}]", "")
        else:
            it["research_note"] = res.get("note") or websearch.unavailable_note()
        results[rule_id] = (it, res)

    # ---- the composed messages ------------------------------------------
    for rule_id in ("R1", "R11"):
        it, res = results[rule_id]
        planned = drip.plan([it], day=TODAY)
        msg = planned["messages"][0] if planned["messages"] else None
        print("-" * 78)
        print(f"  {rule_id} — {it['rule_name']}")
        print("-" * 78)
        if msg is None:
            print("    (no message planned)")
            continue
        body = drip.compose_fallback(msg, address="")
        if it.get("research"):
            body = f"{body}\n\n{it['research']}"
        if it.get("sources"):
            body = f"{body}\n\n{websearch.format_sources(it['sources'])}"
        if it.get("research_note"):
            body = f"{body}\n\n_({it['research_note']}.)_"
        full = drip.with_tags(body, owner_id=SID, owner_name="Vaishnavi")
        for line in full.splitlines():
            print("  | " + line)
        print()
        print(f"    searches billed   {res.get('searches')}")
        print(f"    sources           {len(res.get('sources') or [])}")
        print(f"    errors            {res.get('errors') or 'none'}")
        print()

    # ---- assertions -------------------------------------------------------
    print("=" * 78)
    print("  CHECKS")
    print("=" * 78)

    check("the tool type is the current one",
          config.WEB_SEARCH_TOOL_TYPE, "web_search_20260318")
    check("the tool name is fixed", websearch.tool_definition()["name"], "web_search")
    check("allowed and blocked domains are never both sent",
          not ("allowed_domains" in websearch.tool_definition()
               and "blocked_domains" in websearch.tool_definition()), True)

    for rule_id in ("R1", "R11"):
        it, res = results[rule_id]
        if res.get("ok"):
            check(f"{rule_id} carries sources", len(it.get("sources") or []) > 0, True)
            check(f"{rule_id} every source has a link",
                  all(str(sc.get("url", "")).startswith("http")
                      for sc in it["sources"]), True)
            check(f"{rule_id} the placeholder is gone once research arrived",
                  websearch.WEB_PENDING in it["text"], False)
        else:
            check(f"{rule_id} still produced an item", bool(it.get("text")), True)
            check(f"{rule_id} says research was unavailable",
                  "web research unavailable" in (it.get("research_note") or ""), True)

    used = d.web_searches_today(dl.iso(TODAY))
    print(f"    budget: {used} used, {d.web_search_budget_left(dl.iso(TODAY))} left "
          f"of {config.WEB_SEARCH_DAILY_BUDGET}")
    print(f"    breakdown: {d.web_search_breakdown(dl.iso(TODAY))}")
    check("the ledger banked what was billed",
          used, sum(int(r.get("searches") or 0) for _i, r in results.values()))

    # The budget must degrade, not fail.
    d.record_web_search(on_date=dl.iso(TODAY), rule_id="R2",
                        searches=config.WEB_SEARCH_DAILY_BUDGET)
    check("a spent budget leaves nothing", d.web_search_budget_left(dl.iso(TODAY)), 0)
    note = websearch.unavailable_note(
        websearch.budget_note(used=d.web_searches_today(dl.iso(TODAY)),
                              budget=config.WEB_SEARCH_DAILY_BUDGET))
    print(f"    degraded note: {note}")
    check("...and the note says why", "budget is spent" in note, True)

    print()
    print("  " + ("ALL PASSED" if not failures else f"{failures} FAILED"))
    print("=" * 78)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
