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
- AI news posts **every** weekday (it used to skip alternate days). It had a
  fixed time then, `NEWS_MAIN_TIME`; since NFT2-1069 it has a place in the
  day's order instead — see the last section of this file;
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

> **Corrected 12 Oct 2026.** The letter bands below are history. The write lock is by role now and both
> `RESTRICTED_COLUMN_RANGES` and `NEW_ROW_WRITABLE_RANGES` default to empty: see "Upgrading to "nobody is dropped"
> and the write lock by role" at the end of this file.

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

## Upgrading to NEWS-OCT8 (one news template; the news answer; the follow-up's bar)

What changes for the team: every news message is a bold heading with the day and at most 5 bold headlines, each with
a link to its outlet and a blank line between them; "More AI News" carries only major stories; asking for the news
gives the 5 best stories not sent before and says nothing about when anything is posted. Plan:
`docs/plans/NEWS-OCT8.md`. Wording for the sheet: `docs/rules-for-the-sheet-oct8.md`.

**`.env` on the laptop AND on the server (`/opt/sales-bot/.env`).** Two defaults changed. A `.env` that sets either
line keeps its own value, so set them where they are set:

```
NEWS_OVERFLOW_MIN_IMPORTANCE=5
NEWS_OVERFLOW_MAX_ITEMS=5
```

`NEWS_OVERFLOW_ENABLED` decides whether the follow-up is posted at all (`true` is the default). Then, on the laptop,
`python tools/redact_env.py`. No database migration: answers are recorded in the existing `news_stories` table with
`kind=answer`.

**Check in the test channel, then the live channel**

1. `@Saley any AI news?` → ONE message: `**AI News, <day>**`, a blank line, at most 5 bullets, each a bold headline
   followed by the outlet as a link, a blank line between bullets. Nothing about 2 PM, a schedule, "already posted" or
   "since". Every link opens the story.
2. Ask the same again → none of the first answer's stories. Keep asking until it runs out: only then does it repeat,
   and it gives its 5 best again.
3. `@Saley any news on <a company on the sheet>?` → the same list shape, filtered to that company; a PoC story keeps
   its fuller line with "(Company — on Master Pipeline)".
4. The next AI news post: one message, the heading, the line that tags people, a blank line, the stories; the top PoC
   stories first; none of the stories an answer gave earlier that day.
5. If a "More AI News, <day>" message follows: at most 5 stories, all major. On most days there is none.
6. A breaking post, when one comes: `**Breaking AI News, <day>**`, the same list shape, at most 5.
7. In the log: `[query] … → the news list (a plain news question, answered without the model)` and
   `[news] question: … giving N`.

## Upgrading to NFT2-1069 (the order of the day's posts)

What changes for the team: the day's posts go in a set order for each weekday, two hours apart from 2 PM (2, 4, 6,
8 PM). AI news no longer has a fixed 2 PM slot. Rule 7 is back on Mondays (never for a person rule 13 covers). Rule 11
is Wednesdays only. Every meeting-prep touch and every meeting follow-up posts at 10 AM. The last post of a day can be
at 8 PM (it was 6:30 PM). Plan: `docs/plans/NFT2-1069.md`, section "8 Oct decisions". Report:
`docs/test-reports/NFT2-1069.md`. Wording for the sheet: `docs/rules-for-the-sheet-oct8.md`.

**`bot_rules.yaml` ships with the change** (the `daily_order` block, R7 `enabled: true`, R11 `weekdays: [wed]`). If the
server's copy has local edits, a rule whose `weekdays` disagree with its days in `daily_order` stops every proactive
post, and the first lines of the boot log say which rule: `[rules] ... could not be loaded: R11 (...): its weekdays
say [...] but daily_order lists it on [...]`.

**`.env` on the laptop AND on the server (`/opt/sales-bot/.env`).** Three defaults changed and one name is retired. A
`.env` that sets a line keeps its own value, so set them where they are set:

```
SALES_DRIP_END=20:00
MESSAGE_GAP_MINUTES=120
MESSAGE_GAP_MIN_MINUTES=120
MESSAGE_JITTER_MINUTES=0
DAILY_MESSAGE_CAP=5
COS_FOLLOWUP_CHECK_INTERVAL_MINUTES=15
```

and delete the line `NEWS_MAIN_TIME=...` if it is there (retired; a value left behind does nothing).

- `SALES_DRIP_END` must be `20:00`. Left at `18:30`, the fourth post of Monday and Tuesday (closure support; AI news)
  is never sent, because it would land after the window.
- `MESSAGE_JITTER_MINUTES` must be `0` for the times the team was given. Any other value makes every gap that much
  longer at random, and the 8 PM post then falls after the window. The boot log warns when it is not 0.
- `COS_FOLLOWUP_CHECK_INTERVAL_MINUTES` is how often Saley checks whether a post is due. A post goes out at the first
  check at or after its time, so at 15 a 2 PM post leaves between 2:00 and 2:15. It should divide 120.
- `DAILY_MESSAGE_CAP` stays 5. The longest day has 4 posts in its order.

Then, on the laptop, `python tools/redact_env.py`. No database migration.

**Check in the log after the restart**

```
[rules]   order Mon: R4, R7, R1, R10
[rules]   order Tue: R5, R6, R2, R1
[rules]   order Wed: R11, R1, R3
[rules]   order Thu: R5, R1, R12
[rules]   order Fri: R2, R6, R1
Drip: up to 5 message(s) per weekday in channel ..., first at 14:00 IST, then every 120 min (plus up to 0), nothing after 20:00.
```

and no `MESSAGE_JITTER_MINUTES=...` warning. `python verify_day_order.py` and `python verify_s1.py --no-ddg` check
the schedule offline; `python verify_day_order.py --days` prints one planned day for each weekday.

**The first Monday and the first Wednesday in the live channel** are a checklist in
`docs/test-reports/NFT2-1069.md` (what to expect at 10:00, 14:00, 15:00, 16:00, 18:00 and 20:00).

## Upgrading to REPLIES-OCT8 (replies to "give me a moment", the "today" answer, grouped Next steps, the test clock)

What changes for the team:

- "take ur time", "no rush", "ok thanks" and the like, replied to "Give me a moment…", get a thumbs-up and nothing
  else. A reply to that line is answered only when it carries a real question.
- "What are we doing today?" (and "today's objectives", "what is the team working on today") now answers from
  today's meeting notes, what is due in the next 2 working days, today's channel messages and one line on the
  day's posts. No AI news in it, no times, no schedule talk. No model call.
- The Next steps post writes each ask once and lists the people under it. The opener line is gone.
- A pretend day ("make it Monday") ends when the real day changes.

Plan: `docs/plans/REPLIES-OCT8.md`. Report: `docs/test-reports/REPLIES-OCT8.md`.

**`.env` on the laptop AND on the server (`/opt/sales-bot/.env`).** One new variable; its default is what the team
asked for, so the line is optional:

```
TODAY_LOOKAHEAD_WORKING_DAYS=2
```

Then, on the laptop, `python tools/redact_env.py`. No database migration. Nothing to clear by hand: a pretend day
left over from an earlier day clears itself on the first read after the restart, and the log says so once
(`[clock] the pretend day (...) has ended because the real day changed`).

**The bot's role still needs Add Reactions** in the sales channels: the thumbs-up is the whole answer to "take ur
time".

`python verify_replies_oct8.py --show` checks all of it offline and prints the 11:21 exchange, a "today" answer
and a Next steps post.

## Upgrading to the R11 Outreach PoCs cross-check (9 Oct)

What changes for the team: the Wednesday "new companies" post is compared with Outreach PoCs first. A company with
no contacts there is listed; a yes finds up to 3 people and a second yes ADDS them as rows (Company, Industry, Name,
Designation, Based, paper link, LinkedIn URL, signed on the Name cell). A company already on the tab with a blank
name, title or LinkedIn link is listed as "already on Outreach PoCs, missing some fields"; a yes shows what was
found and asks a person to paste it in, because the bot may not write columns A to I of an existing row. A company
that is complete is not mentioned. Plan: `docs/plans/R11-POC-CROSSCHECK.md`. Report:
`docs/test-reports/R11-POC-CROSSCHECK.md`.

**`.env` on the laptop AND on the server (`/opt/sales-bot/.env`).** Three new variables; the defaults are what was
agreed, so the lines are optional:

```
POC_MANDATORY_FIELDS=name,designation,li_url
POC_SUGGEST_MAX_PER_COMPANY=3
POC_FILL_MAX_ROWS_PER_COMPANY=5
```

`EMAIL_WRITE_ALLOWED` is unchanged and stays `false` by default. With it false, a found email is shown but never
written, on a new row or an existing one. To let the bot write found emails, set `EMAIL_WRITE_ALLOWED=true` in BOTH
files; that is a team decision, not part of this change.

Then, on the laptop, `python tools/redact_env.py`. No database migration. `python verify_poc_crosscheck.py --show`
checks it offline and prints the messages.

## Upgrading to the R5 rebuild (11 Oct): one company at a time

What changes for the team: the Tuesday and Thursday "PoCs to contact" post is about ONE company, chosen by priority
(two or more people already contacted there, then voice AI, AI labs, UK-based, a keyword in a title, a paper link),
and it names the same five people every time until one is contacted. It says how many have been reached and how
many are left, offers to list the rest (reply "yes"), and names the next company (reply "move on" to switch; the
current one is paused, not dropped). On the month's last Tuesday or Thursday it adds two lines if nobody at that
company was contacted that month. It no longer prints emails, and never asks whether to skip anybody.

**`.env` on the laptop AND on the server (`/opt/sales-bot/.env`). THIS RELEASE NEEDS AN EDIT, NOT JUST A RESTART.**
The laptop's `.env` sets the two old values below, and a value in `.env` beats the new default, so until they are
changed the bot keeps the OLD role order. Check the server's file for the same lines. Set:

```
PROSPECT_COMPANIES_PER_WEEK=1
PROSPECT_ROLE_ORDER=founder,co-founder,cofounder,founding team,founding,chief executive,chief technology,chief scientist,chief science,chief product,chief operating,ceo,cto,coo,cpo,cso,chief,president,vice president,vp,head of,head -,director,strategic sourcing,sourcing,human data,data operations,business development,partnerships,principal,staff,tech lead,team lead,lead,professor,reader,senior lecturer,lecturer,phd,senior,sr,research engineer,research scientist,research lead,principal researcher,researcher,research,member of technical staff,member of the founding team,computational,scientist,machine learning engineer,ml engineer,ai engineer,agent engineer,speech ai engineer,software engineer,engineer,product manager,product,program manager,engineering manager,manager
```

(or delete both lines to take the defaults), and delete this line, which now does nothing:

```
PROSPECT_REPEAT_ASK_AT=3
```

Five new variables; the defaults are what was specified, so the lines are optional:

```
PROSPECT_VOICE_INDUSTRIES=AI Voice Agents,AI Voice/Productivity Tools
PROSPECT_LAB_INDUSTRIES=AI Labs
PROSPECT_KEYWORDS=alignment,safety,trust,evaluations,robotics,red-teaming,STT,TTS,speech,duplex
PROSPECT_UK_MARKERS=UK,United Kingdom,London,Cambridge,Oxford,Edinburgh,Manchester,Bristol,Glasgow
PROSPECT_DESIGNATION_MAX_CHARS=60
```

Then, on the laptop, `python tools/redact_env.py`.

**Database.** One new table, `prospect_open`, created on start. `prospect_mentions` and `prospect_week` are left in
place and no longer read or written. Nothing to run by hand.

**What to look for in the log** after the restart:

```
[prospects] startup: 91 company(ies) with people left to contact; 82 of them (116 people) match none of the six priority rules and are queued last
[rules] R5: Sierra AI is open (score 52: warm+voice+uk; 5 left, 3 reached); next is ElevenLabs; 0 paused
```

(the numbers are illustrative). If the boot log warns `PROSPECT_COMPANIES_PER_WEEK=2 ... treated as 1`, the old
line is still in `.env`.

**The Bot Rules tab.** Rule 5's "What the Bot Shares / Checks" wording changed (`sheet_wording` in
`bot_rules.yaml`); paste it into the sheet by hand.

`python verify_prospects_priority.py --show` checks all of it offline and prints the messages.

## Upgrading to the R2 rebuild (12 Oct): new companies in the news

What changes for the team: the Tuesday and Friday "companies in the news" post no longer names companies we
already track. Every company on the Master Pipeline and on Outreach PoCs is checked, in code, however many there
are. Each new company is one line: its name linking to the article, what happened, **Good fit** or **Weak fit**, and
one reason. A company that is on Outreach PoCs but not on the Master Pipeline gets its own line asking for a
pipeline row. Reply with the company's name ("add Mirage Labs"); the bot says back what it understood and asks once
more before an approver's yes adds a row carrying the company name only. A company nobody answered about comes back
next time, for two weeks. When nothing is new, nothing is posted.

**`.env` on the laptop AND on the server (`/opt/sales-bot/.env`).** One default changed. The laptop's `.env` sets
the old value, and a value in `.env` beats the new default, so change the line (or delete it) in BOTH files:

```
NEWS_POC_TARGETS_PER_DAY=40
```

Two new variables; the defaults are what was specified, so the lines are optional:

```
NEWS_SCREEN_STALE_DAYS=14
NEWS_SCREEN_FIRST_RUN_DAYS=7
```

Then, on the laptop, `python tools/redact_env.py`. Nothing is retired.

**Database.** Two new tables, `rule_last_posted` and `news_screen_ledger`, and one index, created on start.
Nothing to run by hand. The first live R2 post has no marker and looks back seven days.

**The Master Pipeline must be appendable** for the add to work: `SHEET_APPENDABLE_TABS` has to include
`researcher_lines` (it does by default and on the laptop).

**What to look for in the log** on a Tuesday or Friday:

```
[news] R2: 382 known companies (281 on the Master Pipeline, 212 on Outreach PoCs, 101 on Outreach PoCs only), read fresh
[news] R2 window 2026-10-13T18:02 to 2026-10-16T14:00 (from the last post): 23 scored story/stories first seen in it (at most 40 are read)
[news] R2: the stories name 9 company/companies; 6 already on the Master Pipeline and dropped in code: ...; 3 left
[news] R2: posted about 3 company/companies (0 held back); the last-posted marker is now ...
```

(the numbers are illustrative). `[news] R2 CANCELLED ...` at ERROR means the sheets could not be read and nothing
was posted. On a test day the log says the marker and the ledger were NOT touched.

**The Bot Rules tab.** Rule 2's "What the Bot Shares / Checks" wording is new (`sheet_wording` in
`bot_rules.yaml`); paste it into the sheet by hand.

`python verify_news_screen.py --show` checks all of it offline and prints the messages.

## Upgrading to the R13 rebuild (12 Oct): next steps as a reminder

> **Corrected the same day.** The three-week drop described below was removed: nobody is dropped,
> `NEXT_STEP_DROP_AFTER_DAYS` is retired and ignored, and `NEXT_STEP_DM_SENT_MARKERS` now defaults to
> `sent,received`. See "Upgrading to "nobody is dropped" and the write lock by role" at the end of this file.

What changes for the team: the 3 PM "Next steps" post stops asking questions about the sheet. It says what needs
doing, grouped ("We need to call these PoCs:", "We need to send out the first email to these people:" and so on),
with one line a person: name and company. It never names a cell and never asks for a date. People who are Connected
with no LI Connected Date now appear. The people who have waited longest come first. If nothing moves for three weeks
after Saley first names somebody for a step, it says once that it will stop reminding about them; they come back when
their row changes. **Saley no longer answers "done" under the post**: update the sheet instead.

**`.env` on the laptop AND on the server (`/opt/sales-bot/.env`).** Two new variables; the defaults are what was
specified, so the lines are optional:

```
NEXT_STEP_DROP_AFTER_DAYS=21
NEXT_STEP_DM_SENT_MARKERS=sent,yes,done
```

Check `NEXT_STEP_DM_SENT_MARKERS` against what the team actually types in **LI DM Sent**: a value that is not on
the list (and is not a ticked checkbox) is not read as sent, and that person never reaches the call stage. Then, on
the laptop, `python tools/redact_env.py`. Nothing changed its default and nothing is retired.

**Database.** One new column, `next_step_followups.first_named`, added on start. Rows written before this release
have it blank, so everybody's three-week clock starts at their next naming. A person the old rule had closed after
the Unresponsive reminder stays closed until their row changes. `next_step_posts` is left in place and no longer
written.

**What to look for in the boot log:**

```
[config] R13 next steps (a reminder; replies are not read): post at 15:00 IST; first reminder +2d, after the previous step +2d, next email +7d; calls from DM +7d every 3d until +21d; an ask is DROPPED after 21d of being named; connected=connected dm_sent=sent,yes,done replied=replied,responded pause_for_booked_meeting=True
```

A warning follows if `NEXT_STEP_DROP_AFTER_DAYS` is under 7 or `NEXT_STEP_DM_SENT_MARKERS` is empty. At 3 PM:

```
[rules] R13: 33 connected, 21 due -> 5 picked, 16 queued; 0 dropped today (of 0 at three weeks); ...
```

(the numbers are illustrative).

**The Bot Rules tab.** Rule 13's "What the Bot Shares / Checks" wording is new end to end (`sheet_wording` in
`bot_rules.yaml`); paste it into the sheet by hand.

`python verify_next_steps.py --show` checks all of it offline and prints the messages.

## Upgrading to the R6 rebuild (12 Oct): connected, no DM yet, and the email check

What changes for the team: the Tuesday and Friday post is now headed "Connected, no DM yet" and is a numbered list
of people whose **LI DM Sent** cell is blank, three days or more after they connected, or with no connected date
at all, longest-connected first. Up to five names, plus up to five more with email news in three short blocks:
addresses Saley found for people with none on the sheet, domains that cannot receive mail (the domain only), and
people it could find nothing for. It never shows an address that is already on the sheet. When it found something
it ends by asking whether to add those emails; an approver's yes writes each into that person's blank Email cell.
Nobody is ever dropped: a person is named on every post until the DM is logged. When nobody is waiting it says
"All clear, nobody is waiting on a DM."

**Install the new dependency**, on the laptop and on the server:

```
pip install -r requirements.txt        # adds dnspython
```

Without it the bot still runs; the "These domains can't receive mail" check is skipped and the log says so.

**`.env` on the laptop AND on the server (`/opt/sales-bot/.env`).** To let the closing question appear and a found
address be added, set this in BOTH files (the laptop already has it; the code's default stays false):

```
EMAIL_WRITE_ALLOWED=true
```

Four new variables; the defaults are what was specified, so the lines are optional:

```
LI_NO_DM_EMAIL_NAMES_PER_POST=5
LI_NO_DM_DM_SENT_MARKERS=sent
LI_NO_DM_DM_REPLIED_MARKERS=replied,received
LI_NO_DM_VERIFY_MX=true
```

Then, on the laptop, `python tools/redact_env.py`. Nothing changed its default and nothing is retired. No database
change: this rule keeps no state.

**What to look for in the boot log:**

```
[config] R6 connected, no DM yet: in after 3d connected, or straight away with no connected date; out when LI DM Sent says sent=sent or replied=replied,received; the plain list has R6's max_items_per_post, the three email blocks share 5 name(s); at most 5 email search(es) a post; MX check=True; EMAIL_WRITE_ALLOWED=True
```

If `EMAIL_WRITE_ALLOWED` is false a warning follows: the closing question will never appear and no found address can
be added. On a Tuesday or Friday:

```
[rules] R6: 670 row(s) read (the whole tab); 664 qualify (31 with a connected date 3+ days old, 633 with no connected date); out: 5 DM sent, 1 replied, ...; the first 30 in order are handed on
[email] R6: 30 handed on -> 5 in the plain list (cap 5), 3 with email news (cap 5: 1 found, 1 dead domain(s), 1 not found); ...
```

(the numbers are illustrative).

**The Bot Rules tab.** Rule 6's "What the Bot Shares / Checks" wording is new (`sheet_wording` in
`bot_rules.yaml`); paste it into the sheet by hand.

`python verify_li_no_dm.py --show` checks all of it offline and prints the messages.

## Upgrading to "nobody is dropped" and the write lock by role (12 Oct, later the same day)

Two changes in one release. **This section corrects two older ones**: the three-week drop in "Upgrading to the R13
rebuild" above is gone, and the letter bands in "Upgrading to RULE 13" (`RESTRICTED_COLUMN_RANGES=A:I,Q:W,Z:AE`,
`NEW_ROW_WRITABLE_RANGES=A:P,X:Y`) are no longer how the lock works.

**1. Rule 13: nobody is dropped.** The 3 PM "Next steps" post no longer says "I'll stop reminding about these".
People keep coming back, in priority order, until their row moves. Inside each group the people named least
recently go first, so a long group is walked five at a time and then starts again. The "look unresponsive" line now
repeats, in its turn, until somebody sets Prospect Status to Unresponsive. In **LI DM Sent**, "Received" now means
they replied (the meeting line), and "Sent" means the DM went out (the call line).

**2. The write lock follows the column, not the letter.** Vaishnavi moved Next Steps and the two LI DM columns on
12 Oct. The old lock was a list of letters, so after the move it left Next Steps and 1st Email Sent writable. The
lock is now a list of ROLES (`RESTRICTED_COLUMN_ROLES`): the identity block (Sr No through LI Url, Email included),
the step block (Next Steps, the three email Sent/Date pairs, LI DM Sent, LI DM Date) and the commercial block
(Notes/Remarks through Deal Status, and Priority). Two things are locked that were not before: **LI DM Sent and LI
DM Date** on an existing row (a reply could fill them until now; Saley now asks a person to), and **Priority**. What
Saley may still fill on an existing row, after a yes: First Contact, First Contact Type, First Contact Date, Sid -
LI Addition, LI Connected Date, Meeting Date, Meeting Status. The two exceptions are unchanged: a blank Email cell
(`EMAIL_WRITE_ALLOWED`), and a new signed row.

**If a restricted column's header is renamed to something Saley cannot map, it refuses every write to the tab** and
logs an ERROR naming the role, until the header maps again (the next read of the tab). Reading is unaffected.

**`.env` on the laptop AND on the server (`/opt/sales-bot/.env`).**

```
# DELETE these lines if present (retired or now wrong):
#   NEXT_STEP_DROP_AFTER_DAYS=...            retired, no longer read
#   RESTRICTED_COLUMN_RANGES=A:I,Q:W,Z:AE     the old letters; harmless (a range only adds), but stale
#   NEW_ROW_WRITABLE_RANGES=...               the old letters; it cannot open a locked column, but it warns at boot

# OPTIONAL, the defaults are what was specified. If either marker line is already set, change it to this:
NEXT_STEP_DM_SENT_MARKERS=sent,received
NEXT_STEP_DM_REPLIED_MARKERS=replied,responded,received

# OPTIONAL, the defaults are the three blocks. Leave both lines out unless you mean to change the lock:
# RESTRICTED_COLUMN_ROLES=...      (the full default is in .env.example)
# NEW_ROW_WRITABLE_ROLES=...       (the full default is in .env.example)
```

The laptop's `.env` has `RESTRICTED_COLUMN_RANGES=A:I,Q:W,Z:AE` and `NEW_ROW_WRITABLE_RANGES=A:R` today; delete
both lines there, then `python tools/redact_env.py`. Set `GTM_LOG_FULL_SCHEMA=true` for the first start so the
whole schema is printed.

**Database.** Nothing to migrate. `next_step_followups.first_named`, `closed` and `closed_date` stay in the table
and are no longer read or written; a person the old rule had closed is named again in their turn.

**What to look for in the boot log.** No `NEXT_STEP_DROP_AFTER_DAYS` warning, and:

```
[config] R13 next steps (a reminder; replies are not read): post at 15:00 IST; ... nobody is dropped, each tier rotates; connected=connected dm_sent=sent,received replied=replied,responded,received pause_for_booked_meeting=True
```

Then, when the Outreach PoCs tab is first read, one line per block giving **today's letter for every restricted
role**, and the writable window as letters and as roles. Read the letters against the sheet:

```
[gtm.lock] 'Outreach PoCs': restricted, identity block (NEVER written on an existing row): sr_no=<letter> 'Sr No', company=<letter> 'Company/Uni', ...
[gtm.lock] 'Outreach PoCs': restricted, step block (NEVER written on an existing row): outreach_step=<letter> 'Next Steps', email_1_sent=<letter> '1st Email Sent', ..., li_dm_sent=<letter> 'LI DM Sent', li_dm_date=<letter> 'LI DM Date'
[gtm.lock] 'Outreach PoCs': restricted, commercial block (NEVER written on an existing row): next_steps=<letter> 'Notes/Remarks', ..., poc_priority=<letter> 'Priority'
[gtm.window] 'Outreach PoCs': restricted today at <letters>. WRITABLE on an existing row: <letters> (roles: first_contact, first_contact_type, first_contact_date, sid_li_added, li_connected_date, meeting_date, meeting_status) — 7 named column(s): ...
[gtm.lock] 'Outreach PoCs': a NEW row may fill <letters> (roles: sr_no, company, ..., li_connected_date)
```

Per the 12 Oct message, `outreach_step` should read O and `li_dm_sent` / `li_dm_date` V and W. The other letters
were not stated, so read them off this log rather than assuming them. **Stop and fix the sheet header if you see**
`[gtm.lock] ... THE WRITE LOCK CANNOT BE RESOLVED ... EVERY WRITE TO THIS TAB IS REFUSED`: it names the role whose
column could not be found.

At 3 PM:

```
[rules] R13: 33 connected, 21 due -> 5 picked, 16 queued (they come round in turn; nobody is dropped); ...
```

(the numbers are illustrative).

**The Bot Rules tab.** Rule 13's "What the Bot Shares / Checks" wording changed again (`sheet_wording` in
`bot_rules.yaml`: people keep coming back in priority order, nobody is dropped, the list moves when the sheet
moves); paste it into the sheet by hand.

`python verify_next_steps.py --show` and `python verify_sheet_locks.py --show` check both parts offline; the second
prints the lock log for three header orders.

## Upgrading to the R7 rebuild (13 Oct): no meeting booked yet

What changes for the team: the Monday 4 PM post, which has been silent, comes back headed **"No meeting booked
yet"**. It is a numbered list of up to three people, name and company only, with one line under it: "We sent them
a DM more than 3 weeks ago and don't have a meeting set up with them." Anyone whose meeting is booked more than a
week after the DM gets a second short block showing the meeting date. Nobody is dropped: people come round in
turn, Monday after Monday, until the Meeting Status says Completed or the Deal Status says Won, Lost or On Hold.
Rule 13 comes first, so anybody its 3 PM post names that Monday is not listed. **Saley does not read replies to
this post**: update the sheet instead.

**`.env` on the laptop AND on the server (`/opt/sales-bot/.env`).** Three defaults changed. A default only applies
when the line is absent, so **if a line is already there, change it**:

```
DM_NO_MEETING_DAYS=21
DM_NO_MEETING_MAX_CONTACTS=3
DAILY_MESSAGE_CAP=7
```

The laptop's `.env` has `DM_NO_MEETING_DAYS=7`, `DM_NO_MEETING_MAX_CONTACTS=5` and `DAILY_MESSAGE_CAP=15` today.
Left at 7, rule 7 would pick people on the day rule 13 starts asking for a call and stay silent; the boot log warns
about it. `DAILY_MESSAGE_CAP=15` already allows the Monday post; whether to bring it down to 7 is your call.

Four new variables; the defaults are what was specified, so the lines are optional:

```
DM_NO_MEETING_BOOKED_GAP_DAYS=7
DM_NO_MEETING_DM_SENT_MARKERS=sent
DM_NO_MEETING_DM_REPLIED_MARKERS=replied
DM_NO_MEETING_STATUS_OPEN=,tbd
```

Keep the leading comma in `DM_NO_MEETING_STATUS_OPEN`: the empty entry is the blank cell. Check
`DM_NO_MEETING_DM_SENT_MARKERS` against what the team actually picks in **LI DM Sent**: a value that is not on the
list is not read as sent. Nothing is retired. Then, on the laptop, `python tools/redact_env.py`.

**Database.** One new table, `dm_no_meeting_mentions` (the contact, the last day rule 7 named them, a count), created
on start. It is the rotation: written only after a real send, never by a preview, and never by a test day on a
database that is not a `*_test.db`. It starts empty, so the first Monday names the three people DM'd longest ago.

**What to look for in the boot log:**

```
[config] R7 no meeting booked yet (Mondays, the escalation behind R13; replies are not read): in 21d after the LI DM Date with no Meeting Date, or with a Meeting Date more than 7d after the DM; LI DM Sent sent=sent replied=replied (checked first); Meeting Status open=(blank),tbd; out on Deal Status on hold=on hold,onhold,on-hold,paused,parked,deferred (R7 only); 3 name(s) a post, least recently named first, nobody is dropped
```

A warning follows if `DM_NO_MEETING_DAYS` is at or below `NEXT_STEP_CALL_AFTER_DM_DAYS`, or if
`DM_NO_MEETING_DM_SENT_MARKERS` is empty. The `DAILY_MESSAGE_CAP` warning now starts above 7 (it was above 6). On
Monday, when the queue is computed:

```
[rules] R7: 670 row(s) read (the whole tab); 5 qualify (5 with no Meeting Date, 0 with a meeting booked late); 2 named by an earlier rule today and skipped; 3 picked, 0 waiting their turn (nobody is dropped); out: ...
```

(the numbers are illustrative). After the post: `[rules] R7: recorded 3 person(s) named on <date>`.

**The Bot Rules tab, by hand.** Rule 7's "What the Bot Shares / Checks" wording is new end to end
(`sheet_wording` in `bot_rules.yaml`), and the Global Rules "Daily cap" row now says "Max 7 posts a day"
(`daily_cap`). Both are pasted into the sheet by hand; `docs/rules-for-the-sheet-oct8.md` has the rule 7 text.

`python verify_dm_no_meeting.py --show` checks all of it offline and prints the messages.

## Upgrading to the R10 rebuild (13 Oct): closure support

What changes for the team: **at first, almost nothing you can see.** Closure Prob% and Prospect Status are blank
on nearly every row, so the Monday post goes on being the one line saying so, now worded "No closure support this
week — Prospect Status and Closure Prob% have empty values in the GTM sheet." **It will keep posting that line every
Monday until somebody fills in Closure Prob%.** Once rows have a closure stage (deal / demo / quote) and a
percentage above 50, the post becomes a list of every such deal, highest percentage first, with no cap: the person,
the company in brackets, the percentage and the stage. Under a deal there is one line of news about that company
when Saley already has a story since its last closure support post. It ends by offering help, as before.

**`.env` on the laptop AND on the server (`/opt/sales-bot/.env`).** One new variable; the default is what was
specified, so the line is optional:

```
CLOSURE_NEWS_FIRST_RUN_DAYS=7
```

Nothing changed its default and nothing is retired. `CLOSURE_SUPPORT_MIN`, `CLOSURE_SUPPORT_STAGES` and
`CLOSURE_STOP_MARKERS` are read as before. Then, on the laptop, `python tools/redact_env.py`.

**Database.** No new table. The news window's marker is one new ROW, `R10`, in the existing `rule_last_posted`
table (R2's row there is untouched). The row appears the first time a deals post really goes out; until then the
first run looks back 7 days. A test day, a preview and the empty-columns line never write it.

**No extra searches.** Until now each deal asked for a news search before the post. That is gone: the news line is
read from what rule 1 has already stored.

**What to look for in the boot log:**

```
[config] R10 closure support (Mondays): a closure stage (deal,demo,quote,demo done,quote sent,proposal,negotiation, whole words) AND Closure Prob% strictly above 50%; a stage with no percentage is left out silently; EVERY qualifying deal is named, highest percentage first, no cap and no rotation; one news line a deal, READ from the stored news first seen since the last closure support post (rule_last_posted 'R10'; the first run looks back 7d), nothing fetched; the empty-columns notice posts every Monday while either column is blank on every active row
[sheet.world] 0 row(s) have Closure Prob% = 0 and are stopped for every rule
```

If the second line counts rows, check them: a 0 typed to mean "not assessed yet" silences that person for every
rule. On a Monday with deals: `[R10] 2 deal(s) in a closure stage above 50%, highest first, all handed on (no cap):
...`, then `[news] R10: ... N of M deal(s) get a news line; nothing was fetched`, and after the post `[news] R10:
posted M deal(s); the last-posted marker is now ...` (the numbers are illustrative).

**The Bot Rules tab, by hand.** Rule 10's "What the Bot Shares / Checks" wording is new (`sheet_wording` in
`bot_rules.yaml`); paste it into the sheet.

`python verify_closure_support.py --show` checks all of it offline and prints the messages.

## Upgrading to the R3 rebuild (14 Oct): AI events and summits

What changes for the team: the Wednesday post becomes **three lines in total**, in up to three labelled blocks:
the events we are registered for that start in the next 14 days; the ones we have not registered for whose
registration is closing; and events that are not on the sheet but look worth adding, from Bangalore, Karnataka or
London. The lines are numbered straight through, so a reply can say "add 2 and 3". It still ends by offering to
remind you again on Monday.

**The first post will be suggestions only.** A row we are not registered for is now listed only when its "Last day
for registration" cell holds a real date that has not passed. Rows where that cell is blank or says "not available"
are no longer listed (the old "no registration deadline on the sheet" line is gone), and that is most of the tab
today. **Filling in the registration deadlines is what brings the "Registration closes soon" block to life.** The
"date unclear" line is gone too: a date Saley cannot read is skipped and logged.

**`.env` on the laptop AND on the server (`/opt/sales-bot/.env`).** One default changed. A default only applies
when the line is absent, so **if the line is already there, change it**:

```
EVENTS_DISCOVERY_MAX_PER_MONTH=12
```

One new variable; the default is what was specified, so the line is optional:

```
EVENTS_SUGGEST_LOCATIONS=Bangalore,Bengaluru,Karnataka,London
```

The other changed default is not in `.env`: rule 3's `max_items_per_post` in `bot_rules.yaml` went from 5 to 3, and
ships with the code. Nothing is retired. Then, on the laptop, `python tools/redact_env.py`.

**Database.** Nothing to migrate. The table that remembered a "date unclear" line is no longer written by rule 3.

**What to look for in the boot log:**

```
[config] R3 events (Wednesdays): THREE lines in the whole post, the sheet first: registered and starting within 14d; not registered with a registration deadline still ahead (the deadline or the start within 14d), closing soonest first; a row with no usable deadline is skipped; then suggestions fill the room left, only from Bangalore/Bengaluru/Karnataka/London, at most 3 a run and 12 a month; nothing is added without a yes; the offer reminds again on weekday 0
```

A warning follows if `EVENTS_SUGGEST_LOCATIONS` is empty, or if `EVENTS_DISCOVERY_MAX_PER_MONTH` is below four
times `EVENTS_DISCOVERY_MAX_PER_RUN`. On a Wednesday, when the queue is computed, one line a skipped row and a
summary:

```
[R3] skipped 'Some Event' (row 4): not registered and no usable registration deadline ('not available')
[R3] 7 row(s) read: 0 registered and starting within 14d, 0 with registration closing; 7 skipped, 0 left out by the cap of 3; room for 3 suggestion(s)
[events] discovery: 5 candidate(s), 3 suggested (room for 3), 2 skipped
```

(the numbers are illustrative). An event outside the three places shows as `[events] not suggesting: <name> —
<place> is not one of EVENTS_SUGGEST_LOCATIONS`. When the month's twelve are used up: `[events] 12 event(s)
already suggested in 2026-10, which is the monthly limit of 12`.

**The Bot Rules tab, by hand.** Rule 3's "What the Bot Shares / Checks" wording is new end to end
(`sheet_wording` in `bot_rules.yaml`); paste it into the sheet.

`python verify_events.py --show` checks all of it offline and prints the messages.

## Upgrading to the R12 redesign (12 Oct): sales packages

What changes for the team: the Thursday post is now the same short list every week: "these Sales Packages aren't
ready yet:", the packages by name only (no status, no per cent), five at most, highest % Completion first, and
"Could you let me know roughly when each might be ready?". **Answer under the post with a date** ("Vibe-check by
24 Oct") and Saley reacts, writes it down, and comes back on that day, but only if Ready? on the tab is still not
Yes. A package leaves the list when Ready? is set to Yes, with no announcement.

**`.env` on the laptop AND on the server (`/opt/sales-bot/.env`).** Two new variables; the defaults are what was
specified, so the lines are optional:

```
PACKAGES_ORDER=completion_desc
PACKAGES_DATE_CAPTURE=true
```

Nothing changed its default and nothing is retired. `REMINDER_DEFAULT_TIME` (14:00) is the time a package's
date-reminder fires. Then, on the laptop, `python tools/redact_env.py`.

**Database.** Nothing to migrate and no new table. A package's date is a row in the existing `scheduled_reminders`
table whose `row_key` starts `package|`.

**What to look for in the boot log:**

```
[config] R12 sales packages (Thursdays): every package whose Ready? is not Yes (blank is not ready), by name only, order=completion_desc with a blank per cent last; the rule's max_items_per_post a post and "(+N more next Thursday.)" for the rest; nothing drops on a clock; a date given in a reply is recorded (a reminder on that day at 14:00, skipped when Ready? is Yes or the day is a Thursday)
[gtm.packages] 0 package(s) say Ready? Yes with % Completion under 100 (Ready? wins, they are not listed)
```

If the second line names packages, the tab disagrees with itself; nothing is posted about it. An `[gtm.packages]`
or `[R12]` ERROR saying there is "no Name or Ready? column I can find" means a header was renamed to something
Saley does not recognise, and the Thursday post is silent until it is put back. On a Thursday: `[R12] 8 row(s)
read; 5 package(s) not ready, order=completion_desc: 5 in the post, 0 held back ...`. When somebody gives a date:
`[packages] <name> said '<package>' will be ready on <date> — reminder #N at 14:00`.

**The Bot Rules tab.** Rule 12 has no `sheet_wording` in `bot_rules.yaml` and none was added; if the tab's rule 12
row describes asking for "the status", it is out of date and is edited by hand.

`python verify_packages.py --show` checks all of it offline and prints the messages.

## Upgrading to the R8 rebuild (12 Oct): meeting preparation

What changes for the team: meeting prep becomes **two posts**. **"Meeting today"** at 10 AM lists the meetings
happening that day, on its own. **"Meeting preparation"** after 2 PM lists the meetings 3 days away and then those
5 days away. Every meeting is named (there is no limit of three any more), with the date and, when there is one, a
recent headline about the company. Both posts go out on Saturdays and Sundays too. Answer under either with
"remind me about 2 the day before" and Saley books it.

**Three things the team should know before the first post:**

- **Only the Meeting Date is read.** A meeting marked Completed or cancelled that still has a date in the future is
  listed. Clear the date, or let it pass, to stop it.
- **A row needs no first contact to qualify.** A Meeting Date on a row nobody has contacted is listed.
- **A date Saley cannot read is asked for once a week**, on its own line in the afternoon post.

**`.env` on the laptop AND on the server (`/opt/sales-bot/.env`).** One new variable; the default is what was
specified, so the line is optional:

```
MEETING_PREP_TIME=14:00
```

Nothing changed its default and nothing is retired. `MEETING_DAYOF_TIME` (10:00) and `MEETING_PREP_DAYS_BEFORE`
(5,3) keep their meaning. Then, on the laptop, `python tools/redact_env.py`.

**One line to check on the server.** If `NEWS_RSS_FEEDS` in `/opt/sales-bot/.env` has an entry that starts
`//news.google.com/...`, put `https:` in front of it. The code now reads such an entry as `https://...` and logs
`[feeds] NEWS_RSS_FEEDS entry '//...' has no scheme` once, so the polls stop raising InvalidSchema either way; the
line in the file is still wrong and is yours to fix.

**Database.** Two new tables, created on boot: `meeting_touches` (which touch went out for which meeting) and
`meeting_dates_seen` (the dates each contact has had). One new column, `drip_sends.people`, added by the usual
migration. Nothing to run by hand. **Both tables start empty**, so on the first day every touch due that day goes
out once, as it would have before.

**What to look for in the boot log:**

```
[config] R8 meeting preparation (every day, weekends included): reads the Meeting Date on every Outreach PoCs row and nothing else; touches 5,3 days before and on the day; "Meeting today" at 10:00 on its own, "Meeting preparation" after 14:00; no cap, outside the 2-hour gap and DAILY_MESSAGE_CAP; the news line is read from the stored feed (last 14 days), nothing is fetched; a touch goes out once (meeting_touches)
```

When the queue is computed, one summary line, and one line a skipped row:

```
[R8] 33 row(s) read: 1 for today's post, 2 for the afternoon one; 3 not on a touch today, 0 touch(es) already out, 9 past, 1 beyond 7 days, 0 unreadable date(s) asked inside the last week, 0 blank name(s), 0 duplicate(s)
[R8] row 14 has a Meeting Date ('15-10-2026') and no Name; skipped
[news] R8: 212 stored story/stories first seen in the last 14 days; 1 of 2 meeting line(s) have a story; nothing was fetched
[rules] R8: recorded 2 touch(es) sent on 2026-10-12 (prep)
```

(the numbers are illustrative). On a Saturday or Sunday the drip line reads `no proactive messages but N meeting
post(s)`. A reminder: `[R8] <name> asked to be reminded about <person> (meeting <date>) on <date> at <time>:
reminder #N`, and when a meeting moves, `[R8] reminder #N for <person>: the meeting moved from <date> to <date>`.
An `[R8]` ERROR saying there is "no Meeting Date column I can find by its header" means the header was renamed to
something Saley does not recognise; nothing is posted until it is put back.

**The Bot Rules tab and the Weekly Schedule tab, by hand.** Rule 8's "What the Bot Shares / Checks" wording is new
(`sheet_wording` in `bot_rules.yaml`), and the schedule has two meeting rows now where it had one; both are in
`docs/rules-for-the-sheet-oct8.md`, ready to paste.

`python verify_meeting_prep.py --show` checks all of it offline and prints the messages.

## Upgrading to the R4 rebuild (12 Oct): deliverables to chase

What changes for the team: Monday's post is now headed **"Deliverables to chase"** and lists **every open P1** on
the Deliverables Checklist, not only what is due this week. Each item is three lines: the title, the team and the
due date. There is no "N days overdue", no link and no closing question any more.

**Three things the team should know before the first post:**

- **The list no longer ages out.** An item leaves only when its Status is set to **Completed**. Nothing drops off
  with time, so the list will grow; past 20 items the post says how many more are on the tab.
- **Only the word "Completed" finishes an item.** "Done", "Closed" or "Shipped" in the Status column no longer take
  a row off the list. Only "P1" in the Priority column puts one on it: "High" or "1" do not.
- **Sunday goes silent.** The Sunday post for a P1 due on the Monday is gone. (The two meeting posts still go at
  the weekend.)

**`.env` on the laptop AND on the server (`/opt/sales-bot/.env`).** Three defaults changed. A default only applies
when the line is absent, and **the laptop's `.env` has all three lines with the old values**, so change them (and
check the server's):

```
DELIVERABLE_P1_MARKERS=p1
DELIVERABLE_DONE_MARKERS=completed
SUNDAY_RULE_IDS=
```

Left as they are, "High" still counts as P1, "Done" still drops a row, and Sunday still has its post. One variable
is retired and can be deleted (it does nothing now):

```
DELIVERABLE_NEAR_DAYS
```

Nothing is new. Then, on the laptop, `python tools/redact_env.py`.

**Database.** Nothing to migrate.

**What to look for in the boot log:**

```
[config] R4 deliverables (Mondays only): every row whose Priority is exactly p1 and whose Status is not exactly completed, whatever its deadline; earliest deadline first, no deadline last; the title, the team and the due date, with no overdue count, no link and no closing line; a row leaves only when it is Completed
[config] daily cap: 7 counted post(s) on every weekday; ... Saturday is silent; Sunday is silent too (SUNDAY_RULE_IDS is empty). Meeting posts (R8) go on their own days, weekends included.
```

If the first line names more than `p1` or `completed`, or the second names a Sunday rule, the `.env` still has the
old lines. On a Monday, when the queue is computed:

```
[R4] skipped 'Case study: voice agents' (row 5): priority 'P2' is not P1 (DELIVERABLE_P1_MARKERS), deadline '15-Oct'
[R4] 'NDA' (row 9): status 'Done' is not recognised as Completed (DELIVERABLE_DONE_MARKERS), so it stays on the list
[R4] 17 row(s) read: 11 open P1 listed (2 with no readable deadline); 4 not P1, 2 Completed, 0 with no Action Item, 1 with a status that looks finished and is not Completed
```

(the numbers are illustrative). The second kind of line is the one to act on: a status somebody typed that is not
"Completed". If the bot comes back on a Monday after `SALES_DRIP_END`, it logs `[drip] ... past 20:00 and are NOT
sent late` and the checklist waits for the next Monday.

**The Bot Rules tab and the Weekly Schedule tab, by hand.** Rule 4's "What the Bot Shares / Checks" wording is new
end to end (`sheet_wording` in `bot_rules.yaml`), the Global Rules "Weekends" row now says both days are silent, and
the schedule's Sunday row no longer has a deliverables post.

`python verify_deliverables.py --show` checks all of it offline and prints the messages.

## Upgrading to R14 (13 Oct): the reconnect follow-up (NFT2-1086)

What changes for the team: a new post, **Time to reconnect**, in the **API Sales channel**. On the day a
person's reconnect date falls it names up to five people (name in bold, company in brackets) and, when more are
due, ends "there are N more — would you like to look at the rest?". A yes under that post lists everybody due
that day. The day is the Reconnect Date; when that is empty, 45 days after the Meeting Date; with no meeting, 45
days after the latest contact date. A day on a Saturday or Sunday is posted on the Monday. Each person is named
once. Nothing else the bot posts changes.

**Before the restart: invite the bot to the API Sales channel** and copy that channel's id.

**`.env` on the laptop AND on the server (`/opt/sales-bot/.env`).** Two new variables. The first has no default
and nothing works without it; the second is optional (15:30 is the default):

```
SALES_API_CHANNEL_ID=<the API Sales channel id>
RECONNECT_TIME=15:30
```

`SALES_API_CHANNEL_ID` is added to `SALES_CHANNEL_IDS` automatically if it is missing from that line, with a
warning; adding it to that line by hand removes the warning. The bot will then answer in that channel when
@-mentioned or replied to, like in every sales channel. If your `.env` spells out `RESTRICTED_COLUMN_ROLES`, leave
it as it is: Reconnect Date is locked in code (`ALWAYS_READ_ONLY_ROLES`), not through that line. No default changed
and nothing is retired. Then, on the laptop, `python tools/redact_env.py`.

**Database.** One new table, `reconnect_fired` (who has been posted about, and for which date), and one new column,
`drip_sends.rest_listed`. Both are created on the first start; nothing to run. **The table starts empty, and that
is safe:** a reconnect day that has already gone is skipped by the rule itself, so on the first day nobody whose
date has passed is posted, and there is no backlog to work through. The first post is for the first day, from
today on, that somebody's date falls on.

**What to look for in the boot log:**

```
[config] R14 reconnect follow-up: one post at 15:30 IST in channel 1234567890, on the day a person's date falls (a weekend moves to the Monday), outside the cap and the gap; five names, and a yes under the post lists the rest
[reconnect] R14 posts in #api-sales (1234567890) at 15:30 IST
[gtm.lock] 'Outreach PoCs': restricted, read-only (never written, on any row): reconnect_date=AB 'Reconnect Date'
[rules] R14: 670 row(s) with a company -> 0 due today, 142 still to come, 528 with no date anywhere (never), 0 already fired, 0 whose day has gone (skipped, no catch-up), 0 Lost or On Hold, 0 with a blank name or company, 0 named by the next-steps post today; by rule: reconnect date 0, meeting date 2, last contact 140
```

(the channel name, the id and the numbers are illustrative; the last line is the shape measured on 13 Oct). The
letter on the `[gtm.lock]` line is today's layout: it follows the column if the team moves it.

**What must NOT be in the boot log.** Each of these means R14 will post nothing:

```
SALES_API_CHANNEL_ID is unset — the reconnect follow-up (R14) has nowhere to post and will post NOTHING. ...
[reconnect] SALES_API_CHANNEL_ID=... cannot be reached: no such channel, or the bot has not been added to it. ...
[gtm.lock] 'Outreach PoCs': the role(s) reconnect_date map to NO column. ...
```

The first two are fixed in `.env` (and by inviting the bot). The third means no header on the tab says
"Reconnect Date" any more: R14 still runs, on the meeting and contact dates alone, until the header is back.
`RECONNECT_TIME=... is outside 14:00-16:00; using 15:30 instead` is a warning, and the post goes at 15:30.

**On the day.** At 15:30, `[drip] SENT slot N (outside the cap) ... reconnect`, then
`[rules] R14: recorded N person(s) as fired on <date>`. A yes under the post:
`[reconnect] listed all N person(s) due on <date> in M message(s), for <who>`.

**The Bot Rules tab, by hand.** Rule 14 is a new row; its "What the Bot Shares / Checks" wording is
`sheet_wording` under R14 in `bot_rules.yaml`. Paste it into the sheet.

`python verify_reconnect.py` checks all of it offline and prints a table of the 35 cases.

## Upgrading to NFT2-1090 (Voice v2: the character card, the voice book, context, empathy)

What changes for the team: Saley greets like a teammate ("Hi Sid, good morning. How's it going?"), answers
small talk in one line, says "we", asks rather than orders, notices how somebody is doing (once), and asks
questions instead of asking for a vote. The tone examples come from `voice_book.md`, which Trishi edits — no
restart needed after an edit. **The fourteen daily post templates are unchanged.** Described in the README
under "Voice v2".

**No deploy step changes.** `git pull`, add the lines below to `.env`, `pm2 restart sales-bot`. The database
migrates itself on boot (three new tables: `person_state`, `prompt_log`, `company_facts`). `voice_book.md`
ships in the repo; keep it next to `bot.py` or point `VOICE_BOOK_PATH` at it.

**The prompt log.** With `SALEY_PROMPT_LOG=true` every model call is written to `state/prompts/<date>/` on the
machine running the bot (`state/prompts/test/` during a test day). The folder is gitignored and pruned after
`SALEY_PROMPT_LOG_DAYS`; it holds the strategy, sheet rows and channel text the prompts carry, so treat it like
the database. Set `SALEY_PROMPT_LOG=false` to stop writing it.

**In `.env`, on the laptop AND on the server (`/opt/sales-bot/.env`)** — all optional, the defaults are what
was specified; add a line only to change it:

```
VOICE_SOURCE=book
VOICE_BOOK_PATH=./voice_book.md
SALEY_SMALL_TALK=react
SALEY_FILLERS=unsure
EMPATHY_ENABLED=true
EMPATHY_STATE_HOURS=48
CONTEXT_MESSAGES=10
SALEY_PROMPT_LOG=true
SALEY_PROMPT_LOG_DAYS=14
```

No default changed and nothing is retired. With `VOICE_SOURCE=book` the weekly voice rebuild stops (no channel
read, no voice-note model call); `VOICE_SOURCE=channel` brings the learned profile back exactly as before.

**What to look for in the log.** `[voice] the voice book ./voice_book.md is missing` means the book was not
found (Saley then writes from the card alone). `[voice] voice book entry skipped — it reads like an
instruction` names an entry to fix. `[prompt-log] cannot write under …` (once) means the state folder is not
writable; composing carries on.

**Try it in the test channel.** "hi" → a one-line hello. "how's it going?" twice → two different lines, a
third → a 👍. Reply "show the prompt for that" (as an approver) to any Saley reply → the prompt behind it.

`python verify_voice_book.py` checks all of it offline and prints the T1-T66 table.
## Upgrading to NFT2-1087 (the AI news flow: one hourly cycle, our names first)

What changes for the team: the feeds are read once an hour from 14:00 IST instead of every 15 minutes; news that
names a company or a person on Master Pipeline or Outreach PoCs (or a competitor on the watch list, or one of our
researchers) is flagged by rule and goes first; the daily post is ONE message of at most 5 stories with the tags
on its heading line, and there is no "More AI News"; breaking news goes out within the hour, any day, with no daily
limit (at most 3 stories a cycle); a day with nothing to send posts nothing. Described in the README under "AI news
(R1): the hourly cycle, our names first (NFT2-1087)".

**No deploy step changes.** `git pull`, edit `.env`, `pm2 restart sales-bot`. The database migrates itself on boot:
five columns on `news_feed_items` (`names`, `flag`, `source_rank`, `carried_from`, `carry_count`) and two new tables
(`news_names`, `news_cycles`). `news_targets` and `news_checks` are left in place and no longer read. No new Python
package: arXiv is read with `feedparser`, which is already installed.

**In `.env`, on the laptop AND on the server (`/opt/sales-bot/.env`).**

```
# CHANGE these two if the line is present (a line that is present overrides the new default):
NEWS_TOPICS=...        # copy the whole line from .env.example (38 topics; leadership, robot, robotics removed)
NEWS_RSS_FEEDS=...     # copy the whole line from .env.example (the five AI desks plus nine Indian and UK feeds)

# CHECK this one: the ticket's bar for INDUSTRY breaking news is 5
NEWS_BREAKING_MIN_IMPORTANCE=5

# OPTIONAL, the defaults are what was specified; add a line only to change it:
# NEWS_PULL_FIRST=14:00
# NEWS_PULL_UNTIL=23:00
# NEWS_PULL_WINDOW_HOURS=2
# NEWS_NAME_BATCH_SIZE=15
# NEWS_GENERIC_NAMES=Google,Meta,Microsoft,NVIDIA,Apple,OpenAI,Amazon,Arcade,Multiple,Cleo
# NEWS_WATCH_NAMES=Scale AI,Surge AI,Mercor,Toloka,Appen,Labelbox,Prolific,Handshake AI,Invisible Technologies
# NEWS_INSTITUTION_WORDS=university,institute of,college,school of,IIT,IISc
# NEWS_REQUEST_GAP_SECONDS=1.5
# NEWS_BREAKING_MAX_PER_CYCLE=3
# NEWS_POC_BREAKING_MIN=4
# NEWS_CARRY_MAX_DAYS=3
# NEWS_BREAKING_MAX_AGE_HOURS=3
# ARXIV_ENABLED=true
# ARXIV_BATCH_SIZE=20

# DELETE these lines if present (retired, no longer read; leaving one in is harmless):
#   (NEWS_BREAKING_MAX_PER_DAY is read again since NEWS-PATCH-OCT16 — see that section)
#   NEWS_FEED_POLL_MINUTES  NEWS_CHECK_TIMES  NEWS_CHECK_MAX_USES  NEWS_BREAKING_MAX_PER_DAY
#   NEWS_POC_TARGETS_PER_DAY  NEWS_POC_SLOTS  NEWS_POC_LOOKBACK_DAYS
#   NEWS_OVERFLOW_ENABLED  NEWS_OVERFLOW_MIN_IMPORTANCE  NEWS_OVERFLOW_MAX_ITEMS
```

`SALES_ALWAYS_TAG_IDS` is who the news heading tags. The templates show two people (Vaishnavi and Sid); if the line
holds one id, the heading tags one person.

**What to look for in the log after the first 14:00.**

```
[news-cycle] 2026-10-14 14:00: BIG pull, the last 24h
[news] name list rebuilt: N name(s) — ... compan(y/ies), 9 watch, ... person/people, ...
[feeds] big pull (when:1d): N Google request(s) for N name(s) and 38 topic(s), 14 outlet feed(s), N arXiv paper(s) — ...
[news] cycle: scored N of N unscored item(s) in one <model> call (N about us, shown first) — ...
[news-cycle] 2026-10-14 14:00: nothing breaking — ...        (or: posted N breaking story/stories in one message)
```

and then, every hour until 23:00, `[news-cycle] ... small pull, the last 2h`. Lines that need a person:

```
[news] name list: the mapping's departures list is not loaded, so NO PERSON is listed — companies only. ...
[news] name list: the sheets could not be read; keeping the list built on <date>
[feeds] Google News answered 429 on request N of M ...; the pull stops here ...
```

The first means no person on Outreach PoCs is watched until the mapping sheet is shared. The third, once, is
harmless (the next cycle reaches back over it); every hour, raise `NEWS_REQUEST_GAP_SECONDS` or lower
`NEWS_NAME_BATCH_SIZE`.

**The Bot Rules tab, by hand.** Rule 1's "What the Bot Shares / Checks" wording still describes the old flow; nothing
in the code reads it, but it should be brought in line with the README section above.

`python verify_news_v2.py` checks all of it offline and prints the T1-T83 table.

## Upgrading to NEWS-PATCH-OCT16 (the Gemini 4 miss)

A bug-fix patch of the v1 news cycle (not the v2 rebuild). On 14 Oct the Gemini 4 "Aragon" launch came in 29 times
and was never posted, each cycle flagged 564-603 items as ours, five of ten hourly cycles never ran, and up to three
breaking posts went out a cycle with no daily cap. What changed: one story is one row however many outlets carry it
(`outlet_count`); short and flooding names stop flagging everything; the scorer works down a ranked list in up to three
calls a cycle and a frontier-model launch is scored 5 in code; breaking is one a cycle and four a day, and an industry
story needs two outlets; a missed hour runs late instead of never; and a health line goes to the test channel at 23:45.

**Why five cycles were missed on 14 Oct.** No `news_cycles` rows for 14-15 Oct were available: the two database
copies in the repo (`sales_bot.db`, 7 Oct; `sales_bot_test.db`, 5 Oct) predate the table, and the server's
`/opt/sales-bot` database is not visible from here. Reasoning from the code instead:

- The news cycle runs only on a sweep tick (`_sweep_once`, before the drip). The loop sleeps
  `COS_FOLLOWUP_CHECK_INTERVAL_MINUTES` AFTER each tick, so one tick follows the last by the interval plus the tick's
  own run time.
- The old `news.cycle_slot` ran only the LATEST slot at or before the tick, and nothing went back for an older one. Any
  gap of more than an hour between two ticks therefore lost a slot for good.
- With the shipped interval of 15 minutes a tick would have to run for over 45 minutes to lose an hour. A big pull is
  about 90 Google requests 1.5 s apart, plus one scoring call. That is minutes, not three quarters of an hour.
- **The laptop's `.env` has `COS_FOLLOWUP_CHECK_INTERVAL_MINUTES=150`** (`.env.agent`), and config logs "is over an
  hour" at boot for it. At 150 minutes the ticks fall at about 14:0x, 16:3x, 19:0x, 21:3x and then after midnight, so
  the cycles that run are 14, 16, 19 and 21. A restart between 22:00 and 23:00 (which ticks at once) adds 22. The
  missed slots are then exactly 15:00, 17:00, 18:00, 20:00 and 23:00, which is what happened. The server's value is
  not visible here: check it with `grep COS_FOLLOWUP_CHECK_INTERVAL_MINUTES /opt/sales-bot/.env`, and look in the log
  for `is over an hour` and for a restart near 22:00 on 14 Oct.

The patch makes every tick run the OLDEST unclaimed slot of today (marked `late by N min`), and skips one that is more
than 3 whole hours late. That is still ONE SLOT A TICK, so **with a 150-minute interval it still cannot keep up**: ten
hourly slots need at least ten ticks between 14:00 and 23:59, and 150 minutes gives about four. Set the interval to 15
(the default, and what Rule 13 and the posting window already expect).

**Lines for `.env`: the laptop AND `/opt/sales-bot/.env`.**

```
# SET (or delete the line; 15 is the default). The cause of the missed cycles if the server has 150 like the laptop:
COS_FOLLOWUP_CHECK_INTERVAL_MINUTES=15

# CHANGE or DELETE: retired by NFT2-1087 and READ AGAIN now, as the day's breaking budget (new default 4).
# The laptop has NEWS_BREAKING_MAX_PER_DAY=2, which would now cap breaking at two a day.
NEWS_BREAKING_MAX_PER_DAY=4

# CHANGE if present: the per-cycle default is now 1 (a line that is present overrides it).
NEWS_BREAKING_MAX_PER_CYCLE=1

# CHECK: the ticket's bar for INDUSTRY breaking news is 5. The laptop has 4, which with two outlets would break
# on a 4 from two outlets.
NEWS_BREAKING_MIN_IMPORTANCE=5

# NEW, all optional: the defaults are the ticket's. Add a line only to change one.
# NEWS_NAME_MIN_CHARS=4
# NEWS_NAME_FLOOD_MAX=30
# NEWS_SCORE_MAX_CALLS=3
# NEWS_BREAKING_MIN_OUTLETS=2
# NEWS_CYCLE_LATE_SKIP_HOURS=3
# NEWS_CYCLE_MAX_SECONDS=600
# NEWS_HEALTH_TIME=23:45
```

`NEWS_PREFERRED_DOMAINS` now matters for more than the old prompt line: a copy of a stored story from one of those
domains takes over the row's link (`best_url`). The laptop's list is the `.env.example` one.

The health line goes to `SALES_TEST_CHANNEL_ID`. With that unset it is only logged, never posted to the sales channel.

**The database** upgrades itself at the first start: five columns on `news_feed_items` (`story_key`, `outlets`,
`outlet_count`, `best_url`, `scored_by`), five on `news_cycles` (`merged`, `noisy`, `scored_model`, `scored_rule`,
`carried`), an index on `story_key`, and a one-off backfill of `story_key` in batches of 500 rows. Look for, once:

```
[db] migrating: adding news_feed_items.story_key        (and the other nine)
[db] story keys back-filled on N stored feed item(s), in batches of 500
```

**What to look for in the log after the next 14:00.**

```
[feeds] name list: 'Xyz' is too short to match (under 4 characters, NEWS_NAME_MIN_CHARS) — never matched   (once per name)
[feeds] hourly pull (when:2h): ... N new, N copies merged[, noisy names: ...] — 0 API calls
[news] cycle: N frontier launch(es) scored 5 by rule, not sent to the model: ...
[news] cycle: scored N of N unscored item(s) in N <model> call(s) (pre-ranked, N about us) — ...[; carried N to the next cycle]
[news-cycle] 2026-10-17 15:00: small pull, the last 2h — late by 40 min                   (a catch-up)
[news-cycle] 2026-10-17 15:00: skipped — late by 4 h 10 min — more than 3 h late ...     (after an outage)
[news-cycle] ...: the day's breaking budget is spent (NEWS_BREAKING_MAX_PER_DAY=4) — N candidate(s) held ...
[news-health] 2026-10-17: posted to the test channel (outside the daily cap)
```

A `carried N` every hour means three calls of 40 are not enough for the day's volume: look at the health line's
noisy names first, then raise `NEWS_SCORE_MAX_CALLS`. `the pull has run 600s` every hour means the pull is too slow
for the cap: look at `NEWS_REQUEST_GAP_SECONDS` and the number of names.

`python verify_news_patch.py` checks all of it offline and prints the T1-T14 table.

### NEWS-PATCH-OCT16, F7: "Research published:" in the daily post

**The daily post gained one optional group on 16 Oct, by decision**: "Research published:", between "Our PoCs are in
the news:" and "Elsewhere in AI". It is printed only when there is research, takes at most `NEWS_RESEARCH_MAX_PER_POST`
(2) of the five lines, and is never breaking. A story by one of our researchers, or a paper (a link on arxiv.org or
huggingface.co/papers) with one of `NEWS_RESEARCH_KEYWORDS` in its title or summary, is importance 4 by rule.
`docs/test-reports/templates-main.txt` was updated for this one change only: three daily-post renders were added (ours,
research and the industry; research alone; ours and the industry with no research). Nothing else in the file moved.

Nothing reads an arXiv category feed or Hugging Face papers today: the arXiv lane looks up our researchers as authors.
For the keyword lane, add the feed URLs you want to `NEWS_RSS_FEEDS` (on both machines). Both new variables are
optional:

```
# NEWS_RESEARCH_MAX_PER_POST=2
# NEWS_RESEARCH_KEYWORDS=evaluation,benchmark,preference,RLHF,DPO,human feedback,annotator,red team,red-teaming,speech,ASR,TTS,voice,Hindi,Hinglish,Indic,multilingual,low-resource,alignment,reward model
```

In the log: `[news] cycle: N research item(s) scored 4 by rule (F7)`, and in the daily post's skipped lines "research,
and the post has room for N — carried to the next daily post".

## Upgrading to NEWS-ANSWER-OCT16 (a question gets the real top 5)

Runs after NEWS-PATCH-OCT16, on the same build. A news question now gets the top stories of the window it names
(default the last 24 hours), POSTED BEFORE OR NOT, ranked by score and then newest, five at a time; the list ends on
"There are N more in the <window>. These are the top 5. Would you like to see the rest?" when more are left, and a
"yes" / "next" / "show me the rest" within 30 minutes gives the next page. "top news in the last 24 hours", "since
yesterday" and "this week" stay on the no-model path. Only the daily post and the breaking post are unchanged.

**Lines for `.env`: the laptop AND `/opt/sales-bot/.env`.** Both are new and optional: the defaults are the ticket's.

```
# NEWS_ASK_DEFAULT_HOURS=24      # the window of a news question that names none
# NEWS_ASK_REST_MINUTES=30       # how long a "yes" under an answer still gives the next page
```

Nothing else changes: no database change, no new table (the page state is in memory, per channel, and a restart
forgets it — a "yes" after a restart is read like any other reply).

**What to look for in the log** after somebody asks:

```
[news] question: Thu 15 Oct 16:10 to Fri 16 Oct 16:10 IST (last 24 hours) — N collected, N worth 3+ (N posted before), 0 unrated; giving 5 from 0, N left
[news] msg=... the rest of the news: stories 6-10 of N
[news] msg=... a yes to the rest of the news came late; asked back
[news] question: both hours=48 and days=1 were given; hours wins
```

`python verify_news_window.py` checks all of it offline and prints the T1-T16 table.

## Upgrading to NEWS-ANSWER-OCT16-B (a named day is that day only)

A follow-up to NEWS-ANSWER-OCT16, same build. "news from last wednesday" is Wednesday 00:00-23:59:59 IST and nothing
else, headed "AI News, Wed 14 Oct"; "from monday to wednesday" is "12–14 Oct"; a day that cannot be read, or has not
happened, is asked back. "most important" / "top" with a named day keeps only stories scored 4 or 5. The first answer is
the top 5, and "yes" gives every other 4-or-5 story of that day at once. Copies of one story are one line with "+N
outlets". Only the answers change: the daily post and the breaking post are byte-identical.

**Lines for `.env`: the laptop AND `/opt/sales-bot/.env`.** One new, optional variable:

```
# NEWS_ASK_IMPORTANT_MIN=4      # "most important" / "top" for a named day or range: this score or more
```

No database change. **What to look for in the log:**

```
[news] question: Wed 14 Oct 00:00 to Wed 14 Oct 23:59 IST (Wed 14 Oct, day) — N collected, N at 4+ ...
[news] question: the rest of Wed 14 Oct — 23 at 4 or more
[news] todays_news: date='2026-10-14' given with hours=48 days=None; the date wins
```

`python verify_news_window.py` checks it offline (B1-B20, beside NEWS-ANSWER-OCT16's T1-T16).

## Upgrading to RULES-OCT16 (R6 shows only the email work; R7 is off)

**R6, "Email check: connected, no DM yet" (Tuesday and Friday).** The post is the email work only: "Found an email:",
"Email on the sheet cannot receive mail:" (the address and the reason) and "Couldn't find an email:", five people at
most, in turn (least recently named first). Somebody whose email on the sheet works is not named. A person with no LI
Connected Date is not eligible. When nobody needs anything: "Everyone connected has a working email on file." A new
table, `li_no_dm_mentions`, holds the rotation; it is created at the first start.

**R7, "No meeting booked yet", is OFF** (`enabled: false`, out of Monday's order). Monday is R4 at 14:00, R1 at 16:00,
R10 at 18:00: the AI news moves up one slot, and its window starts at Monday 16:00. To turn R7 back on: `enabled: true`
on R7 AND R7 back in `daily_order` (`mon: [R4, R7, R1, R10]`), then restart. A switched-off rule left in the order is
refused at startup.

**Lines for `.env`: the laptop AND `/opt/sales-bot/.env`.**

```
# NEW, optional: the defaults are the ticket's.
# LI_NO_DM_SHOW_WITH_EMAIL=false
# LI_NO_DM_REQUIRE_DATE=true

# RETIRED while R7 is off (harmless if left; they apply again if R7 is turned back on):
#   DM_NO_MEETING_DAYS  DM_NO_MEETING_BOOKED_GAP_DAYS  DM_NO_MEETING_MAX_CONTACTS
```

**What to look for in the log** on the next Tuesday or Friday:

```
[rules] R6: N row(s) read (the whole tab); N qualify ...; N with no connected date; all N handed on in rotation order
[rules] R6: <name> (<company>) has no LI Connected Date, so is not eligible (LI_NO_DM_REQUIRE_DATE)   (once each)
[email] R6: N handed on -> N named (...; N with email work, cap 5: N found, N cannot receive mail, N not found); N verified and not named; ...
[rules] R6: recorded N person(s) named on <date>
```

**The Bot Rules tab, by hand:** rule 6's and rule 7's "What the Bot Shares / Checks" wording should follow the yaml
(`sheet_wording` on R6; rule 7 can say "Off since 16 Oct. Rule 13 reminds about calls after a DM.").

`python verify_li_no_dm.py`, `python verify_dm_no_meeting.py` and `python verify_day_order.py` check it offline.
