"""THE REAL CONFIGURATION, WITHOUT THE SECRETS.

    python tools/redact_env.py

Writes `.env.agent` beside `.env`: every setting the bot actually runs with on
this machine, with every secret replaced by `<set, hidden>` or `<empty>`.

WHY IT EXISTS. `.env` holds the Discord token and the Anthropic key, so no
agent may read it — and yet a plan or a test written against `.env.example`
alone is written against defaults the bot may not be running with. On 6 Oct the
notes sync was failing because of a value only `.env` knew. This file is how an
agent sees that value without seeing the token on the line above it.

PARSED AS config.py PARSES IT — `dotenv.dotenv_values`, so Windows line endings,
quotes and inline comments come out exactly as the bot reads them. A line that
is broken in `.env` is broken here in the same way, which is the point.

WHAT COUNTS AS A SECRET, any one of:
  * the key is in SECRET_KEYS;
  * the key ENDS in _TOKEN, _KEY, _SECRET, _PASSWORD or _CREDENTIALS. A key
    that merely STARTS with TOKEN_ is not one: TOKEN_DAILY_BUDGET is a number;
  * the VALUE looks like a credential whatever it is called — an Anthropic key,
    a three-part Discord token, a PEM block — or has a secret's `NAME=` inside
    it, which is what two lines run together by a missing newline look like.

"READ BY THE CODE" MEANS "LISTED IN .env.example". That file is the contract
(tests/test_env_example.py fails the build when it falls behind), so it is the
one list of what the code reads that cannot be stale.

PRINTS COUNTS AND THE PATH, NEVER A VALUE. `.env.agent` is gitignored by the
`.env.*` rule.
"""
import os
import re
import sys
from datetime import datetime

from dotenv import dotenv_values

SECRET_KEYS = (
    "DISCORD_TOKEN",
    "ANTHROPIC_API_KEY",
    "GOOGLE_CSE_KEY",
    "LINKEDIN_API_ACCESS_TOKEN",
    "LINKEDIN_API_CLIENT_SECRET",
)
SECRET_SUFFIXES = ("_TOKEN", "_KEY", "_SECRET", "_PASSWORD", "_CREDENTIALS")

HIDDEN = "<set, hidden>"
EMPTY = "<empty>"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV = os.path.join(ROOT, ".env")
EXAMPLE = os.path.join(ROOT, ".env.example")
OUT = os.path.join(ROOT, ".env.agent")

# header.payload.signature, as a Discord bot token is laid out.
_DISCORD_RE = re.compile(r"^[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{20,}$")
# A secret's own `NAME=` inside another key's value: two lines run together.
_EMBEDDED_RE = re.compile(
    r"(?:[A-Z][A-Z0-9_]*(?:" + "|".join(SECRET_SUFFIXES) + r")|"
    + "|".join(SECRET_KEYS) + r")\s*=")


def secret_key(key: str) -> bool:
    key = str(key or "").strip().upper()
    return key in SECRET_KEYS or key.endswith(SECRET_SUFFIXES)


def secret_value(value: str) -> bool:
    text = str(value or "").strip().strip("'\"")
    return (text.startswith("sk-ant-") or "sk-ant-" in text
            or "-----BEGIN" in text
            or bool(_DISCORD_RE.match(text))
            or bool(_EMBEDDED_RE.search(text)))


def shown(key: str, value, *, placeholder_ok: bool = False) -> tuple:
    """(what to write, was it masked). `placeholder_ok` lets `.env.example`'s
    own `<REQUIRED: …>` through for a secret key — it is an instruction to a
    human, not a credential."""
    text = "" if value is None else str(value)
    if placeholder_ok and text.strip().startswith("<") and not secret_value(text):
        return text, False
    if secret_key(key) or secret_value(text):
        return (HIDDEN if text.strip() else EMPTY), True
    return text, False


def main() -> int:
    if not os.path.isfile(ENV):
        print("redact_env: no .env here — nothing to redact", file=sys.stderr)
        return 1
    real = dict(dotenv_values(ENV) or {})
    example = dict(dotenv_values(EXAMPLE) or {}) if os.path.isfile(EXAMPLE) else {}

    stamp = datetime.now().astimezone().isoformat(timespec="seconds")
    lines = [
        f"# .env.agent — generated {stamp}",
        "# generated from .env, do not edit; re-run python tools/redact_env.py",
        f"# Secrets are written as {HIDDEN} or {EMPTY}. Everything else is the",
        "# value the bot runs with on this machine.",
        "",
        "# === SET IN .env ===",
    ]
    masked = 0
    for key, value in real.items():
        text, hidden = shown(key, value)
        masked += 1 if hidden else 0
        lines.append(f"{key}={text}")

    defaults = [k for k in example if k not in real]
    lines += ["", "# === NOT IN .env, the .env.example default applies ==="]
    for key in defaults:
        text, hidden = shown(key, example[key], placeholder_ok=True)
        masked += 1 if hidden else 0
        lines.append(f"{key}={text}")

    unread = [k for k in real if k not in example]
    lines += ["", "# === IN .env BUT NOT READ BY THE CODE (names only) ==="]
    lines += [f"# {key}" for key in unread]

    with open(OUT, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines) + "\n")

    print(f"set in .env:                 {len(real)}")
    print(f"  of all lines, masked:      {masked}")
    print(f"default from .env.example:   {len(defaults)}")
    print(f"in .env but not read:        {len(unread)}")
    print(f"written: {os.path.relpath(OUT, ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
