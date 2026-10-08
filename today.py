"""WHAT THE TEAM IS DOING TODAY — the answer, built by code from what the day holds.

THE QUESTION THIS EXISTS FOR (8 Oct 2026). Asked "what are we supposed to do
today?", the bot pasted its own posts for the day, word for word, and on 7 Oct
it opened "Today's a Wednesday, so here's what's on the schedule: Scheduled for
today (Wed 7 Oct): AI news post at 2 PM (already posted) …". The human:

    "it shouldnt just answer from the scheduled messages for that day: it can
    go through any meeting notes for that day, discord chats, any important
    things which have to be done that day (it can look for events / tasks
    whose deadlines are closer to that day), and may be a very short summary
    of that days schedules messages (except the AI news and news related to
    the Pocs)"

So the answer is about the TEAM'S day, not the bot's: what came out of today's
meetings, what is due today and in the next couple of working days, what was
said in the channel today that somebody has to act on, and one or two lines on
what the bot's own posts cover.

    **From today's meetings**
    - Vaishnavi: send the pilot proposal to Acme (Pipeline review)

    **Due soon**
    - Tomorrow: registration for Voice AI Summit closes
    - Mon 12 Oct: MSA template (Legal)

    **In the channel today**
    - Sid: I'll call PolyAI about the quote tomorrow

    **Also today**
    - My posts today cover the deliverables checklist and closure support.

THIS MODULE IS PURE. It is handed what was found and returns text; `bot.py`
does the reading (`_today_brief`). NO MODEL WRITES ANY OF IT, for the reason
the news answer is written by code: a model handed the day explains the
schedule. Every line here is an action item or a date somebody else wrote,
quoted and shortened, or a count.

WHAT IT NEVER SAYS, by construction: a time of day (clock times are taken out
of quoted text, `without_times`), "scheduled", "already posted", what weekday
it is, a rule's number, and the AI news — the news posts are left out of the
summary (`NEWS_TYPES`) and channel messages about the news are not quoted.
"""
import re
from datetime import date
from typing import Optional

import wording

# Post types left out of the "Also today" summary: the AI news and the screen
# of companies in the news. "Any AI news?" is its own question and answer.
NEWS_TYPES = frozenset({"ai_news", "news_company_screen"})

# What each kind of post is called in the one-line summary. Plain words for
# what the post is ABOUT; never a rule number.
POST_LABELS = {
    "deliverables": "the deliverables checklist",
    "prospects": "prospects to contact",
    "li_no_dm": "LinkedIn connections with no DM yet",
    "dm_no_meeting": "DMs with no meeting yet",
    "meeting_prep": "meeting prep",
    "meeting_followup": "meetings with no next steps logged",
    "closure_support": "closure support",
    "new_pipeline_company": "new companies in the pipeline",
    "sales_packages": "sales packages",
    "events": "AI events and summits",
    "next_step_followups": "next steps for connected contacts",
}

# How much of each group is shown. A "today" answer is a glance, not a report.
MAX_MEETING_LINES = 6
MAX_DUE_LINES = 8
MAX_CHANNEL_LINES = 5
LINE_MAX_CHARS = 160

_MENTION_RE = re.compile(r"<[@#][!&]?\d+>")
_URL_RE = re.compile(r"<?https?://\S+>?", re.IGNORECASE)
# A clock time, with the word that leads into it: "at 3 PM", "by 15:30",
# "@ 2pm", "3:30 p.m. IST". Dates are not touched.
_TIME_RE = re.compile(
    r"(?:\b(?:at|by|around|before|after|till|until)\s+|@\s*)?"
    r"(?:\b(?:[01]?\d|2[0-3]):[0-5]\d(?:\s*(?:a\.?m\.?|p\.?m\.?))?"
    r"|\b(?:1[0-2]|0?[1-9])\s*(?:a\.?m\.?|p\.?m\.?))"
    r"(?:\s*\(?\bIST\b\)?)?",
    re.IGNORECASE)
# Words that make a channel message something somebody has to DO. Liberal on
# purpose: the line is quoted, never interpreted, so a false positive costs
# one extra line and a false negative hides a commitment.
_ACTION_RE = re.compile(
    r"\b(i'?ll|i\s+will|we'?ll|we\s+will|will\s+(?:send|share|call|do|finish|follow|"
    r"update|check|get|post|book|set)|need(?:s)?\s+to|have\s+to|has\s+to|must|"
    r"please|pls|can\s+(?:you|someone|anyone)|could\s+(?:you|someone)|todo|to-do|"
    r"deadline|due|by\s+(?:today|tomorrow|tmrw|eod|monday|tuesday|wednesday|thursday|"
    r"friday)|tomorrow|tmrw|follow\s*-?\s*up|remind|let'?s|action\s+item|pending|"
    r"waiting\s+on|blocked)\b", re.IGNORECASE)
_NEWS_RE = re.compile(r"\b(news|headlines?)\b", re.IGNORECASE)


def first_name(name) -> str:
    """"Vaishnavi" from "Vaishnavi Reddy" — people are named the way the team
    names them in the channel."""
    parts = str(name or "").strip().split()
    return parts[0] if parts else ""


def without_times(text: str) -> str:
    """`text` with clock times taken out ("call them at 3 PM" -> "call them").
    A "today" answer carries action items and dates, never a time of day."""
    out = _TIME_RE.sub("", str(text or ""))
    out = re.sub(r"\s+([,.;:!?])", r"\1", out)
    return " ".join(out.split())


def tidy(text: str, *, limit: int = LINE_MAX_CHARS) -> str:
    """One quoted line: no mentions, no links, no clock times, one line, and
    cut at a word when it runs long."""
    body = _URL_RE.sub("", _MENTION_RE.sub("", str(text or "")))
    body = without_times(" ".join(body.split())).strip(" -–—•*")
    if len(body) <= limit:
        return body
    cut = body[:limit].rsplit(" ", 1)[0].rstrip(",;:")
    return cut + "…"


def is_action(text: str) -> bool:
    """Does this channel message carry something to do? Never a message about
    the news: that is the one subject this answer leaves out."""
    body = str(text or "")
    return bool(_ACTION_RE.search(body)) and not _NEWS_RE.search(body)


def meeting_lines(notes) -> list:
    """The action items of today's meeting notes, one a line.

    `notes` is [{"label", "next_steps": [{"owner_name", "task"}]}]. A note
    with no action items is named once, so the meeting is not silently
    missing; its discussion is not summarised here.
    """
    lines: list = []
    for note in notes or ():
        label = str(note.get("label") or "").strip()
        tail = f" ({label})" if label else ""
        steps = [s for s in (note.get("next_steps") or ()) if str(s.get("task") or "").strip()]
        if not steps:
            lines.append(f"- {label or 'A meeting'}: no action items in the note")
            continue
        for step in steps:
            who = first_name(step.get("owner_name"))
            task = tidy(step.get("task"))
            if task:
                lines.append(f"- {who}: {task}{tail}" if who else f"- {task}{tail}")
    return _capped(lines, MAX_MEETING_LINES)


def day_label(due: date, today: date) -> str:
    """"Today", "Tomorrow", or "Mon 12 Oct"."""
    gap = (due - today).days
    if gap == 0:
        return "Today"
    if gap == 1:
        return "Tomorrow"
    return f"{due.strftime('%a')} {due.day} {due.strftime('%b')}"


def due_lines(items, *, today: date) -> list:
    """What falls due from today to the end of the look-ahead, soonest first.

    `items` is [{"due": date, "what": str}]; `what` is already a phrase
    ("MSA template (Legal)", "registration for Voice AI Summit closes").
    The same thing reported by two sources is listed once.
    """
    seen, rows = set(), []
    for item in items or ():
        due, what = item.get("due"), tidy(item.get("what"))
        if not isinstance(due, date) or not what:
            continue
        key = (due, what.lower())
        if key in seen:
            continue
        seen.add(key)
        rows.append((due, what))
    rows.sort(key=lambda r: (r[0], r[1].lower()))
    return _capped([f"- {day_label(due, today)}: {what}" for due, what in rows],
                   MAX_DUE_LINES)


def channel_lines(messages) -> list:
    """Today's channel messages that somebody has to act on, oldest first, in
    the person's own words: [{"author", "text"}]."""
    lines, seen = [], set()
    for msg in messages or ():
        text = str(msg.get("text") or "")
        if not is_action(text):
            continue
        said = tidy(text)
        if not said or said.lower() in seen:
            continue
        seen.add(said.lower())
        who = first_name(msg.get("author"))
        lines.append(f"- {who}: {said}" if who else f"- {said}")
    return lines[-MAX_CHANNEL_LINES:]


def posts_summary(posts) -> list:
    """ONE OR TWO LINES on what the bot's own posts cover today.

    `posts` is [{"type"}] for every post gone or still to go today. The news
    posts are left out (NEWS_TYPES); so is anything with no plain label. No
    time, no "posted", no rule number and NO COUNT: what the posts are about.
    (A post that has gone out is remembered by its text, not by how many
    people were on it, and a number that might be wrong is worse than none.)
    """
    kinds: list = []
    for post in posts or ():
        kind = str(post.get("type") or "")
        if kind in NEWS_TYPES or kind not in POST_LABELS or kind in kinds:
            continue
        kinds.append(kind)
    if not kinds:
        return []
    parts = [POST_LABELS[k] for k in kinds]
    if len(parts) <= 4:
        return [f"- {wording.TODAY_POSTS_LEAD} {_and(parts)}."]
    return [f"- {wording.TODAY_POSTS_LEAD} {', '.join(parts[:4])},",
            f"  {_and(parts[4:])}."]


def todo_line(open_total: int, link: str = "") -> Optional[str]:
    """"- 4 open to-dos on the sheet: <link>", or None when there are none."""
    n = int(open_total or 0)
    if n <= 0:
        return None
    line = f"- {n} open to-do{'s' if n != 1 else ''} on the to-do sheet"
    return f"{line}: {link}" if link else f"{line}."


def render(*, meetings=(), due=(), channel=(), also=()) -> str:
    """The answer: each group that has something under its heading, a blank
    line between groups, and ONE LINE when there is nothing at all."""
    blocks = []
    for heading, lines in ((wording.TODAY_MEETINGS, meetings), (wording.TODAY_DUE, due),
                           (wording.TODAY_CHANNEL, channel), (wording.TODAY_ALSO, also)):
        lines = [l for l in (lines or ()) if str(l).strip()]
        if lines:
            blocks.append("\n".join([f"**{heading}**"] + lines))
    return "\n\n".join(blocks) if blocks else wording.NOTHING_TODAY


def _and(parts: list) -> str:
    parts = [p for p in parts if p]
    if len(parts) <= 1:
        return "".join(parts)
    return ", ".join(parts[:-1]) + " and " + parts[-1]


def _capped(lines: list, cap: int) -> list:
    if len(lines) <= cap:
        return lines
    return lines[:cap] + [f"- (+{len(lines) - cap} more)"]


def _self_test() -> int:
    """`python -m today` — the rendering, on made-up inputs."""
    failures = 0

    def check(name, got, want=True):
        nonlocal failures
        ok = got == want
        failures += 0 if ok else 1
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f": got {got!r}, want {want!r}"))

    day = date(2026, 10, 8)
    print("quoted text")
    for text, want in (("call them at 3 PM", "call them"), ("demo by 15:30 IST tomorrow", "demo tomorrow"),
                       ("send by Friday", "send by Friday"), ("sync @ 2pm", "sync"),
                       ("ship on 12 Oct", "ship on 12 Oct"), ("3 decks, 2 pilots", "3 decks, 2 pilots")):
        check(f"without_times({text!r})", without_times(text), want)
    check("no mention, no link", tidy("<@123> see https://x.example/a please send"), "see please send")
    check("a long line is cut at a word", (len(tidy("word " * 80)) <= LINE_MAX_CHARS + 1,
                                           tidy("word " * 80).endswith("…")), (True, True))

    print("\nthe groups")
    meetings = meeting_lines([
        {"label": "Pipeline review", "next_steps": [
            {"owner_name": "Vaishnavi Reddy", "task": "send the pilot proposal to Acme by Friday"},
            {"owner_name": None, "task": "check the MSA at 4 PM"}]},
        {"label": "Acme sync", "next_steps": []}])
    check("an action item a line, first names, the meeting in brackets", meetings,
          ["- Vaishnavi: send the pilot proposal to Acme by Friday (Pipeline review)",
           "- check the MSA (Pipeline review)", "- Acme sync: no action items in the note"])
    due = due_lines([{"due": date(2026, 10, 12), "what": "MSA template (Legal)"},
                     {"due": date(2026, 10, 9), "what": "registration for Voice AI Summit closes"},
                     {"due": day, "what": "Reminder: the pricing deck"},
                     {"due": date(2026, 10, 12), "what": "msa template (legal)"}], today=day)
    check("soonest first, Today and Tomorrow by name, a duplicate once", due,
          ["- Today: Reminder: the pricing deck", "- Tomorrow: registration for Voice AI Summit closes",
           "- Mon 12 Oct: MSA template (Legal)"])
    channel = channel_lines([
        {"author": "Sid Rao", "text": "I'll call PolyAI about the quote tomorrow at 11 AM"},
        {"author": "Kushal", "text": "nice one"},
        {"author": "Kushal", "text": "any AI news today? need to check"},
        {"author": "Vaishnavi", "text": "<@9> please send the deck"}])
    check("only messages with something to do, never one about the news", channel,
          ["- Sid: I'll call PolyAI about the quote tomorrow", "- Vaishnavi: please send the deck"])
    also = posts_summary([{"type": "ai_news", "count": 5}, {"type": "deliverables", "count": 2},
                          {"type": "news_company_screen", "count": 3},
                          {"type": "closure_support", "count": 1}])
    check("one line, no news posts", also,
          ["- My posts today cover the deliverables checklist and closure support."])
    check("many posts: two lines at most", len(posts_summary(
        [{"type": k, "count": 1} for k in POST_LABELS])), 2)
    check("only news posts: no summary", posts_summary([{"type": "ai_news", "count": 5}]), [])

    print("\nthe answer")
    text = render(meetings=meetings, due=due, channel=channel, also=also)
    check("four headings, in order",
          [l for l in text.splitlines() if l.startswith("**")],
          ["**From today's meetings**", "**Due soon**", "**In the channel today**", "**Also today**"])
    check("an empty group is skipped",
          [l for l in render(due=due).splitlines() if l.startswith("**")], ["**Due soon**"])
    check("nothing anywhere: one line", render(), wording.NOTHING_TODAY)
    banned = re.compile(r"\b\d{1,2}(:\d{2})?\s*(am|pm)\b|\b\d{1,2}:\d{2}\b|scheduled|already posted|"
                        r"\bR\d+\b|\brule\s+\d+|AI news|Today's a ", re.IGNORECASE)
    check("no time of day, no 'scheduled', no rule number, no AI news",
          [m.group(0) for m in banned.finditer(text + wording.NOTHING_TODAY)], [])

    print(f"\n{'ALL PASSED' if not failures else str(failures) + ' FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_self_test())
