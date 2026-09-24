"""REGRESSION TESTS — one per bug found in the last few rounds.

Every test here is a bug that shipped, was caught by running something real, and
is now pinned so it cannot come back. The docstrings say what broke and how,
because a regression test whose name is `test_matches_direction` tells the next
person nothing about why anybody cared.
"""
from datetime import date

import pytest


# --------------------------------------------------------------------------
# 1. focus.matches() — containment direction
# --------------------------------------------------------------------------

class TestFocusContainmentDirection:
    """THE BUG: `matches()` tested containment BOTH ways — `want in cell or
    cell in want`. A focus on "Quantum Robotics" therefore matched every row
    whose industry cell said "Robotics", because "robotics" is inside "quantum
    robotics". The focus was NARROWER than the cell and the match made it WIDER.

    Caught by `verify_approvals.py`, which expected the no-match fallback and
    got two matching rows instead.

    THE RULE: the cell must be at least as specific as the focus, never less.
    """

    def _row(self, **kw):
        base = {"company": "", "industry": "", "designation": "", "based": ""}
        base.update(kw)
        return base

    def test_narrow_focus_does_not_match_broad_cell(self):
        import focus
        assert focus.matches(self._row(industry="Robotics"), "Quantum Robotics") == ""

    def test_broad_focus_does_match_specific_cell(self):
        import focus
        assert focus.matches(
            self._row(industry="AI voice agents (conversational)"), "AI Voice Agents"
        ) == "industry"

    def test_exact_match(self):
        import focus
        assert focus.matches(self._row(industry="AI Voice Agents"), "AI Voice Agents") \
            == "industry"

    def test_word_order_differs(self):
        """"founders" against "Co-Founder / CEO" — the all-words path."""
        import focus
        assert focus.matches(self._row(designation="Co-Founder / CEO"), "founder") \
            == "designation"

    def test_unrelated(self):
        import focus
        assert focus.matches(self._row(industry="Fintech"), "AI Voice Agents") == ""

    def test_apply_to_reports_the_no_match_fallback(self):
        """The fallback must SAY it matched nothing — a silent no-match reads
        exactly like a quiet week."""
        import focus
        rows = [self._row(company="Borealis", industry="Robotics")]
        ordered, note = focus.apply_to(rows, {"value": "Quantum Robotics"})
        assert len(ordered) == 1
        assert "Nothing on the sheet matches" in note
        assert "sheet order" in note


# --------------------------------------------------------------------------
# 2. activation.row_key() — three contacts at one company
# --------------------------------------------------------------------------

class TestRowKeyDistinctPerContact:
    """THE BUG: `row_key` read only the RETIRED `poc` role. On rows parsed from
    a sheet the compatibility shim filled it, so it worked; on any row built
    without the shim it produced "company|" — THE SAME KEY FOR EVERY CONTACT AT
    ONE COMPANY. Three colleagues at one account collapsed into one identity and
    the dedup silently dropped two of them.

    Caught by `nextaction.py`'s own self-test, where three Acme contacts came
    back as one.

    THE RULE: read the canonical `name` first, the retired `poc` second.
    """

    def test_three_contacts_at_one_company_have_three_keys(self, pocs_tab):
        import activation
        keys = {activation.row_key(r) for r in pocs_tab.rows}
        assert len(pocs_tab.rows) == 3
        assert len(keys) == 3, f"expected 3 distinct keys, got {keys}"

    def test_the_key_uses_the_canonical_name_role(self):
        import activation
        # No `poc` anywhere — only the canonical `name`.
        row = {"company": "Wispr Flow", "name": "Sahaj Garg"}
        assert activation.row_key(row) == "wispr flow|sahaj garg"

    def test_the_retired_spelling_still_resolves(self):
        import activation
        row = {"company": "Wispr Flow", "poc": "Sahaj Garg"}
        assert activation.row_key(row) == "wispr flow|sahaj garg"

    def test_a_company_with_no_contact_is_not_a_collision_source(self):
        import activation
        a = activation.row_key({"company": "Wispr Flow", "name": "A"})
        b = activation.row_key({"company": "Wispr Flow", "name": "B"})
        assert a != b


# --------------------------------------------------------------------------
# 3. the terminal-words gate on a stored proposal approved with a bare "yes"
# --------------------------------------------------------------------------

class TestTerminalWordsSurviveApproval:
    """THE RISK: `said_terminal_words` is matched against WHAT THE HUMAN WROTE,
    deliberately — Dead and Unresponsive stop a row permanently and the bot
    never infers one. Once a write waits behind a yes, "yes" is the message in
    hand and it contains NO terminal word at all.

    Re-deriving the plan from the approval would silently disarm the one gate
    that stops a row being killed by inference. The plan is therefore computed
    ONCE from the original message and stored whole, with the original text
    beside it.
    """

    def test_a_bare_yes_contains_no_terminal_word(self):
        import sheetwrite
        assert sheetwrite.said_terminal_words("yes") == ""
        assert sheetwrite.said_terminal_words("yes please") == ""
        assert sheetwrite.said_terminal_words("go ahead") == ""

    def test_the_original_text_does(self):
        import sheetwrite
        assert sheetwrite.said_terminal_words("call it, they are dead") != ""

    def test_the_proposal_stores_the_original_text(self, db):
        """The round trip: the terminal word is still checkable after approval."""
        import sheetwrite
        db.open_proposal(
            proposal_key="p1", kind="cell_update", tab="Outreach PoCs", sheet_row=2,
            row_key="acme|ann", company="Acme", poc="Ann",
            payload={"writes": {"prospect_status": "Dead"}},
            reply_text="call it, they are dead", trigger="reply",
            proposed_text="Shall I set Prospect Status to Dead for Ann (Acme)?",
            requested_by="Trishi", channel_id=1, message_id="m1",
            created_at="2026-09-21T14:00:00",
        )
        stored = db.proposal("p1")
        assert stored["reply_text"] == "call it, they are dead"
        assert sheetwrite.said_terminal_words(stored["reply_text"]) != ""

    def test_planning_from_the_approval_would_have_lost_it(self, pocs_tab):
        """The counterfactual, pinned: re-planning with "yes" as the reply text
        refuses the terminal write. This is what the stored text prevents."""
        import sheetwrite
        row = pocs_tab.rows[0]
        fields = [{"role": "prospect_status", "value": "Dead", "supersedes": True}]
        replanned = sheetwrite.plan_writes(
            tab=pocs_tab, row=row, fields=fields,
            trigger=sheetwrite.TRIGGER_COMMAND, reply_text="yes",
        )
        assert replanned["writes"] == {}, (
            "re-planning from the approval must NOT produce a terminal write"
        )


# --------------------------------------------------------------------------
# 4. bare-URL extraction
# --------------------------------------------------------------------------

class TestLinkExtraction:
    """THE BUG: the bare-URL scan used a lookbehind `(?<![(<])` to avoid
    double-capturing markdown links. It also refused every URL preceded by "(" —
    which is every link written as plain prose parentheses, "the Series B page
    (https://example.com/post)". R11's live run found ONE source where the text
    had TWO, and the link list was silently halved.

    Caught by `verify_websearch.py` against the live API.

    THE RULE: strip markdown links first, then scan the remainder.
    """

    def test_a_parenthesised_url_is_found(self):
        import websearch
        got = websearch.links_in_text(
            "the Series B page (https://wisprflow.ai/post/series-b) says so"
        )
        assert [s["url"] for s in got] == ["https://wisprflow.ai/post/series-b"]

    def test_a_markdown_link_is_found_with_its_title(self):
        import websearch
        got = websearch.links_in_text("see [Intros](https://startupintros.com/orgs/x)")
        assert got == [{"url": "https://startupintros.com/orgs/x",
                        "title": "Intros", "quote": ""}]

    def test_all_three_shapes_together(self):
        import websearch
        got = websearch.links_in_text(
            "the Series B page (https://a.example/post) and "
            "[Intros](https://b.example/orgs) plus https://c.example/a."
        )
        assert [s["url"] for s in got] == [
            "https://b.example/orgs",      # markdown first
            "https://a.example/post",
            "https://c.example/a",
        ]

    def test_a_trailing_stop_is_not_part_of_the_url(self):
        import websearch
        got = websearch.links_in_text("at https://example.com/a.")
        assert got[0]["url"] == "https://example.com/a"

    def test_a_markdown_url_is_not_captured_twice(self):
        import websearch
        got = websearch.links_in_text("[A](https://a.example) and [A again](https://a.example)")
        assert len(got) == 1


# --------------------------------------------------------------------------
# 5. the posting window
# --------------------------------------------------------------------------

class TestPostingWindow:
    """THE BUG: `slot_times` had no upper bound. Six posts at 90-minute gaps
    from 14:00 ran to 21:13 — the daily cap kept the COUNT down and nothing kept
    the last one out of somebody's evening.

    Caught by `verify_monday.py`, which printed the 21:13 and had to flag it.

    THE RULE: the gap shrinks evenly to fit SALES_DRIP_START..SALES_DRIP_END,
    down to MESSAGE_GAP_MIN_MINUTES; the rest rolls.
    """

    MONDAY = date(2026, 9, 21)

    def _end_minutes(self):
        import config
        h, m = config.drip_end_ist()
        return h * 60 + m

    def test_six_posts_from_1400_all_land_by_1830(self):
        import drip
        times = drip.slot_times(self.MONDAY, count=6)
        assert len(times) == 6
        latest = max(t.hour * 60 + t.minute for t in times)
        assert latest <= self._end_minutes(), (
            "six posts must fit the window; latest was "
            + max(times).strftime("%H:%M")
        )

    @pytest.mark.parametrize("count", [1, 2, 3, 4, 5, 6, 8, 12, 25])
    def test_no_post_ever_lands_after_the_window(self, count):
        import drip
        times = drip.slot_times(self.MONDAY, count=count)
        assert all(
            (t.hour * 60 + t.minute) <= self._end_minutes() for t in times
        ), f"{count} posts overran the window"

    def test_the_first_post_lands_exactly_on_the_start(self):
        import config
        import drip
        assert drip.slot_times(self.MONDAY, count=6)[0].strftime("%H:%M") \
            == config.SALES_DRIP_START

    def test_a_light_day_keeps_the_full_gap(self):
        import config
        import drip
        gap, _fits = drip.fitted_gap(3)
        assert gap == config.MESSAGE_GAP_MINUTES

    def test_a_heavy_day_shrinks_the_gap_but_not_below_the_floor(self):
        import config
        import drip
        gap, _fits = drip.fitted_gap(10)
        assert gap < config.MESSAGE_GAP_MINUTES
        assert gap >= config.MESSAGE_GAP_MIN_MINUTES

    def test_an_impossible_day_reports_what_fits(self):
        import drip
        _gap, fits = drip.fitted_gap(40)
        assert 0 < fits < 40

    def test_jitter_never_pushes_a_gap_below_the_floor(self):
        """THE BUG: the fitted gap respected MESSAGE_GAP_MIN_MINUTES but the
        per-slot jitter was then ADDED to it unclamped. On a compressed day a
        42-minute gap minus 15 minutes of jitter is 27 — below the 30 the floor
        promised. Caught by `verify_monday.py`, whose spacing check failed.

        THE RULE: the STEP is clamped, not the jitter, so the schedule stays
        deterministic and simply cannot place two posts too close together.
        """
        import config
        import drip
        for count in (4, 5, 6, 7, 8, 10, 12):
            # Only the slots that FIT are spaced — `plan` caps at this count and
            # rolls the rest, and slots past it are clamped to the window end on
            # purpose (see `slot_times`).
            _gap, fits = drip.fitted_gap(count)
            times = drip.slot_times(self.MONDAY, count=count)[:fits]
            gaps = [
                int((b - a).total_seconds() // 60) for a, b in zip(times, times[1:])
            ]
            assert all(g >= config.MESSAGE_GAP_MIN_MINUTES for g in gaps), (
                f"{count} posts ({fits} fitting) produced gaps {gaps}, floor is "
                f"{config.MESSAGE_GAP_MIN_MINUTES}"
            )

    def test_slots_past_what_fits_are_never_assigned(self):
        """The degenerate tail must be unreachable through `plan`."""
        import drip
        _gap, fits = drip.fitted_gap(12)
        assert fits < 12
        items = [
            {"rule": "x", "rule_id": f"R{i}", "type": "x", "rule_name": "x",
             "label": "x", "owner": f"O{i}", "priority": 2, "priority_label": "p",
             "due_date": self.MONDAY, "due_iso": "2026-09-21", "overdue_days": 0,
             "company": f"C{i}", "poc": "", "poc_designation": "", "sheet_row": 2,
             "row_key": f"c{i}|", "contact_key": f"c{i}|",
             "max_items_per_post": 5, "counts_toward_cap": False,
             "destination": "channel", "web_pending": False, "why": "x",
             "text": "x", "key": f"R{i}:c{i}"}
            for i in range(12)
        ]
        planned = drip.plan(items, day=self.MONDAY)
        assert len(planned["messages"]) <= fits
        assert len(planned["rolled"]) >= 12 - fits

    def test_gaps_are_deterministic_across_calls(self):
        """The restart guard: the same day must recompute identically."""
        import drip
        assert drip.slot_times(self.MONDAY, count=6) == \
            drip.slot_times(self.MONDAY, count=6)


# --------------------------------------------------------------------------
# 6. append_row — the duplicate refusal
# --------------------------------------------------------------------------

class TestAppendDuplicateRefusal:
    """A row appended for a company already on the tab puts a second row into a
    sheet people read as one-row-per-account. The check is normalised the same
    way the news-screen matcher is, so "Wispr Flow", "wispr flow" and
    "Wispr  Flow." are one company.
    """

    def test_an_exact_duplicate_is_found(self, pipeline_tab):
        import gtm_sheet
        dup = gtm_sheet.SHEETS.find_duplicate(pipeline_tab, company="Wispr Flow")
        assert dup is not None

    def test_case_and_spacing_do_not_hide_a_duplicate(self, pipeline_tab):
        import gtm_sheet
        for spelling in ("wispr flow", "WISPR  FLOW", "Wispr Flow."):
            assert gtm_sheet.SHEETS.find_duplicate(
                pipeline_tab, company=spelling
            ) is not None, f"{spelling!r} should have matched"

    def test_a_genuinely_new_company_is_not_a_duplicate(self, pipeline_tab):
        import gtm_sheet
        assert gtm_sheet.SHEETS.find_duplicate(
            pipeline_tab, company="Nova Labs"
        ) is None

    def test_append_refuses_and_says_so(self, pipeline_tab):
        import gtm_sheet
        out = gtm_sheet.SHEETS.append_row(
            pipeline_tab, {"company": "wispr flow"}, reason="test", dry_run=True,
        )
        assert out["ok"] is False
        assert out["duplicate"] is not None
        assert "already on" in out["error"]

    def test_outreach_pocs_keys_on_company_and_person(self, pocs_tab):
        """Several rows per company is NORMAL there; only the same PERSON at the
        same company is a duplicate."""
        import gtm_sheet
        assert gtm_sheet.SHEETS.find_duplicate(
            pocs_tab, company="Wispr Flow", poc="Sahaj Garg"
        ) is not None
        assert gtm_sheet.SHEETS.find_duplicate(
            pocs_tab, company="Wispr Flow", poc="Someone New"
        ) is None

    def test_the_commercial_block_is_refused_on_a_new_row(self, pocs_tab):
        """A:I is writable on a NEW row; S-X never is, on any row."""
        import gtm_sheet
        out = gtm_sheet.SHEETS.append_row(
            pocs_tab,
            {"company": "Nova Labs", "name": "Someone", "closure_prob": "80%"},
            reason="test", dry_run=True,
        )
        refused = {r["role"] for r in out.get("refused", [])}
        assert "closure_prob" in refused
        written = {c["role"] for c in out.get("written", [])}
        assert "company" in written and "closure_prob" not in written

    def test_sr_no_is_max_plus_one_not_count_plus_one(self, pipeline_tab):
        import gtm_sheet
        assert gtm_sheet.SHEETS._next_sr_no(pipeline_tab) == "3"


# --------------------------------------------------------------------------
# 7. the proposal sweep — nudged once, then dropped
# --------------------------------------------------------------------------

class TestProposalSweep:
    """One nudge, then dropped. Two nudges is nagging; a proposal that never
    expires is a queue of half-decisions nobody can see the end of.
    """

    def _open(self, db, key, created):
        db.open_proposal(
            proposal_key=key, kind="cell_update", tab="Outreach PoCs", sheet_row=2,
            row_key=f"{key}|x", company=key.title(), poc="",
            payload={"writes": {"meeting_date": "24-09-2026"}},
            reply_text="met them", trigger="reply",
            proposed_text=f"Shall I set Meeting Date for {key.title()}? Reply yes.",
            requested_by="Trishi", channel_id=1, message_id=f"m-{key}",
            created_at=created,
        )

    def test_a_fresh_proposal_is_not_nudged(self, db):
        self._open(db, "acme", "2026-09-22T14:00:00")
        due = db.stale_proposals(before_iso="2026-09-21", nudged=False)
        assert due == []

    def test_an_old_proposal_is_due_a_nudge(self, db):
        self._open(db, "acme", "2026-09-18T14:00:00")
        due = db.stale_proposals(before_iso="2026-09-21", nudged=False)
        assert [p["proposal_key"] for p in due] == ["acme"]

    def test_it_is_nudged_exactly_once(self, db):
        self._open(db, "acme", "2026-09-18T14:00:00")
        assert len(db.stale_proposals(before_iso="2026-09-21", nudged=False)) == 1
        db.mark_proposal_nudged("acme", on_date="2026-09-21")
        assert db.stale_proposals(before_iso="2026-09-21", nudged=False) == []

    def test_after_the_nudge_it_becomes_droppable(self, db):
        self._open(db, "acme", "2026-09-18T14:00:00")
        db.mark_proposal_nudged("acme", on_date="2026-09-21")
        droppable = db.stale_proposals(before_iso="2026-09-21", nudged=True)
        assert [p["proposal_key"] for p in droppable] == ["acme"]

    def test_a_dropped_proposal_is_never_mentioned_again(self, db):
        self._open(db, "acme", "2026-09-18T14:00:00")
        db.mark_proposal_nudged("acme", on_date="2026-09-21")
        db.close_proposal(proposal_key="acme", status="expired",
                          decision="nobody answered", decided_by="",
                          decided_at="2026-09-22T14:00:00")
        assert db.stale_proposals(before_iso="2026-09-30", nudged=True) == []
        assert db.stale_proposals(before_iso="2026-09-30", nudged=False) == []
        assert db.proposal("acme")["status"] == "expired"

    def test_an_answered_proposal_is_never_swept(self, db):
        self._open(db, "acme", "2026-09-18T14:00:00")
        db.close_proposal(proposal_key="acme", status="applied", decision="Sid said yes",
                          decided_by="Sid", decided_at="2026-09-19T10:00:00")
        assert db.stale_proposals(before_iso="2026-09-30", nudged=False) == []

    def test_one_combined_message_however_many_are_pending(self, db):
        import approvals
        for name in ("acme", "borealis", "cinder"):
            self._open(db, name, "2026-09-18T14:00:00")
        pending = db.stale_proposals(before_iso="2026-09-21", nudged=False)
        text = approvals.pending_text(pending)
        assert text.count("\n  - ") == 3
        assert text.startswith("A few things still waiting for a yes:")

    def test_no_message_when_nothing_is_pending(self):
        import approvals
        assert approvals.pending_text([]) == ""

    def test_a_timestamp_is_compared_against_a_date_not_a_string(self, db):
        """THE BUG: `created_at` is a full ISO TIMESTAMP
        ("2026-09-18T14:00:00") and `before_iso` is a bare DATE
        ("2026-09-18"). Compared as strings the timestamp is the LONGER value,
        so "2026-09-18T14:00:00" <= "2026-09-18" is FALSE — a proposal created
        on the cutoff day never qualified and the sweep silently found nothing,
        for ever.

        Caught by `verify_heavy_monday.py`, where the combined message came back
        empty with two proposals sitting in the table.
        """
        self._open(db, "acme", "2026-09-18T14:00:00")
        due = db.stale_proposals(before_iso="2026-09-18", nudged=False)
        assert [p["proposal_key"] for p in due] == ["acme"], (
            "a proposal created ON the cutoff day must qualify"
        )

    def test_midnight_ist_counts_as_today_not_yesterday(self, db):
        """THE DAY-BOUNDARY CASE. A proposal made at 00:30 IST was made at
        19:00 UTC THE PREVIOUS DAY. Taking the date off the front of the stored
        string reads it as yesterday's whenever the writer stored UTC — a 5.5
        hour window either side of midnight where "which day is this?" has two
        answers.

        `dl.ist_date_of` converts to IST before taking the date, so the SAME
        INSTANT written three different ways all land on the same IST day.
        """
        import deadlines as dl

        ist_midnight = date(2026, 9, 22)
        # The same moment, three ways.
        for label, stamp in (
            ("IST with offset", "2026-09-22T00:30:00+05:30"),
            ("UTC with offset", "2026-09-21T19:00:00+00:00"),
            ("UTC with a Z", "2026-09-21T19:00:00Z"),
        ):
            assert dl.ist_date_of(stamp) == ist_midnight, (
                f"{label} ({stamp}) should be {ist_midnight} in IST"
            )

    def test_a_proposal_made_at_0030_ist_is_swept_as_today(self, db):
        """The same case, through the sweep itself and stored as UTC — the
        shape `substr` would have got wrong."""
        self._open(db, "acme", "2026-09-21T19:00:00+00:00")   # 00:30 IST on the 22nd
        # Cutoff = the 22nd. A proposal made ON the 22nd (IST) must qualify.
        due = db.stale_proposals(before_iso="2026-09-22", nudged=False)
        assert [p["proposal_key"] for p in due] == ["acme"], (
            "00:30 IST on the 22nd must count as the 22nd, not the 21st"
        )

    def test_the_same_proposal_does_not_qualify_against_the_previous_day(self, db):
        """The other half: it is the 22nd, so a cutoff of the 21st excludes it.
        Without the conversion the UTC date (the 21st) would have matched."""
        self._open(db, "acme", "2026-09-21T19:00:00+00:00")
        assert db.stale_proposals(before_iso="2026-09-21", nudged=False) == []

    def test_late_evening_ist_does_not_leak_into_tomorrow(self, db):
        """The mirror boundary: 23:59 IST is 18:29 UTC the SAME day, so both
        conventions agree — but the conversion must not move it forward."""
        import deadlines as dl
        assert dl.ist_date_of("2026-09-22T23:59:59+05:30") == date(2026, 9, 22)
        assert dl.ist_date_of("2026-09-22T18:29:59+00:00") == date(2026, 9, 22)

    def test_a_naive_timestamp_is_read_as_ist(self, db):
        """Every timestamp this bot writes comes from `now_ist()` and carries an
        offset; a naive one is legacy or hand-written. Reading it as UTC would
        move it 5.5 hours backwards — the exact error being fixed."""
        import deadlines as dl
        assert dl.ist_date_of("2026-09-22T00:30:00") == date(2026, 9, 22)

    def test_an_unreadable_created_at_is_skipped_not_dropped(self, db):
        """A malformed row must not be swept on a guess — it is skipped and
        logged, so a bad timestamp costs one stuck proposal rather than a
        proposal dropped for the wrong reason."""
        self._open(db, "acme", "not a timestamp")
        assert db.stale_proposals(before_iso="2026-09-30", nudged=False) == []
        assert db.proposal("acme")["status"] == "open"

    def test_an_unreadable_cutoff_sweeps_nothing(self, db):
        self._open(db, "acme", "2026-09-18T14:00:00+05:30")
        assert db.stale_proposals(before_iso="rubbish", nudged=False) == []

    def test_a_proposal_created_after_the_cutoff_does_not_qualify(self, db):
        self._open(db, "acme", "2026-09-19T09:00:00")
        assert db.stale_proposals(before_iso="2026-09-18", nudged=False) == []

    def test_working_days_not_calendar_days(self):
        """A proposal made Friday afternoon is not stale on Monday morning."""
        import deadlines as dl
        monday = date(2026, 9, 21)
        assert dl.subtract_working_days(monday, 1) == date(2026, 9, 18)
