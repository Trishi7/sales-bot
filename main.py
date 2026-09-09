"""Entry point for the sales & marketing bot.

Validates the environment, says out loud what the bot is scoped to and what it
can see, then connects. The startup log is deliberately explicit about SCOPE and
SOURCES: the two things most likely to be misconfigured are a channel id that the
bot's role can't actually see and a source everyone assumes is connected. Both
are silent failures at runtime, so they get named here.

    python -m main                              run the bot
    python -m main --dry-run-drip               plan TODAY's drip, send nothing
    python -m main --dry-run-drip 2026-09-07    plan that day's drip

THE DRY RUN EXISTS TO BE CHECKED BEFORE A DEPLOY. It answers the question the
volume contract is written in terms of: how many messages would go out, to whom,
about what, at what times, and does the spacing hold. `--dry-run-digest` is kept
as an alias for the same thing — the digest it named is retired.

It connects to nothing, sends nothing and creates nothing.
"""
import asyncio
import logging
import sys
from datetime import date

import config
import deadlines as dl
import persona
import sources
from bot import SalesBot

log = logging.getLogger(__name__)


def _dry_run_drip(argv: list[str]) -> int:
    """Plan one day's drip and print it. Sends nothing, writes nothing.

    The date is optional and is parsed strictly: a typo must not silently become
    "today", because the whole point is to look at a SPECIFIC day (a Monday, a
    Friday, a Saturday to prove it stays quiet) and a plan for the wrong day
    would answer a question nobody asked.
    """
    logging.basicConfig(
        level=getattr(logging, config.LOG_LEVEL, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("discord").setLevel(logging.WARNING)

    day = dl.today_ist()
    if argv:
        try:
            day = date.fromisoformat(argv[0])
        except ValueError:
            print(f"Not a date: {argv[0]!r}. Use YYYY-MM-DD.", file=sys.stderr)
            return 2

    log.info(
        "[dry-run] planning the drip for %s (%s). NOTHING will be sent, written or "
        "created.", dl.iso(day), day.strftime("%A"),
    )
    bot = SalesBot()
    body = asyncio.run(bot.dry_run_drip(today=day))

    print()
    print("=" * 78)
    print(
        f"DRY-RUN DRIP — {day.strftime('%A %d %b %Y')} — up to "
        f"{config.DAILY_MESSAGE_CAP} message(s), one per (type x owner)"
    )
    print("=" * 78)
    print(body)
    print("=" * 78)
    return 0


def main() -> None:
    logging.basicConfig(
        level=getattr(logging, config.LOG_LEVEL, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # Quiet the noisy third-party loggers so our own step-by-step lines are readable.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("discord").setLevel(logging.INFO)

    log.info("[main] starting %s (log level=%s)", config.COS_NAME, config.LOG_LEVEL)

    missing = config.validate()
    if missing:
        log.error("[main] missing required environment variables: %s", ", ".join(missing))
        print(
            "Missing required environment variables: "
            + ", ".join(missing)
            + "\nCopy .env.example to .env and fill in the values.",
            file=sys.stderr,
        )
        sys.exit(2)

    log.info(
        "[main] config OK. sales_channels=%s ask_channel=%s roster=%d model=%s db=%s state=%s",
        config.SALES_CHANNEL_IDS,
        config.SALES_ASK_CHANNEL_ID or "(none — digest falls back to the first channel)",
        len(config.TEAM_ROSTER_IDS),
        config.MODEL,
        config.DB_PATH,
        config.STATE_DIR,
    )
    log.info(
        "[main] SCOPE: this bot reads and posts ONLY in the %d channel(s) above. It "
        "never DMs anyone and only @-mentions the %d person(s) on the roster. Server-side, "
        "its Discord role should also be denied View Channel everywhere else (see DEPLOY.md).",
        len(config.SALES_CHANNEL_IDS), len(config.TEAM_ROSTER_IDS),
    )
    log.info(
        "[main] ANSWERING: only when @-mentioned, or when someone replies to one of the "
        "bot's own messages — in EVERY sales channel, the ask channel included. Messages "
        "tagging anyone else are never answered."
    )

    for src in sources.status_report():
        log.info("[main] source %-20s %-16s %s", src["key"], src["status"], src["detail"])

    pol = persona.policy_status()
    if pol["loaded"]:
        log.info("[main] policy loaded from %s (%d chars); re-read on every question", pol["path"], pol["chars"])
    else:
        log.warning(
            "[main] NO policy file at %r — the bot will run on its persona and source "
            "statuses alone and will say so when asked what it enforces.",
            pol["path"],
        )

    log.info("[main] connecting to the Discord gateway...")
    bot = SalesBot()
    bot.run(config.DISCORD_TOKEN, log_handler=None)


if __name__ == "__main__":
    # --dry-run-digest is kept as an alias: the digest it named is retired, but
    # an operator's muscle memory and any deploy script that used it should not
    # break on a rename.
    for flag in ("--dry-run-drip", "--dry-run-digest"):
        if flag in sys.argv:
            i = sys.argv.index(flag)
            sys.exit(_dry_run_drip(sys.argv[i + 1:]))
    main()
