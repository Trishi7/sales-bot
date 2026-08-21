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

## 4. Meeting-notes sync (optional, enables the third source)

The bot reads local files only and holds no Google credential. A separate process
puts the notes there:

```bash
sudo apt-get install -y rclone
rclone config                       # add a Google Drive remote, once, interactively
mkdir -p /opt/sales-bot/notes
rclone copy "gdrive:Sales Meeting Notes" /opt/sales-bot/notes
```

Then in `.env`:

```
NOTES_DIR=/opt/sales-bot/notes
NOTES_SYNC_CMD=rclone copy "gdrive:Sales Meeting Notes" /opt/sales-bot/notes
```

Add a cron entry for the periodic pull (`NOTES_SYNC_CMD` only runs on demand,
when a question is clearly about a recent meeting):

```bash
crontab -e
# every 15 minutes
*/15 * * * * rclone copy "gdrive:Sales Meeting Notes" /opt/sales-bot/notes >/dev/null 2>&1
```

Leave `NOTES_DIR` unset and the source simply reports *awaiting-access*, which
the bot states plainly when asked. That is a supported state, not a broken one.

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
`./venv/bin/python main.py`, and keeps one instance — a second would double every
nudge and race on the SQLite file.

---

## 6. Verify

```bash
pm2 logs sales-bot --lines 100
```

A healthy start logs, in order:

```
[main] starting SalesCoS (log level=INFO)
[main] config OK. sales_channels=[…] ask_channel=… roster=N …
[main] SCOPE: this bot reads and posts ONLY in the N channel(s) above …
[main] source sales_spreadsheet     awaiting-access  …
[main] source strategy_doc          awaiting-access  …
[main] source sales_meeting_notes   connected        …
[main] policy loaded from ./sales_policy.md (N chars); re-read on every question
[main] connecting to the Discord gateway...
[bot] connected as SalesCoS#1234 (id=…), in 1 guild(s)
[bot] reading #sales-ask (…)
[bot] chase sweeper started (every 15 min)
```

Then check the four things that matter:

| Check | How | Expected |
|---|---|---|
| It joined | member list of a sales channel | the bot is there |
| It answers in persona and states its sources | post `what can you do?` in the ask channel | a direct, emoji-free reply that **names the sources awaiting access** |
| It ignores everything else | post a question in a NON-sales channel | no reply, and nothing in the logs beyond DEBUG |
| It can't see other channels | member list of a non-sales channel | the bot is **not** there |

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
- **Logs** go to `./logs` via PM2 (gitignored). Rotation:
  `pm2 install pm2-logrotate`.
- **Verbosity**: `LOG_LEVEL` in `.env` (`DEBUG` | `INFO` | `WARNING` | `ERROR`).
  `DEBUG` shows every skipped channel and routing decision — useful while setting
  up, far too loud to leave on.
- **Backups**: `sales_bot.db` holds the open chases and the nudge rate-limit log.
  Losing it means in-flight chases are forgotten and the caps reset; it is not
  catastrophic, but a nightly copy is cheap.
- **Two bots, one box**: `pm2 status` should show both, with distinct names. If
  the sales bot ever starts answering in the PM bot's channels, the cause is
  `SALES_CHANNEL_IDS`, not the code — check it, and check the role permissions
  from step 0.
