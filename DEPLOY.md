# Deploying the sales bot (Linux VPS + PM2)

A single long-running Python process holding a Discord gateway connection. It
runs 24/7 under **PM2** as `sales-bot`, keeps a small SQLite file
(`sales_bot.db`) and writes its state contract to `state/`.

It runs **alongside the PM bot**, not instead of it. Everything is separate: a
different Discord application and token, a different directory, a different venv,
a different `.env`, a different database, a different PM2 process name. Do not
share any of them — in particular, do not point `DB_PATH` at the PM bot's
`bot_state.db`.

---

## 0. Discord setup — do this FIRST

This section is the important one. **The channel scoping is enforced twice, and
the server-side layer is the one that actually matters.** The code refuses to
read or post outside `SALES_CHANNEL_IDS` (`guardrails.py`), but code can have
bugs; Discord permissions cannot be argued with by a language model.

### Create the application

1. <https://discord.com/developers/applications> → **New Application**.
2. **Bot** → add a bot user → **Reset Token** → copy it into `DISCORD_TOKEN`.
   This is a *new* token. Do not reuse the PM bot's.
3. **Bot → Privileged Gateway Intents → MESSAGE CONTENT INTENT: ON.**
   Without it every message arrives with empty content and the bot silently does
   nothing at all.
4. **OAuth2 → URL Generator**: scopes `bot`; permissions *View Channels*, *Send
   Messages*, *Read Message History*, *Add Reactions*. Nothing else — it doesn't
   manage anything.

### Lock the role to the sales channels — REQUIRED

The invite gives the role server-wide View Channel. Take it away, then grant it
back only where it belongs:

1. **Server Settings → Roles →** the bot's role → **Permissions**: turn
   **View Channels OFF**. The bot now sees nothing.
2. For **each sales channel** → **Edit Channel → Permissions →** add the bot's
   role and **allow**: View Channel, Send Messages, Read Message History, Add
   Reactions.
3. Verify: the bot should appear in the member list of the sales channels **and
   nowhere else**.

Then put those same channel ids in `SALES_CHANNEL_IDS`. The two layers must
agree — the server-side permission is what makes the scope true, and the config
is what makes the bot's behaviour deliberate rather than accidental.

> **Why both.** With only the code layer, one bug or one future `send()` that
> bypasses `guardrails` puts the bot in a channel it was never meant to see. With
> only the server layer, the bot might still *try*, and the attempt would be
> invisible. With both, an attempt is refused, logged at ERROR, and written to
> `state/audit.jsonl` as `send_refused` — which is exactly the event you want to
> alert on.

To copy ids: **User Settings → Advanced → Developer Mode**, then right-click a
channel → **Copy Channel ID**.

---

## 1. Get the code onto the server

```bash
ssh USER@SERVER_IP
sudo mkdir -p /opt/sales-bot && sudo chown "$USER" /opt/sales-bot
git clone https://github.com/Trishi7/sales-bot.git /opt/sales-bot
cd /opt/sales-bot
```

---

## 2. Its own venv

Separate from the PM bot's. Two bots sharing a venv means one `pip install`
upgrading a dependency under the other.

```bash
sudo apt-get update && sudo apt-get install -y python3 python3-venv python3-pip
cd /opt/sales-bot
python3 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt
```

Python 3.10+ is required (the code uses `list[int]` / `X | None` syntax).

---

## 3. Its own `.env`

```bash
cd /opt/sales-bot
cp .env.example .env
nano .env
chmod 600 .env          # it holds two secrets; keep it to the owner
```

Fill in `DISCORD_TOKEN`, `ANTHROPIC_API_KEY`, `SALES_CHANNEL_IDS`, and — strongly
recommended — `SALES_ASK_CHANNEL_ID` and `TEAM_ROSTER_IDS`. Every variable is
commented in `.env.example`.

**`.env` is gitignored and must never be committed.** If a token ever does land in
a commit, rotate it in the Developer Portal — rewriting history is not enough,
because the token was public for as long as the push existed.

---

## 3b. The GTM Playbook (Google Sheets API)

The bot reads the sheet **live over the API** — nothing is exported, downloaded or
rclone-synced. Two steps:

**1. The service-account key.** In the Google Cloud console: create (or pick) a
project, **enable the Google Sheets API**, create a service account, then
Keys → Add key → JSON. Put the file on the server and point at it:

```bash
scp sales-bot-write.json USER@SERVER_IP:/opt/sales-bot/
ssh USER@SERVER_IP "chmod 600 /opt/sales-bot/sales-bot-write.json"
# in .env:
GOOGLE_SERVICE_ACCOUNT_JSON=/opt/sales-bot/sales-bot-write.json
```

The key is a live credential. `.gitignore` covers the usual key filenames; if one
is ever committed, **revoke it in Google Cloud** — deleting the file is not
enough.

**2. Share all three sheets with it.** This is the step people miss, and nothing
works without it. Find the address in the key file:

```bash
python3 -c "import json;print(json.load(open('/opt/sales-bot/sales-bot-write.json'))['client_email'])"
```

Then, in Google Sheets, Share each one with that address:

| Sheet | Access | Why |
|---|---|---|
| *NFThing <> GTM Playbook* (original) | **Viewer** | read-only is the point |
| *… — BOT COPY (sandbox)* | **Editor** | the bot's deadline column is written here |
| *membrane.social - Researcher Buyer Mapping* | **Viewer** | read-only by policy — the bot has no write path to it at all |

**Viewer is correct *and sufficient* for the mapping sheet.** Do not grant it
Editor out of habit: the bot cannot write there in any case (no write method, a
read-only OAuth scope, and every write path refuses that spreadsheet id), so
Editor would widen the blast radius of the key for no benefit.

Until you do, every read is a `403` and the startup log tells you exactly what to
run:

```
[bot] GTM original sheet NOT reachable: the service account cannot open the original sheet (permission denied)
[bot] ACTION REQUIRED: Share "NFThing <> GTM Playbook" with sales-bot@… as Viewer (…)
```

The bot **does not crash** on this — the spreadsheet source reports
awaiting-access and it says so when asked. It just can't answer sheet questions.

**Writes** default to the sandbox (`SHEET_WRITE_TARGET=copy`). Set `original`
only deliberately; set `off` to disable sheet writes entirely (deadlines still
work, they're just not mirrored). Either way the write surface is one column,
one cell at a time, and a human-entered date is never overwritten.

---

## 4. Meeting-notes sync (optional, enables the third source)

The bot holds no Google credential — rclone does. **The bot runs the sync
itself** now: at startup, every `NOTES_SYNC_MINUTES`, and on demand before a
notes question. No cron entry is needed.

```bash
sudo apt-get install -y rclone
rclone config                       # add a Google Drive remote, once, interactively
rclone listremotes                  # confirm the remote's real name
```

Then in `.env`:

```
NOTES_DIR=/opt/sales-bot/notes
NOTES_SYNC_CMD=rclone copy "gdrive:Sales Meeting Notes" /opt/sales-bot/notes
NOTES_SYNC_MINUTES=30
```

`NOTES_DIR` is created at startup if it doesn't exist, so no `mkdir` is needed.

**The command must run as the bot's user.** `rclone config` writes
`~/.rclone.conf` for whoever ran it; a bot running under a different user (or as
a Windows service) has its own, empty one and will fail with *"didn't find
section in config file"*. Check with:

```bash
sudo -u <bot-user> rclone copy "gdrive:Sales Meeting Notes" /opt/sales-bot/notes
```

**On Windows, a PowerShell alias for `rclone` is invisible to the subprocess** —
put `rclone.exe` on `PATH` or write its full path into `NOTES_SYNC_CMD`
(`C:\rclone\rclone.exe copy ...`).

A failing sync doesn't stop the bot. It is logged **once** at ERROR with the fix
named, the `sales_meeting_notes` source reports **degraded**, and answers say the
notes may be stale. Watch for it with:

```bash
pm2 logs sales-bot | grep "\[notes\]"
# healthy:  [notes] synced 72 docs, 2 loaded, 70 excluded (standups)
# broken:   [notes] SYNC FAILED — … FIX: …
```

Every synced note is read **except** the recurring product standups, matched by
title against `NOTES_EXCLUDE_TITLE_PATTERNS` (default `AM sync,PM sync`) — see
[README.md](README.md#meeting-notes-the-sync-and-the-standup-exclusion).
`synced 72 docs, 0 loaded, 72 excluded` means the patterns are catching real
meetings: widen or clear them. `0 loaded, 0 excluded` with docs on disk means
nothing that came down carried a parseable date, so none of it is a meeting note.
The startup log says which case you are in.

Leave `NOTES_DIR` unset and the source simply reports *awaiting-access*, which
the bot states plainly when asked. That is a supported state, not a broken one.

---

## 4b. SearXNG (the search backend)

The bot searches the web through **SearXNG**, a metasearch engine you run on the
same server. It is free, needs no API key and has no daily quota. The bot calls
it over loopback and reads its JSON; nothing else can reach it.

**Without it the bot still searches** — `SEARCH_FALLBACKS=ddg` answers through
DuckDuckGo — but that path is scraped, rate-limited and sometimes empty. It is
the fallback, not the plan. R1's news and company news (R8/R10) need neither:
they are RSS.

### Install Docker (once)

```bash
sudo apt-get update && sudo apt-get install -y docker.io docker-compose-v2
sudo usermod -aG docker "$USER"     # then log out and back in
```

### Two files

```bash
sudo mkdir -p /opt/searxng/config && sudo chown -R "$USER" /opt/searxng
cd /opt/searxng
```

`/opt/searxng/docker-compose.yml`:

```yaml
services:
  searxng:
    image: docker.io/searxng/searxng:latest
    container_name: searxng
    restart: unless-stopped
    ports:
      - "127.0.0.1:8888:8080"        # loopback ONLY — never "8888:8080"
    volumes:
      - ./config:/etc/searxng
    environment:
      - SEARXNG_BASE_URL=http://127.0.0.1:8888/
    logging:
      driver: json-file
      options:
        max-size: "1m"
        max-file: "1"
```

`/opt/searxng/config/settings.yml`:

```yaml
use_default_settings: true

server:
  secret_key: "REPLACE_WITH_openssl_rand_hex_32"
  limiter: false          # the limiter exists to block bots, and the bot is one
  image_proxy: false

search:
  safe_search: 0
  formats:
    - html
    - json                # REQUIRED — without it format=json answers 403
```

Three lines there matter:

- **`formats: [html, json]`.** A stock SearXNG serves only HTML and answers the
  bot's `format=json` with **403**. This is the step people miss.
- **`limiter: false`.** The limiter rejects clients that do not look like a
  browser. It is safe to turn off *because* of the next point.
- **`127.0.0.1:8888:8080`** in the compose file. Docker publishes ports past
  `ufw`, so `"8888:8080"` would put an open, unlimited search proxy on the
  public internet. Bound to loopback, only this machine can call it.

Fill in the secret and start it:

```bash
sed -i "s/REPLACE_WITH_openssl_rand_hex_32/$(openssl rand -hex 32)/" config/settings.yml
docker compose up -d
```

### Check it, then point the bot at it

```bash
curl -s 'http://127.0.0.1:8888/search?q=searxng&format=json' | head -c 300
# healthy: {"query": "searxng", "number_of_results": …, "results": [{"url": …
# 403:     json is not in search.formats — fix settings.yml, `docker compose restart`
# refused: the container is not up — `docker compose ps`, `docker compose logs`
```

In the bot's `.env`:

```
SEARCH_BACKEND=searxng
SEARXNG_URL=http://127.0.0.1:8888
SEARCH_FALLBACKS=ddg
```

Then, from `/opt/sales-bot`, the live check — real searches, no model call,
nothing posted:

```bash
./venv/bin/python verify_search_backend.py --only a
```

It should end `ALL PASSED` with `backend=searxng` on the search lines. If they
say `backend=ddg`, SearXNG did not answer and the line above them says why.

In the bot's log a healthy search is one line, and a fallback is two:

```
[search] query='"Meera Iyer" "IIT Madras" email contact' backend=searxng n=8 cached=no
[search] searxng failed (ConnectionError); falling back to ddg
```

After a connection failure the bot leaves SearXNG alone for five minutes and
uses the fallback, so a stopped container costs one timeout rather than one per
search. `pm2 restart sales-bot` clears that wait.

### Keeping it running

| Action | Command (in `/opt/searxng`) |
|---|---|
| Status | `docker compose ps` |
| Logs | `docker compose logs --tail 50` |
| Restart (after editing `settings.yml`) | `docker compose restart` |
| Update | `docker compose pull && docker compose up -d` |

`restart: unless-stopped` brings it back after a reboot. Results that turn up
empty while the container is healthy usually mean the upstream engines have
rate-limited the server's IP; the bot treats "every engine unresponsive" as a
failure and falls back, and `docker compose logs` names the engines.

**Google Custom Search** is the third option (`SEARCH_BACKEND=google_cse`, or add
it to `SEARCH_FALLBACKS`): set `GOOGLE_CSE_KEY` and `GOOGLE_CSE_CX`. It is free
for 100 queries a day and the bot stops itself at 100, counted on Google's
(Pacific) day, so it is never billed.

---

## 5. Start it under PM2

```bash
sudo npm install -g pm2            # if PM2 isn't already there
cd /opt/sales-bot
mkdir -p logs state
pm2 start ecosystem.config.js
pm2 save                           # persist the process list across reboots
pm2 startup                        # prints a command to run once, as root
```

`ecosystem.config.js` sets the process name to **`sales-bot`**, runs
`./venv/bin/python main.py`, and keeps one instance — a second would post the
daily digest twice (each process holds its own connection, and the once-a-day
marker is written after the send) and race on the SQLite file.

---

## 6. Verify

**Before starting the bot**, prove the sheet path on its own — it needs no
Discord token and tells you in seconds whether the service account is actually
shared in:

```bash
cd /opt/sales-bot && ./venv/bin/python -m gtm_sheet            # access + schema
cd /opt/sales-bot && ./venv/bin/python -m gtm_sheet --write    # + write round-trip
cd /opt/sales-bot && ./venv/bin/python -m mapping_sheet        # the mapping sheet
```

`python -m mapping_sheet` checks access, dumps the discovered schema, and prints
the rules it loaded from the legend — the ICP lanes, the tier definitions, the
refresh window and how many people are on the DEPARTURES do-not-pitch list. If
the departures count is `0`, the Edge Map row is missing or renamed: the bot will
still answer, but it will say the departure check could not run. There is no
`--write` for this one, and that absence is the point.

`--write` writes one cell of the bot's own column on the **sandbox copy**, reads
it back and confirms it matches; it refuses to run unless `SHEET_WRITE_TARGET=copy`.
A healthy run ends `PASSED`. If it reports the sheets are unreachable, it prints
the exact `share the sheet with <address>` instruction — do that and re-run
before going further.

Then start it and watch the log:

```bash
pm2 logs sales-bot --lines 100
```

A healthy start logs, in order:

```
[main] starting SalesCoS (log level=INFO)
[main] config OK. sales_channels=[…] ask_channel=… roster=N …
[main] SCOPE: this bot reads and posts ONLY in the N channel(s) above …
[bot] channel scope: N of N configured sales channel(s) visible
[bot] GTM service account: sales-bot@….iam.gserviceaccount.com
[bot] GTM original sheet reachable: 'NFThing <> GTM Playbook' (3 tabs)
[bot] GTM copy sheet reachable: 'NFThing <> GTM Playbook — BOT COPY (sandbox)' (3 tabs)
[bot] GTM schema: tab 'Outreach Tracker' kind=outreach_tracker rows=42 cols=18 mapped=[…]
[main] source sales_spreadsheet     connected        …
[main] source strategy_doc          awaiting-access  …
[main] source sales_meeting_notes   connected        …
[main] policy loaded from ./sales_policy.md (N chars); re-read on every question
[main] connecting to the Discord gateway...
[bot] connected as SalesCoS#1234 (id=…), in 1 guild(s)
[bot] reading #sales-ask (…)
[bot] chase sweeper started (every 15 min)
```

Then check the things that matter:

| Check | How | Expected |
|---|---|---|
| It joined | member list of a sales channel | the bot is there |
| It answers in persona and states its sources | post `@<bot> what can you do?` in the ask channel | a direct, emoji-free reply that **names the sources awaiting access** |
| It answers a reply | reply to that answer with a follow-up question, tagging nobody | a reply — replying to the bot counts as addressing it |
| It stays quiet when untagged | post `what can you do?` in the ask channel with nobody tagged | **no reply**, and `[gate] ignored msg=… reason=ignored` in the log (the no-mention mode is gone; the ask channel is only the digest's home) |
| It ignores a room-wide ping | post `@here taking the first half off` in a sales channel | no reply — `@here`/`@everyone` is never a bot mention |
| It stays out of other people's conversations | post `@<a teammate> can you check OpenAI?` in a sales channel | no reply — a message tagging someone else is never answered |
| It ignores everything else | post a question in a NON-sales channel | no reply, and nothing in the logs beyond DEBUG |
| It can't see other channels | member list of a non-sales channel | the bot is **not** there |
| It can read the sheet | ask `@<bot> where are we with <a company in the tracker>?` | an answer citing the tab and the company |
| It can write the sandbox | ask `@<bot> when should we follow up with <a company with no date>?` | a "No deadline was set — setting … shout to change" post, and the date appearing in the sandbox's `Next Deadline (bot)` column |

Watch for `[bot] sales channel … is NOT visible` — that means a configured id has
no View Channel grant (or is wrong), and the bot will skip it silently at
runtime. It's also recorded in `state/summary.json` under `notable_events` as
`channel_not_visible`.

---

## Updating

```bash
cd /opt/sales-bot
git pull
./venv/bin/pip install -r requirements.txt   # only if requirements changed
pm2 restart sales-bot
```

`.env`, `sales_bot.db` and `state/` are all gitignored, so a pull never touches
them.

**Editing `sales_policy.md` does not need a restart** — it's re-read on every
question. `git pull` alone is enough for a policy change.

---

## Common operations

| Action | Command |
|---|---|
| Live logs | `pm2 logs sales-bot` |
| Last 200 lines | `pm2 logs sales-bot --lines 200 --nostream` |
| Restart | `pm2 restart sales-bot` |
| Stop | `pm2 stop sales-bot` |
| Status | `pm2 status sales-bot` |
| Edit config | `nano /opt/sales-bot/.env` then `pm2 restart sales-bot` |
| Current state | `cat /opt/sales-bot/state/summary.json` |
| What it has done | `tail -f /opt/sales-bot/state/audit.jsonl` |
| Guardrail violations | `grep send_refused /opt/sales-bot/state/audit.jsonl` |

---

## Notes

- **Outbound only.** No inbound ports, no reverse proxy, no firewall rules.
  SearXNG (section 4b) listens on `127.0.0.1:8888` only — if `ss -ltn` ever
  shows `0.0.0.0:8888`, the compose file's port line is wrong.
- **Logs** go to `./logs` via PM2 (gitignored). Rotation:
  `pm2 install pm2-logrotate`.
- **Verbosity**: `LOG_LEVEL` in `.env` (`DEBUG` | `INFO` | `WARNING` | `ERROR`).
  `DEBUG` shows every skipped channel and routing decision — useful while setting
  up, far too loud to leave on.
- **Backups**: `sales_bot.db` holds the open chases, the deadlines, the digest's
  carry-forward ages and the marker that says whether today's digest already went
  out. Losing it means in-flight chases are forgotten, the caps reset, every
  digest item restarts at "1st day", and the day it is lost may get a second
  digest. Not catastrophic, but a nightly copy is cheap.
- **Two bots, one box**: `pm2 status` should show both, with distinct names. If
  the sales bot ever starts answering in the PM bot's channels, the cause is
  `SALES_CHANNEL_IDS`, not the code — check it, and check the role permissions
  from step 0.

---

## Upgrading to the S1 build (cap, schedule, one reminder lane)

Nothing to run by hand: the first boot adds two columns to `drip_sends`
(`counts_toward_cap`, `pinned`) through the usual "columns added later" pass,
and older rows keep NULL.

In the server's `.env`:

```bash
DAILY_MESSAGE_CAP=5          # was 3 — five counted posts, every weekday
# DAILY_MESSAGE_CAP_BY_DAY=  # delete the line: retired, no longer read
```

What changes on the first day after the restart:

- up to **5** counted posts a weekday; meeting prep, meeting follow-ups,
  reminders and urgent news are on top of that;
- AI news posts **every** weekday at `NEWS_MAIN_TIME` (it used to skip
  alternate days);
- a meeting's day-of prep note goes at `MEETING_DAYOF_TIME` (10:00), not when
  the window opens;
- **Sunday can now post** — one Deliverables post at `SALES_DRIP_START`, only
  when a P1 is due on the Monday. Set `SUNDAY_RULE_IDS=` (empty) to keep Sunday
  silent;
- any reminder whose date has already passed and is still open posts **once**
  on the first tick, marked "(this was due <date>)". Check
  `list_reminders` beforehand if there may be old ones;
- R9 follow-ups now climb their ladder — the second and third go to the owner
  (as DMs when `SALES_DMS_ENABLED=true`), the fourth to `ESCALATION_ADDRESSEE`,
  and then they stop.

`python verify_s1.py` checks all of it offline (one free DuckDuckGo request).

### Upgrading to S2 and S3

Nothing to run by hand; the first boot adds the new columns.

- **PoC news (S2)** starts by itself: 15 names a day from the sheet are looked
  up on Google News RSS. `NEWS_POC_TARGETS_PER_DAY=0` turns it off.
- **R3 now posts every Wednesday** (it was alternate weeks), and the separate
  "one reminder at T-20" events post is gone. Delete `EVENTS_ENABLED`,
  `EVENT_LEAD_DAYS` and `EVENTS_ANCHOR_DATE` from `.env`.
- **R5 will name more people**: it now reads every never-contacted row, not
  only active ones, two companies a week.
- **Email lookups** run for R5/R6 contacts with a blank Email cell (one search
  each, at most 5 a post). They need a working search backend — until SearXNG
  is up (section 4b) they go through the DuckDuckGo fallback.
- **Writing a found email is OFF** until you set `EMAIL_WRITE_ALLOWED=true`.
  It is the one exception to the restricted band: only the Email cell, only on
  an approver's yes, only if the cell is still blank, undoable.

`python verify_s2.py` and `python verify_s3.py` check both offline.
