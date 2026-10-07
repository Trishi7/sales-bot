"""THE TO-DO ROWS THE BOT WILL NOT SHOW, listed for a human to clean up.

    python tools/list_hidden_todos.py

WHY IT EXISTS. A row on the to-do sheet is shown only when its "Source meeting"
is a sales note the bot can read (todos.split_visible). Every other row — one
that cites an AM/PM product standup, an internal meeting, or nothing — is
hidden from every reply, and the bot says NOTHING about it in the channel. It
also never edits or deletes such a row. So the sheet keeps them, nobody is
told, and this script is the one place they are listed: sheet row, #, To-do,
Owner, Source meeting, Date raised, Status and the reason, as a Markdown table
that can be pasted into a ticket.

READ-ONLY, ALL THE WAY DOWN. It reads the sheet through `todos.read_rows` and
sorts the rows with `todos.split_visible` — ALL rows, open and closed. It never
calls `todos.ensure` (which would create the sheet), never writes a cell, never
runs the notes sync, and opens the bot's database read-only to find the stored
sheet id. It applies the SAME rule the bot applies, against the notes readable
on THIS machine right now: run it on the server to see what the server hides.

PRINTS ROWS, NEVER A KEY. Configuration is read only through config.py.

Exit 0 always — including "the sheet has not been created yet" — unless the
sheet exists and could not be read, which prints the error and exits 1.
"""
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import config  # noqa: E402
import notes  # noqa: E402
import todos  # noqa: E402

NOT_CREATED = "The to-do sheet has not been created yet: 0 rows."

COLUMNS = (
    ("Sheet row", "row"), ("#", "n"), ("To-do", "task"), ("Owner", "owner"),
    ("Source meeting", "source_meeting"), ("Date raised", "date_raised"),
    ("Status", "status"), ("Reason", "reason"),
)


class _StoredId:
    """`get_meta`, and nothing else, over the bot's database opened READ-ONLY.

    `todos.sheet_id` needs only that one method. Building the bot's own DB
    object would run its schema script against the file — a write, and on a
    machine with no database yet, a new file. A missing or unreadable database
    simply means "no stored id".
    """

    def __init__(self, path: str) -> None:
        self.path = path

    def get_meta(self, key: str):
        if not self.path or not os.path.isfile(self.path):
            return None
        uri = "file:" + os.path.abspath(self.path).replace("\\", "/") + "?mode=ro"
        try:
            conn = sqlite3.connect(uri, uri=True)
        except sqlite3.Error:
            return None
        try:
            row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
            return row[0] if row else None
        except sqlite3.Error:
            return None
        finally:
            conn.close()


def _cell(value) -> str:
    """One table cell: a pipe or a line break in a to-do must not break the row."""
    return " ".join(str(value if value is not None else "").split()).replace("|", "\\|")


def render(hidden: list, total: int) -> str:
    """The Markdown the script prints for `hidden` out of `total` rows."""
    if not hidden:
        return f"No hidden rows: {total} row(s) on the to-do sheet, all shown."
    lines = [
        f"{len(hidden)} of {total} row(s) on the to-do sheet are hidden from the "
        "bot's replies. Nothing was changed.",
        "",
        "| " + " | ".join(title for title, _ in COLUMNS) + " |",
        "|" + "|".join(" --- " for _ in COLUMNS) + "|",
    ]
    for r in sorted(hidden, key=lambda r: int(r.get("row") or 0)):
        lines.append("| " + " | ".join(_cell(r.get(key)) for _, key in COLUMNS) + " |")
    return "\n".join(lines)


def main(argv=None, *, db=None) -> int:
    """`db` is injectable so a check can hand in a fake; by default the stored
    sheet id is read from DB_PATH, read-only."""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    if db is None:
        db = _StoredId(config.DB_PATH)

    if not todos.sheet_id(db):
        print(NOT_CREATED)
        return 0

    data = todos.read_rows(db)
    if not data.get("ok"):
        print(f"The to-do sheet could not be read: {data.get('error')} "
              f"{data.get('remedy') or ''}".strip())
        return 1

    rows = data.get("rows") or []
    _, hidden = todos.split_visible(rows)
    which = "TODO_SHEET_ID" if config.TODO_SHEET_ID else "the id stored in the bot's database"
    print(f"Sheet: {config.TODO_SHEET_TITLE} (found through {which}).")
    print(f"Notes source on this machine: {notes.source_state()}. A row is shown only "
          "when its Source meeting is a sales note readable here.")
    print()
    print(render(hidden, len(rows)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
