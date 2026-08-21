# Sales & Marketing CoS bot

A Discord bot that acts as a sales & marketing chief of staff for the NFThing
team. It reads the sales channels, answers questions from what it can actually
see, and chases the deadlines people commit to in passing.

It is a **separate bot from the PM bot** — its own Discord application, its own
token, its own channels, its own database, its own venv, its own PM2 process.
Nothing is shared between the two.

---

## What it does

**Answers questions.** In the ask channel (`SALES_ASK_CHANNEL_ID`) every message
is treated as a potential question; in the other sales channels it answers when
explicitly @-mentioned. Questions go to a read-only tool-use loop that can search
the sales channels' history, summarise recent activity, look up one person's
posts, read the meeting notes, and report what the bot is currently chasing.

**Chases deadlines.** When someone says "I'll send Acme the deck tomorrow" in a
sales channel, that becomes a *chase*. Once it's overdue the bot asks them about
it — in the channel the promise was made in, referencing what they actually said.
It asks at most `COS_NUDGE_MAX_ATTEMPTS` times, never twice within
`COS_NUDGE_WINDOW_HOURS`, and then stops and flags it once. Stopping is
deliberate: a bot that keeps asking gets muted, and a muted bot enforces nothing.

**Tells the truth about its blind spots.** Two of its three sources are still
awaiting access. Ask it what it can do and it says so, by name, and says what
that means it can't answer. That honesty is not a prompt preference — the same
`sources.status_report()` feeds the answer, the system prompt, and
`state/summary.json`, so what it tells a person and what it tells a supervisor
process cannot drift apart.

**What it does not do.** No triage, no classification, no ticket creation, no
approval channel, nothing to approve. Its only output is a Discord message in a
sales channel. It contacts nobody outside Discord and writes to nothing.

---

## Hard guardrails

These are enforced **in code** (`guardrails.py`), not in prompt text. A rule that
only exists in a prompt is a suggestion.

| Rule | Where |
|---|---|
| **Never DMs anyone.** Not as a fallback, not for a failed post. | `guardrails.send()` refuses any non-guild destination |
| **Only @-mentions people on the team roster.** Everyone else is named in plain text. | `guardrails.mention_for()` is the only source of a mention token; `sanitize()` strips any the model invented |
| **Reads and posts only in `SALES_CHANNEL_IDS`.** | `guardrails.may_read()` gates every incoming message and every history scan; `send()` gates every post |
| **Every action is audited** with a timestamp and a reason — including refusals. | `guardrails.send()` → `state.audit()` |

Code scoping is the **second** layer. The first is server-side: the bot's Discord
role must be denied **View Channel** on every non-sales channel. See
[DEPLOY.md](DEPLOY.md).

---

## Quick start

```bash
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env              # then fill in the REQUIRED values
python main.py
```

Required: `DISCORD_TOKEN`, `ANTHROPIC_API_KEY`, `SALES_CHANNEL_IDS`. Every other
variable is documented and commented in `.env.example`.

The Discord application needs the **Message Content Intent** enabled (Developer
Portal → your app → Bot → Privileged Gateway Intents). Without it every message
arrives with empty content and the bot silently does nothing.

For production (PM2, process name `sales-bot`), see [DEPLOY.md](DEPLOY.md).

---

## The policy file

`sales_policy.md` at the repo root is the bot's operating policy: its role, what
it enforces (strategy currency, outreach against the plan, deadlines), its tone,
and its hard limits.

**It is re-read on every question.** Edit the file, ask the next question, and
the new policy is already in force — no restart, no redeploy. `persona.py` caches
on the file's mtime and size, so an unchanged file costs one `stat()` per turn.

The role paragraph is explicitly marked as a placeholder for Sid to rewrite. The
rest of the file can stay as-is.

---

## Sources

| Source | Status | What it's for |
|---|---|---|
| `sales_meeting_notes` | **wired up** | what was said, decided and committed to in sales meetings |
| `sales_spreadsheet` | stub — awaiting access | pipeline, targets, the outreach log |
| `strategy_doc` | stub — awaiting access | the current strategy, and how current it is |

Each source self-reports `connected` / `awaiting-access` / `error` with a
one-sentence detail a human can act on. The two stubs have their interfaces
defined in `sources.py` and their readers land in the next prompt; setting their
env vars records the intended location but does not make them readable, and the
status line says so precisely.

**Meeting notes** are read from `NOTES_DIR`, a local folder that a separate sync
process (rclone or equivalent) copies the Drive docs into. The bot only ever
reads local files and holds no Google credential. A file counts as a meeting note
if its filename or first lines carry a date; `.docx`, `.txt`, `.md` and `.html`
are all readable, and the usual `Summary` / `Decisions` / `Next steps` structure
is parsed when present.

---

## State contract

Two files under `STATE_DIR` (default `./state`), written by this bot and intended
to be read by a supervising process (COSA). Treat them as a published interface:
new fields may be **added**, but existing ones are not renamed or repurposed
without updating this section, because something else is parsing them.

Both are gitignored — they are local operational state, not source.

### `state/summary.json`

A snapshot, **rewritten wholesale at startup and once a day** (at
`STATE_DAILY_HOUR`). Written to a temp file and atomically renamed, so a reader
never catches it half-written and can always assume valid JSON.

```jsonc
{
  "schema_version": 1,                  // bumped when a field changes meaning
  "bot_name": "SalesCoS",               // config.COS_NAME
  "generated_at": "2026-08-21T08:00:00Z",   // UTC, second precision, always 'Z'
  "trigger": "startup",                 // "startup" | "daily" — why this rewrite happened

  "scope": {                            // what the bot is allowed to touch
    "sales_channel_ids": ["123", "456"],// ids as strings (JS-safe)
    "ask_channel_id": "456",            // "" when unset
    "roster_size": 3,                   // how many people it may @-mention
    "never_dms": true                   // invariant, always true
  },

  "sources": [                          // one entry per source, fixed order
    {
      "key": "sales_spreadsheet",       // stable machine key
      "label": "the sales spreadsheet", // how the bot refers to it to a human
      "purpose": "pipeline, targets and the outreach log",
      "status": "awaiting-access",      // "connected" | "awaiting-access" | "error"
      "detail": "I don't have access…"  // one actionable sentence
    }
  ],

  "open_chases": [                      // commitments still being waited on
    {
      "person": "Trishi",
      "what": "the Acme deck",          // quotable noun phrase
      "promised_at": "2026-08-20 14:03:00",  // UTC 'YYYY-MM-DD HH:MM:SS'
      "due_at": "2026-08-21 14:03:00",
      "reminders_sent": 1,
      "channel_id": "123",
      "jump_url": "https://discord.com/channels/…"
    }
  ],

  "notable_events": [                   // things a supervisor should look at
    { "kind": "source_unavailable",  "detail": "…" },
    { "kind": "policy_missing",      "detail": "…" },
    { "kind": "channel_not_visible", "detail": "…" },  // role lacks View Channel
    { "kind": "nudges_last_24h",     "detail": "…" }
  ],

  "counts": {                           // derived; convenience for a dashboard
    "sources_connected": 1,
    "sources_awaiting_access": 2,
    "open_chases": 1,
    "notable_events": 2
  }
}
```

### `state/audit.jsonl`

**Append-only**, one JSON object per line, never rewritten or reordered — safe to
tail. One line per action the bot took, each with a timestamp and a reason.

```jsonc
{"ts":"2026-08-21T09:14:02Z","bot":"SalesCoS","event":"message_sent","reason":"chasing an overdue commitment (1 of 2)","kind":"nudge","channel_id":123,"message_id":789,"jump_url":"…","preview":"…","person":"Trishi","what":"the Acme deck","chase_id":4}
```

Guaranteed on every line: `ts` (UTC, `Z`), `bot`, `event`, `reason`. Everything
else varies by event type.

| `event` | Meaning |
|---|---|
| `startup` | the bot connected and rewrote its summary |
| `message_sent` | something was posted (`kind`: `reply` / `nudge` / `flag`) |
| `send_refused` | **a guardrail blocked a send** — a DM attempt, or a channel outside the scope |
| `send_failed` | Discord rejected an otherwise-allowed send |
| `chase_opened` | a commitment was detected and is now tracked |
| `chase_closed` | they came back, or someone ✅'d the reminder |
| `chase_given_up` | the attempt cap was reached; the bot stopped asking |

`send_refused` is the line to alert on: it means code attempted something the
guardrails forbid.

---

## Layout

| File | Role |
|---|---|
| `main.py` | entry point; validates env, logs scope and source statuses, connects |
| `bot.py` | the Discord client: routing, question answering, chasing, the sweeper |
| `guardrails.py` | **the hard rules** — every send and every read passes through here |
| `config.py` | environment → typed settings, with loud warnings for likely mistakes |
| `persona.py` | the voice, plus loading `sales_policy.md` fresh on every question |
| `sales_policy.md` | the operating policy (Sid's to rewrite) |
| `sources.py` | the three sources and their connected / awaiting-access status |
| `notes.py` | reads the Drive-synced meeting notes out of `NOTES_DIR` |
| `query.py` | Discord read primitives, scoped to the sales channels |
| `query_engine.py` | the bounded tool-use loop; holds no tools of its own |
| `llm.py` | the short model calls: routing, replies, commitment detection |
| `followups.py` | commitment prefilter, due-time maths, fallback nudge text |
| `db.py` | SQLite: `chases`, `nudges` (the rate limit), `meta` |
| `memory.py` | short-term per-channel conversation memory (in-memory only) |
| `state.py` | writes the state contract above |
| `ecosystem.config.js` | PM2 process definition (`sales-bot`) |

---

## Design notes

**Why the engine has no built-in tools.** `QueryEngine.answer(tools=…)` takes its
complete tool set from the caller. It holds no credential and no data source, so
it cannot reach anything `bot.py` didn't hand it — which is what makes the channel
scoping hold on the query path too.

**Why chases are capped.** The bot posts unprompted @-mentions. The `nudges` table
is checked immediately before every send and written immediately after, and the
check **fails closed**: if the log can't be read, the bot assumes it already
nudged. Nudging twice is worse than missing one.

**Why "I can't see that yet" is a feature.** The failure mode for an assistant
with two of three sources missing isn't silence, it's a confident answer built
from the third. The source statuses are in the system prompt, the prompt forbids
answering from an unreachable source, and the capability answer has a
deterministic fallback (`persona.fallback_capability_reply`) so it stays honest
even when the model call fails.

---

Scaffolded from the PM bot (`discord--linear-bot`), with the issue-tracker
integration and the whole triage-to-ticket pipeline removed.
