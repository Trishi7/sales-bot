# Wording for the sheet — 8 Oct 2026

For Vaishnavi. Plain words, ready to paste. Two changes are in here:

1. **The order of the day's posts (NFT2-1069).** Posts are 2 hours apart from 2 PM, in a set order for each
   weekday. AI news no longer has a fixed 2 PM slot. Rule 7 is back on Mondays. Rule 11 is Wednesdays only.
2. **How a news message looks, and what Saley does when asked for the news** (the same day's earlier change).

Saley does not read these tabs to decide what to do; they are the team's record. The bot's own copy is
`bot_rules.yaml`, and every quoted block below is copied from it word for word.

## Weekly Schedule tab

| Day | 2 PM | 4 PM | 6 PM | 8 PM |
|---|---|---|---|---|
| Monday | Deliverables checklist (rule 4) | DM sent, no meeting (rule 7) | AI news (rule 1) | Closure support (rule 10) |
| Tuesday | Prospects to contact (rule 5) | LinkedIn connected, no DM (rule 6) | News-company screen (rule 2) | AI news (rule 1) |
| Wednesday | New companies in the pipeline (rule 11) | AI news (rule 1) | AI events and summits (rule 3) | — |
| Thursday | Prospects to contact (rule 5) | AI news (rule 1) | Sales packages (rule 12) | — |
| Friday | News-company screen (rule 2) | LinkedIn connected, no DM (rule 6) | AI news (rule 1) | — |

Under the table:

- Posts are 2 hours apart, starting at 2 PM. The gap is never shortened.
- A rule with nothing to post takes no slot, and the next one moves up. Example: a Monday with no P1 deliverable
  due is DM sent, no meeting at 2 PM, AI news at 4 PM, Closure support at 6 PM.
- Nothing in this order is posted after 8 PM. What is left waits for that rule's next day.
- Outside this order, at their own times (they never move a post in the table, and no post in the table waits for
  them):

| When | Post |
|---|---|
| Any day, 10 AM | Meeting preparation (rule 8): 5 days before, 3 days before and on the day |
| Any day, 10 AM | Meeting done, no next steps (rule 9) |
| Monday to Friday, 3 PM | Next steps for connected contacts (rule 13) |
| The minute it was asked for | A reminder somebody asked for |
| Hourly, 11 AM to 11 PM, weekends too | Urgent AI news, only when something is major |
| Sunday, 2 PM | Deliverables checklist, only when a P1 item is due on the Monday. Nothing else |
| Saturday | Nothing |

- At most 5 posts a day count (the longest day above has 4). Meeting posts, next-step follow-ups, reminders,
  urgent news and answers to questions don't count.
- A post goes out at the first check at or after its time (Saley checks every 15 minutes), never before it.

## Global Rules tab — row "Order of the day" (new)

> Posts are 2 hours apart, starting at 2 PM, in this order. Monday: Deliverables checklist, DM sent no meeting, AI
> news, Closure support. Tuesday: Prospects to contact, LinkedIn connected no DM, News-company screen, AI news.
> Wednesday: New companies in the pipeline, AI news, AI events and summits. Thursday: Prospects to contact, AI news,
> Sales packages. Friday: News-company screen, LinkedIn connected no DM, AI news. A rule with nothing to post takes no
> slot and the next one moves up. Nothing in this order posts after 8 PM; what is left waits for that rule's next day.
> Meeting prep and meeting follow-ups post at 10 AM and next-step follow-ups at 3 PM, outside this order; reminders
> and urgent news keep their own times. None of those moves a post in the order.

## Global Rules tab — row "AI news"

> AI news posts every weekday, in its place in the day's order: 6 PM on Monday and Friday, 8 PM on Tuesday, 4 PM on
> Wednesday and Thursday, and earlier when a rule ahead of it has nothing to post. It covers the industry and our own
> PoCs — the top 2 stories about people and companies on the sheet go first, each marked with its sheet row. It is new
> every day, so it is never held back as "asked recently", and it counts as one of the day's 5 posts. Every news
> message has the same shape: a bold heading with the day, then at most 5 stories, each a bold headline with a link to
> the outlet. A major story that didn't fit follows at once in "More AI News" (major stories only, at most 5), which
> doesn't count toward the 5. Asking for the news in the channel gives the 5 best stories not sent before, newest
> first; a story is repeated only when nothing new is left. Urgent news is checked for every hour, weekends included,
> and posts only when something is major; there is no AI news post at the weekend, and Monday's post covers it.

## Bot Rules tab, column "What the Bot Shares / Checks"

### Rule 1 (AI news)

> Today's AI news worth reading — the industry, and our own PoCs. One post every weekday, in its place in the day's
> order (6 PM on Monday and Friday, 8 PM on Tuesday, 4 PM on Wednesday and Thursday; earlier when a rule ahead of it
> has nothing to post), with up to 5 stories since the last AI news post (Monday's covers the weekend). First the top
> 2 stories about people on Outreach PoCs or companies on Master Pipeline / Outreach PoCs, each marked with its sheet
> row, e.g. "(Synthflow AI — on Master Pipeline)"; then the best of everything else. Every news message looks the
> same: a bold heading with the day, then at most 5 stories, each a bold headline with a link to the outlet. Major
> stories that didn't fit follow straight away in one "More AI News" message (major stories only, at most 5), which
> doesn't count toward the 5 posts a day. Asked for the news in the channel, Saley gives the 5 best stories it hasn't
> sent before, newest first, and repeats a story only when nothing new is left. Never held back as "asked recently".
> Checked again every hour, weekends included, and posted straight away only when something is major. No AI news post
> on Saturday or Sunday. Nobody on the departures list is looked up.

### Rule 7 (DM sent, no meeting)

> People we DM'd more than 7 days ago who haven't booked a meeting, every Monday, second in the day's order: days
> since the DM and the last note logged. Rule 13 comes first: anyone it covers (Sid - LI Addition says Connected and
> the LI Connected Date is filled in) gets Rule 13's reminders and is never listed here. At most 5 people a post.

### Rule 8 (Meeting preparation)

> Meetings coming up that need prep. Three touches for each meeting on Outreach PoCs: 5 days before, 3 days before and
> on the day. Every touch posts at 10 AM, outside the day's order, and doesn't count toward the 5 posts a day. Asks
> whether the deck, package and demo are ready, with any recent news on the company.

### Rule 9 (Meeting done, no next steps)

> Meetings that happened with no next steps recorded. Checks Outreach PoCs for a completed meeting whose Notes/Remarks
> is blank. Asks 3 days after the meeting, then every 3 days, one step at a time: a channel post, then two DMs, then
> one escalation to Sid — and then stops. Asks for the next steps, the package discussed and an estimated deal size.
> Filling in Notes/Remarks ends it. Posts at 10 AM, outside the day's order, and doesn't count toward the 5 posts a
> day.

### Rule 11 (New company in the Master Pipeline)

> New companies in the Master Pipeline, every Wednesday, first in the day's order (2 PM): every company that appeared
> on the tab since last Wednesday's post, whatever day it appeared. Names them and asks whether to look for relevant
> PoCs; nothing is searched until an approver says yes, and nobody is added to Outreach PoCs without a separate yes.
> Up to 10 companies a post.

## Bot Rules tab, column for the day or time

| Rule | Was | Now |
|---|---|---|
| 1 AI news | Every weekday, 2 PM | Every weekday, in the day's order: Mon 6 PM, Tue 8 PM, Wed 4 PM, Thu 4 PM, Fri 6 PM |
| 7 DM sent, no meeting | Replaced by rule 13 | Monday, second in the order (4 PM) |
| 8 Meeting preparation | Any day; 10 AM on the day of the meeting | Any day, every touch at 10 AM |
| 9 Meeting done, no next steps | Any day | Any day, 10 AM |
| 11 New company in the Master Pipeline | The next working day | Wednesday, first in the order (2 PM) |

## What changed from the old wording (the order of the day)

- AI news is no longer "every weekday at 2 PM" and is no longer "planned first". It has a place in each day's order.
  It still posts every weekday, and never at the weekend.
- Rule 7 is switched back on, Mondays only. It never lists a person rule 13 covers (Sid - LI Addition says
  Connected and LI Connected Date is filled in); those people get rule 13's reminders.
- Rule 11 is Wednesdays only. A company that appears on any day is named on the next Wednesday; one that appears
  on a Wednesday is named the Wednesday after. Up to 10 companies a post (it was 3).
- Every meeting-prep touch and every meeting follow-up posts at 10 AM. Before, only the prep note on the day of the
  meeting had a fixed time.
- The day's posts used to end by 6:30 PM with the gaps squeezed to fit. Now the gap is always 2 hours and the last
  post is at 8 PM.

## What a news message looks like

```
**AI News, Thu 8 Oct**

- **State AGs launch investigations into OpenAI AI safety** ([Reuters](link))

- **TM Forum and Accenture launch AI trust framework for telecoms** ([techcrunch.com](link))
```

- The same shape for the AI news post, "More AI News", "Breaking AI News" and an answer to "any AI news?".
- A story about one of our PoCs keeps its fuller line, as before: the headline, what happened, and whose it is —
  "(Synthflow AI — on Master Pipeline)".
- Never more than 5 stories in one message.

## What changed from the old wording (the news messages)

- "More AI news today" is now "More AI News, <day>", and carries only major stories (it used to carry anything worth
  posting, up to 8).
- An answer in the channel no longer says when the news is posted or what was "already posted", and does not repeat
  what it has already given.
