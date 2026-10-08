# Rule 13: wording for the Bot Rules sheet

For Vaishnavi. Plain wording for the "Sales Bot_membrane" sheet after the 7 Oct
change. Paste each block into the cell named above it. Nothing here is posted
by Saley; it is the description of what Saley does.

## 1. Bot Rules tab, rule 13, "What the Bot Shares / Checks"

> Next steps for the people we're connected with. Monday to Friday at 3 PM:
> Outreach PoCs rows where Sid - LI Addition is Connected and LI Connected Date
> is filled. One post naming up to 5 people, taken from the top of the sheet
> down and then from the top again; someone whose call reminder is due goes
> first. One line each, saying what to do next according to Next Steps: if it
> is blank, asks what the next step is (2 days after connecting); "Research the
> PoC", asks whether they have been researched and to set Next Steps to "Send
> email 1"; "Send email 1/2/3", asks whether that email has gone out and to
> mark its Sent cell and date (2 days after the previous step), and a week
> after the email's date asks to move Next Steps on (after email 3, to "Reach
> by LI DM"); "Reach by LI DM", asks whether the DM has gone out. A week after
> the LI DM Date with no meeting, reminds to call them, again every 3 days
> until 3 weeks after the DM; then asks once to set Prospect Status to
> "Unresponsive" and stops for that person. If LI DM Sent says Replied, asks
> whether a meeting is being set up instead. A booked meeting pauses it; a
> Completed meeting ends it. Saley only reminds; it never fills these cells.
> Doesn't count toward the 5 posts a day.

The other cells on the rule 13 row:

| Column | Value |
|---|---|
| Frequency | Daily, Monday to Friday |
| Time | 3 PM IST |
| Max per post | 5 |
| Where | API Sales channel |

What a post looks like:

> **Next steps**
> A few next steps on people we're connected with:
> • Priya Rao (Acme Labs): Next Steps says Send email 1. Has it gone out? If so, mark 1st Email Sent and the date.

## 2. Bot Rules tab, rule 7, "What the Bot Shares / Checks"

> Replaced by rule 13 from 7 Oct 2026. Rule 13's call reminders cover the
> people we DM'd who haven't booked a meeting.

## 3. Bot Rules tab, rule 6

No change to when it runs or who it picks. Its line in the post changed: it
talks about the email only and no longer says "no DM logged".

Before: "Acme Labs · Priya Rao (CTO) — connected 9 days ago, no DM logged. No email on file"

Now: "Acme Labs · Priya Rao (CTO) — connected 9 days ago. No email on file"

Still open (question 8 below): the rule's name, its heading in the post and
its fallback sentences still say "no DM yet".

## 4. Bot Rules tab, rule 9, "What the Bot Shares / Checks"

> Meetings that happened with no next steps recorded. Checks Outreach PoCs for
> a completed meeting whose Notes/Remarks is blank. Asks 3 days after the
> meeting, then every 3 days, one step at a time: a channel post, then two DMs,
> then one escalation to Sid — and then stops. Asks for the next steps, the
> package discussed and an estimated deal size. Filling in Notes/Remarks ends
> it. Doesn't count toward the 5 posts a day.

## 5. Weekly Schedule tab, and the Global Rules "Daily cap" row

Weekly Schedule, new row:

> Monday to Friday, 3 PM · Next steps for connected contacts (up to 5 people a post)

Weekly Schedule, Monday row: remove "DM sent with no meeting after 7 days".

Global Rules, "Daily cap":

> Max 5 posts a day; meeting prep, meeting follow-ups, next-step follow-ups,
> reminders, urgent news and answers to questions don't count.

## 6. Seven things I assumed. Yes or no to each?

a. Weekdays only, at 3 PM IST. Saturday and Sunday stay silent. Yes?

b. The next-steps post does not count toward the 5 posts a day. Yes?

c. Saley only reminds. It never fills Next Steps, the email columns or LI DM
   cells itself; it asks you to. Yes?

d. A reply of "done" from anyone on the team counts, not only from you or Sid.
   Yes? (Replies to this post are not built yet; they come next.)

e. People are taken in sheet order, top to bottom. Priority (P1, P2) is read
   but does not change the order. Yes?

f. When LI DM Sent says "Replied", Saley does not send call reminders for that
   person. It asks whether a meeting is being set up. Yes?

g. A meeting booked for today or later pauses rule 13 for that person. A past
   meeting that is not marked Completed lets the call reminders carry on. A
   Completed meeting ends rule 13 for them, and rule 9 takes over. Yes?

## 7. Three more, from building it

h. On a day rule 13 names someone, rules 5, 6 and 10 skip that person (one
   mention a day). Is that right, or may two rules name the same person on the
   same day?

i. After the reminder to mark someone Unresponsive, Saley never raises them in
   rule 13 again, even if their Next Steps changes later. Is that right, or
   should a later change bring them back?

j. A person whose DM got a reply is asked about the meeting each time the
   rotation reaches them, with no end, until a meeting is booked or their
   status changes. Is that right?

## 8. Rule 6's remaining wording

Rule 6 is now only the email check, but its name ("LinkedIn connected, no
DM"), its heading in the post ("LinkedIn connected, no DM yet") and the
sentence it falls back to ("Want to send one this week?") still talk about the
DM. What should they say?

## 9. Four rows that rule 13 skips today

Four rows say Connected but have no LI Connected Date, so rule 13 skips them.
Saley lists them when asked for the "cadence preview". Filling in the date
brings them in. Six more rows have the date typed as text (05.10.2026); those
are read correctly.
