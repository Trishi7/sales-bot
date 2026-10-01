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
    print(f"\n{'ALL PASSED' if not failures else str(failures) + ' FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_self_test())
