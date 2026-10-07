"""LINKS AS SHORT NAMES — the one helper every channel message uses.

Every url the bot posts is a Discord masked link with its embed suppressed:

    [techcrunch.com](<https://techcrunch.com/2026/09/29/...>)

The short name is the page title when one is known (at most six words), else
the site name, else "link". The full url stays intact inside the angle
brackets, which is also what stops Discord unfurling a preview card under
every message.

`drip.link` re-exports `link` so callers have one name for it; this module
exists only so `guardrails` can use the same helper without importing the
drip. `mask_bare` is the last line: `guardrails.send` runs it on every body,
so a url that reached a message by any other route still goes out masked.
"""
import re

_MAX_WORDS = 6

# Google's editors, named for what they are rather than "docs.google.com".
_DOC_KINDS = (
    ("docs.google.com/document", "Doc"),
    ("docs.google.com/spreadsheets", "Sheet"),
    ("docs.google.com/presentation", "Slides"),
    ("docs.google.com/forms", "Form"),
    ("drive.google.com", "File"),
)


def site_name(url: str) -> str:
    """"techcrunch.com" from any url on it. "" when there is no host."""
    m = re.match(r"^\s*https?://([^/\s?#]+)", str(url or ""), re.IGNORECASE)
    if not m:
        return ""
    host = m.group(1).lower().split("@")[-1].split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    if host in ("discord.com", "discordapp.com") and "/channels/" in str(url):
        return "message"
    return host


def doc_label(url: str) -> str:
    """"Doc", "Sheet", "Slides"… for Google links; the site name otherwise."""
    low = str(url or "").lower()
    for marker, label in _DOC_KINDS:
        if marker in low:
            return label
    return site_name(url) or "link"


def short_name(text: str, url: str) -> str:
    """The page title in at most six words, else the site name, else "link"."""
    words = re.sub(r"[\[\]<>`*_|]", " ", str(text or "")).split()
    if words:
        name = " ".join(words[:_MAX_WORDS])
        return name + ("…" if len(words) > _MAX_WORDS else "")
    return site_name(url) or "link"


def link(text: str, url: str) -> str:
    """`[short name](<url>)`. An empty url gives the text alone."""
    url = str(url or "").strip().strip("<>")
    if not url:
        return str(text or "").strip()
    return f"[{short_name(text, url)}](<{url}>)"


# Already masked: [name](<url>) — left exactly as it is.
_MASKED_RE = re.compile(r"\[[^\]\n]{1,200}\]\(<https?://[^\s>]+>\)")
# Markdown without the angle brackets: [name](url) — gets them, so no embed.
_MD_RE = re.compile(r"\[([^\]\n]{1,200})\]\((https?://[^\s)]+)\)")
# <url>, or a bare url.
_ANGLE_RE = re.compile(r"<(https?://[^\s>]+)>")
_BARE_RE = re.compile(r"https?://[^\s<>()\[\]]+")


def mask_bare(text: str) -> str:
    """Every url in `text` as a masked link. Masked ones are left alone."""
    body = str(text or "")
    if "http" not in body:
        return body
    kept: list = []

    def _hold(s: str) -> str:
        kept.append(s)
        return f"\x00{len(kept) - 1}\x00"

    body = _MASKED_RE.sub(lambda m: _hold(m.group(0)), body)
    body = _MD_RE.sub(lambda m: _hold(link(m.group(1), m.group(2))), body)
    body = _ANGLE_RE.sub(lambda m: _hold(link("", m.group(1))), body)
    body = _BARE_RE.sub(lambda m: _hold(link("", m.group(0).rstrip(".,;:!?"))) +
                        m.group(0)[len(m.group(0).rstrip(".,;:!?")):], body)
    return re.sub(r"\x00(\d+)\x00", lambda m: kept[int(m.group(1))], body)


def bare_urls(text: str) -> list:
    """Urls in `text` that are NOT inside a masked link — should be []."""
    body = _MASKED_RE.sub(" ", str(text or ""))
    return _BARE_RE.findall(body)


def _host_path(url: str) -> tuple:
    """(host, path) of a url: host lower-cased without "www.", path without a
    query string, a fragment, a trailing slash or trailing punctuation.
    ("", "") when it is not an http(s) url."""
    m = re.match(r"^\s*<?https?://([^/\s?#]+)([^\s?#>]*)", str(url or ""),
                 re.IGNORECASE)
    if not m:
        return "", ""
    host = m.group(1).lower().split("@")[-1].split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    return host, m.group(2).rstrip(".,;:!?").rstrip("/")


def profile_kind(url: str) -> str:
    """What a LinkedIn url points at: "profile", "post", "company" or "other".

    A SEARCH FOR SOMEBODY'S PROFILE OFTEN RETURNS A POST THAT MENTIONS THEM, a
    comment thread, or their employer's page — all on linkedin.com, all with
    the person's name in the title. Presented as "their LinkedIn" that is a
    wrong link the reader has no reason to doubt. Only /in/… is a profile.
    Read from the url alone: nothing is fetched, LinkedIn least of all.
    """
    host, path = _host_path(url)
    if host != "linkedin.com" and not host.endswith(".linkedin.com"):
        return "other"
    low = path.lower() + "/"
    if low.startswith("/in/") and len(low) > len("/in/"):
        return "profile"
    if low.startswith(("/posts/", "/feed/", "/pulse/")):
        return "post"
    if low.startswith(("/company/", "/school/")):
        return "company"
    return "other"


_TRACKING = ("utm_", "trk", "ref", "fbclid", "gclid", "originalsubdomain")


def _query(url: str) -> tuple:
    """The query parameters that IDENTIFY a page, sorted; tracking ones dropped."""
    m = re.search(r"\?([^#\s>]*)", str(url or ""))
    if not m:
        return ()
    parts = [p for p in m.group(1).rstrip(".,;:!").split("&") if p]
    return tuple(sorted(p for p in parts
                        if not p.split("=")[0].lower().startswith(_TRACKING)))


def same_url(a: str, b: str) -> bool:
    """Do two urls name the same page? Scheme, "www.", host case, a trailing
    slash, trailing punctuation and tracking parameters are ignored — the ways
    a link a search returned differs from the same link written out again. The
    PATH must match exactly: /in/x and /in/x-123 are two different people.

    A LINKEDIN URL'S QUERY IS IGNORED WHOLE (it never names the page). Anywhere
    else the identifying parameters must match too: on Google Scholar the
    person IS the query (citations?user=…), and ignoring it would let a made-up
    profile pass as the found one."""
    one, two = _host_path(a), _host_path(b)
    if not one[0] or one != two:
        return False
    if one[0] == "linkedin.com" or one[0].endswith(".linkedin.com"):
        return True
    return _query(a) == _query(b)


def _self_test() -> int:
    failures = 0

    def check(name, got, want):
        nonlocal failures
        ok = got == want
        failures += 0 if ok else 1
        print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}, want {want!r}")

    u = "https://www.techcrunch.com/2026/09/29/story"
    check("site name", site_name(u), "techcrunch.com")
    check("a title, capped at six words",
          link("One two three four five six seven", u),
          f"[One two three four five six…](<{u}>)")
    check("no title -> the site", link("", u), f"[techcrunch.com](<{u}>)")
    check("no site -> link", short_name("", "not a url"), "link")
    check("a Google doc is a Doc", doc_label("https://docs.google.com/document/d/x"), "Doc")
    check("bare url masked", mask_bare(f"see {u}."), f"see [techcrunch.com](<{u}>).")
    check("angle url masked", mask_bare(f"see <{u}>"), f"see [techcrunch.com](<{u}>)")
    check("markdown gets its brackets", mask_bare(f"[Story]({u})"), f"[Story](<{u}>)")
    already = f"[Story](<{u}>)"
    check("masked is left alone", mask_bare(already), already)
    check("nothing bare after masking", bare_urls(mask_bare(f"a {u} b <{u}>")), [])
    check("a jump link reads as 'message'",
          link("", "https://discord.com/channels/1/2/3"),
          "[message](<https://discord.com/channels/1/2/3>)")
    check("a profile", profile_kind("https://in.linkedin.com/in/jane-doe-1a2b/"),
          "profile")
    check("a post is not a profile",
          profile_kind("https://www.linkedin.com/posts/jane-doe_ai-activity-1"), "post")
    check("a company page", profile_kind("https://www.linkedin.com/company/acme"),
          "company")
    check("another site's /in/", profile_kind("https://notlinkedin.com/in/jane"),
          "other")
    check("same page, written two ways",
          same_url("http://LinkedIn.com/in/x?trk=1", "https://www.linkedin.com/in/x/"),
          True)
    check("a different person", same_url("https://linkedin.com/in/x-123",
                                         "https://linkedin.com/in/x"), False)
    check("a different Scholar profile",
          same_url("https://scholar.google.com/citations?user=abc",
                   "https://scholar.google.com/citations?user=xyz"), False)
    check("the same Scholar profile, with a tracking parameter",
          same_url("https://scholar.google.com/citations?user=abc&utm_source=x",
                   "https://scholar.google.com/citations?user=abc"), True)
    print(f"\n{'ALL PASSED' if not failures else str(failures) + ' FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_self_test())
