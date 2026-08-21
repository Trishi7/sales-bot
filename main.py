"""Entry point for the sales & marketing bot.

Validates the environment, says out loud what the bot is scoped to and what it
can see, then connects. The startup log is deliberately explicit about SCOPE and
SOURCES: the two things most likely to be misconfigured are a channel id that the
bot's role can't actually see and a source everyone assumes is connected. Both
are silent failures at runtime, so they get named here.
"""
import logging
import sys

import config
import persona
import sources
from bot import SalesBot

log = logging.getLogger(__name__)


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
        config.SALES_ASK_CHANNEL_ID or "(none — @-mention required everywhere)",
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
    main()
