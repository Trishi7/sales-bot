# NFT2-1062 — plan addendum

Amends `docs/plans/NFT2-1062.md` (revision 2). Where the two disagree, this file wins.

## A1. `NOTES_REQUIRE_TITLE_TAGS`: a tag must be distinctive (2026-10-07, raised by the tester)

**The fact.** Title matching (plan 2.3) normalises the title and the tag the same way and
matches on token boundaries. A tag of `[Sales]` therefore normalises to the single token
`sales` and matches any title containing the word: `Sales sync – …`, `Acme sales call`.
The brackets carry no meaning. Such a tag does not discriminate.

**What does not change.** The matching rule itself (the standup guard depends on it, and
`( AM Sync)` must keep matching `am sync`). The default stays EMPTY (D3), so the shipped
and the server configuration are unaffected.

**Changes**

- Plan 2.4, 6 (E7) and 3 (`config.validate`): every example that used `[Sales]` now uses
  `[SalesNotes]`. E7 reads: with `NOTES_REQUIRE_TITLE_TAGS="[SalesNotes]"` and 3 untagged
  docs, one of them titled `Sales sync – …` → loaded 0, bucket `missing_tag == 3`, the
  WARNING fired once; a fourth doc titled `[SalesNotes] Acme call – …` loads.
- New pure test in `tests/test_notes_scope.py`, `tag_is_matched_as_a_word`: the tag
  `[Sales]` DOES match `Sales sync – 2026-10-06` and `Acme sales call – 2026-10-06`
  (pins the documented behaviour, so nobody later assumes brackets are literal);
  `[SalesNotes]` matches neither, and matches `[SalesNotes] Acme call` and
  `salesnotes – Acme call`.
- Builder, documentation only — no behaviour change:
  - `.env.example`, the comment above `NOTES_REQUIRE_TITLE_TAGS=`: punctuation and case
    are ignored and the tag is matched as a whole word or words, so `[Sales]` is the same
    as the word "sales" and matches ordinary titles. If a tag is ever used, make it a word
    that appears in no normal title, e.g. `[SalesNotes]`. Leave empty: the folder is the tag.
  - `README.md` (the meeting-notes section) and `DEPLOY.md` §4: the same warning, in one
    or two sentences, with the `[Sales]` vs `[SalesNotes]` example.
  - `config.validate()`: the existing info line when the variable is set also says the
    tag is matched as whole words, ignoring punctuation and case. No new check, no new
    variable.

**Env variables:** no change to NEW / CHANGED / RETIRED or to the lines for either `.env`.

Status: applied by the builder and verified in the diff (`.env.example:236`,
`README.md:631`, `DEPLOY.md:229`, `config.py:2993`).

## A2. A stray that cannot be moved stays unread (2026-10-07, found in the planner's diff review)

**The gap.** Plan 2.1(c) wrote the manifest from the top-level listing after every run.
A stray the sweep could not move (a locked file, no permission, or the quarantine folder
not creatable) was still at the top level, so it would have been written into the
manifest and read.

**The rule.** `notes._sweep_strays` returns the names it could NOT move, on both failure
paths. `notes.sync_now` leaves those names out of the manifest. The sweep retries on
every sync. Accepted limit: if the sync itself brings down a file with the same name, it
is also left out until the stray is gone.

**Test** (tester, `verify_notes_scope.py`), E5e `unmovable_stray_stays_unread`: two strays
in `NOTES_DIR` before the first sync, `shutil.move` patched to raise for one. After
`sync_now`: the stuck file is still at the top level, byte-identical; it is not in the
manifest; `list_notes()` and `read_note()` never return it; the other stray is in
`_quarantine`; nothing was deleted. Second case: creating the quarantine folder raises,
so neither stray moves and neither is in the manifest.

Status: applied by the builder and verified in the diff (`notes.py:538-603`, `:1368`,
`:1411`).

## A3. Q1 and Q2 are settled by the human (2026-10-07, relayed by the lead)

Both were answered "as built". They are requirements now; section 11 of the plan has no
open question left. No code change.

- **D7 (was Q1). A to-do row with a BLANK "Source meeting" stays hidden.** Plan 2.7
  rule 2 stands: reason `no_source_meeting`. A hand-added to-do is shown only when its
  Source meeting names a note in the sales folder. In code: `todos._HIDE_BLANK_SOURCE =
  True`. It is a settled decision, not a switch to flip; the comment calling it an open
  question may be reworded by the builder but the value must not change.
- **D8 (was Q2). When the folder synced successfully before and the latest sync fails,
  the bot keeps answering from the sales folder's own last good copy and says it may be
  stale.** Plan 2.2 stands: the state is `ok`, the source status is DEGRADED, and the
  existing stale rule applies (`query_engine.py`, "IF sync.ok IS FALSE"). The sentence
  "I can't reach the sales notes folder right now, so I haven't checked the notes." is
  said only when the folder has never been pulled for this source, or when it is empty
  and that emptiness is not confirmed by a clean sync. In code:
  `notes._SERVE_LAST_GOOD_COPY_WHEN_SYNC_FAILS = True`.

**Tests that pin them** (tester): E6 already asserts a blank source is hidden with reason
`no_source_meeting`. For D8 add or confirm `last_good_copy_is_served_when_sync_fails`:
one clean sync with a sales note, then a failing sync → `source_state() == "ok"`, the note
is still returned by `read_note`, `nothing_to_say()` is None, the tool result's
`sync.degraded` is true, and the source status is DEGRADED.

## Accepted deviations from the plan (builder's list, reviewed)

1. The notes tools' `sync` dict carries only `ran / ok / forced / configured / degraded`;
   the error and remedy stay in the log and the source detail.
2. `README.md` does not name the folder or print the `.env` lines; it points to
   `DEPLOY.md` §4 (plan section 0 wins over section 3).
3. `docs/SOURCES.md` lists one source the plan's table missed: the Discord leave channel
   (`HOLIDAY_CHANNEL_ID`, read-only; `guardrails.py:103`, `leave.py:115`).
4. `quarantined=Q` in the counts line is files moved by this process since startup; the
   quarantine folder is never walked.
5. Mirror detection tokenises the command with quoted strings kept whole.
6. A file not in the manifest is never opened; its date comes from the filename only.
7. `notes.freshness()` takes the mtime over loaded notes only.
8. `source_state()` returns `unreachable` when `NOTES_DIR` is set but the directory does
   not exist.
9. `_notes_nothing_to_read()` and `_notes_sync()` are `SalesBot` methods, not closures.
