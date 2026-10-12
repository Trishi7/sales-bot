"""R6 — THE THREE CHEAP CHECKS ON AN EMAIL ADDRESS. `python -m email_checks` runs the self-test.

There is no free way to confirm that a mailbox exists. The only real test is
sending to it; an SMTP probe is answered ambiguously, by design, by Gmail,
Outlook and most providers, and is a reliable way to get the server
blocklisted. SO THERE IS NO SMTP PROBE HERE AND NO PAID VERIFICATION API, and
nothing built on this module ever claims an address exists. Three checks, no
more:

  1. IS IT AN ADDRESS AT ALL (`addresses_in`): syntax. "not available", a
     dash or a phone number is not an address, and a cell holding an address
     AND a phone number yields the address.
  2. CAN THE DOMAIN RECEIVE MAIL (`mx_status`): one DNS lookup. Plain DNS, no
     API, no cost. THIS IS THE ONLY FAILURE ANYBODY IS EVER TOLD ABOUT, because
     it is the only one that can be proved.
  3. DOES IT MATCH THE COMPANY (`company_check`): the domain against the
     company's name. LOG ONLY. A personal address is not an error, and a
     company cell that is not a company name ("Multiple", a university, two
     organisations together) means the check is SKIPPED, never failed.

`addresses_in` and `company_check` are pure. `mx_status` is the one function
here that touches the network, and it is called by the bot, never by the
rules engine.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Optional

log = logging.getLogger("sales-bot.email")

_ADDRESS_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")

# Where a person's own mailbox lives. An address here says nothing about the
# company either way.
PERSONAL_DOMAINS = frozenset({
    "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com", "msn.com",
    "yahoo.com", "yahoo.co.in", "yahoo.co.uk", "icloud.com", "me.com", "mac.com",
    "proton.me", "protonmail.com", "pm.me", "aol.com", "zoho.com", "rediffmail.com",
    "hey.com", "fastmail.com", "gmx.com", "gmx.de",
})

# A COMPANY CELL THAT IS NOT A COMPANY NAME. The domain comparison cannot run
# against these, and "cannot run" is SKIPPED, never "does not match".
_NOT_A_COMPANY_RE = re.compile(
    r"\b(multiple|various|several|independent|freelance|self[- ]employed|n/?a|none|"
    r"university|universit[aäe]t|college|institute|institut|school|academy|iit|iisc|"
    r"consortium|foundation|government|ministry|lab(?:oratory)?s? of)\b|[/,;&+]|\band\b",
    re.IGNORECASE)

OK, DEAD, UNKNOWN, SKIPPED = "ok", "dead", "unknown", "skipped"
MATCH, MISMATCH, PERSONAL = "match", "mismatch", "personal"

_MX_CACHE: dict = {}
_MX_CACHE_SECONDS = 24 * 3600
_SAID_NO_DNS = [False]


def addresses_in(cell) -> list:
    """Every email address written in a cell, in order, each once.

    WHAT IS NOT AN ADDRESS IS IGNORED, not reported: "not available", "-", a
    phone number, a name. A cell holding two addresses (on two lines, or
    separated by a comma) gives both.
    """
    out: list = []
    for found in _ADDRESS_RE.findall(str(cell or "")):
        address = found.rstrip(".")
        if address.lower() not in [a.lower() for a in out]:
            out.append(address)
    return out


def domain_of(address: str) -> str:
    return str(address or "").rsplit("@", 1)[-1].strip().lower().rstrip(".")


def _squeeze(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(text or "").lower())


def company_check(address: str, company: str) -> str:
    """MATCH, MISMATCH, PERSONAL or SKIPPED. FOR THE LOG ONLY.

    SKIPPED when the company cell is not a company name (`_NOT_A_COMPANY_RE`)
    or is too short to compare: nobody is ever flagged for an address that
    does not match a company the bot cannot identify. MATCH when the company's
    name, squeezed, is in the domain's own label or the other way round
    ("PolyAI" and poly.ai, "Gnani.ai" and gnani.ai).
    """
    domain = domain_of(address)
    if not domain:
        return SKIPPED
    if domain in PERSONAL_DOMAINS:
        return PERSONAL
    name = " ".join(str(company or "").split())
    if not name or _NOT_A_COMPANY_RE.search(name):
        return SKIPPED
    want = _squeeze(re.sub(r"\.(ai|com|io|co|org|net|in)\b", "", name, flags=re.IGNORECASE))
    if len(want) < 3:
        return SKIPPED
    labels = domain.split(".")
    have = _squeeze("".join(labels[:-1])) or _squeeze(domain)
    whole = _squeeze(domain)
    if want in have or have in want or want in whole:
        return MATCH
    return MISMATCH


def have_dns() -> bool:
    """Is dnspython installed? Without it the MX check is SKIPPED."""
    try:
        import dns.resolver  # noqa: F401
        return True
    except Exception:
        return False


def mx_status(domain: str, *, timeout: float = 4.0) -> str:
    """Can this domain receive mail? OK, DEAD, UNKNOWN or SKIPPED.

    DEAD ONLY WHEN IT IS PROVED: the domain does not exist (NXDOMAIN), it
    publishes a null MX ("." — RFC 7505, "this domain accepts no mail"), or it
    has no MX record and no address record either. A domain with no MX and an
    A record is OK: mail falls back to the A record.

    UNKNOWN for anything else — a timeout, a resolver error, no nameserver.
    Unknown is not dead, and nothing is said about it.

    SKIPPED when dnspython is not installed. That is logged once, and no
    domain is ever reported dead because a library was missing.

    One answer a domain a day is kept in memory, so a post that checks forty
    people at six companies asks six questions.
    """
    domain = str(domain or "").strip().lower().rstrip(".")
    if not domain or "." not in domain:
        return UNKNOWN
    hit = _MX_CACHE.get(domain)
    if hit and time.time() - hit[1] < _MX_CACHE_SECONDS:
        return hit[0]
    try:
        import dns.exception
        import dns.resolver
    except Exception:
        if not _SAID_NO_DNS[0]:
            _SAID_NO_DNS[0] = True
            log.warning("[email] dnspython is not installed, so the MX check is SKIPPED: no "
                        "domain is checked and none is reported as unable to receive mail. "
                        "Install it with `pip install dnspython`.")
        return SKIPPED

    resolver = dns.resolver.Resolver()
    resolver.lifetime = resolver.timeout = float(timeout)
    status = UNKNOWN
    try:
        answers = resolver.resolve(domain, "MX")
        hosts = [str(r.exchange).rstrip(".") for r in answers]
        status = DEAD if hosts and not any(hosts) else OK
    except dns.resolver.NXDOMAIN:
        status = DEAD
    except dns.resolver.NoAnswer:
        status = DEAD
        for kind in ("A", "AAAA"):
            try:
                if resolver.resolve(domain, kind):
                    status = OK
                    break
            except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN):
                continue
            except Exception:
                status = UNKNOWN
                break
    except Exception as e:
        log.info("[email] the MX lookup for %s could not be completed (%s); nothing is "
                 "said about it", domain, type(e).__name__)
        status = UNKNOWN
    if status != UNKNOWN:
        _MX_CACHE[domain] = (status, time.time())
    log.info("[email] MX %s -> %s", domain, status)
    return status


if __name__ == "__main__":
    import sys

    FAILED = 0

    def check(name, got, want):
        global FAILED
        ok = got == want
        FAILED += 0 if ok else 1
        print(("  ok    " if ok else "  FAIL  ") + name
              + ("" if ok else f"\n        got  {got!r}\n        want {want!r}"))

    print("is it an address")
    check("not available", addresses_in("not available"), [])
    check("a dash, a phone number", (addresses_in("-"), addresses_in("+44 20 7946 0958")), ([], []))
    check("an address and a phone number", addresses_in("a.b@poly.ai / +44 20 7946 0958"), ["a.b@poly.ai"])
    check("two addresses on two lines",
          addresses_in("g.neff@qmul.ac.uk\ngn123@cam.ac.uk"), ["g.neff@qmul.ac.uk", "gn123@cam.ac.uk"])
    print("\ndoes it match the company (log only)")
    check("PolyAI and poly.ai", company_check("x@poly.ai", "PolyAI"), MATCH)
    check("Gnani.ai and gnani.ai", company_check("x@gnani.ai", "Gnani.ai"), MATCH)
    check("a personal address is not an error", company_check("x@gmail.com", "PolyAI"), PERSONAL)
    check('"Multiple" is not a company: skipped', company_check("g@qmul.ac.uk", "Multiple"), SKIPPED)
    check("a university: skipped", company_check("g@cam.ac.uk", "University of Cambridge"), SKIPPED)
    check("two organisations together: skipped", company_check("g@x.org", "Acme / Globex"), SKIPPED)
    check("a different company's domain", company_check("x@globex.com", "Acme Robotics"), MISMATCH)
    print("\nALL PASSED" if not FAILED else f"\n{FAILED} FAILED")
    sys.exit(1 if FAILED else 0)
