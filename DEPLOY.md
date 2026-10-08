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

## 4. Meeting notes: the sales notes folder (optional, enables the third source)

The bot reads sales meeting notes from **one Drive folder and nothing else**. It
does not read "whatever is in Drive", and it never reads the AM/PM sync notes.
The full list of what it reads is in [docs/SOURCES.md](docs/SOURCES.md).

The bot holds no Google credential — rclone does. **The bot runs the sync
itself**: at startup, every `NOTES_SYNC_MINUTES`, and on demand before a notes
question. No cron entry is needed.

**Not connected is a supported state.** With `NOTES_SOURCE_FOLDER` unset (the
default) no sync runs, nothing in `NOTES_DIR` is read or moved, the source
reports *awaiting-access*, and asked about a meeting the bot says *"Meeting
notes aren't connected to me yet."* Deploying this version with `.env` untouched
therefore changes nothing on disk.

### Setup

1. **In Drive, create the folder `Saley – Sales Notes`.** The dash is an EN DASH
   (`–`), not a hyphen.
2. **Share it with `claudedrive@nfthing.com`** — the account behind the rclone
   remote.
3. **Put the sales meeting notes in it**, at the TOP LEVEL (subfolders are not
   read), or Drive shortcuts to them. **Do not put AM/PM sync notes in it.** A
   doc whose title carries any of these is refused even inside the folder: `am
   sync`, `pm sync`, `nfthing kick-off`, `nfthing wrap-up`, `standup`,
   `stand-up`, `daily sync`. A bare "sync" is fine — "Sales sync" is read.
4. **rclone, once, as the bot's user:**

   ```bash
   sudo apt-get install -y rclone
   rclone config                       # add a Google Drive remote, once, interactively
   rclone listremotes                  # confirm the remote's real name (here: gdrive)
   ```

5. **In `.env`** (server, `/opt/sales-bot/.env`):

   ```
   NOTES_SOURCE_FOLDER=Saley – Sales Notes
   NOTES_SYNC_CMD=rclone sync "gdrive:Saley – Sales Notes" /opt/sales-bot/notes --exclude "/_quarantine/**"
   NOTES_EXCLUDE_TITLE_PATTERNS=AM sync,PM sync,NFThing Kick-off,NFThing Wrap-up,standup,stand-up,daily sync
   NOTES_REQUIRE_TITLE_TAGS=
   ```

   * **Copy the folder name, don't retype it** — a hyphen in place of the en dash
     makes the command name a folder that does not exist. Save `.env` as UTF-8.
   * The name in `NOTES_SOURCE_FOLDER` must appear, character for character, in
     `NOTES_SYNC_CMD`. If it doesn't, the bot **does not run the sync** and says
     so in the log.
   * **`rclone sync`, not `rclone copy`.** A mirror removes a note here when it
     is taken out of the Drive folder; a copy would leave it readable for ever.
   * **The `--exclude "/_quarantine/**"` is required.** Without it `rclone sync`
     would delete the quarantine (below). The bot refuses to run a mirror that
     lacks it, and the log names the flag.
   * **The sync destination must be the same folder as `NOTES_DIR`.**
     `NOTES_DIR=./notes` is `/opt/sales-bot/notes` when the bot runs from
     `/opt/sales-bot`. If they differ the bot sees an empty folder and says so.
   * `NOTES_REQUIRE_TITLE_TAGS` stays empty: the folder is the tag, and nobody
     has to rename a meeting. If it is ever set, **the tag must be a distinctive
     word**: tags are matched as whole words, ignoring punctuation and case, so
     `[Sales]` is just the word "sales" and matches "Sales sync – …" and "Acme
     sales call". Use something like `[SalesNotes]`.

   On the laptop the destination is `./notes` instead of `/opt/sales-bot/notes`.
   A machine with no `gdrive` remote leaves `NOTES_SOURCE_FOLDER` and
   `NOTES_SYNC_CMD` blank and runs not connected.

6. **Restart.** `NOTES_DIR` is created if it doesn't exist. On the first sync
   the bot **moves every file already in the notes folder** to
   `notes/_quarantine/<timestamp>/` and logs it once — those files came from an
   older, broader sync and are not from the sales folder. **Nothing is deleted.**

`NOTES_SYNC_MINUTES` is `1440` on the server, so the timer syncs once a day. A
note added to the folder shows up after a restart, at the next daily tick, or
when somebody asks about a recent meeting ("the latest call", "today's
meeting"), which forces a sync.

**The command must run as the bot's user.** `rclone config` writes
`~/.rclone.conf` for whoever ran it; a bot running under a different user (or as
a Windows service) has its own, empty one and will fail with *"didn't find
section in config file"*. Check with:

```bash
sudo -u <bot-user> rclone sync "gdrive:Saley – Sales Notes" /opt/sales-bot/notes --exclude "/_quarantine/**" --dry-run
```

**On Windows, a PowerShell alias for `rclone` is invisible to the subprocess** —
put `rclone.exe` on `PATH` or write its full path into `NOTES_SYNC_CMD`
(`C:\rclone\rclone.exe sync ...`).

### What to look for in the log

```bash
pm2 logs sales-bot | grep "\[notes\]"
# healthy:        [notes] source=Saley – Sales Notes state=ok on_disk=3 from_folder=3 loaded=3 standup=0 missing_tag=0 undated=0 quarantined=0
# first sync:     [notes] moved 76 file(s) that did not come from Saley – Sales Notes to …/notes/_quarantine/20261007-101500 — nothing was deleted
# broken sync:    [notes] SYNC FAILED — … FIX: …
# refused config: [notes] SYNC NOT RUN — … FIX: …
```

`on_disk` is every file at the top level of the notes folder; each one is in
exactly one of `loaded`, `standup`, `missing_tag`, `undated` or not from the
folder (`on_disk − from_folder`). When files came from the folder and **none**
loaded, a WARNING names the bucket that took them and the setting to look at —
"nothing loaded" is never silent. A failing sync doesn't stop the bot: it is
logged **once** at ERROR with the fix named.

What the team hears in the channel when there is nothing to read is one of
three sentences, and never anything from another source:

| Situation | The bot says |
|---|---|
| not connected (or a command it refuses to run) | Meeting notes aren't connected to me yet. |
| the folder is reachable and holds no sales notes | There are no sales meeting notes in the Saley – Sales Notes folder yet. |
| the folder has never been reached | I can't reach the sales notes folder right now, so I haven't checked the notes. |

If the folder synced before and the latest sync fails, the bot keeps answering
from that last good copy of the sales folder and says it may be stale.

### The quarantine: inspect and clear

```bash
ls -R /opt/sales-bot/notes/_quarantine
```

shows what was moved and when (one folder per sweep, named by timestamp). **The
bot never reads it — at any depth — and never deletes it.** A file lands there
when it is in the notes folder but did not come from the sales folder: the
leftovers of the old sync on the first run, everything on disk when
`NOTES_SOURCE_FOLDER` changes, or a file somebody dropped in by hand.

Once a human has looked, clear it by hand:

```bash
rm -r /opt/sales-bot/notes/_quarantine/<timestamp>
```

To put a note back, **move it into the Drive folder**, not into `notes/` — a
file dropped into `notes/` by hand is quarantined again at the next sync.

### Hidden to-do rows

A row on the to-do sheet is shown only when its *Source meeting* is a sales note
the bot can read. Rows that cite a standup, an internal meeting or nothing are
hidden from every reply; the bot never edits or deletes them and says nothing
about them in the channel. To list them for cleaning the sheet by hand:

```bash
cd /opt/sales-bot && python tools/list_hidden_todos.py
```

It is read-only: it never creates the sheet, never writes a cell and never runs
the sync. See [README.md](README.md#meeting-notes-the-sales-folder-the-sync-and-the-standup-guard).

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

## Upgrading to RULE 13 (next steps for connected contacts, and the 7 Oct sheet layout)

The Outreach PoCs tab changed shape on 7 Oct 2026: 32 columns, A to AF, with
the Next Steps dropdown on Q, the three emails on R to W, the meeting columns
on X and Y, and Notes/Remarks on Z. The column bands are letters, so they must
match.

1. **Set three lines in `/opt/sales-bot/.env`** (and in the laptop's `.env`):

   ```
   RESTRICTED_COLUMN_RANGES=A:I,Q:W,Z:AE
   NEW_ROW_WRITABLE_RANGES=A:P,X:Y
   GTM_COLUMN_MAP=
   ```

   `GTM_COLUMN_MAP` goes blank: the header aliases now put the notes role on
   "Notes/Remarks" and the step role on "Next Steps" by themselves. A leftover
   `{"outreach_pocs":{"next_steps":"Notes/Remarks"}}` is harmless; an entry
   pointing `next_steps` at "Next Steps" is ignored with a warning.
2. **Check `COS_FOLLOWUP_CHECK_INTERVAL_MINUTES` is 15 or unset.** Rule 13
   posts on the first sweep at or after `NEXT_STEP_TIME` (15:00), so the
   interval is how late it can be.
3. **Restart**, then read the boot log:
   - `[gtm.window]` says `restricted A:I, Q:W, Z:AE` and `Writable window J:P,
     X:Y holds 9 named column(s)`, ending with `X='Meeting Date'` and
     `Y='Meeting Status'`;
   - the role report shows `outreach_step` on 'Next Steps', the six
     `email_N_sent` / `email_N_date` roles, `next_steps` on 'Notes/Remarks' and
     `poc_priority` on 'Priority';
   - there is **no** `NEW_ROW_WRITABLE_RANGES=… reaches into restricted
     column(s)` warning;
   - the `[rules]` block lists 13 rules, R7 DISABLED, and R13 on Mon to Fri,
     5 a post, outside the cap.
4. Nothing to run by hand: the two new tables (`next_step_followups`,
   `next_step_posts`) are created on boot. The ten `NEXT_STEP_*` settings are
   optional; the defaults in `.env.example` apply without them.
5. **Try it in the test channel first** (`SALES_TEST_MODE=true` and a
   `DB_PATH` ending in `_test.db`). Test mode records rule 13's rotation in
   `DB_PATH` like every other ledger; a "make it Monday" test day records it
   only on a `*_test.db`.

`python verify_rule13.py` checks all of it offline.

## Upgrading to NFT2-1065 (web search on every question, profile links)

Nothing to run by hand and no schema change.

**1. Four lines, in the laptop's `.env` AND the server's `/opt/sales-bot/.env`.**
Both files SET the first two today (to 2 and 5), so the new defaults change
nothing until the lines are edited; the last two are new:

```bash
WEB_QUESTION_MAX_SEARCHES=4
QUERY_ENGINE_MAX_TOOL_ITERATIONS=7
WEB_QUESTION_EXTENDED_SEARCHES=6
QUERY_ENGINE_EXTENDED_TOOL_ITERATIONS=9
```

Restart (`pm2 restart sales-bot`). The startup log must show

```
[config] one question: up to 4 web search(es) and 7 tool round(s); one extension to 6 and 9.
```

and no `… is below … + 3` warning. A question that goes past four searches logs
one `[engine] msg=… LIMIT EXTENDED ONCE` line, and never a second.

**2. Check SearXNG answers** (section 4b: `curl` the loopback url, and the
startup line `backend=searxng`). A question may now make up to six searches; with
SearXNG down every one of them is a DuckDuckGo scrape, which is rate-limited
and sometimes empty. If the log shows `backend=ddg` on question searches, fix
SearXNG before relying on profile lookups.

**3. The add offer.** `SHEET_ROW_ADDITIONS_ENABLED=true` and `outreach_pocs` in
`SHEET_APPENDABLE_TABS` are what let Saley ask "Want me to add … to Outreach
PoCs?". The offer and the write are ON (`bot.POC_ROW_ADD_WRITE_WIRED = True`).
**An approver's yes writes the REAL sheet, in test mode too** when
`SHEET_WRITES_ENABLED=true`: one new row with the serial number, Name, Company
and — only when a search returned a `linkedin.com/in` link — the LinkedIn URL,
with Saley's note on the Name cell. No existing row is touched. Two things in
code still await the human's answer, each one line: whether the serial number
is written (`bot.POC_ROW_ADD_FILL_SERIAL`, `True` today) and what the note's
"source link" is (`approvals.ROW_SIGNATURE`, which carries the approval link
and the LinkedIn link today). Vaishnavi confirms the offer wording before
deploy. To switch the whole feature off without a code change, set
`SHEET_ROW_ADDITIONS_ENABLED=false` or take `outreach_pocs` out of
`SHEET_APPENDABLE_TABS`.

**4. Live-channel checklist** (record the `SALES_TEST_MODE` it ran under; test
mode and live behave the same, the `[TEST…]` tag aside):

1. "@Saley LinkedIn and research profile links for Janajit Bagchi and Suryansh
   Shukla (ARTPARK India)" → it searches on the first ask; a per-person answer;
   every link opens the right page; no "I don't have web search".
2. The same question worded "…for the PoCs at ARTPARK" → still searches.
3. "@Saley where are we with ARTPARK?" then "@Saley and their LinkedIn?" →
   searches.
4. A profile for someone with no public profile → "no public profile found",
   no link.
5. Six or more people at once → what was found, and "not checked yet" for
   the rest; no mention of a limit; exactly one `LIMIT EXTENDED ONCE` line in
   the log for that message.
6. The offer appears as its own message, in the confirmed wording. Vaishnavi
   confirms it reads clearly.
7. A non-approver replies "yes" → the polite no; the sheet is unchanged.
8. An approver replies "no" → "Nothing has changed in the sheet."; check the
   sheet.
9. Only if a real row is wanted (this writes the real sheet): an approver
   replies "yes" → the row appears at the bottom of Outreach PoCs with the
   serial number, Name, Company and LinkedIn URL only, and the Name cell
   carries a note: "Added by Saley · approved by <approver> · <date> IST",
   the link to the approver's message, and (when there is a LinkedIn URL)
   "LinkedIn link found by web search: …". The reply ends "with my note on
   the Name cell." No existing row is touched. Delete the row by hand
   afterwards if it was a test. (With `SHEET_WRITES_ENABLED=true` this writes the REAL sheet in test
   mode too.)
10. "@Saley what do we need to do today?" → the to-do sheet answer, as after
    NFT2-1062.
11. "@Saley what can you do?" → mentions web search.
12. "@Saley what did you cost today" → search requests and tokens are in line
    with docs/plans/NFT2-1065.md, section 3.

`python verify_profile_lookup.py` and `python verify_replay_oct6.py` check it
offline.

## Upgrading to NFT2-1063 (replies read as replies, objectives on demand)

**1. Discord, before the restart.** Give the bot's role **Add Reactions** in
every sales channel (and the test channel). "Sure" or "thanks" under a message
that asked nothing now gets one 👍 and no text. Without the permission nothing
is said instead; the log shows `[guardrails] Discord rejected the reaction`
and the audit a `reaction_failed`.

**2. `.env`, on the laptop AND in `/opt/sales-bot/.env`.** No line is
required; the default applies without it. To make it explicit:

```bash
PROPOSAL_BARE_YES_MINUTES=30
```

A bare "@Saley yes" that is not a reply answers the one open proposal in that
channel only if it is younger than this many minutes; otherwise Saley asks
which. `0` = always ask. While in the server's file, check the values that
change what the checklist shows: `SALES_APPROVER_IDS` (who can say yes),
`SALES_FINAL_SAY_ID`, `COS_FOLLOWUP_CHECK_INTERVAL_MINUTES` (15 or unset: the
scheduled posts go out on a sweep tick, so a large value makes the 2pm post
late), `SALES_TEST_MODE` and `SIMULATION_PREFIX`. On the laptop, re-run
`python tools/redact_env.py` after any change.

**3. Restart** (`pm2 restart sales-bot`). Two columns are added to
`drip_sends` on boot (`body`, `part_ids`); nothing to run by hand.

**4. The day of the restart only.** Posts sent before the restart have no
stored text, so that day's "what are today's objectives?" shows only what is
still to come. From the next day it shows everything.

**5. One thing to undo by hand.** The 7 Oct "sure" scheduled a reminder for
Mon 12 Oct 14:00 that nobody asked for. Unless the team wants it:
"@Saley list reminders", then cancel it.

**6. Live-channel checklist** (test channel first, then a live sales channel;
record the `SALES_TEST_MODE` it ran under):

1. "@Saley any AI news?" The interim line, if one appears, says nothing about
   the web unless a search really runs. The stories come back.
2. Reply "sure" to the interim line (or to the answer) while an offer is open
   elsewhere in the channel. One 👍, no message, no new reminder
   ("@Saley list reminders"), the offer still open.
3. Reply "ok", "thanks", "got it" to three different bot messages. One 👍
   each, nothing else.
4. On a Wednesday, reply "yes" to the events post. "I'll post the AI events
   list again on Mon … at 2 PM." Reply "yes" again: "… was already answered
   …", and still one reminder.
5. Tell Saley an update ("met Sahaj today"). It proposes. A non-approver
   replies "sure" to THAT message: the polite no. An approver replies "yes"
   to it: applied.
6. With two offers open, an approver types "@Saley yes" (not a reply). "Which
   one do you mean?" with both named. Reply "2" to that: the second is
   applied.
7. Reply to an answer with a real question ("and for Globex?"). A normal
   answer that understands what "and" refers to.
8. At about 11:00, "@Saley what are the sales objectives for today?" The day's
   items, laid out as the posts are, then the to-do sheet. No times, no rules,
   no schedule. Ask again from a second account at once: the same text.
9. Ask at 13:58 and at 14:05. The 2pm post still appears once, at its time,
   with its tags.
10. On a day with nothing due: "Nothing's due today."
11. "@Saley cadence preview" still gives the rule-by-rule queue.
12. Next morning: the previous day's `drip_sends` has one row per post and no
    extras, and the audit has no `proposal_vote` that was not a reply to its
    own proposal or a bare yes inside the window:
    `grep '"event": "proposal_vote"' state/audit.jsonl | tail`.

**To confirm what happened on 6 and 7 Oct**, the greps to run in
`/opt/sales-bot` are in `docs/plans/NFT2-1063.md`, section 3.4.

## Upgrading to NFT2-1064 (answers that read like a teammate)

Nothing to run by hand, no schema change, and **no line is required** in
either `.env`. The one new variable, `ANSWER_GUARD_ENABLED`, defaults to on.

**1. Recommended, in the laptop's `.env` AND the server's `/opt/sales-bot/.env`.**
`SALEY_EMOJI=medium` is not a valid value (none / light / expressive); it
already falls back to `light` and logs a warning on every start. Behaviour
does not change; the warning goes.

```bash
SALEY_EMOJI=light
# optional, only to make the default explicit
ANSWER_GUARD_ENABLED=true
```

`SALEY_HUMOUR`'s default is now `light` in `.env.example` as well as in the
code. Both `.env` files SET `SALEY_HUMOUR=off`, so nothing changes until that
line is edited: set `SALEY_HUMOUR=light` in both only if proactive messages
should use the new default.

Also recommended in both files: `ROSTER_DISPLAY_NAMES` with one entry per id
in `SALES_APPROVER_IDS`. Three refusal lines now name who may approve from
that setting; today only one approver id has a name there, so they read
"Vaishnavi or another approver".

**2. Restart** (`pm2 restart sales-bot`) and read the boot log:

```
[boot] voice profile: EXISTS …
```

If it says `NONE`, answers run on the fixed voice rules alone until the weekly
build (or an approver's "refresh voice"). Note which it said.

**3. The samples page, only when the human says go.** It calls the real model
20 times (hard cap 25). The key comes from the shell that runs it, never from
`.env`:

```bash
python tools/tone_samples.py            # dry run first: no model, prints the page
export ANTHROPIC_API_KEY=…              # in the shell, for this run only
python tools/tone_samples.py --live-model --voice-db ./sales_bot_test.db
```

Wait for NFT2-1063 to land first. It writes `docs/tone-samples.md`, which is
what Kushal signs off. `--voice-db` is required (or `--no-voice`): the file is
opened read-only and copied, never written, and the page prints only the
profile's date and counts. Exit 2: no key in the shell, or no usable profile
in that file. Exit 3: the page is incomplete and says so in its header.

**If it prints `NO-TOOLS FINAL CALL FAILED`**, the API rejected tool results
sent without a tool list. The script carries on with the list attached and
the page's second line says so, but tell whoever owns `query_engine.py`: the
engine's forced final call (`query_engine.py:732`, made at the tool-round
limit) sends the same shape and would fail the same way.

**4. Live-channel checklist** (record the `SALES_TEST_MODE` it ran under; test
mode and live behave the same, the `[TEST…]` tag aside):

1. "@Saley where are we with <a company on the sheet>?" → one to three lines,
   the status first, no "Here's", no "Based on", no list of sources.
2. "@Saley is <company> on hold?" → "Yes/No …", and if it comes from a
   meeting, one bracket: `(<meeting>, <date>)`. No "according to the meeting
   notes".
3. "@Saley what do we need to do today?" → the to-do link and the open items;
   no line repeating the question.
4. "@Saley who are the PoCs at <company>?" → the people list (`find_people`'s
   own text).
5. "@Saley LinkedIn links for <two people> (<org>)" → still the per-person
   lines, with "not checked yet" where it applies and no mention of a limit.
6. "@Saley top 5 AI headlines" → five bullets, nothing before them.
7. A question it cannot fully answer (a source awaiting access) → the answer
   it has, plus ONE plain line about what it couldn't check.
8. "@Saley hi" → one line. Reply "thanks" to an answer → one 👍 reaction and
   no text (NFT2-1063).
9. "@Saley what can you do?" → still names what it cannot see; mentions web
   search.
10. A slow question → the interim line names no sheet and no notes.
11. In the log: `grep "\[voice\]"`. Each answer has a `q_words= lines= chars=`
    line; a `[voice] guard … rule=` line means the model still wrote a banned
    opener and the guard removed it. Many in a day: the prompt needs another
    pass.
12. The same question in the test channel and a live sales channel: the same
    reply, tag aside.
13. Ten real replies from this list, read by Kushal, are the acceptance sample
    once the samples page is signed off.

To switch the guard off without a deploy: `ANSWER_GUARD_ENABLED=false`, restart.

`python verify_answer_voice.py`, `python -m replyguard` and `python -m wording`
check it offline.
