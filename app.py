"""
SMH + AFR Paywall Bypass -- Reverse Proxy (v5)
----------------------------------------------
Full reverse proxy for smh.com.au and afr.com with link rewriting.
Browse the homepage, click articles, all through the proxy.
Article pages get clean text extraction from embedded JSON.

SMH routes:  / , /<path>
AFR routes:  /afr , /afr/<path>
"""
import os
import re
import json
import gzip
import logging
import time
from collections import OrderedDict
from urllib.parse import urljoin, urlparse
from flask import Flask, request, Response
import requests
from bs4 import BeautifulSoup
from markupsafe import escape

logging.basicConfig(level=os.environ.get("SMH_LOG_LEVEL", "INFO").upper())
log = logging.getLogger(__name__)

app = Flask(__name__)

SESSION = requests.Session()

UPSTREAM = "https://www.smh.com.au"

# ── Response compression ────────────────────────────────────────────────────
# The origin serves gzip; we re-serialise its HTML, so without this we ship it
# uncompressed — measured /national at 376KB against 65KB gzipped, and the SMH
# homepage at 1.2MB against ~165KB. Cached pages pre-compress once at store
# time, so a cache hit costs no compression CPU at all.
GZIP_LEVEL = 4
GZIP_MIN_BYTES = 1024
COMPRESSIBLE_TYPES = (
    "text/",
    "application/json",
    "application/javascript",
    "application/xml",
    "image/svg+xml",
)


def _is_compressible(mimetype):
    if not mimetype:
        return False
    mime = mimetype.split(";", 1)[0].strip().lower()
    return any(mime == t or mime.startswith(t) for t in COMPRESSIBLE_TYPES)


def _client_accepts_gzip():
    return "gzip" in request.headers.get("Accept-Encoding", "").lower()


def _gzip(data):
    """Compress a str/bytes body, or return None if that is not worthwhile."""
    if not isinstance(data, bytes):
        try:
            data = data.encode("utf-8")
        except UnicodeEncodeError:
            return None
    if len(data) < GZIP_MIN_BYTES:
        return None
    try:
        return gzip.compress(data, GZIP_LEVEL)
    except (OSError, ValueError):
        return None


def _add_vary(resp, value):
    parts = [p.strip() for p in resp.headers.get("Vary", "").split(",") if p.strip()]
    if value.lower() not in {p.lower() for p in parts}:
        parts.append(value)
    resp.headers["Vary"] = ", ".join(parts)


@app.after_request
def _compress_response(resp):
    """Gzip responses we did not pre-compress (articles, JSON, errors)."""
    if resp.direct_passthrough or "Content-Encoding" in resp.headers:
        return resp
    if not _is_compressible(resp.mimetype):
        return resp
    _add_vary(resp, "Accept-Encoding")
    if not _client_accepts_gzip():
        return resp
    data = resp.get_data()
    gz = _gzip(data) if len(data) >= GZIP_MIN_BYTES else None
    if gz is None:
        return resp
    resp.set_data(gz)
    resp.headers["Content-Encoding"] = "gzip"
    return resp


# ── Page cache: serve rendered pages from memory for 5 minutes ────────
# Clean article renders and index/section pages are cached. Live blogs never
# are — their content changes minute to minute.
PAGE_CACHE_TTL = 300  # seconds
PAGE_CACHE_MAX_ENTRIES = 200
PAGE_CACHE_MAX_BYTES = 32 * 1024 * 1024
# Index pages are identical server-side for PAGE_CACHE_TTL seconds, so let the
# browser reuse them for a slice of that. They used to be sent `no-store`,
# which forced a full round trip on every repeat visit.
INDEX_CACHE_HEADER = {"Cache-Control": "public, max-age=60"}
# Insertion order doubles as LRU order: hits are moved to the end and the
# oldest entry is evicted first.
# entry -> (monotonic_ts, body, gz_body|None, mimetype, headers, status)
_page_cache = OrderedDict()


def _page_cache_get(key):
    entry = _page_cache.get(key)
    if entry is None:
        return None
    ts, body, gz, mimetype, headers, status = entry
    if time.monotonic() - ts > PAGE_CACHE_TTL:
        # Only drop the entry we actually read: another thread may have
        # already replaced it with a fresh one, and a bare `del` would raise
        # KeyError (a 500) on that race.
        if _page_cache.get(key) is entry:
            _page_cache.pop(key, None)
        return None
    _page_cache.move_to_end(key)
    return body, gz, mimetype, headers, status


def _page_cache_set(key, body, mimetype, headers, status):
    """Store a render, pre-compressed. Returns the gzip bytes (or None)."""
    gz = None
    if _is_compressible(mimetype):
        gz = _gzip(body)
    _page_cache[key] = (time.monotonic(), body, gz, mimetype, headers, status)
    _page_cache.move_to_end(key)
    while len(_page_cache) > PAGE_CACHE_MAX_ENTRIES:
        _page_cache.popitem(last=False)
    while len(_page_cache) > 1 and sum(
            len(e[1]) + (len(e[2]) if e[2] else 0)
            for e in _page_cache.values()) > PAGE_CACHE_MAX_BYTES:
        _page_cache.popitem(last=False)
    return gz


def _cache_and_respond(key, body, mimetype, headers, status):
    """Cache this render and return the response for it.

    Serving the already-compressed bytes here means a cold request compresses
    once (in _page_cache_set) instead of once to store and again in
    _compress_response.
    """
    gz = _page_cache_set(key, body, mimetype, headers, status)
    out_headers = dict(headers)
    out_headers.setdefault("Vary", "Accept-Encoding")
    if gz is not None and _client_accepts_gzip():
        out_headers["Content-Encoding"] = "gzip"
        return Response(gz, mimetype=mimetype, headers=out_headers, status=status)
    return Response(body, mimetype=mimetype, headers=out_headers, status=status)


def _cached_response(key):
    """Serve this request's cached render, gzip-first. None on a miss."""
    cached = _page_cache_get(key)
    if not cached:
        return None
    body, gz, mimetype, headers, status = cached
    headers = dict(headers)
    if "Vary" not in headers:
        headers["Vary"] = "Accept-Encoding"
    if gz is not None and _client_accepts_gzip():
        headers["Content-Encoding"] = "gzip"
        return Response(gz, mimetype=mimetype, headers=headers, status=status)
    return Response(body, mimetype=mimetype, headers=headers, status=status)


# Query parameters that name a campaign or tracker rather than content. They
# are dropped from the cache key so that ?utm_source=... cannot fragment it.
_TRACKING_PARAM_RE = re.compile(
    r"^utm_|^(gclid|gbraid|wbraid|fbclid|igshid|msclkid|yclid|twclid|"
    r"li_fat_id|mc_cid|mc_eid|_ga|ref_src|ref_url|cmpid)$",
    re.I,
)


def _page_cache_key(brand):
    """Cache key for the current request: brand + path + normalised query.

    Query values are decoded defensively because Werkzeug's
    ``request.full_path`` raises UnicodeDecodeError on non-UTF-8 bytes.
    Remaining parameters are sorted, so ordering cannot create duplicates.
    """
    parts = []
    for part in request.query_string.decode("utf-8", "replace").split("&"):
        name = part.split("=", 1)[0]
        if part and not _TRACKING_PARAM_RE.match(name):
            parts.append(part)
    key = f"{brand}:{request.path}"
    if parts:
        key += "?" + "&".join(sorted(parts))
    return key


def _upstream_url(base, path):
    """Upstream URL for the current request's path and query string."""
    if request.query_string:
        # Never raise on malformed (non-UTF-8) query bytes.
        return f"{base}/{path}?{request.query_string.decode('utf-8', 'replace')}"
    return f"{base}/{path}" if path else base


def _upstream_error_response(resp, upstream):
    """Report an upstream 4xx/5xx with its real status code.

    Previously ``raise_for_status()`` turned every upstream 404 into a 502
    "Upstream fetch failed", which hides the actual outcome.
    """
    log.warning("Upstream returned %s %s for %s",
                resp.status_code, resp.reason or "", upstream)
    body = (
        f"<h1>{resp.status_code} {escape(resp.reason or '')}</h1>"
        f"<p>The upstream server returned {resp.status_code} for "
        f'<a href="{escape(upstream)}">{escape(upstream)}</a>.</p>'
    )
    return Response(body, mimetype="text/html", status=resp.status_code,
                    headers={"Cache-Control": "no-store"})

GRAPHQL_URL = "https://api.ffx.io/graphql"
NAV_ASSET_TYPES = [
    "ARTICLE", "FEATURE_ARTICLE", "LIVE_ARTICLE",
    "GALLERY", "VIDEO", "BESPOKE",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-AU,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Referer": "https://www.google.com/",
}

# Hosts each proxy route can actually fetch. A route only has one upstream, so
# only its own brand may be rewritten onto that route's prefix — rewriting a
# link to another masthead onto it would fetch the wrong origin (e.g. an AFR
# page linking to smh.com.au would ask afr.com for that path). Cross-brand
# links are therefore left absolute and resolved by the browser.
SMH_DOMAINS = ["smh.com.au"]

# Paywall-related URL patterns to block (compiled)
BLOCK_PATTERNS = [
    re.compile(p, re.I)
    for p in (
        r"piano\.io",
        r"tinypass",
        r"poool\.",
        r"paywall",
        r"metering",
        r"subscribe",
    )
]

# ── AFR-specific constants ──────────────────────────────────────────────────
AFR_UPSTREAM = "https://www.afr.com"

AFR_GRAPHQL_URL = "https://api.afr.com/graphql"

# Hosts the AFR route (prefix "/afr") can fetch — see SMH_DOMAINS.
AFR_DOMAINS = [
    "afr.com",
]

# Superset of BLOCK_PATTERNS plus AFR-specific trackers (compiled).
# Entries subsumed by a broader pattern (e.g. buy-au.piano.io ⊂ piano.io,
# tinypass.min.js ⊂ tinypass, partner.googleadservices ⊂ googleadservices)
# are omitted.
AFR_BLOCK_PATTERNS = [
    re.compile(p, re.I)
    for p in (
        r"piano\.io",
        r"tinypass",
        r"poool\.",
        r"paywall",
        r"metering",
        r"subscribe",
        r"gtm\.js",
        r"snowplow",
        r"alib\.nine\.com\.au",
        r"adkit\.9pub",
        r"googletagmanager",
        r"googleadservices",
        r"doubleclick",
        r"googlesyndication",
        r"tp\.push",
        r"tp\.pianoId",
        r"afx_prid",
    )
]


def rewrite_url(url, base_url, domains, prefix=""):
    """Convert a URL to route through the proxy. Returns rewritten URL string.

    ``prefix`` is "" for the SMH proxy (served at /) and "/afr" for the
    AFR proxy. ``domains`` lists the hosts this route can fetch, so only
    those are mapped onto the prefix; anything else is left alone.
    """
    if not url or url.startswith(("#", "javascript:", "mailto:", "tel:")):
        return url

    # Protocol-relative ("//host/path"): normalise to absolute so the host is
    # tested below. Handled as root-relative it would swallow the host into
    # the proxy path ("/afr/api.ffx.io/...") or, for SMH, send the browser
    # straight to the origin outside the proxy.
    if url.startswith("//"):
        url = f"{urlparse(base_url).scheme or 'https'}:{url}"

    # Root-relative URL
    if url.startswith("/"):
        if not prefix:
            # SMH: already proxy-relative
            return url
        if url == prefix or url.startswith(prefix + "/"):
            return url
        if "?" in url:
            path, qs = url.lstrip("/").split("?", 1)
            return f"{prefix}/{path}?{qs}"
        return f"{prefix}/{url.lstrip('/')}"

    # Absolute URL
    parsed = urlparse(url)
    if parsed.scheme in ("http", "https"):
        host = parsed.hostname or ""
        if any(host.endswith(d) for d in domains):
            path = parsed.path.lstrip("/")
            frag = f"#{parsed.fragment}" if parsed.fragment else ""
            if parsed.query:
                return f"{prefix}/{path}?{parsed.query}{frag}"
            return f"{prefix}/{path}{frag}"
        # External link — leave as-is
        return url

    # Relative URL — resolve against base
    resolved = urljoin(base_url, url)
    parsed = urlparse(resolved)
    host = parsed.hostname or ""
    if any(host.endswith(d) for d in domains):
        tail = parsed.path
        if parsed.query:
            tail += f"?{parsed.query}"
        if parsed.fragment:
            tail += f"#{parsed.fragment}"
        return rewrite_url(tail, base_url, domains, prefix)

    return url


AD_SELECTORS = [
    '[data-testid="ad"]',
    '[data-testid="outbrain"]',
    ".adWrapper",
    '[id^="adspot-"]',
    '[id="gptHeadScript"]',
    '[id^="google_ads"]',
    '[class*="google-auto-placed"]',
    "ins.adsbygoogle",
    'iframe[src*="doubleclick"]',
    'iframe[src*="googlesyndication"]',
]

PARTNER_SELECTORS = [
    '[data-testid^="from-our-partners"]',
    '[data-testid="right-rail-partners"]',
    '[data-an-name*="from our partners" i]',
]

# Non-article content units embedded in the top-stories region (newsletter
# call-to-action, weather widget). NOTE: do not strip the parent
# "top-stories-strap" — it wraps the news wells, "Just in" and editor's picks.
STRIP_SELECTORS = [
    '[data-testid="storysetcallstoaction-strap"]',
    '[data-testid="weather-widget"]',
]

SECTION_TITLES = {"explore", "shorts"}


def _strip_named_sections(soup):
    """Remove sections by their heading text (e.g. Explore, Shorts)."""
    for heading in soup.find_all(["h1", "h2", "h3"]):
        if heading.get_text(" ", strip=True).lower() not in SECTION_TITLES:
            continue
        section = heading.find_parent("section")
        (section or heading).decompose()


def _strip_footer_below_socials(soup):
    """Remove footer content that sits below the social media links."""
    footers = soup.find_all("footer") + _elements_matching(
        soup, ['#footer, [data-testid="footer"]'])
    seen = set()
    for footer in footers:
        if id(footer) in seen:
            continue
        seen.add(id(footer))
        socials = footer.find(attrs={"data-testid": "footer-socials"})
        if socials is None:
            socials = footer.find(
                "a",
                href=re.compile(r"twitter\.com|facebook\.com|instagram\.com", re.I),
            )
        if socials is None:
            continue
        node = socials
        while node is not None and node is not footer:
            for sib in list(node.find_next_siblings()):
                sib.decompose()
            node = node.parent


PAYWALL_SELECTORS = [
    '#paywall_prompt',
    '#paywall-piano',
    '#subscribe',
    '[data-testid="PianoContainer"]',
    '[data-testid="PianoSubButton"]',
    '#tp-iframe',
    '.paywall',
    '.regwall',
    '.gateway',
    '.meter-wall',
]

PAYWALL_CLASS_RE = re.compile(r"paywall|subscribe-prompt|regwall|gateway|meter-wall", re.I)
PAYWALL_ID_RE = re.compile(r"piano|tp-|tif-wrapper", re.I)
ADSPOT_ID_RE = re.compile(r"adspot|ad-slot|consent|cookie", re.I)

# ── Native replacement for soup.select() on the simple selectors above ──────
# soupsieve (BeautifulSoup's CSS engine) re-walks the whole tree once per
# selector. Measured on smh.com.au/ that is 207ms of a 352ms process_html —
# 59% of all processing — for 26 selectors. Every selector we use is a plain
# tag / #id / .class / [attr...] test with no combinators, so a single native
# walk with cheap predicates produces the identical element set.
_SELECTOR_PART_RE = re.compile(
    r"""(?P<tag>[a-zA-Z][\w-]*)
       | \#(?P<id>[\w-]+)
       | \.(?P<cls>[\w-]+)
       | \[(?P<attr>[a-zA-Z][\w-]*)(?P<op>\^=|\*=|=)"(?P<val>[^"]*)"(?P<ci>\s*i)?\]
    """,
    re.X,
)


def _attr_values(tag, names):
    """Read the attributes the selector set cares about, once per element.

    ``class`` is normalised to a token tuple (bs4 parses it to a list) so the
    per-selector predicates are plain membership tests with no string work.
    """
    vals = {}
    for name in names:
        val = tag.get(name)
        if name == "class":
            if isinstance(val, str):
                val = tuple(val.split())
            elif isinstance(val, list):
                val = tuple(val)
            else:
                val = ()
        vals[name] = val
    return vals


def _vals_str(vals, name):
    """Attribute value as a string for prefix/substring tests."""
    val = vals.get(name)
    if val is None:
        return ""
    if isinstance(val, (list, tuple)):
        return " ".join(val)
    return val


def _compile_selector(sel):
    """Compile one combinator-free CSS selector.

    Returns ``(attrs_needed, test, bucketable)`` where ``test(vals, tag)``
    reads its attributes from the pre-read ``vals`` dict (see
    :func:`_attr_values`). ``bucketable`` is False when some comparison
    value is empty — a missing attribute would then look like a match, so
    the caller may not skip the test on absent attributes.

    Raises ValueError for selector features we do not translate; callers
    fall back to ``soup.select`` for those.
    """
    tests = []
    needed = set()
    bucketable = True
    pos = 0
    for m in _SELECTOR_PART_RE.finditer(sel):
        if m.start() != pos:
            raise ValueError(f"unsupported selector: {sel!r}")
        pos = m.end()
        if m.group("tag"):
            name = m.group("tag").lower()
            tests.append(lambda vals, el, n=name: el.name == n)
        elif m.group("id"):
            needed.add("id")
            want = m.group("id")
            bucketable = bucketable and bool(want)
            tests.append(lambda vals, el, w=want: vals.get("id") == w)
        elif m.group("cls"):
            needed.add("class")
            want = m.group("cls")
            bucketable = bucketable and bool(want)
            tests.append(lambda vals, el, w=want: w in vals["class"])
        else:
            attr = m.group("attr")
            op = m.group("op")
            want = m.group("val")
            ci = bool(m.group("ci"))
            needed.add(attr)
            bucketable = bucketable and bool(want)
            if op == "=":
                if ci:
                    want = want.lower()
                    tests.append(
                        lambda vals, el, a=attr, w=want:
                        _vals_str(vals, a).lower() == w)
                else:
                    tests.append(
                        lambda vals, el, a=attr, w=want: _vals_str(vals, a) == w)
            elif op == "^=":
                tests.append(
                    lambda vals, el, a=attr, w=want: _vals_str(vals, a).startswith(w))
            else:  # "*="
                if ci:
                    want = want.lower()
                    tests.append(
                        lambda vals, el, a=attr, w=want:
                        w in _vals_str(vals, a).lower())
                else:
                    tests.append(
                        lambda vals, el, a=attr, w=want: w in _vals_str(vals, a))
    if not tests or pos != len(sel):
        raise ValueError(f"unsupported selector: {sel!r}")

    def test(vals, el):
        return all(t(vals, el) for t in tests)

    return needed, test, bucketable


def _split_selector_list(sel):
    """Split a comma-separated selector list, ignoring commas in [...] and quotes."""
    parts, buf, depth, quote = [], "", 0, None
    for ch in sel:
        if quote:
            buf += ch
            if ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
            buf += ch
        elif ch == "[":
            depth += 1
            buf += ch
        elif ch == "]":
            depth = max(0, depth - 1)
            buf += ch
        elif ch == "," and depth == 0:
            if buf.strip():
                parts.append(buf.strip())
            buf = ""
        else:
            buf += ch
    if buf.strip():
        parts.append(buf.strip())
    return parts


def _elements_matching(soup, selectors):
    """All elements matching any selector, via one native walk.

    Selectors we cannot translate fall back to ``soup.select`` so behaviour
    is never lost — only the fast path is conditional.

    Tests are bucketed by the attribute they read, so an element only runs
    the predicates whose attribute it actually carries. On the SMH homepage
    that is ~3x fewer predicate calls than evaluating all of them.
    """
    always, by_attr, lazy, names = [], {}, [], set()
    for sel in selectors:
        for part in _split_selector_list(sel):
            try:
                needed, test, bucketable = _compile_selector(part)
            except ValueError:
                lazy.append(part)
                continue
            names.update(needed)
            if needed and bucketable:
                for attr in needed:
                    by_attr.setdefault(attr, []).append(test)
            else:
                always.append(test)

    matches = []
    if always or by_attr:
        attr_names = tuple(names)
        find_all = soup.find_all
        for el in find_all(True):
            vals = _attr_values(el, attr_names)
            if any(t(vals, el) for t in always):
                matches.append(el)
                continue
            for attr, group in by_attr.items():
                # Absent (or empty) attribute -> every test in the bucket
                # fails anyway, so skip the whole group.
                if not vals.get(attr):
                    continue
                if any(t(vals, el) for t in group):
                    matches.append(el)
                    break
    for part in lazy:
        matches.extend(soup.select(part))

    if not matches:
        return matches
    seen = set()
    unique = []
    for el in matches:
        if id(el) not in seen:
            seen.add(id(el))
            unique.append(el)
    return unique


def _strip_matching(soup, selectors):
    """Decompose every element matching any selector.

    Equivalent to running each selector as its own ``soup.select`` pass:
    the removed set is always the union of the matched subtrees, so
    ancestor/descendant overlaps do not change the resulting tree. The
    nesting decision is made before any decompose() so a node is never
    decomposed twice.
    """
    matches = _elements_matching(soup, selectors)
    if not matches:
        return 0

    doomed = {id(el) for el in matches}
    nested = set()
    for el in matches:
        parent = el.parent
        while parent is not None:
            if id(parent) in doomed:
                nested.add(id(el))
                break
            parent = parent.parent

    removed = 0
    for el in matches:
        if id(el) not in nested:
            el.decompose()
            removed += 1
    return removed


# Markers identifying a framework hydration payload rather than a paywall or
# tracker script. Matched against the lowercased inline script body:
#   window.INITIAL_STATE            — SMH index/section/article hydration
#   window.APOLLO_STATE             — SMH article hydration (what the
#                                     article extractor actually looks for)
#   __staticRouterHydrationData     — AFR
#   __redux_state__/__apollo_state__ — AFR's state blobs
# The previous list spelled ``__staticRouterHydrationData`` as
# ``__staticrouterydrationdata`` — missing the "h" — so it never matched.
STATE_SCRIPT_MARKERS = (
    "__redux_state__",
    "__apollo_state__",
    "__staticrouterhydrationdata",
    "window.initial_state",
    "window.apollo_state",
)


def process_html(html, base_url, domains, prefix, block_patterns,
                 extra_id_re=None, strip_state_scripts=False):
    """Single-pass cleanup of proxied HTML.

    Strips paywall/tracking scripts, paywall and ad containers, partner
    sections, named sections and footer cruft, then rewrites all
    href/src/action attributes to route through the proxy.
    """
    if not html:
        return html
    soup = BeautifulSoup(html, "html.parser")

    # Paywall/tracking script removal
    for tag in soup.find_all("script"):
        src = (tag.get("src") or "").lower()
        if any(p.search(src) for p in block_patterns):
            tag.decompose()
            continue
        body = tag.string or ""
        if not body:
            continue
        low = body.lower()
        if any(m in low for m in STATE_SCRIPT_MARKERS):
            # Hydration payload: keep it unless the caller asked for state to
            # be stripped. It is article/state data, so it routinely contains
            # words like "paywall"/"subscribe" — keyword-matching it deleted
            # SMH's entire INITIAL_STATE bundle (and its bootstrap code).
            if strip_state_scripts:
                tag.decompose()
            continue
        if any(p.search(low) for p in block_patterns):
            tag.decompose()

    for tag in soup.find_all("link"):
        href = (tag.get("href") or "").lower()
        if any(p.search(href) for p in block_patterns):
            tag.decompose()

    # Paywall / partner / ad element removal. One native walk instead of a
    # full soupsieve pass per selector (see _strip_matching).
    _strip_matching(
        soup, PAYWALL_SELECTORS + AD_SELECTORS + PARTNER_SELECTORS + STRIP_SELECTORS)

    # Remove empty wrapper shells left behind after ad stripping
    # (e.g. the header banner div that only contained an ad slot).
    header = soup.find("header")
    if header:
        for div in header.find_all("div"):
            if div.get_text(strip=True) or div.find(
                    ["img", "iframe", "svg", "canvas", "video"]):
                continue
            div.decompose()

    for el in soup.find_all(True, class_=PAYWALL_CLASS_RE):
        el.decompose()

    for el in soup.find_all(True, id=PAYWALL_ID_RE):
        el.decompose()

    if extra_id_re is not None:
        for el in soup.find_all(True, id=extra_id_re):
            el.decompose()

    _strip_named_sections(soup)
    _strip_footer_below_socials(soup)

    # Rewrite link attributes
    for tag in soup.find_all(True):
        for attr in ("href", "src", "action"):
            val = tag.get(attr)
            if val:
                tag[attr] = rewrite_url(val, base_url, domains, prefix)

    # Fix base tag if present
    base = soup.find("base")
    if base:
        base.decompose()

    return str(soup)


APOLLO_SCRIPT_RE = re.compile(r"<script[^>]*>(.*?)</script>", re.DOTALL | re.I)

# Distinguishes "no pre-parsed state passed in" from "state parsed to None".
_STATE_UNSET = object()


def _extract_apollo_state(html):
    """Parse ``window.APOLLO_STATE = {...}`` from a page's inline scripts.

    The marker check comes first: index pages carry no APOLLO_STATE, and
    without it this walked and copied every inline script body (~10ms) only
    to find nothing.
    """
    if "window.APOLLO_STATE" not in html:
        return None
    for m in APOLLO_SCRIPT_RE.finditer(html):
        text = m.group(1)
        marker = "window.APOLLO_STATE"
        idx = text.find(marker)
        if idx == -1:
            continue
        eq = text.find("=", idx)
        if eq == -1:
            continue
        start = text.find("{", eq)
        if start == -1:
            continue
        try:
            data, _ = json.JSONDecoder().raw_decode(text[start:])
            return data
        except json.JSONDecodeError:
            return None
    return None


def extract_article_data(html, hydration_data=_STATE_UNSET):
    """Extract article body from embedded APOLLO_STATE JSON.

    Pass ``hydration_data`` when the state has already been parsed, so a
    single request does not decode the same multi-megabyte blob twice.
    """
    if hydration_data is _STATE_UNSET:
        hydration_data = _extract_apollo_state(html)

    if not hydration_data:
        return None

    # Build a publicId -> canonical path map for all assets in the state
    asset_urls = {}
    for key, value in hydration_data.items():
        if not isinstance(value, dict):
            continue
        public_id = value.get("publicId")
        if not public_id:
            continue
        path = _get_nested(value, "urls", "canonical", "path")
        if path:
            asset_urls[public_id] = path

    for key, value in hydration_data.items():
        if not isinstance(value, dict):
            continue
        if value.get("__typename") in ("ArticleAsset", "FeatureArticleAsset"):
            body = value.get("body")
            if isinstance(body, dict):
                blocks = body.get("blocks")
                if isinstance(blocks, list) and len(blocks) > 0:
                    return {
                        "headline": _get_nested(value, "headlines", "headline") or "",
                        "overview": (_get_nested(value, "overview", "intro")
                                     or _get_nested(value, "overview", "about")
                                     or ""),
                        "byline": _get_byline(value, hydration_data),
                        "date": _get_nested(value, "dates", "published") or "",
                        "blocks": blocks,
                        "asset_urls": asset_urls,
                    }
    return None


def _state_asset_urls(hydration_data):
    """Build a publicId -> canonical path map for all assets in the state."""
    asset_urls = {}
    for value in hydration_data.values():
        if not isinstance(value, dict):
            continue
        public_id = value.get("publicId")
        path = _get_nested(value, "urls", "canonical", "path")
        if public_id and path:
            asset_urls[public_id] = path
    return asset_urls


def extract_live_article_data(html, hydration_data=_STATE_UNSET):
    """Extract a live blog (LiveArticleAsset + its Posts) from APOLLO_STATE.

    Live blogs have no article ``body``; the content is a stream of ``Post``
    objects (each with its own headline, timestamp and blocks). Posts are
    returned newest-first, matching how SMH presents a live feed.

    Pass ``hydration_data`` when the state has already been parsed (see
    :func:`extract_article_data`).
    """
    if hydration_data is _STATE_UNSET:
        hydration_data = _extract_apollo_state(html)
    if not hydration_data:
        return None

    live = None
    for value in hydration_data.values():
        if isinstance(value, dict) and value.get("__typename") == "LiveArticleAsset":
            live = value
            break
    if live is None:
        return None

    asset_urls = _state_asset_urls(hydration_data)

    posts = []
    seen = set()
    for value in hydration_data.values():
        if not isinstance(value, dict) or value.get("__typename") != "Post":
            continue
        pid = value.get("publicId") or value.get("id")
        if not pid or pid in seen:
            continue
        seen.add(pid)
        body = value.get("body")
        blocks = body.get("blocks") if isinstance(body, dict) else None
        posts.append({
            "id": pid,
            "headline": _get_nested(value, "headlines", "headline") or "",
            "published": (_get_nested(value, "dates", "published")
                          or _get_nested(value, "dates", "firstPublished") or ""),
            "byline": _get_byline(value, hydration_data),
            "blocks": blocks if isinstance(blocks, list) else [],
        })

    posts.sort(key=lambda p: p["published"], reverse=True)

    return {
        "headline": _get_nested(live, "headlines", "headline") or "",
        "overview": (_get_nested(live, "overview", "about")
                     or _get_nested(live, "overview", "intro") or ""),
        "byline": _get_byline(live, hydration_data),
        "date": (_get_nested(live, "dates", "published")
                 or _get_nested(live, "dates", "firstPublished") or ""),
        "asset_urls": asset_urls,
        "posts": posts,
    }


LIVE_FEED_STYLE = """<style>
  #__live_posts { max-width: 720px; }
  .live-post { border-top: 1px solid #e5e5e5; padding: 1.25rem 0; }
  .live-post:first-child { border-top: 0; }
  .live-post-meta { font-size: .8rem; color: #777; margin-bottom: .35rem; }
  .live-post-meta time { font-weight: 700; color: #c8102e;
    text-transform: uppercase; letter-spacing: .03em; }
  .live-post-title { margin: .1rem 0 .5rem; font-size: 1.15rem; line-height: 1.3; }
  .live-post p { margin: .6rem 0; }
  .tweet { border-left: 3px solid #1da1f2; padding-left: 1rem; }
  .live-header { display: flex; align-items: center; gap: .6rem;
    margin: 0 0 1rem; }
  .live-badge { background: #c8102e; color: #fff; font-weight: 700;
    font-size: .7rem; letter-spacing: .08em; padding: .15rem .5rem;
    border-radius: 3px; }
  #__live_updated { font-size: .8rem; color: #999; }
</style>"""


LIVE_FEED_SCRIPT = """<script>
(function () {
  var list = document.getElementById('__live_posts');
  if (!list) return;
  var fmt = function (root) {
    root.querySelectorAll('time[data-date]').forEach(function (t) {
      var d = new Date(t.getAttribute('data-date'));
      if (isNaN(d.getTime())) return;
      t.textContent = d.toLocaleString(undefined,
        { day: 'numeric', month: 'short', hour: 'numeric', minute: '2-digit' });
    });
  };
  fmt(document);
  var refresh = function () {
    fetch(location.href, { headers: { 'X-Requested-With': 'fetch' } })
      .then(function (r) { return r.text(); })
      .then(function (txt) {
        var doc = new DOMParser().parseFromString(txt, 'text/html');
        var have = {};
        list.querySelectorAll('.live-post[data-post-id]').forEach(function (e) {
          have[e.getAttribute('data-post-id')] = 1;
        });
        var fresh = [];
        doc.querySelectorAll('.live-post[data-post-id]').forEach(function (p) {
          var id = p.getAttribute('data-post-id');
          if (!have[id]) { have[id] = 1; fresh.push(p); }
        });
        if (fresh.length) {
          fresh.reverse().forEach(function (p) {
            var node = document.importNode(p, true);
            fmt(node);
            list.insertBefore(node, list.firstChild);
          });
          var b = document.getElementById('__live_updated');
          if (b) b.textContent = 'Updated ' + new Date().toLocaleTimeString();
        }
      })
      .catch(function () {});
  };
  setInterval(refresh, 30000);
})();
</script>"""


def render_live_posts(posts, asset_urls=None):
    """Render live-blog posts as a timeline (newest first)."""
    parts = []
    for post in posts:
        body = blocks_to_html(post.get("blocks"), asset_urls)
        headline = post.get("headline") or ""
        head_html = (f'<h3 class="live-post-title">{escape(headline)}</h3>'
                     if headline else "")
        byline = post.get("byline") or ""
        by = (f'<span class="live-post-byline"> {escape(byline)}</span>'
              if byline else "")
        published = post.get("published") or ""
        parts.append(
            f'<article class="live-post" data-post-id="{escape(post["id"])}" '
            f'data-published="{escape(published)}">'
            f'<div class="live-post-meta">'
            f'<time class="live-time" datetime="{escape(published)}" '
            f'data-date="{escape(published)}">{escape(published[:16])}</time>'
            f'{by}</div>'
            f'{head_html}{body}</article>'
        )
    return "\n".join(parts)


def _get_nested(obj, *keys):
    for k in keys:
        if isinstance(obj, dict):
            obj = obj.get(k)
        else:
            return None
    if isinstance(obj, str):
        return obj.strip()
    return None


def _get_byline(obj, state=None):
    byline = obj.get("byline", [])
    if isinstance(byline, list) and byline:
        names = []
        for b in byline:
            if not isinstance(b, dict):
                continue
            author = b.get("author")
            if isinstance(author, dict):
                ref = author.get("__ref")
                if ref and isinstance(state, dict) and isinstance(state.get(ref), dict):
                    author = state[ref]
                name = author.get("name", "")
                if name:
                    names.append(name)
        return ", ".join(names)
    return ""


def _resolve_placeholders(markup, placeholders, asset_urls=None):
    """Replace <x-placeholder id="..."> tags with <a> tags using placeholder data."""
    asset_urls = asset_urls or {}
    if not placeholders:
        markup = re.sub(r"<x-placeholder[^>]*>", "", markup)
        markup = re.sub(r"</x-placeholder>", "", markup)
        return markup

    by_id = {p["key"]: p for p in placeholders if isinstance(p, dict)}

    def replacer(m):
        tag = m.group(0)
        id_m = re.search(r'id="([^"]*)"', tag)
        if not id_m:
            return ""
        pid = id_m.group(1)
        ph = by_id.get(pid)
        if not ph:
            return ""
        text = ph.get("text", "")
        ptype = ph.get("type", "")
        url = ph.get("url", "")
        if ptype == "LINK_ASSET" and not url:
            asset_id = ph.get("assetId", "")
            if asset_id:
                url = asset_urls.get(asset_id) or f"/p/{asset_id}.html"
        elif url:
            # Convert absolute Nine URLs to proxy-relative paths
            parsed = urlparse(url)
            host = parsed.hostname or ""
            if any(host.endswith(d) for d in SMH_DOMAINS):
                url = parsed.path
        if url and text:
            return f'<a href="{escape(url)}">{escape(text)}</a>'
        return escape(text)

    markup = re.sub(r"<x-placeholder[^>]*>.*?</x-placeholder>", replacer, markup, flags=re.DOTALL)
    markup = re.sub(r"<x-placeholder[^>]*/?>", replacer, markup)
    return markup


def blocks_to_html(blocks, asset_urls=None):
    """Convert article body blocks to HTML."""
    asset_urls = asset_urls or {}
    parts = []
    for block in blocks:
        btype = block.get("type", "")

        if btype == "MARKUP":
            if _is_promo_block(block):
                continue
            markup = block.get("markup", "")
            markup = re.sub(r"\\u([0-9a-fA-F]{4})",
                           lambda m: chr(int(m.group(1), 16)), markup)
            placeholders = block.get("placeholders", [])
            markup = _resolve_placeholders(markup, placeholders, asset_urls)
            parts.append(markup)

        elif btype == "IMAGE":
            img = block.get("image", {})
            media_id = img.get("mediaId", "")
            caption = block.get("caption") or img.get("caption", "")
            credit = img.get("credit", "")
            if media_id:
                src = f"https://static.ffx.io/images/$width_756,q_86,f_auto/{media_id}"
                parts.append(f'<img src="{src}" alt="{escape(caption)}">')
                if caption:
                    parts.append(f'<p class="caption">{escape(caption)} ({escape(credit)})</p>')

        elif btype == "QUOTE":
            markup = block.get("markup", "")
            markup = re.sub(r"\\u([0-9a-fA-F]{4})",
                            lambda m: chr(int(m.group(1), 16)), markup)
            byline = block.get("byline", "")
            parts.append(f"<blockquote>{markup}<br><small>&mdash; {escape(byline)}</small></blockquote>")

        elif btype == "IFRAME":
            url = block.get("url", "")
            if url:
                parts.append(
                    f'<div class="embed"><iframe src="{escape(url)}" scrolling="no" '
                    f'frameborder="0" title="Embedded chart" '
                    f'style="width:100%;border:0;min-height:420px"></iframe></div>'
                )

        elif btype == "TWITTER":
            url = block.get("url", "")
            if url:
                parts.append(
                    f'<blockquote class="tweet"><a href="{escape(url)}">'
                    f'View post on X</a></blockquote>'
                )

        elif btype == "VIDEO":
            provider = block.get("provider") or {}
            vid = provider.get("id", "")
            if vid:
                parts.append(f'<p class="video-note">Video: {escape(vid)}</p>')

    return "\n".join(parts)


def _parse_initial_state(html):
    """Parse ``window.INITIAL_STATE = JSON.parse("...")`` from the page.

    Uses a C-level ``find`` plus ``json.JSONDecoder.raw_decode`` instead of a
    regex that captures the whole 700KB payload into a Python string and then
    copies it twice more (measured: 76ms on the SMH homepage).

    Behaviour is identical to that regex: only the ``JSON.parse("...")`` form
    is accepted (article pages carry a bare object literal, which was — and
    remains — ignored), and a payload that fails to decode stops the search
    rather than falling through to a later occurrence.
    """
    marker = "window.INITIAL_STATE"
    marker_len = len(marker)
    parse_prefix = 'JSON.parse("'
    idx = html.find(marker)
    while idx != -1:
        p = idx + marker_len
        while p < len(html) and html[p] in " \t\r\n":
            p += 1
        if p < len(html) and html[p] == "=":
            value = p + 1
            while value < len(html) and html[value] in " \t\r\n":
                value += 1
            if html.startswith(parse_prefix, value):
                # raw_decode expects to start *at* the opening quote.
                start = value + len(parse_prefix) - 1
                try:
                    encoded, end = json.JSONDecoder().raw_decode(html, start)
                except (json.JSONDecodeError, ValueError, IndexError):
                    return None
                if end < len(html) and html[end] == ")":
                    try:
                        return json.loads(encoded)
                    except (json.JSONDecodeError, ValueError):
                        return None
        idx = html.find(marker, idx + 1)
    return None


def _iter_index_asset_ids(page_data):
    """Yield asset public IDs from an index page in document order."""
    try:
        groups = page_data["index"]["contentUnitGroups"]
    except (KeyError, TypeError):
        return
    for group in groups:
        for cu in group.get("contentUnits", []):
            for entry in cu.get("assets", []):
                aid = entry.get("id")
                if aid:
                    yield aid


def extract_index_meta(html):
    """If the page is a section/index page, return metadata for pagination."""
    state = _parse_initial_state(html)
    if not state:
        return None
    pages = state.get("page")
    if not isinstance(pages, dict):
        return None

    for key, page_data in pages.items():
        if not isinstance(page_data, dict) or page_data.get("type") != "index":
            continue
        ids = list(_iter_index_asset_ids(page_data))
        return {
            "kind": "nav",
            "path": page_data.get("contentPath") or key,
            "brand": "smh",
            "shown_ids": ids,
        }
    return None


TOPIC_RE = re.compile(r"^/topic/[a-z0-9-]*-([0-9a-z]+)$")


def extract_topic_meta(path, html):
    """If the page is a topic/tag page (Metro app), return pagination metadata."""
    m = TOPIC_RE.match(path)
    if not m:
        return None
    tag_id = m.group(1)
    shown = list(dict.fromkeys(re.findall(r"-p([0-9a-z]+)\.html", html)))
    return {
        "kind": "tag",
        "tag_id": tag_id,
        "path": path,
        "brand": "smh",
        "shown_ids": shown,
    }


def extract_afr_topic_meta(path, html):
    """If the page is an AFR topic/tag page, return pagination metadata.

    AFR topic pages are server-rendered (no INITIAL_STATE), so the already
    shown asset IDs are recovered from the article hrefs, which end in
    ``-<assetid>`` (e.g. ``...-p60ylb``).
    """
    m = TOPIC_RE.match(path)
    if not m:
        return None
    tag_id = m.group(1)
    # AFR canonical URLs end in ``-YYYYMMDD-p<assetid>``.
    shown = list(dict.fromkeys(
        re.findall(r"[0-9]{8}-p([0-9a-z]+)(?=[\"'?#]|$)", html)))
    return {
        "kind": "tag",
        "tag_id": tag_id,
        "path": path,
        "brand": "afr",
        "shown_ids": shown,
    }


GRAPHQL_HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": HEADERS["User-Agent"],
    "Origin": UPSTREAM,
    "Referer": UPSTREAM + "/",
}


def _graphql_post(query, variables, url=None, origin=None):
    """POST a query to the FFX GraphQL API and return the data dict."""
    headers = dict(GRAPHQL_HEADERS)
    if origin:
        headers["Origin"] = origin
        headers["Referer"] = origin + "/"
    resp = SESSION.post(
        url or GRAPHQL_URL,
        json={"query": query, "variables": variables},
        headers=headers,
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json().get("data") or {}


ASSET_FIELDS = (
    "assets{id urls{canonical{path brand}} "
    "asset{headlines{headline} about} "
    "featuredImages{landscape16x9{data{id}}}} "
    "pageInfo{endCursor hasNextPage}"
)


def graphql_more(path, brand, since, count=12):
    """Query the FFX GraphQL API for more assets on a navigation path."""
    query = (
        "query CategoryIndexMore($brand:String!,$count:Int!,$path:String!,$since:ID,$types:[String!]!){"
        "assetsConnection:assetsConnectionByNavigationPath("
        "brand:$brand,path:$path,count:$count,sinceID:$since,types:$types){"
        + ASSET_FIELDS + "}}"
    )
    variables = {
        "brand": brand,
        "count": count,
        "path": path,
        "since": since,
        "types": NAV_ASSET_TYPES,
    }
    conn = _graphql_post(query, variables).get("assetsConnection") or {}
    return conn.get("assets", []), conn.get("pageInfo", {})


def graphql_tag_more(tag_id, brand, since, count=12):
    """Query the FFX GraphQL API for more assets under a topic/tag."""
    query = (
        "query TagIndexMore($brand:String!,$count:Int!,$tag:String!,$since:ID){"
        "assetsConnection:assetsConnectionByTag("
        "brand:$brand,tagID:$tag,count:$count,sinceID:$since,"
        "types:[ARTICLE,FEATURE_ARTICLE,LIVE_ARTICLE,GALLERY,VIDEO,BESPOKE]){"
        + ASSET_FIELDS + "}}"
    )
    variables = {"brand": brand, "count": count, "tag": tag_id, "since": since}
    if brand == "afr":
        data = _graphql_post(query, variables,
                             url=AFR_GRAPHQL_URL, origin=AFR_UPSTREAM)
    else:
        data = _graphql_post(query, variables)
    conn = data.get("assetsConnection") or {}
    return conn.get("assets", []), conn.get("pageInfo", {})


MOST_POPULAR_FIELDS = (
    "id urls{canonical{path brand}} "
    "asset{headlines{headline} about} "
    "featuredImages{landscape16x9{data{id}}}"
)


def graphql_most_popular(brand, count=6):
    """Fetch the most-read articles for a brand from the FFX GraphQL API."""
    query = (
        "query MostPopular($brand:String!,$count:Int!){"
        "mostPopularStories(brand:$brand,count:$count){"
        + MOST_POPULAR_FIELDS + "}}"
    )
    data = _graphql_post(query, {"brand": brand, "count": count})
    return data.get("mostPopularStories") or []



def render_more_cards(assets, prefix=""):
    """Render GraphQL asset results as HTML cards."""
    cards = []
    for a in assets:
        aid = a.get("id", "")
        path = prefix + (_get_nested(a, "urls", "canonical", "path") or "")
        headline = _get_nested(a, "asset", "headlines", "headline") or ""
        about = _get_nested(a, "asset", "about") or ""
        img_id = _get_nested(a, "featuredImages", "landscape16x9", "data", "id") or ""

        img_html = ""
        if img_id:
            src = f"https://static.ffx.io/images/$width_400,q_86,f_auto/{img_id}"
            img_html = f'<img class="__smh_card_img" src="{src}" alt="" loading="lazy">'

        about_html = f'<p class="__smh_card_about">{escape(about)}</p>' if about else ""
        cards.append(
            f'<article class="__smh_card" data-id="{escape(aid)}">'
            f'{img_html}'
            f'<h3 class="__smh_card_title"><a href="{escape(path)}">{escape(headline)}</a></h3>'
            f'{about_html}'
            f'</article>'
        )
    return "".join(cards)


SHOW_MORE_CSS = """
<style>
  #__smh_more_wrap { max-width: 900px; margin: 2rem auto; padding: 0 1rem; text-align: center; }
  #__smh_more_btn { padding: .8rem 2rem; font-size: 1rem; border: 0; border-radius: 6px;
    background: #c8102e; color: #fff; cursor: pointer; }
  #__smh_more_btn:disabled { background: #999; cursor: default; }
  #__smh_more_container { max-width: 900px; margin: 1rem auto; padding: 0 1rem;
    display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 1.5rem; }
  .__smh_card { border-bottom: 1px solid #eee; padding-bottom: 1rem; }
  .__smh_card_img { width: 100%; height: auto; border-radius: 4px; }
  .__smh_card_title { font-size: 1.05rem; line-height: 1.3; margin: .5rem 0 .3rem; }
  .__smh_card_title a { color: #111; text-decoration: none; }
  .__smh_card_title a:hover { color: #c8102e; }
  .__smh_card_about { font-size: .9rem; color: #555; line-height: 1.4; margin: 0; }
  @media (max-width: 767px) {
    /* Match the native single-column mobile list layout. */
    #__smh_more_container { grid-template-columns: 1fr; max-width: 720px; }
  }
</style>
"""


def _js_json(value):
    """JSON-encode a value for safe embedding inside a <script> tag."""
    return json.dumps(value).replace("</", "<\\/")


def inject_show_more(html, meta):
    """Inject a working 'Show more' button + JS into an index page."""
    shown = _js_json(meta.get("shown_ids", []))

    script = SHOW_MORE_CSS + """
<div id="__smh_more_container"></div>
<div id="__smh_more_wrap">
  <button id="__smh_more_btn" type="button">Show more</button>
  <div id="__smh_more_status" style="margin-top:.6rem;font-size:.85rem;color:#888;"></div>
</div>
<script>
(function () {
  var btn = document.getElementById('__smh_more_btn');
  var wrap = document.getElementById('__smh_more_wrap');
  var status = document.getElementById('__smh_more_status');
  var container = document.getElementById('__smh_more_container');
  if (!btn) return;
  var cursor = '';
  var kind = %KIND%;
  var path = %PATH%;
  var tag = %TAG%;
  var brand = %BRAND%;
  var shown = new Set(%SHOWN%);
  var firstLoad = true;

  // Hide the site's own (non-functional) "Show more" button, if present.
  Array.prototype.forEach.call(document.querySelectorAll('button'), function (b) {
    if (b.id !== '__smh_more_btn' && /^\\s*show more\\s*$/i.test(b.textContent || '')) {
      b.style.display = 'none';
    }
  });

  function buildUrl() {
    var u = '/__more?brand=' + encodeURIComponent(brand) +
            '&count=' + (firstLoad ? 30 : 12) +
            (cursor ? '&since=' + encodeURIComponent(cursor) : '');
    if (kind === 'tag') {
      return u + '&kind=tag&tag=' + encodeURIComponent(tag);
    }
    return u + '&kind=nav&path=' + encodeURIComponent(path);
  }

  btn.addEventListener('click', function () {
    btn.disabled = true;
    btn.textContent = 'Loading\\u2026';
    status.textContent = '';
    var url = buildUrl();
    fetch(url).then(function (r) { return r.json(); }).then(function (data) {
      if (data.error) throw new Error(data.error);
      var tmp = document.createElement('div');
      tmp.innerHTML = data.html;
      var added = 0;
      var lastAdded = null;
      Array.prototype.forEach.call(tmp.children, function (card) {
        var id = card.getAttribute('data-id');
        if (id && shown.has(id)) return;
        if (id) shown.add(id);
        container.appendChild(card);
        lastAdded = card;
        added++;
      });
      cursor = data.nextCursor || '';
      firstLoad = false;
      if (data.hasNextPage && cursor) {
        btn.disabled = false;
        btn.textContent = 'Show more';
        status.textContent = added ? '' : 'No new articles in this batch \\u2014 click again.';
        if (lastAdded) lastAdded.scrollIntoView({block: 'center'});
      } else if (added) {
        status.textContent = 'You\\u2019ve reached the end.';
        wrap.removeChild(btn);
      } else {
        status.textContent = 'No more articles.';
        wrap.removeChild(btn);
      }
    }).catch(function (e) {
      btn.disabled = false;
      btn.textContent = 'Show more';
      status.textContent = 'Failed to load: ' + e;
      console.error('show more failed', e);
    });
  });
})();
</script>
"""
    script = (script
              .replace("%KIND%", _js_json(meta.get("kind", "nav")))
              .replace("%PATH%", _js_json(meta.get("path", "")))
              .replace("%TAG%", _js_json(meta.get("tag_id", "")))
              .replace("%BRAND%", _js_json(meta["brand"]))
              .replace("%SHOWN%", shown))

    # Insert before the footer so new cards appear at the end of the content.
    # Different templates use <footer> or <div id="footer">.
    low = html.lower()
    candidates = []
    i = low.find("<footer")
    if i != -1:
        candidates.append(i)
    # AFR: place before the "AFR Magazine" pre-footer section.
    i = low.find('data-testid="prefooter"')
    if i != -1:
        lt = low.rfind("<", 0, i)
        if lt != -1:
            candidates.append(lt)
    for pat in ('id="footer"', "id='footer'"):
        i = low.find(pat)
        if i != -1:
            lt = low.rfind("<", 0, i)
            if lt != -1:
                candidates.append(lt)
    if candidates:
        idx = min(candidates)
        return html[:idx] + script + html[idx:]

    body_idx = low.rfind("</body>")
    if body_idx == -1:
        return html + script
    return html[:body_idx] + script + html[body_idx:]


SMH_UI = """
<style id="__smh_ui_style">
  #__smh_bar { position: sticky; top: 0; z-index: 2147483647;
    display: flex; align-items: center; gap: 1rem;
    padding: .5rem 1rem; background: #111; color: #fff;
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    font-size: .9rem; box-shadow: 0 1px 4px rgba(0,0,0,.3); }
  #__smh_bar a { color: #fff; text-decoration: none; font-weight: 600; }
  #__smh_bar a:hover { color: #c8102e; }
  #__smh_bar .__smh_spacer { flex: 1; }
  #__smh_dark_btn { background: transparent; border: 1px solid rgba(255,255,255,.6);
    color: #fff; border-radius: 6px; padding: .25rem .6rem; cursor: pointer;
    font-size: .85rem; line-height: 1; }
  #__smh_dark_btn:hover { border-color: #fff; }
  [data-testid="login-button-myaccount"],
  [data-testid="subscribe-button"] { display: none !important; }
  html.__smh_dark { filter: invert(1) hue-rotate(180deg); }
  html.__smh_dark img, html.__smh_dark video,
  html.__smh_dark canvas, html.__smh_dark svg, html.__smh_dark iframe,
  html.__smh_dark embed, html.__smh_dark object,
  html.__smh_dark #__smh_bar, html.__smh_dark #__smh_ptr { filter: invert(1) hue-rotate(180deg); }
  #__smh_ptr { position: fixed; top: 0; left: 0; right: 0; z-index: 2147483647;
    display: flex; align-items: center; justify-content: center; gap: .45rem;
    height: 56px; margin-top: -56px; background: #111; color: #fff;
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    font-size: .85rem; font-weight: 600; will-change: transform;
    transition: transform .2s ease; }
  #__smh_ptr.__smh_ptr_ready { background: #c8102e; }
  #__smh_ptr.__smh_ptr_spin .__smh_ptr_icon { animation: __smh_spin .8s linear infinite; }
  @keyframes __smh_spin { to { transform: rotate(360deg); } }
  .__smh_mv { display: grid; gap: 1rem; padding: 1rem;
    grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); }
  .__smh_mv .__smh_card { border-bottom: 1px solid rgba(128,128,128,.3);
    padding-bottom: .6rem; }
  .__smh_mv .__smh_card_img { width: 100%; height: auto; border-radius: 4px; }
  .__smh_mv .__smh_card_title { font-size: 1rem; line-height: 1.3; margin: .4rem 0 .2rem; }
  .__smh_mv .__smh_card_title a { color: inherit; text-decoration: none; }
  .__smh_mv .__smh_card_title a:hover { color: #c8102e; }
  .__smh_mv .__smh_card_about { font-size: .85rem; opacity: .75; margin: 0; }
</style>
<div id="__smh_bar">
  <a href="/">SMH</a>
  <span class="__smh_spacer"></span>
  <a href="/afr">AFR &rarr;</a>
  <button id="__smh_dark_btn" type="button" aria-pressed="false">Dark</button>
</div>
<script>
(function () {
  var KEY = 'smh_dark';
  var root = document.documentElement;
  var btn = document.getElementById('__smh_dark_btn');
  function apply(on) {
    root.classList.toggle('__smh_dark', on);
    if (btn) {
      btn.textContent = on ? 'Light' : 'Dark';
      btn.setAttribute('aria-pressed', on ? 'true' : 'false');
    }
  }
  var on = false;
  try { on = localStorage.getItem(KEY) === '1'; } catch (e) {}
  apply(on);
  if (btn) btn.addEventListener('click', function () {
    on = !root.classList.contains('__smh_dark');
    apply(on);
    try { localStorage.setItem(KEY, on ? '1' : '0'); } catch (e) {}
  });
})();
</script>
<div id="__smh_ptr" aria-hidden="true"><span class="__smh_ptr_icon">&#9660;</span><span class="__smh_ptr_text"></span></div>
<script>
(function () {
  if (!('ontouchstart' in window) || window.__smh_ptr_init) return;
  window.__smh_ptr_init = 1;
  var bar = document.getElementById('__smh_ptr');
  if (!bar) return;
  var label = bar.querySelector('.__smh_ptr_text');
  var THRESHOLD = 70, MAX = 110, REST = 56;
  var startY = 0, dy = 0, pulling = false, active = false;
  function render() {
    var d = Math.max(0, Math.min(MAX, dy * 0.5));
    bar.style.transform = 'translateY(' + d + 'px)';
    var ready = dy >= THRESHOLD;
    bar.classList.toggle('__smh_ptr_ready', ready);
    if (label) label.textContent = ready ? 'Release to refresh' : 'Pull to refresh';
  }
  document.addEventListener('touchstart', function (e) {
    if (e.touches.length !== 1 || window.pageYOffset > 0) { active = false; return; }
    active = true; pulling = false; dy = 0; startY = e.touches[0].clientY;
    bar.style.transition = 'none';
  }, { passive: true });
  document.addEventListener('touchmove', function (e) {
    if (!active) return;
    dy = e.touches[0].clientY - startY;
    if (!pulling) {
      if (dy > 8 && window.pageYOffset <= 0) {
        pulling = true;
      } else if (dy < -4) {
        active = false; bar.style.transition = '';
        return;
      }
    }
    if (pulling) {
      if (e.cancelable) e.preventDefault();
      document.documentElement.style.overscrollBehaviorY = 'contain';
      render();
    }
  }, { passive: false });
  function end() {
    if (!active) return;
    active = false;
    bar.style.transition = '';
    document.documentElement.style.overscrollBehaviorY = '';
    if (pulling && dy >= THRESHOLD) {
      bar.classList.add('__smh_ptr_spin');
      bar.style.transform = 'translateY(' + REST + 'px)';
      bar.classList.add('__smh_ptr_ready');
      if (label) label.textContent = 'Refreshing\u2026';
      setTimeout(function () { location.reload(); }, 120);
    } else {
      bar.classList.remove('__smh_ptr_ready');
      bar.style.transform = 'translateY(0)';
      if (label) label.textContent = '';
    }
    pulling = false; dy = 0;
  }
  document.addEventListener('touchend', end, { passive: true });
  document.addEventListener('touchcancel', end, { passive: true });
})();
</script>
<script>
(function () {
  var TITLE = /^\\s*most viewed today\\s*$/i;
  function findSection() {
    var hs = document.querySelectorAll('h1,h2,h3');
    for (var i = 0; i < hs.length; i++) {
      if (TITLE.test(hs[i].textContent || '')) {
        return hs[i].closest('section') || hs[i].parentElement;
      }
    }
    return null;
  }
  function hasArticles(s) {
    return !!s.querySelector('a[href*=".html"]');
  }
  function fill() {
    var s = findSection();
    if (!s || s.getAttribute('data-smh-mv') === 'done' || hasArticles(s)) return;
    s.setAttribute('data-smh-mv', 'done');
    fetch('/__mostviewed?brand=smh&count=6')
      .then(function (r) { return r.json(); })
      .then(function (d) {
        var sec = findSection();
        if (!d || !d.html || !sec || hasArticles(sec)) return;
        var wrap = sec.querySelector('.__smh_mv');
        if (!wrap) {
          wrap = document.createElement('div');
          wrap.className = '__smh_mv';
          sec.appendChild(wrap);
        }
        wrap.innerHTML = d.html;
        var header = sec.querySelector('header');
        Array.prototype.slice.call(sec.children).forEach(function (c) {
          if (c !== header && c !== wrap) c.style.display = 'none';
        });
        sec.style.height = 'auto';
        sec.setAttribute('data-smh-mv', 'done');
      })
      .catch(function () {});
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', fill);
  } else {
    fill();
  }
  var t;
  new MutationObserver(function () {
    clearTimeout(t);
    t = setTimeout(fill, 300);
  }).observe(document.documentElement, { childList: true, subtree: true });
})();
</script>
"""


def inject_smh_ui(html):
    """Inject the SMH header bar + dark mode toggle near the top of a page."""
    m = re.search(r"<body[^>]*>", html, re.IGNORECASE)
    if m:
        return html[:m.end()] + SMH_UI + html[m.end():]
    return SMH_UI + html


PROMO_KEYWORDS = re.compile(
    r"newsletter|sign[\s-]?up|subscribe|inbox|notifications?|briefing", re.I
)

_PROMO_ALLOWED_TAGS = {
    "p", "b", "i", "strong", "em", "x-placeholder",
    "a", "br", "span", "u", "sub", "sup", "small",
}


def _is_promo_block(block):
    """Detect end-of-article newsletter/app promo blocks.

    These are MARKUP blocks whose paragraph is entirely bold-italic (e.g.
    ``<p><b><i>...</i></b></p>``) and which link to a newsletter signup or
    app notifications. Detected structurally rather than by matching copy.
    """
    if block.get("type") != "MARKUP":
        return False

    markup = block.get("markup", "") or ""
    tags = {t.lower() for t in re.findall(r"<\s*/?\s*([a-zA-Z0-9-]+)", markup)}
    if not tags <= _PROMO_ALLOWED_TAGS:
        return False

    for ph in block.get("placeholders") or []:
        if not isinstance(ph, dict):
            continue
        if PROMO_KEYWORDS.search(f"{ph.get('url', '')} {ph.get('text', '')}"):
            return True

    return bool(PROMO_KEYWORDS.search(re.sub(r"<[^>]+>", " ", markup)))


def _fully_bold_italic(el):
    """True if every text node inside el has both a bold and italic ancestor."""
    strings = [s for s in el.strings if s.strip()]
    if not strings:
        return False
    for s in strings:
        bold = italic = False
        node = s.parent
        while node is not None and node is not el:
            if node.name in ("b", "strong"):
                bold = True
            elif node.name in ("i", "em"):
                italic = True
            node = node.parent
        if not (bold and italic):
            return False
    return True


def strip_promos(html):
    """Remove newsletter/notification promo paragraphs from article body HTML.

    Matches the bold-italic signup blurbs structurally rather than by their
    (changing) copy.
    """
    if not html:
        return html
    soup = BeautifulSoup(html, "html.parser")
    for p in soup.find_all("p"):
        if p.find(["p", "div", "ul", "ol", "h1", "h2", "h3", "h4", "h5",
                   "h6", "blockquote"]):
            continue
        text = p.get_text(" ", strip=True)
        if not text or not PROMO_KEYWORDS.search(text):
            continue
        if p.find("a", href=re.compile(r"newsletter|sign[\s-]?up|subscribe", re.I)):
            p.decompose()
        elif _fully_bold_italic(p):
            p.decompose()
    return str(soup)



EMBED_RESIZE_SCRIPT = """<script>
window.addEventListener('message', function (e) {
  var heights = e.data && e.data['datawrapper-height'];
  if (!heights) return;
  var frames = document.querySelectorAll('iframe');
  for (var key in heights) {
    for (var i = 0; i < frames.length; i++) {
      if (frames[i].contentWindow === e.source) {
        frames[i].style.height = heights[key] + 'px';
      }
    }
  }
});
</script>"""


ARTICLE_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>{title}</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    body {{ font-family: Georgia, 'Times New Roman', serif;
           max-width: 720px; margin: 2rem auto; padding: 0 1rem;
           color: #1a1a1a; line-height: 1.7; font-size: 18px; }}
    h1 {{ font-size: 2rem; margin-bottom: 0.5rem; line-height: 1.2; }}
    .byline {{ color: #666; font-size: 0.9rem; margin-bottom: 1.5rem; }}
    .overview {{ font-style: italic; color: #444; margin-bottom: 1.5rem;
                border-left: 3px solid #c8102e; padding-left: 1rem; }}
    p {{ margin-bottom: 1.2rem; }}
    img {{ max-width: 100%; height: auto; margin: 1rem 0; }}
    .caption {{ font-size: 0.85rem; color: #666; margin-top: -0.5rem; }}
    blockquote {{ border-left: 3px solid #ccc; padding-left: 1rem;
                 color: #555; font-style: italic; margin: 1.5rem 0; }}
    a {{ color: #c8102e; }}
    .embed {{ margin: 1.5rem 0; }}
    .embed iframe {{ width: 100%; border: 0; min-height: 420px; }}
    .nav {{ margin-bottom: 2rem; font-size: 0.9rem; }}
    .nav a {{ color: #666; text-decoration: none; }}
    .nav a:hover {{ color: #c8102e; }}
    .footer {{ margin-top: 3rem; padding-top: 1rem; border-top: 1px solid #ddd;
              font-size: 0.85rem; color: #999; }}
  </style>
</head>
<body>
  <div class="nav"><a href="/">← Back to homepage</a></div>
  <h1>{title}</h1>
  {overview}
  <div class="byline">{byline}<time datetime="{date}" data-date="{date}">{date_short}</time></div>
  {body}
  <div class="footer">
    <p>Source: <a href="{url}">{url}</a></p>
  </div>
  {embed_script}
  <script>
  document.querySelectorAll('time[data-date]').forEach(function (t) {{
    var d = new Date(t.getAttribute('data-date'));
    if (isNaN(d.getTime())) return;
    if (t.classList.contains('live-time')) {{
      t.textContent = d.toLocaleString(undefined,
        {{ day: 'numeric', month: 'short', hour: 'numeric', minute: '2-digit' }});
    }} else {{
      t.textContent = d.toLocaleDateString(undefined,
        {{ year: 'numeric', month: 'long', day: 'numeric' }});
    }}
  }});
  </script>
</body>
</html>"""


# ── AFR-specific functions ──────────────────────────────────────────────────


def _afr_extract_hydration_data(html):
    """Extract window.__staticRouterHydrationData from AFR HTML."""
    marker = "window.__staticRouterHydrationData = JSON.parse(\""
    idx = html.find(marker)
    if idx == -1:
        return None

    # Position of the opening quote of the JSON.parse string literal.
    quote_at = idx + len(marker) - 1
    if quote_at < 0 or quote_at >= len(html):
        return None

    try:
        # raw_decode parses the JSON string literal in C, handling escapes
        # correctly. Scanning for the closing quote by hand was both slow
        # (a Python-level pass over a megabyte of HTML) and wrong for the
        # `\\"` sequence, which would run past the real terminator.
        encoded, _ = json.JSONDecoder().raw_decode(html, quote_at)
        return json.loads(encoded)
    except (json.JSONDecodeError, ValueError, IndexError):
        return None


# LRU cache of resolved AFR story ids (key order = least to most recent).
AFR_STORY_CACHE_MAX = 1000
_afr_story_cache = OrderedDict()


def strip_afr_header_cruft(html):
    """Remove AFR homepage header cruft that the generic pass misses:
    the 'Today's Paper' top bar, the empty market-snapshot loading bar and
    empty styled-ad shells (their filling scripts are blocked)."""
    soup = BeautifulSoup(html, "html.parser")

    # 'Today's Paper / Markets / Data / Events / Lists' top bar (desktop
    # and mobile variants). CSS-module class names are build-hashed, so
    # match on the stable suffixes.
    targets = [el for el in soup.find_all("div")
               if any(c.endswith(sfx) for c in (el.get("class") or [])
                      for sfx in ("-headerTop", "-topHeader"))]
    for el in targets:
        if el.parent is not None:
            el.decompose()

    for el in soup.find_all(attrs={"data-testid": "market-snapshot-loading"}):
        if not el.get_text(strip=True):
            el.decompose()

    for el in soup.find_all("div", class_=re.compile(r"styledAd")):
        if el.get_text(strip=True) or el.find(["img", "iframe", "svg", "canvas"]):
            continue
        el.decompose()

    # Sticky leaderboard ad placeholder.
    for el in soup.find_all(id="stickyLeaderboard"):
        el.decompose()

    # Newsletter driver tiles (e.g. 'StoryTileDriverSmall') and any content
    # unit left empty by their removal.
    for el in soup.find_all(attrs={"data-testid": "StoryTileDriverSmall"}):
        el.decompose()
    for sec in soup.find_all("section", attrs={"data-contentunit-id": True}):
        if sec.find_parent(attrs={"data-contentunit-id": True}):
            continue
        if not sec.get_text(strip=True) and not sec.find(
                ["img", "iframe", "svg", "canvas", "video"]):
            sec.decompose()

    # Lazy-loaded images: the real URLs live in data-src/data-srcset and the
    # swap-in script is blocked, so promote them to src/srcset.
    for img in soup.find_all("img"):
        if img.get("data-src"):
            img["src"] = img["data-src"]
            del img["data-src"]
        if img.get("data-srcset"):
            img["srcset"] = img["data-srcset"]
            del img["data-srcset"]
    for src_el in soup.find_all("source"):
        if src_el.get("data-srcset"):
            src_el["srcset"] = src_el["data-srcset"]
            del src_el["data-srcset"]

    return str(soup)


def _afr_resolve_story(story_id):
    """Resolve an AFR story ID to (canonical_path, headline) via GraphQL. Cached."""
    if story_id in _afr_story_cache:
        _afr_story_cache.move_to_end(story_id)
        return _afr_story_cache[story_id]

    result = (None, None)
    try:
        resp = SESSION.post(
            AFR_GRAPHQL_URL,
            json={
                "query": (
                    "query($id: String!) { asset(id: $id) { "
                    "urls { canonical { path } } "
                    "headlines { headline } } }"
                ),
                "variables": {"id": story_id},
            },
            headers={"Content-Type": "application/json"},
            timeout=10,
        )
        asset = resp.json().get("data", {}).get("asset")
        if asset:
            path = asset.get("urls", {}).get("canonical", {}).get("path", "")
            headline = asset.get("headlines", {}).get("headline", "")
            if path:
                result = (path, headline)
    except (requests.RequestException, json.JSONDecodeError, ValueError):
        log.warning("AFR: Failed to resolve story id %s", story_id)

    _afr_story_cache[story_id] = result
    while len(_afr_story_cache) > AFR_STORY_CACHE_MAX:
        _afr_story_cache.popitem(last=False)
    return result


def _afr_resolve_placeholders(body, placeholders):
    """Replace <x-placeholder id="..."> tags with resolved content."""
    if not placeholders:
        body = re.sub(r"<x-placeholder[^>]*>", "", body)
        body = re.sub(r"</x-placeholder>", "", body)
        return body

    def replacer(m):
        tag = m.group(0)
        id_m = re.search(r'id="([^"]*)"', tag)
        if not id_m:
            return ""
        pid = id_m.group(1)
        ph = placeholders.get(pid)
        if not ph:
            return ""

        ptype = ph.get("type", "")
        data = ph.get("data", {})

        if ptype == "image":
            file_name = data.get("fileName", "")
            if file_name:
                src = f"https://static.ffx.io/images/{file_name}"
                alt = data.get("altText", "")
                caption = data.get("caption", "")
                credit = data.get("credit", "")
                img_html = f'<img src="{src}" alt="{escape(alt)}">'
                if caption:
                    img_html += f'<p class="caption">{escape(caption)}'
                    if credit:
                        img_html += f' ({escape(credit)})'
                    img_html += '</p>'
                return img_html
            return ""

        elif ptype == "linkExternal":
            url = data.get("url", "")
            text = data.get("text", "")
            new_tab = data.get("newTab", False)
            target = ' target="_blank" rel="noopener"' if new_tab else ""
            if url and text:
                return f'<a href="{escape(url)}"{target}>{escape(text)}</a>'
            return escape(text)

        elif ptype == "relatedStory":
            story_id = data.get("id", "")
            if story_id:
                path, headline = _afr_resolve_story(story_id)
                if path:
                    text = headline or "Related story"
                    return (
                        f'<p class="related"><strong>Related:</strong> '
                        f'<a href="/afr{escape(path)}">{escape(text)}</a></p>'
                    )
                # Fallback — link to the external article directly
                return f'<p class="related"><strong>Related:</strong> <a href="https://www.afr.com/{story_id}">Related story</a></p>'
            return ""

        return ""

    body = re.sub(r"<x-placeholder[^>]*>.*?</x-placeholder>", replacer, body, flags=re.DOTALL)
    body = re.sub(r"<x-placeholder[^>]*/?>", replacer, body)
    return body


def _afr_resolve_unicode_escapes(text):
    """Resolve \\uXXXX unicode escapes in AFR body text."""
    return re.sub(
        r"\\u([0-9a-fA-F]{4})",
        lambda m: chr(int(m.group(1), 16)),
        text,
    )


def afr_extract_article_data(html, data=_STATE_UNSET):
    """Extract article data from AFR's __staticRouterHydrationData.

    Pass ``data`` when the payload has already been parsed, so a request
    does not scan and decode the same blob twice.
    """
    if data is _STATE_UNSET:
        data = _afr_extract_hydration_data(html)
    if not data:
        return None

    loader = data.get("loaderData", {})
    for key, val in loader.items():
        content_data = val.get("content", {})
        if not isinstance(content_data, dict):
            continue

        asset = content_data.get("asset", {})
        if not isinstance(asset, dict):
            continue

        # Check for article body
        body = asset.get("body", "")
        if not body:
            continue

        # Decode unicode escapes in body
        body = _afr_resolve_unicode_escapes(body)

        # Resolve placeholders
        placeholders = asset.get("bodyPlaceholders", {})
        body = _afr_resolve_placeholders(body, placeholders)

        # Rewrite any raw AFR links in the body to go through the proxy
        def _rewrap(m):
            quote, url = m.group(1), m.group(2)
            return f'href={quote}{rewrite_url(url, "", AFR_DOMAINS, "/afr")}{quote}'

        body = re.sub(r'href=(["\'])(.*?)\1', _rewrap, body)

        # Extract metadata
        headline = asset.get("headlines", {}).get("headline", "")
        byline = asset.get("byline", "")
        about = asset.get("about", "")

        # Extract dates
        dates = content_data.get("dates", {})
        published = dates.get("published") or dates.get("firstPublished") or ""

        # Extract authors
        authors = []
        participants = content_data.get("participants", {})
        if isinstance(participants, dict):
            for author in participants.get("authors", []):
                if isinstance(author, dict):
                    name = author.get("name", "")
                    if name:
                        authors.append(name)
        if authors and not byline:
            byline = ", ".join(authors)

        # Extract featured image
        featured = content_data.get("featuredImages", {})
        hero_img = ""
        if isinstance(featured, dict):
            for ratio in ["landscape16x9", "landscape3x2", "square1x1"]:
                img_data = featured.get(ratio, {})
                if isinstance(img_data, dict):
                    file_name = img_data.get("data", {}).get("fileName", "")
                    if file_name:
                        hero_img = f"https://static.ffx.io/images/{file_name}"
                        break

        return {
            "headline": headline,
            "byline": byline,
            "about": about,
            "date": published,
            "body": body,
            "hero_img": hero_img,
            "url": content_data.get("urls", {}).get("canonical", {}).get("path", ""),
        }

    return None


def afr_extract_live_article_data(html, data=_STATE_UNSET):
    """Extract a live blog from AFR's __staticRouterHydrationData.

    AFR live articles have posts in ``content.asset.posts`` and optionally
    ``content.asset.pinnedPosts``.  Each post has its own body, headline,
    byline and dates.  Posts are returned newest-first.

    Pass ``data`` when the payload has already been parsed (see
    :func:`afr_extract_article_data`).
    """
    if data is _STATE_UNSET:
        data = _afr_extract_hydration_data(html)
    if not data:
        return None

    loader = data.get("loaderData", {})
    for key, val in loader.items():
        content_data = val.get("content", {})
        if not isinstance(content_data, dict):
            continue

        asset = content_data.get("asset", {})
        if not isinstance(asset, dict):
            continue

        asset_type = asset.get("assetType", "")
        is_live = asset.get("isLive", False)
        if asset_type != "liveArticle" and not is_live:
            continue

        posts_raw = asset.get("posts") or []
        pinned_raw = asset.get("pinnedPosts") or []

        # Merge pinned + posts, dedup by id
        seen = set()
        posts = []
        for p in pinned_raw + posts_raw:
            if not isinstance(p, dict):
                continue
            pid = p.get("id")
            if not pid or pid in seen:
                continue
            seen.add(pid)
            post_asset = p.get("asset") or {}
            body = post_asset.get("body", "")
            if body:
                body = _afr_resolve_unicode_escapes(body)
                placeholders = post_asset.get("bodyPlaceholders") or {}
                body = _afr_resolve_placeholders(body, placeholders)
                # Rewrite AFR links
                def _rewrap(m):
                    q, url = m.group(1), m.group(2)
                    return f'href={q}{rewrite_url(url, "", AFR_DOMAINS, "/afr")}{q}'
                body = re.sub(r'href=(["\'])(.*?)\1', _rewrap, body)
            dates = p.get("dates") or {}
            published = dates.get("published") or dates.get("firstPublished") or ""
            post_headline = _get_nested(post_asset, "headlines", "headline") or ""
            byline = post_asset.get("byline", "")
            posts.append({
                "id": pid,
                "headline": post_headline,
                "published": published,
                "byline": byline,
                "body": body,
            })

        # Sort newest first (same as SMH)
        posts.sort(key=lambda p: p["published"], reverse=True)

        if not posts:
            continue

        # Extract article-level metadata
        headline = _get_nested(asset, "headlines", "headline") or ""
        byline = asset.get("byline", "")
        if isinstance(byline, list):
            names = []
            for b in byline:
                if isinstance(b, dict):
                    name = b.get("name", "")
                    if name:
                        names.append(name)
            byline = ", ".join(names) if names else ""
        about = asset.get("about", "")
        dates = content_data.get("dates") or {}
        published = dates.get("published") or dates.get("firstPublished") or ""
        hero_img = ""
        featured = content_data.get("featuredImages") or {}
        if isinstance(featured, dict):
            for ratio in ("landscape16x9", "landscape3x2", "square1x1"):
                img_data = featured.get(ratio, {})
                if isinstance(img_data, dict):
                    file_name = img_data.get("data", {}).get("fileName", "")
                    if file_name:
                        hero_img = f"https://static.ffx.io/images/{file_name}"
                        break

        return {
            "headline": headline,
            "byline": byline,
            "about": about,
            "date": published,
            "hero_img": hero_img,
            "posts": posts,
            "url": (content_data.get("urls") or {}).get("canonical", {}).get("path", ""),
        }

    return None


AFR_ARTICLE_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>{title}</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    body {{ font-family: Georgia, 'Times New Roman', serif;
           max-width: 720px; margin: 2rem auto; padding: 0 1rem;
           color: #1a1a1a; line-height: 1.7; font-size: 18px; }}
    h1 {{ font-size: 2rem; margin-bottom: 0.5rem; line-height: 1.2; }}
    .byline {{ color: #666; font-size: 0.9rem; margin-bottom: 1.5rem; }}
    .overview {{ font-style: italic; color: #444; margin-bottom: 1.5rem;
                border-left: 3px solid #0f6cc9; padding-left: 1rem; }}
    p {{ margin-bottom: 1.2rem; }}
    img {{ max-width: 100%; height: auto; margin: 1rem 0; }}
    .caption {{ font-size: 0.85rem; color: #666; margin-top: -0.5rem; }}
    blockquote {{ border-left: 3px solid #ccc; padding-left: 1rem;
                 color: #555; font-style: italic; margin: 1.5rem 0; }}
    a {{ color: #0f6cc9; }}
    .embed {{ margin: 1.5rem 0; }}
    .embed iframe {{ width: 100%; border: 0; min-height: 420px; }}
    .nav {{ margin-bottom: 2rem; font-size: 0.9rem; }}
    .nav a {{ color: #666; text-decoration: none; }}
    .nav a:hover {{ color: #0f6cc9; }}
    .footer {{ margin-top: 3rem; padding-top: 1rem; border-top: 1px solid #ddd;
              font-size: 0.85rem; color: #999; }}
  </style>
</head>
<body>
  <div class="nav"><a href="/afr">← Back to AFR homepage</a></div>
  <h1>{title}</h1>
  {hero_img}
  {overview}
  <div class="byline">{byline}<time datetime="{date}" data-date="{date}">{date_short}</time></div>
  {body}
  <div class="footer">
    <p>Source: <a href="{url}">{url}</a></p>
  </div>
  {embed_script}
  <script>
  document.querySelectorAll('time[data-date]').forEach(function (t) {{
    var d = new Date(t.getAttribute('data-date'));
    if (!isNaN(d.getTime())) {{
      t.textContent = d.toLocaleDateString(undefined,
        {{ year: 'numeric', month: 'long', day: 'numeric' }});
    }}
  }});
  </script>
</body>
</html>"""


AFR_LIVE_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>{title}</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    body {{ font-family: Georgia, 'Times New Roman', serif;
           max-width: 720px; margin: 2rem auto; padding: 0 1rem;
           color: #1a1a1a; line-height: 1.7; font-size: 18px; }}
    h1 {{ font-size: 2rem; margin-bottom: 0.5rem; line-height: 1.2; }}
    .byline {{ color: #666; font-size: 0.9rem; margin-bottom: 1.5rem; }}
    .overview {{ font-style: italic; color: #444; margin-bottom: 1.5rem;
                border-left: 3px solid #0f6cc9; padding-left: 1rem; }}
    img {{ max-width: 100%; height: auto; margin: 1rem 0; }}
    .nav {{ margin-bottom: 2rem; font-size: 0.9rem; }}
    .nav a {{ color: #666; text-decoration: none; }}
    .nav a:hover {{ color: #0f6cc9; }}
    .footer {{ margin-top: 3rem; padding-top: 1rem; border-top: 1px solid #ddd;
              font-size: 0.85rem; color: #999; }}
    #__live_posts {{ max-width: 720px; }}
    .live-post {{ border-top: 1px solid #e5e5e5; padding: 1.25rem 0; }}
    .live-post:first-child {{ border-top: 0; }}
    .live-post-meta {{ font-size: .8rem; color: #777; margin-bottom: .35rem; }}
    .live-post-meta time {{ font-weight: 700; color: #c8102e;
      text-transform: uppercase; letter-spacing: .03em; }}
    .live-post-title {{ margin: .1rem 0 .5rem; font-size: 1.15rem; line-height: 1.3; }}
    .live-post p {{ margin: .6rem 0; }}
    .live-header {{ display: flex; align-items: center; gap: .6rem;
      margin: 0 0 1rem; }}
    .live-badge {{ background: #c8102e; color: #fff; font-weight: 700;
      font-size: .7rem; letter-spacing: .08em; padding: .15rem .5rem;
      border-radius: 3px; }}
    #__live_updated {{ font-size: .8rem; color: #999; }}
  </style>
</head>
<body>
  <div class="nav"><a href="/afr">← Back to AFR homepage</a></div>
  <h1>{title}</h1>
  {hero_img}
  {overview}
  <div class="byline">{byline}<time datetime="{date}" data-date="{date}">{date_short}</time></div>
  <div class="live-header"><span class="live-badge">LIVE</span>
    <span id="__live_updated"></span></div>
  <div id="__live_posts">{posts}</div>
  <div class="footer">
    <p>Source: <a href="{url}">{url}</a></p>
  </div>
  {focus_script}
  <script>
  document.querySelectorAll('time[data-date]').forEach(function (t) {{
    var d = new Date(t.getAttribute('data-date'));
    if (!isNaN(d.getTime())) {{
      t.textContent = d.toLocaleString(undefined,
        {{ day: 'numeric', month: 'short', hour: 'numeric', minute: '2-digit' }});
    }}
  }});
  (function () {{
    var list = document.getElementById('__live_posts');
    if (!list) return;
    var refresh = function () {{
      fetch(location.href, {{ headers: {{ 'X-Requested-With': 'fetch' }} }})
        .then(function (r) {{ return r.text(); }})
        .then(function (txt) {{
          var doc = new DOMParser().parseFromString(txt, 'text/html');
          var have = {{}};
          list.querySelectorAll('.live-post[data-post-id]').forEach(function (e) {{
            have[e.getAttribute('data-post-id')] = 1;
          }});
          var fresh = [];
          doc.querySelectorAll('.live-post[data-post-id]').forEach(function (p) {{
            var id = p.getAttribute('data-post-id');
            if (!have[id]) {{ have[id] = 1; fresh.push(p); }}
          }});
          if (fresh.length) {{
            fresh.reverse().forEach(function (p) {{
              var node = document.importNode(p, true);
              node.querySelectorAll('time[data-date]').forEach(function (t) {{
                var d = new Date(t.getAttribute('data-date'));
                if (!isNaN(d.getTime())) {{
                  t.textContent = d.toLocaleString(undefined,
                    {{ day: 'numeric', month: 'short', hour: 'numeric', minute: '2-digit' }});
                }}
              }});
              list.insertBefore(node, list.firstChild);
            }});
            var b = document.getElementById('__live_updated');
            if (b) b.textContent = 'Updated ' + new Date().toLocaleTimeString();
          }}
        }})
        .catch(function () {{}});
    }};
    setInterval(refresh, 30000);
  }})();
  </script>
</body>
</html>"""


def afr_build_article_html(article, upstream_url):
    """Build clean HTML for an AFR article."""
    hero_img = ""
    if article.get("hero_img"):
        hero_img = f'<img src="{article["hero_img"]}" alt="" style="max-width:100%;height:auto;margin-bottom:1.5rem;">'

    overview = ""
    if article.get("about"):
        overview = f'<div class="overview">{escape(article["about"])}</div>'

    date = (article.get("date") or "")
    byline = article.get("byline", "")
    byline_html = f"By {escape(byline)} &mdash; " if byline else ""

    return AFR_ARTICLE_TEMPLATE.format(
        title=escape(article.get("headline", "")),
        overview=overview,
        byline=byline_html,
        date=escape(date),
        date_short=escape(date[:10]),
        body=strip_promos(article.get("body", "")),
        hero_img=hero_img,
        url=escape(upstream_url),
        embed_script=EMBED_RESIZE_SCRIPT,
    )


@app.route("/afr/", defaults={"path": ""}, methods=["GET"])
@app.route("/afr/<path:path>", methods=["GET"])
def afr_proxy(path):
    """Proxy for afr.com."""
    upstream = _upstream_url(AFR_UPSTREAM, path)

    log.debug("AFR Proxying: %s", upstream)

    cache_key = _page_cache_key("afr")
    hit = _cached_response(cache_key)
    if hit is not None:
        log.debug("AFR page cache hit: %s", request.path)
        return hit

    try:
        resp = SESSION.get(upstream, headers=HEADERS, timeout=15, allow_redirects=True)
    except requests.RequestException as e:
        log.exception("AFR: Failed to fetch upstream")
        return f"<h1>Upstream fetch failed</h1><p>{escape(e)}</p>", 502

    if resp.status_code >= 400:
        return _upstream_error_response(resp, upstream)

    content_type = resp.headers.get("Content-Type", "")

    # Only rewrite HTML responses
    if "text/html" not in content_type:
        return Response(resp.content,
                       content_type=content_type,
                       headers={"Cache-Control": "public, max-age=300"})

    html = resp.text

    # Parse the hydration payload once and share it between both extractors.
    hydration_data = _afr_extract_hydration_data(html)

    # Try to extract clean article data
    article = afr_extract_article_data(html, hydration_data)

    if article and article.get("headline"):
        log.debug("AFR Clean article: %s", article["headline"])
        rendered = inject_smh_ui(afr_build_article_html(article, upstream))
        # Clean articles are immutable once published: cache the render so a
        # repeat read skips the upstream fetch, hydration parse and rewrite.
        return _cache_and_respond(cache_key, rendered, "text/html", {}, 200)

    # Try to extract live blog data
    live = afr_extract_live_article_data(html, hydration_data)

    if live and live.get("headline"):
        log.debug("AFR Live blog: %s (%d posts)",
                  live["headline"], len(live["posts"]))
        # Determine which post to scroll to
        focus_post = request.args.get("post", "")

        # Render posts as HTML
        live_posts_html = []
        for post in live["posts"]:
            post_id = post["id"]
            anchor = f' id="post-{escape(post_id)}"' if post_id else ""
            body = strip_promos(post.get("body", ""))
            headline = post.get("headline") or ""
            head_html = (f'<h3 class="live-post-title">{escape(headline)}</h3>'
                         if headline else "")
            byline = post.get("byline") or ""
            by = (f'<span class="live-post-byline"> {escape(byline)}</span>'
                  if byline else "")
            published = post.get("published") or ""
            live_posts_html.append(
                f'<article class="live-post" data-post-id="{escape(post_id)}" '
                f'data-published="{escape(published)}"{anchor}>'
                f'<div class="live-post-meta">'
                f'<time class="live-time" datetime="{escape(published)}" '
                f'data-date="{escape(published)}">{escape(published[:16])}</time>'
                f'{by}</div>'
                f'{head_html}{body}</article>'
            )
        posts_html = "\n".join(live_posts_html)

        hero_img = ""
        if live.get("hero_img"):
            hero_img = f'<img src="{live["hero_img"]}" alt="" style="max-width:100%;height:auto;margin-bottom:1.5rem;">'
        overview = ""
        if live.get("about"):
            overview = f'<div class="overview">{escape(live["about"])}</div>'
        date = live.get("date") or ""
        byline = live.get("byline", "")
        byline_html = f"By {escape(byline)} &mdash; " if byline else ""

        # Focus script: scroll to the ?post= anchor
        focus_script = ""
        if focus_post:
            safe_id = escape(focus_post)
            focus_script = (
                '<script>'
                f'var el=document.getElementById("post-{safe_id}");'
                'if(el)el.scrollIntoView({behavior:"smooth",block:"start"});'
                '</script>'
            )

        rendered = AFR_LIVE_TEMPLATE.format(
            title=escape(live.get("headline", "")),
            overview=overview,
            byline=byline_html,
            date=escape(date),
            date_short=escape(date[:10]),
            hero_img=hero_img,
            url=escape(upstream),
            posts=posts_html,
            focus_script=focus_script,
        )
        return Response(inject_smh_ui(rendered), mimetype="text/html",
                        headers={"Cache-Control": "no-store"})

    # Not an article — proxy with link rewriting (single pass)
    rewritten = process_html(
        html, upstream + "/", AFR_DOMAINS, "/afr",
        AFR_BLOCK_PATTERNS,
        extra_id_re=ADSPOT_ID_RE,
        strip_state_scripts=True,
    )
    rewritten = strip_afr_header_cruft(rewritten)

    # Inject a working "Show more" button on topic pages
    req_path = "/" + path if path else "/"
    page_meta = extract_afr_topic_meta(req_path, html)
    if page_meta:
        log.debug("AFR pagination page %s — injecting show more", req_path)
        rewritten = inject_show_more(rewritten, page_meta)

    rewritten = inject_smh_ui(rewritten)

    return _cache_and_respond(cache_key, rewritten, "text/html",
                              INDEX_CACHE_HEADER, 200)


@app.route("/__more")
def more():
    """Return a JSON batch of article cards for index/topic pagination."""
    kind = request.args.get("kind", "nav").strip()
    brand = request.args.get("brand", "smh").strip()
    since = request.args.get("since", "").strip() or None
    try:
        count = max(1, min(50, int(request.args.get("count", "30"))))
    except ValueError:
        count = 30

    try:
        if kind == "tag":
            tag_id = request.args.get("tag", "").strip()
            if not tag_id:
                return {"error": "missing tag"}, 400
            assets, page_info = graphql_tag_more(tag_id, brand, since, count=count)
        else:
            path = request.args.get("path", "").strip()
            if not path:
                return {"error": "missing path"}, 400
            assets, page_info = graphql_more(path, brand, since, count=count)
    except requests.RequestException as e:
        log.exception("GraphQL more request failed")
        return {"error": str(e)}, 502

    prefix = "/afr" if brand == "afr" else ""
    return {
        "html": render_more_cards(assets, prefix),
        "nextCursor": page_info.get("endCursor"),
        "hasNextPage": bool(page_info.get("hasNextPage")),
    }


@app.route("/__mostviewed")
def most_viewed():
    """Return a JSON batch of most-read article cards for the Most Viewed strip."""
    brand = request.args.get("brand", "smh").strip()
    try:
        count = max(1, min(20, int(request.args.get("count", "6"))))
    except ValueError:
        count = 6

    try:
        assets = graphql_most_popular(brand, count=count)
    except requests.RequestException as e:
        log.exception("GraphQL most-popular request failed")
        return {"error": str(e)}, 502

    return {"html": render_more_cards(assets)}



@app.route("/", defaults={"path": ""}, methods=["GET"])
@app.route("/<path:path>", methods=["GET"])
def proxy(path):
    # Build upstream URL
    upstream = _upstream_url(UPSTREAM, path)

    log.debug("Proxying: %s", upstream)

    cache_key = _page_cache_key("smh")
    hit = _cached_response(cache_key)
    if hit is not None:
        log.debug("Page cache hit: %s", request.path)
        return hit

    try:
        resp = SESSION.get(upstream, headers=HEADERS, timeout=15, allow_redirects=True)
    except requests.RequestException as e:
        log.exception("Failed to fetch upstream")
        return f"<h1>Upstream fetch failed</h1><p>{escape(e)}</p>", 502

    if resp.status_code >= 400:
        return _upstream_error_response(resp, upstream)

    content_type = resp.headers.get("Content-Type", "")

    # Only rewrite HTML responses
    if "text/html" not in content_type:
        return Response(resp.content,
                       content_type=content_type,
                       headers={"Cache-Control": "public, max-age=300"})

    html = resp.text

    # Try to extract clean article data (regular article or live blog).
    # The APOLLO_STATE blob is parsed once and shared by both extractors.
    hydration_data = _extract_apollo_state(html)
    article = (extract_article_data(html, hydration_data)
               or extract_live_article_data(html, hydration_data))

    if article and article.get("headline"):
        is_live = "posts" in article
        if is_live:
            log.debug("Clean live blog: %s (%d posts)",
                      article["headline"], len(article["posts"]))
            body_html = (LIVE_FEED_STYLE
                         + '<div class="live-header"><span class="live-badge">LIVE</span>'
                         + '<span id="__live_updated"></span></div>'
                         + '<div id="__live_posts">'
                         + render_live_posts(article["posts"], article.get("asset_urls"))
                         + '</div>' + LIVE_FEED_SCRIPT)
        else:
            log.debug("Clean article: %s (%d blocks)",
                      article["headline"], len(article["blocks"]))
            body_html = blocks_to_html(article["blocks"], article.get("asset_urls"))
            body_html = strip_promos(body_html)
        overview = ""
        if article.get("overview"):
            overview = f'<div class="overview">{escape(article["overview"])}</div>'
        date = (article.get("date") or "")
        byline = article.get("byline", "")
        byline_html = f"By {escape(byline)} &mdash; " if byline else ""

        rendered = ARTICLE_TEMPLATE.format(
            title=escape(article["headline"]),
            overview=overview,
            byline=byline_html,
            date=escape(date),
            date_short=escape(date[:10]),
            body=body_html,
            url=escape(upstream),
            embed_script=EMBED_RESIZE_SCRIPT,
        )
        body = inject_smh_ui(rendered)
        if is_live:
            # Live blogs change minute to minute — never cache the render.
            return Response(body, mimetype="text/html",
                            headers={"Cache-Control": "no-store"})
        return _cache_and_respond(cache_key, body, "text/html", {}, 200)

    # Not an article — proxy with link rewriting (single pass)
    rewritten = process_html(html, upstream + "/", SMH_DOMAINS, "", BLOCK_PATTERNS)

    # Inject a working "Show more" button on section/index/topic pages
    req_path = "/" + path if path else "/"
    page_meta = extract_index_meta(html) or extract_topic_meta(req_path, html)
    if page_meta:
        log.debug("Pagination page %s (%s) — injecting show more",
                  page_meta.get("path"), page_meta.get("kind"))
        rewritten = inject_show_more(rewritten, page_meta)

    rewritten = inject_smh_ui(rewritten)

    return _cache_and_respond(cache_key, rewritten, "text/html",
                              INDEX_CACHE_HEADER, 200)


if __name__ == "__main__":
    if os.environ.get("SMH_DEBUG") == "1":
        app.run(host="0.0.0.0", port=5008, debug=True, use_reloader=True)
    else:
        from waitress import serve

        serve(app, host="0.0.0.0", port=5008,
              threads=int(os.environ.get("SMH_THREADS", "16")),
              ident="smh-proxy")
