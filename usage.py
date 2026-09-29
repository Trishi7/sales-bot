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


def record(*, site: str, model: str, response=None, seconds: float = 0.0,
           ok: bool = True) -> dict:
    """Log one call and hand it to the sink. Never raises."""
    usage = getattr(response, "usage", None)
    row = {
        "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
        "site": str(site or "?"),
        "model": str(model or ""),
        "input_tokens": _num(usage, "input_tokens"),
        "cache_write": _num(usage, "cache_creation_input_tokens"),
        "cache_read": _num(usage, "cache_read_input_tokens"),
        "output_tokens": _num(usage, "output_tokens"),
        "seconds": round(float(seconds or 0.0), 2),
        "ok": bool(ok),
    }
    log.info("[tokens] site=%s in=%d cache_r=%d cache_w=%d out=%d t=%.1fs%s",
             row["site"], row["input_tokens"], row["cache_read"], row["cache_write"],
             row["output_tokens"], row["seconds"], "" if ok else " FAILED")
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
