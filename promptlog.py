"""SEEING THE PROMPT — every model call written to a file (NFT2-1090).

When a message reads wrong, the first question is "what was it told?", and
until now the answer was in nobody's hands. With SALEY_PROMPT_LOG=true every
model call made through `llm._create` writes ONE file:

    state/prompts/<date>/<HHMMSS>-<site>.txt          a live call
    state/prompts/test/<date>/<HHMMSS>-<site>.txt     during a test day or a simulation

holding the system prompt, the user prompt and the reply; the composer then
appends its verdict, and a retry is appended to the SAME file, so one file
tells the whole story of one message.

NO REDACTION: prompts hold the strategy, the sheet rows and the channel text,
never a secret. The folder is gitignored. Files older than
SALEY_PROMPT_LOG_DAYS are deleted at the daily tick (`prune`).

A FOLDER THAT CANNOT BE WRITTEN IS LOGGED ONCE and composing carries on: a
log that stops the bot talking would be worse than no log.

THE LAST FILE WRITTEN IN THIS TASK is kept in a context variable (`current`),
so the code that sends the message can map the Discord message id to it
(`db.record_prompt_log`) without threading a path through every call.
"""
import contextvars
import logging
import os
import re
import shutil
import time
from datetime import datetime, timedelta
from typing import Callable, Optional

import config

log = logging.getLogger(__name__)

_CURRENT: contextvars.ContextVar = contextvars.ContextVar("prompt_log_path", default=None)
# ONE FILE FOR A MULTI-CALL ANSWER: while a group is open (the question
# engine's tool loop), every call after the first is appended to the first's
# file, so the file behind an answer holds every turn of it.
_GROUP: contextvars.ContextVar = contextvars.ContextVar("prompt_log_group", default=False)
_is_test: Optional[Callable] = None
_warned: set = set()


def bind(is_test: Optional[Callable]) -> None:
    """How to tell a test day or a simulation from a live call: `is_test()`."""
    global _is_test
    _is_test = is_test


def enabled() -> bool:
    raw = (os.getenv("SALEY_PROMPT_LOG", "") or "").strip().lower()
    if not raw:
        return bool(getattr(config, "SALEY_PROMPT_LOG", True))
    return raw not in ("0", "false", "no", "off")


def keep_days() -> int:
    try:
        return max(1, int(os.getenv("SALEY_PROMPT_LOG_DAYS", "")
                          or getattr(config, "SALEY_PROMPT_LOG_DAYS", 14)))
    except (TypeError, ValueError):
        return 14


def root() -> str:
    return os.path.join(str(getattr(config, "STATE_DIR", "") or "./state"), "prompts")


def _testing() -> bool:
    try:
        return bool(_is_test()) if _is_test is not None else False
    except Exception:
        return False


def folder(now: Optional[datetime] = None) -> str:
    now = now or datetime.now()
    base = os.path.join(root(), "test") if _testing() else root()
    return os.path.join(base, now.strftime("%Y-%m-%d"))


def _text_of(system) -> str:
    if isinstance(system, str):
        return system
    out = []
    for b in system or []:
        out.append(str(b.get("text") or "") if isinstance(b, dict) else str(b))
    return "\n\n".join(out)


def reset() -> None:
    """Forget the last file of this task (a new message is being handled)."""
    _CURRENT.set(None)
    _GROUP.set(False)


def begin_group() -> None:
    """Every call from here on, in this task, goes into ONE file."""
    _CURRENT.set(None)
    _GROUP.set(True)


def end_group() -> None:
    _GROUP.set(False)


def current() -> Optional[str]:
    return _CURRENT.get()


def set_current(path: Optional[str]) -> None:
    """Name the last file IN THE CALLING TASK. `write` runs on a worker thread
    (`asyncio.to_thread`), whose context changes never reach the caller, so
    the caller hands the path back here."""
    if path:
        _CURRENT.set(path)


def write(*, site: str, system, prompt: str, reply: str = "", model: str = "",
          path: Optional[str] = None) -> Optional[str]:
    """Write (or, with `path`, append to) one call's file. The path, or None
    when logging is off or the folder cannot be written."""
    if not enabled():
        return None
    if path is None and _GROUP.get() and _CURRENT.get():
        path = _CURRENT.get()
    try:
        if path is None:
            now = datetime.now()
            where = folder(now)
            os.makedirs(where, exist_ok=True)
            safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(site or "call"))[:60]
            path = os.path.join(where, f"{now.strftime('%H%M%S')}-{int(time.time() * 1000) % 1000:03d}-{safe}.txt")
            head = ""
        else:
            head = "\n\n" + "#" * 70 + "\n# RETRY\n" + "#" * 70 + "\n"
        body = (head + f"site: {site}\nmodel: {model}\nat: {datetime.now().isoformat(timespec='seconds')}\n"
                "\n=== SYSTEM ===\n" + _text_of(system)
                + "\n\n=== USER ===\n" + str(prompt or "")
                + "\n\n=== REPLY ===\n" + str(reply or "") + "\n")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(body)
        _CURRENT.set(path)
        return path
    except OSError as e:
        key = root()
        if key not in _warned:
            _warned.add(key)
            log.warning("[prompt-log] cannot write under %s (%s); composing continues "
                        "without the log", key, type(e).__name__)
        return None


def append(path: Optional[str], heading: str, text: str) -> None:
    """Add a section (the verdict) to a call's file. Never raises."""
    if not path or not enabled():
        return
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"\n=== {heading} ===\n{text}\n")
    except OSError:
        log.debug("[prompt-log] could not append to %s", path, exc_info=True)


def prune(*, now: Optional[datetime] = None, days: Optional[int] = None) -> int:
    """Delete the day folders older than SALEY_PROMPT_LOG_DAYS, live and test.
    How many folders went. Never raises."""
    now = now or datetime.now()
    cutoff = (now - timedelta(days=days if days is not None else keep_days())).date()
    gone = 0
    for base in (root(), os.path.join(root(), "test")):
        try:
            names = os.listdir(base)
        except OSError:
            continue
        for name in names:
            try:
                day = datetime.strptime(name, "%Y-%m-%d").date()
            except ValueError:
                continue
            if day < cutoff:
                shutil.rmtree(os.path.join(base, name), ignore_errors=True)
                gone += 1
    if gone:
        log.info("[prompt-log] pruned %d day folder(s) older than %s", gone, cutoff)
    return gone


def read(path: str) -> str:
    try:
        return open(path, encoding="utf-8").read()
    except OSError:
        return ""
