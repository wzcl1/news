"""
SMH + AFR Paywall Bypass -- Reverse Proxy (v5)
----------------------------------------------
Full reverse proxy for smh.com.au and afr.com with link rewriting.
Browse the homepage, click articles, all through the proxy.
Article pages get clean text extraction from embedded JSON.

SMH routes:  / , /<path>
AFR routes:  /afr , /afr/<path>
"""
import re
import json
import logging
from urllib.parse import urljoin, urlparse, parse_qs, urlencode, urlunparse
from flask import Flask, request, Response, abort
import requests
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.DEBUG)
log = logging.getLogger(__name__)

app = Flask(__name__)

UPSTREAM = "https://www.smh.com.au"

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

NINE_DOMAINS = [
    "smh.com.au",
    "theage.com.au",
    "brisbanetimes.com.au",
    "watoday.com.au",
]

# Paths/extensions to skip proxying (serve as pass-through)
SKIP_EXTENSIONS = {
    ".js", ".css", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico",
    ".woff", ".woff2", ".ttf", ".eot", ".mp4", ".mp3", ".webp",
}

# Paywall-related URL patterns to block
BLOCK_PATTERNS = [
    r"piano\.io",
    r"tinypass\.com",
    r"poool\.",
    r"paywall",
    r"metering",
    r"subscribe",
]

# ── AFR-specific constants ──────────────────────────────────────────────────
AFR_UPSTREAM = "https://www.afr.com"

AFR_GRAPHQL_URL = "https://api.afr.com/graphql"

AFR_DOMAINS = [
    "afr.com",
    "theage.com.au",
    "smh.com.au",
    "brisbanetimes.com.au",
    "watoday.com.au",
]

AFR_BLOCK_PATTERNS = [
    r"piano\.io",
    r"tinypass\.com",
    r"poool\.",
    r"paywall",
    r"metering",
    r"subscribe",
    r"tinypass\.min\.js",
    r"buy-au\.piano\.io",
    r"c2-au\.piano\.io",
    r"gtm\.js",
    r"snowplow",
    r"alib\.nine\.com\.au",
    r"adkit\.9pub",
    r"googletagmanager",
    r"googleadservices",
    r"doubleclick",
    r"googlesyndication",
    r"c2-au\.piano\.io",
    r"tp\.push",
    r"tp\.pianoId",
    r"tinypass",
    r"afx_prid",
    r"partner\.googleadservices",
    r"securepubads\.g\.doubleclick",
    r"tpc\.googlesyndication",
]


def make_proxy_url(path, qs=""):
    """Build a proxy URL for a given upstream path."""
    if qs:
        return f"/{path}?{qs}"
    return f"/{path}"


def rewrite_url(url, base_url):
    """Convert a URL to route through the proxy. Returns rewritten URL string."""
    if not url or url.startswith(("#", "javascript:", "mailto:", "tel:")):
        return url

    # Already a proxy URL
    if url.startswith("/"):
        return url

    # Absolute URL
    parsed = urlparse(url)
    if parsed.scheme in ("http", "https"):
        # Check if it's a Nine domain
        host = parsed.hostname or ""
        if any(host.endswith(d) for d in NINE_DOMAINS):
            path = parsed.path.lstrip("/")
            if parsed.query:
                return f"/{path}?{parsed.query}"
            return f"/{path}"
        # External link — leave as-is
        return url

    # Relative URL — resolve against base
    resolved = urljoin(base_url, url)
    parsed = urlparse(resolved)
    host = parsed.hostname or ""
    if any(host.endswith(d) for d in NINE_DOMAINS):
        path = parsed.path.lstrip("/")
        if parsed.query:
            return f"/{path}?{parsed.query}"
        return f"/{path}"

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
    footers = soup.find_all("footer") + soup.select('#footer, [data-testid="footer"]')
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


def strip_page_chrome(html):
    """Remove partner sections, ad slots, login prompts and footer cruft."""
    if not html:
        return html
    soup = BeautifulSoup(html, "html.parser")

    for sel in PARTNER_SELECTORS + AD_SELECTORS:
        for el in soup.select(sel):
            el.decompose()

    _strip_named_sections(soup)
    _strip_footer_below_socials(soup)

    return str(soup)


def rewrite_html_links(html, upstream_url):
    """Rewrite all href/src/action attributes in HTML to route through the proxy."""
    soup = BeautifulSoup(html, "html.parser")

    # Paywall script/link removal
    for tag in soup.find_all("script"):
        src = (tag.get("src") or "").lower()
        body = (tag.string or "").lower()
        blob = src + " " + body
        if any(re.search(p, blob) for p in BLOCK_PATTERNS):
            tag.decompose()

    for tag in soup.find_all("link"):
        href = (tag.get("href") or "").lower()
        if any(re.search(p, href) for p in BLOCK_PATTERNS):
            tag.decompose()

    # Paywall element removal
    for sel in ["#paywall_prompt", "#paywall-piano", "#subscribe"]:
        for el in soup.select(sel):
            el.decompose()

    for el in soup.find_all(True, class_=re.compile(r"paywall|subscribe-prompt|regwall|gateway|meter-wall", re.I)):
        el.decompose()

    for el in soup.find_all(True, id=re.compile(r"piano|tp-|tif-wrapper", re.I)):
        el.decompose()

    # Rewrite link attributes
    for tag in soup.find_all(True):
        for attr in ("href", "src", "action"):
            val = tag.get(attr)
            if val:
                tag[attr] = rewrite_url(val, upstream_url)

    # Fix base tag if present
    base = soup.find("base")
    if base:
        base.decompose()

    return str(soup)


def extract_article_data(html):
    """Extract article body from embedded APOLLO_STATE JSON."""
    soup = BeautifulSoup(html, "html.parser")

    hydration_data = None
    for script in soup.find_all("script"):
        text = script.string or ""
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
            hydration_data, _ = json.JSONDecoder().raw_decode(text[start:])
        except json.JSONDecodeError:
            pass
        break

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
                        "overview": _get_nested(value, "overview", "about") or "",
                        "byline": _get_byline(value),
                        "date": _get_nested(value, "dates", "published") or "",
                        "blocks": blocks,
                        "asset_urls": asset_urls,
                    }
    return None


def _get_nested(obj, *keys):
    for k in keys:
        if isinstance(obj, dict):
            obj = obj.get(k)
        else:
            return None
    if isinstance(obj, str):
        return obj.strip()
    return None


def _get_byline(obj):
    byline = obj.get("byline", [])
    if isinstance(byline, list) and byline:
        names = []
        for b in byline:
            if isinstance(b, dict):
                author = b.get("author", {})
                if isinstance(author, dict):
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
            if any(host.endswith(d) for d in NINE_DOMAINS):
                url = parsed.path
        if url and text:
            return f'<a href="{url}">{text}</a>'
        return text

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
                parts.append(f'<img src="{src}" alt="{caption}">')
                if caption:
                    parts.append(f'<p class="caption">{caption} ({credit})</p>')

        elif btype == "QUOTE":
            markup = block.get("markup", "")
            markup = re.sub(r"\\u([0-9a-fA-F]{4})",
                           lambda m: chr(int(m.group(1), 16)), markup)
            byline = block.get("byline", "")
            parts.append(f"<blockquote>{markup}<br><small>&mdash; {byline}</small></blockquote>")

        elif btype == "IFRAME":
            url = block.get("url", "")
            if url:
                parts.append(
                    f'<div class="embed"><iframe src="{url}" scrolling="no" '
                    f'frameborder="0" title="Embedded chart" '
                    f'style="width:100%;border:0;min-height:420px"></iframe></div>'
                )

    return "\n".join(parts)


def _parse_initial_state(html):
    """Parse window.INITIAL_STATE = JSON.parse("...") from the page."""
    m = re.search(
        r'window\.INITIAL_STATE\s*=\s*JSON\.parse\("((?:[^"\\]|\\.)*)"\)',
        html,
        re.DOTALL,
    )
    if not m:
        return None
    try:
        inner = json.loads('"' + m.group(1) + '"')
        return json.loads(inner)
    except (json.JSONDecodeError, ValueError):
        return None


def _brand_for_path(path):
    return "smh"


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
            "brand": _brand_for_path(key),
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


def _graphql_post(query, variables):
    """POST a query to the FFX GraphQL API and return the assetsConnection."""
    payload = {"query": query, "variables": variables}
    headers = {
        "Content-Type": "application/json",
        "User-Agent": HEADERS["User-Agent"],
        "Origin": UPSTREAM,
        "Referer": UPSTREAM + "/",
    }
    resp = requests.post(GRAPHQL_URL, json=payload, headers=headers, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    conn = (data.get("data") or {}).get("assetsConnection") or {}
    return conn.get("assets", []), conn.get("pageInfo", {})


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
    return _graphql_post(query, variables)


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
    return _graphql_post(query, variables)


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
    payload = {"query": query, "variables": {"brand": brand, "count": count}}
    headers = {
        "Content-Type": "application/json",
        "User-Agent": HEADERS["User-Agent"],
        "Origin": UPSTREAM,
        "Referer": UPSTREAM + "/",
    }
    resp = requests.post(GRAPHQL_URL, json=payload, headers=headers, timeout=15)
    resp.raise_for_status()
    return (resp.json().get("data") or {}).get("mostPopularStories") or []



def render_more_cards(assets):
    """Render GraphQL asset results as HTML cards."""
    cards = []
    for a in assets:
        aid = a.get("id", "")
        path = _get_nested(a, "urls", "canonical", "path") or ""
        headline = _get_nested(a, "asset", "headlines", "headline") or ""
        about = _get_nested(a, "asset", "about") or ""
        img_id = _get_nested(a, "featuredImages", "landscape16x9", "data", "id") or ""

        img_html = ""
        if img_id:
            src = f"https://static.ffx.io/images/$width_400,q_86,f_auto/{img_id}"
            img_html = f'<img class="__smh_card_img" src="{src}" alt="" loading="lazy">'

        about_html = f'<p class="__smh_card_about">{about}</p>' if about else ""
        cards.append(
            f'<article class="__smh_card" data-id="{aid}">'
            f'{img_html}'
            f'<h3 class="__smh_card_title"><a href="{path}">{headline}</a></h3>'
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
</style>
"""


def inject_show_more(html, meta):
    """Inject a working 'Show more' button + JS into an index page."""
    shown = json.dumps(meta.get("shown_ids", []))

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
              .replace("%KIND%", json.dumps(meta.get("kind", "nav")))
              .replace("%PATH%", json.dumps(meta.get("path", "")))
              .replace("%TAG%", json.dumps(meta.get("tag_id", "")))
              .replace("%BRAND%", json.dumps(meta["brand"]))
              .replace("%SHOWN%", shown))

    # Insert before the footer so new cards appear at the end of the content.
    # Different templates use <footer> or <div id="footer">.
    low = html.lower()
    candidates = []
    i = low.find("<footer")
    if i != -1:
        candidates.append(i)
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
  html.__smh_dark #__smh_bar { filter: invert(1) hue-rotate(180deg); }
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
  <div class="byline">{byline} &mdash; {date}</div>
  {body}
  <div class="footer">
    <p>Source: <a href="{url}">{url}</a></p>
  </div>
  {embed_script}
</body>
</html>"""


# ── AFR-specific functions ──────────────────────────────────────────────────


def afr_rewrite_url(url, base_url):
    """Convert an AFR URL to route through the proxy."""
    if not url or url.startswith(("#", "javascript:", "mailto:", "tel:")):
        return url

    # Already an AFR proxy URL
    if url == "/afr" or url.startswith("/afr/"):
        return url

    # Absolute URL
    parsed = urlparse(url)
    if parsed.scheme in ("http", "https"):
        host = parsed.hostname or ""
        if any(host.endswith(d) for d in AFR_DOMAINS):
            path = parsed.path.lstrip("/")
            if parsed.query:
                return f"/afr/{path}?{parsed.query}"
            return f"/afr/{path}"
        return url

    # Root-relative URL — leave static assets alone, prefix everything else
    if url.startswith("/"):
        if url.startswith(("/assets/", "/fonts/", "/favicon", "/apple-touch-icon", "/manifest")):
            return url
        ext = url.rsplit(".", 1)[-1].lower() if "." in url.rsplit("/", 1)[-1] else ""
        if ext in SKIP_EXTENSIONS:
            return url
        if "?" in url:
            path, qs = url.lstrip("/").split("?", 1)
            return f"/afr/{path}?{qs}"
        return f"/afr/{url.lstrip('/')}"

    # Relative URL — resolve against base
    resolved = urljoin(base_url, url)
    parsed = urlparse(resolved)
    host = parsed.hostname or ""
    if any(host.endswith(d) for d in AFR_DOMAINS):
        return afr_rewrite_url(parsed.path + (f"?{parsed.query}" if parsed.query else ""), base_url)

    return url


def afr_strip_paywall(html):
    """Strip paywall elements, tracking scripts, and ads from AFR HTML."""
    soup = BeautifulSoup(html, "html.parser")

    # Remove tracking/paywall/ad scripts
    for tag in soup.find_all("script"):
        src = (tag.get("src") or "").lower()
        body = (tag.string or "").lower()
        blob = src + " " + body
        if any(re.search(p, blob) for p in AFR_BLOCK_PATTERNS):
            tag.decompose()

    # Remove paywall-related links
    for tag in soup.find_all("link"):
        href = (tag.get("href") or "").lower()
        if any(re.search(p, href) for p in AFR_BLOCK_PATTERNS):
            tag.decompose()

    # Remove paywall containers
    for sel in [
        '[data-testid="PianoContainer"]',
        '[data-testid="PianoSubButton"]',
        "#paywall_prompt",
        "#paywall-piano",
        "#subscribe",
        ".paywall",
        ".regwall",
        ".gateway",
        ".meter-wall",
        "#tp-iframe",
    ]:
        for el in soup.select(sel):
            el.decompose()

    for el in soup.find_all(True, class_=re.compile(r"paywall|subscribe-prompt|regwall|gateway|meter-wall", re.I)):
        el.decompose()

    for el in soup.find_all(True, id=re.compile(r"piano|tp-|tif-wrapper", re.I)):
        el.decompose()

    # Remove data blocks that contain paywall state
    for tag in soup.find_all("script"):
        text = tag.string or ""
        if any(p in text.lower() for p in ["__REDUX_STATE__", "__APOLLO_STATE__", "__staticRouterHydrationData"]):
            tag.decompose()

    # Remove ad/cookie consent elements
    for el in soup.find_all(True, id=re.compile(r"adspot|ad-slot|consent|cookie", re.I)):
        el.decompose()

    return str(soup)


def afr_rewrite_html_links(html, upstream_url):
    """Rewrite all href/src/action attributes in AFR HTML to route through the proxy."""
    soup = BeautifulSoup(html, "html.parser")

    # Paywall script/link removal
    for tag in soup.find_all("script"):
        src = (tag.get("src") or "").lower()
        body = (tag.string or "").lower()
        blob = src + " " + body
        if any(re.search(p, blob) for p in AFR_BLOCK_PATTERNS):
            tag.decompose()

    for tag in soup.find_all("link"):
        href = (tag.get("href") or "").lower()
        if any(re.search(p, href) for p in AFR_BLOCK_PATTERNS):
            tag.decompose()

    # Paywall element removal
    for sel in [
        '[data-testid="PianoContainer"]',
        '[data-testid="PianoSubButton"]',
        "#paywall_prompt",
        "#paywall-piano",
        "#subscribe",
    ]:
        for el in soup.select(sel):
            el.decompose()

    for el in soup.find_all(True, class_=re.compile(r"paywall|subscribe-prompt|regwall|gateway|meter-wall", re.I)):
        el.decompose()

    for el in soup.find_all(True, id=re.compile(r"piano|tp-|tif-wrapper", re.I)):
        el.decompose()

    # Rewrite link attributes
    for tag in soup.find_all(True):
        for attr in ("href", "src", "action"):
            val = tag.get(attr)
            if val:
                tag[attr] = afr_rewrite_url(val, upstream_url)

    # Fix base tag if present
    base = soup.find("base")
    if base:
        base.decompose()

    return str(soup)


def _afr_extract_hydration_data(html):
    """Extract window.__staticRouterHydrationData from AFR HTML."""
    marker = "window.__staticRouterHydrationData = JSON.parse(\""
    idx = html.find(marker)
    if idx == -1:
        return None

    json_start = idx + len(marker)
    # Find the closing quote for the JSON.parse argument
    i = json_start
    while i < len(html):
        if html[i] == '"' and html[i - 1] != '\\':
            break
        i += 1
    json_str = html[json_start:i]

    try:
        # Unescape the JSON string (it's double-escaped)
        unescaped = json.loads('"' + json_str + '"')
        data = json.loads(unescaped)
        return data
    except (json.JSONDecodeError, ValueError):
        return None


_afr_story_cache = {}


def _afr_resolve_story(story_id):
    """Resolve an AFR story ID to (canonical_path, headline) via GraphQL. Cached."""
    if story_id in _afr_story_cache:
        return _afr_story_cache[story_id]

    result = (None, None)
    try:
        resp = requests.post(
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
                img_html = f'<img src="{src}" alt="{alt}">'
                if caption:
                    img_html += f'<p class="caption">{caption}'
                    if credit:
                        img_html += f' ({credit})'
                    img_html += '</p>'
                return img_html
            return ""

        elif ptype == "linkExternal":
            url = data.get("url", "")
            text = data.get("text", "")
            new_tab = data.get("newTab", False)
            target = ' target="_blank" rel="noopener"' if new_tab else ""
            if url and text:
                return f'<a href="{url}"{target}>{text}</a>'
            return text

        elif ptype == "relatedStory":
            story_id = data.get("id", "")
            if story_id:
                path, headline = _afr_resolve_story(story_id)
                if path:
                    text = headline or "Related story"
                    return (
                        f'<p class="related"><strong>Related:</strong> '
                        f'<a href="/afr{path}">{text}</a></p>'
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


def afr_extract_article_data(html):
    """Extract article data from AFR's __staticRouterHydrationData."""
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
            return f'href={quote}{afr_rewrite_url(url, "")}{quote}'

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
  <div class="byline">{byline} &mdash; {date}</div>
  {body}
  <div class="footer">
    <p>Source: <a href="{url}">{url}</a></p>
  </div>
  {embed_script}
</body>
</html>"""


def afr_build_article_html(article, upstream_url):
    """Build clean HTML for an AFR article."""
    hero_img = ""
    if article.get("hero_img"):
        hero_img = f'<img src="{article["hero_img"]}" alt="" style="max-width:100%;height:auto;margin-bottom:1.5rem;">'

    overview = ""
    if article.get("about"):
        overview = f'<div class="overview">{article["about"]}</div>'

    date = (article.get("date") or "")[:10]

    return AFR_ARTICLE_TEMPLATE.format(
        title=article.get("headline", ""),
        overview=overview,
        byline=article.get("byline", ""),
        date=date,
        body=strip_promos(article.get("body", "")),
        hero_img=hero_img,
        url=upstream_url,
        embed_script=EMBED_RESIZE_SCRIPT,
    )


@app.route("/afr/", defaults={"path": ""}, methods=["GET"])
@app.route("/afr/<path:path>", methods=["GET"])
def afr_proxy(path):
    """Proxy for afr.com."""
    if request.query_string:
        upstream = f"{AFR_UPSTREAM}/{path}?{request.query_string.decode()}"
    else:
        upstream = f"{AFR_UPSTREAM}/{path}" if path else AFR_UPSTREAM

    log.debug("AFR Proxying: %s → %s", request.full_path, upstream)

    try:
        resp = requests.get(upstream, headers=HEADERS, timeout=15, allow_redirects=True)
        resp.raise_for_status()
    except requests.RequestException as e:
        log.exception("AFR: Failed to fetch upstream")
        return f"<h1>Upstream fetch failed</h1><p>{e}</p>", 502

    content_type = resp.headers.get("Content-Type", "")

    # Only rewrite HTML responses
    if "text/html" not in content_type:
        return Response(resp.content,
                       content_type=content_type,
                       headers={"Cache-Control": "public, max-age=300"})

    html = resp.text

    # Try to extract clean article data
    article = afr_extract_article_data(html)

    if article and article.get("headline"):
        log.debug("AFR Clean article: %s", article["headline"])
        rendered = afr_build_article_html(article, upstream)
        return Response(inject_smh_ui(rendered), mimetype="text/html")

    # Not an article — proxy with link rewriting
    rewritten = afr_strip_paywall(html)
    rewritten = afr_rewrite_html_links(rewritten, upstream + "/")
    rewritten = strip_page_chrome(rewritten)
    rewritten = inject_smh_ui(rewritten)

    return Response(rewritten, mimetype="text/html",
                    headers={"Cache-Control": "no-store"})


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

    return {
        "html": render_more_cards(assets),
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
    if request.query_string:
        upstream = f"{UPSTREAM}/{path}?{request.query_string.decode()}"
    else:
        upstream = f"{UPSTREAM}/{path}" if path else UPSTREAM

    log.debug("Proxying: %s → %s", request.full_path, upstream)

    try:
        resp = requests.get(upstream, headers=HEADERS, timeout=15, allow_redirects=True)
        resp.raise_for_status()
    except requests.RequestException as e:
        log.exception("Failed to fetch upstream")
        return f"<h1>Upstream fetch failed</h1><p>{e}</p>", 502

    content_type = resp.headers.get("Content-Type", "")

    # Only rewrite HTML responses
    if "text/html" not in content_type:
        return Response(resp.content,
                       content_type=content_type,
                       headers={"Cache-Control": "public, max-age=300"})

    html = resp.text

    # Try to extract clean article data
    article = extract_article_data(html)

    if article and article.get("headline"):
        log.debug("Clean article: %s (%d blocks)",
                  article["headline"], len(article["blocks"]))
        body_html = blocks_to_html(article["blocks"], article.get("asset_urls"))
        body_html = strip_promos(body_html)
        overview = ""
        if article.get("overview"):
            overview = f'<div class="overview">{article["overview"]}</div>'
        date = (article.get("date") or "")[:10]

        rendered = ARTICLE_TEMPLATE.format(
            title=article["headline"],
            overview=overview,
            byline=article.get("byline", ""),
            date=date,
            body=body_html,
            url=upstream,
            embed_script=EMBED_RESIZE_SCRIPT,
        )
        return Response(inject_smh_ui(rendered), mimetype="text/html")

    # Not an article — proxy with link rewriting
    rewritten = rewrite_html_links(html, upstream + "/")
    rewritten = strip_page_chrome(rewritten)

    # Inject a working "Show more" button on section/index/topic pages
    req_path = "/" + path if path else "/"
    page_meta = extract_index_meta(html) or extract_topic_meta(req_path, html)
    if page_meta:
        log.debug("Pagination page %s (%s) — injecting show more",
                  page_meta.get("path"), page_meta.get("kind"))
        rewritten = inject_show_more(rewritten, page_meta)

    rewritten = inject_smh_ui(rewritten)

    return Response(rewritten, mimetype="text/html",
                    headers={"Cache-Control": "no-store"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5008, debug=True)
