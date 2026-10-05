"""THE TOKEN LOG — one row and one log line per Anthropic API call.

Every model call the bot makes goes through one of three places —
`llm.LLM._create`, `llm.LLM.web_research` and `query_engine.QueryEngine` — and
each of them hands its response here. What comes back is the API's own
accounting, read from `response.usage`:

    input_tokens                  the uncached tail, full price
    cache_creation_input_tokens   written to the prompt cache (~1.25x)
    cache_read_input_tokens       served from the cache (~0.1x)
    output_tokens

`site` names the calling method (parse_query, engine, web_research:R1-main …),
so "where did the tokens go" is a GROUP BY rather than a guess.

NO DATABASE HANDLE HERE. The LLM classes do no I/O beyond the model call; the
bot installs a sink (`set_sink`) that writes the row. Without one — a verify
script, a self-test — the log line still goes out and nothing is stored.
"""
import logging
import time
from datetime import datetime
from typing import Callable, Optional
import deadlines as dl

log = logging.getLogger(__name__)

_sink: Optional[Callable[[dict], None]] = None


def set_sink(fn: Optional[Callable[[dict], None]]) -> None:
    """Where rows go. `fn(row)` is called synchronously — callers already run
    `record` on a worker thread."""
    global _sink
    _sink = fn


def _num(obj, name: str) -> int:
    try:
        return max(0, int(getattr(obj, name, 0) or 0))
    except (TypeError, ValueError):
        return 0


# -- what a token costs --------------------------------------------------------
#
# DOLLARS PER MILLION TOKENS: (input, output, cache write, cache read). Matched
# on the model family's name, so a dated or renamed id still prices. An unknown
# model is priced as Sonnet — over-reporting a cost is the safe direction.
PRICES = {
    "haiku": (1.00, 5.00, 1.25, 0.10),
    "sonnet": (3.00, 15.00, 3.75, 0.30),
    "opus": (5.00, 25.00, 6.25, 0.50),
}


def family(model: str) -> str:
    """"haiku" | "sonnet" | "opus" for a model id. Unknown reads as sonnet."""
    low = str(model or "").lower()
    for name in ("haiku", "opus", "sonnet"):
        if name in low:
            return name
    return "sonnet"


def dollars(row: dict) -> float:
    """What one llm_calls row (or a sum of them for ONE model) cost."""
    p_in, p_out, p_write, p_read = PRICES[family(row.get("model") or "")]
    try:
        import config
        if str(getattr(config, "CACHE_TTL", "5m")) == "1h":
            p_write = p_in * 2.0           # a 1-hour write is 2x input
    except Exception:
        pass
    return (
        int(row.get("input_tokens") or 0) * p_in
        + int(row.get("output_tokens") or 0) * p_out
        + int(row.get("cache_write") or 0) * p_write
        + int(row.get("cache_read") or 0) * p_read
    ) / 1_000_000.0


def money(amount: float) -> str:
    """"$0.0123" under ten cents, "$1.23" above — small numbers stay readable."""
    amount = float(amount or 0.0)
    return f"${amount:.4f}" if abs(amount) < 0.1 else f"${amount:.2f}"


# THE TEST-RUN TALLY. A test day or a simulation opens one and logs it at the
# end as `[test-cost] date=… calls=N searches=N requests=N cache_hits=N
# dollars=$…`, so "did the second run cost anything" is one grep. `searches`
# is Anthropic's server-side web searches (zero unless SEARCH_BACKEND=
# anthropic); `requests` is search-API requests. None when no run is open.
_TALLY_ZERO = {"calls": 0, "searches": 0, "requests": 0, "cache_hits": 0,
               "dollars": 0.0}
_tally: Optional[dict] = None


def start_tally() -> dict:
    global _tally
    _tally = dict(_TALLY_ZERO)
    return _tally


def stop_tally() -> dict:
    global _tally
    got, _tally = (_tally or dict(_TALLY_ZERO)), None
    return got


def count(field: str, n: int = 1) -> None:
    if _tally is not None:
        _tally[field] = _tally.get(field, 0) + int(n or 0)


def spend(amount: float) -> None:
    """Add dollars to the open tally — a search request, or a model call."""
    if _tally is not None:
        _tally["dollars"] = float(_tally.get("dollars", 0.0)) + float(amount or 0.0)


def tally_line(label: str, cost: dict) -> str:
    """The one `[test-cost]` line, for a day or a week."""
    return ("[test-cost] date=%s calls=%d searches=%d requests=%d cache_hits=%d "
            "dollars=%s" % (label, int(cost.get("calls", 0)),
                            int(cost.get("searches", 0)), int(cost.get("requests", 0)),
                            int(cost.get("cache_hits", 0)),
                            money(cost.get("dollars", 0.0))))


# -- the daily token budget ----------------------------------------------------
#
# TOKEN_DAILY_BUDGET, in input tokens with cache reads at 10%, read from
# llm_calls. `budget_state()` is asked before every model call (it is what logs
# the 50% and 80% warnings); the callers that must DEGRADE when it is spent —
# research, the hourly news check, the engine's web tools — ask `over_budget()`.

_budget_reader: Optional[Callable[[str], int]] = None
_budget = {"day": "", "used": 0, "read_at": 0.0, "warned": set()}
_BUDGET_REREAD_SECONDS = 30.0


def set_budget_reader(fn: Optional[Callable[[str], int]]) -> None:
    """`fn(since_ts)` -> budget tokens spent since then (db.llm_budget_tokens_since)."""
    global _budget_reader
    _budget_reader = fn
    _budget.update(day="", used=0, read_at=0.0, warned=set())


def _day_start() -> tuple:
    from datetime import time as _time
    today = dl.real_today_ist()
    start = datetime.combine(today, _time(0, 0), tzinfo=dl.IST)
    return dl.iso(today), start.isoformat(timespec="seconds")


def budget_state() -> dict:
    """{"used", "budget", "left", "over"} for the REAL day. Never raises.

    The ledger is re-read at most every 30 seconds; in between, `record` adds
    each call to the running figure, so the number is exact without a query
    per call. A budget of 0 (or no reader) is "no budget": never over.
    """
    try:
        import config
        total = max(0, int(getattr(config, "TOKEN_DAILY_BUDGET", 0) or 0))
    except Exception:
        total = 0
    if not total or _budget_reader is None:
        return {"used": 0, "budget": total, "left": total, "over": False}
    day, since = _day_start()
    now = time.monotonic()
    if _budget["day"] != day or now - _budget["read_at"] > _BUDGET_REREAD_SECONDS:
        if _budget["day"] != day:
            _budget["warned"] = set()
        try:
            _budget["used"] = int(_budget_reader(since) or 0)
        except Exception:
            log.debug("[tokens] could not read the budget", exc_info=True)
        _budget.update(day=day, read_at=now)
    used = int(_budget["used"])
    for level in (50, 80, 100):
        if used * 100 >= total * level and level not in _budget["warned"]:
            _budget["warned"].add(level)
            log.warning(
                "[tokens] %d%% of TOKEN_DAILY_BUDGET used today (%s of %s input "
                "tokens, cache reads at 10%%)%s", level, f"{used:,}", f"{total:,}",
                " — research and the hourly checks now skip, and answers go out "
                "without web tools" if level == 100 else "")
    return {"used": used, "budget": total, "left": max(0, total - used),
            "over": used >= total}


def over_budget() -> bool:
    return bool(budget_state()["over"])


def budget_note() -> str:
    """What research says when the token budget is spent. One honest sentence."""
    state = budget_state()
    return ("web research unavailable today — the daily token budget is spent "
            f"({state['used']:,} of {state['budget']:,} input tokens used)")


def record(*, site: str, model: str, response=None, seconds: float = 0.0,
           ok: bool = True) -> dict:
    """Log one call and hand it to the sink. Never raises."""
    count("calls")
    usage = getattr(response, "usage", None)
    row = {
        "ts": dl.real_now_ist().isoformat(timespec="seconds"),
        "site": str(site or "?"),
        "model": str(model or ""),
        "input_tokens": _num(usage, "input_tokens"),
        "cache_write": _num(usage, "cache_creation_input_tokens"),
        "cache_read": _num(usage, "cache_read_input_tokens"),
        "output_tokens": _num(usage, "output_tokens"),
        "seconds": round(float(seconds or 0.0), 2),
        "ok": bool(ok),
    }
    spend(dollars(row))
    if _budget["day"]:
        _budget["used"] = (int(_budget["used"]) + row["input_tokens"]
                           + row["cache_write"] + int(round(row["cache_read"] * 0.1)))
    log.info("[tokens] site=%s model=%s in=%d cache_r=%d cache_w=%d out=%d t=%.1fs%s",
             row["site"], row["model"], row["input_tokens"], row["cache_read"],
             row["cache_write"], row["output_tokens"], row["seconds"],
             "" if ok else " FAILED")
    if _sink is not None:
        try:
            _sink(row)
        except Exception:
            log.debug("[tokens] could not store the row", exc_info=True)
    return row


class Timer:
    """`with usage.Timer() as t: ...` then `t.seconds`."""

    def __enter__(self):
        self._t0 = time.monotonic()
        self.seconds = 0.0
        return self

    def __exit__(self, *exc):
        self.seconds = time.monotonic() - self._t0
        return False
