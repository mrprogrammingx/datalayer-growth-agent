"""Finds real, external social_commerce prospects (small Armenian businesses
selling online outside the shopify_smb profile - see app/apify_prospecting.py
for that segment) via free, public sources, for the customer acquisition
report's social_commerce discovery pipeline - the automated counterpart to
the manual WebSearch/WebFetch research that originally found monamie.am,
yerevancakes.am, and nissi.am.

Deliberately NOT an Apify actor call, unlike apify_prospecting.py: the user
explicitly ruled out a new paid subscription for this segment. Instead:

1. spyur.am (an Armenian business directory) - its own robots.txt allows
   crawling category and company pages (only paginated search/listing URLs
   are disallowed), with a `Crawl-delay: 10` we honor via
   SPYUR_REQUEST_DELAY_SECONDS on every spyur.am request. A category page
   (DEFAULT_CATEGORY_IDS below) lists company page links; a company page
   surfaces business name, website, Instagram/Facebook handles, and phone -
   but NEVER a real email (spyur.am's own "E-mail" field is an on-site
   contact-form proxy, confirmed live, not a mailto:/plaintext address).
2. The candidate's own website (if any) - fetched for a genuine email via
   _discover_email/_extract_email, including decoding Cloudflare's
   data-cfemail obfuscation (verified live against monamie.am's own
   info@monamie.am, the same technique used in the original manual
   research).

Only candidates with a resolvable real email (gate 3, hard requirement - see
the original social_commerce research brief) and a non-myshopify.com domain
(gate 5 - this segment must not overlap shopify_smb) are returned; that
filtering happens here, in code, before any LLM call, since it's cheap,
deterministic, and the LLM should never be trusted to invent or overlook a
missing email (see app/social_commerce_qualification.py for the LLM step).

Optional feature, same graceful-degradation contract as apify_prospecting.py
and the rest of this app: a single bad category or company or site fetch is
caught, logged, and skipped so the rest of the run still produces
candidates. fetch_social_commerce_candidates() raises only when every
configured category fails (a systemic outage), and callers
(app/scheduler.py) must catch that and degrade gracefully.
"""
import logging
import os
import re
import time
from datetime import date
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlsplit

import requests

from .apify_prospecting import normalize_prospect_domain

LOGGER = logging.getLogger(__name__)

SPYUR_BASE_URL = 'https://www.spyur.am'

# spyur.am's own robots.txt declares "Crawl-delay: 10" - honored on every
# request to spyur.am (category and company pages alike). Never lower this
# without re-checking https://www.spyur.am/robots.txt first.
SPYUR_REQUEST_DELAY_SECONDS = 10

# A short, polite pacing between fetches of arbitrary third-party candidate
# websites (not spyur.am, so no Crawl-delay directive applies) - still worth
# not hammering small business sites back-to-back.
SITE_REQUEST_DELAY_SECONDS = 2

REQUEST_TIMEOUT_SECONDS = 15
REQUEST_HEADERS = {'User-Agent': 'Mozilla/5.0 (compatible; DataLayerGrowthAgent/1.0)'}

# Small-business, retail-shaped verticals with a real page-1 company count
# (spyur.am pages at exactly 20 companies/page, confirmed live - every id
# below returned real `/en/companies/...` links on
# `/en/yellow_pages/yp/<id>/`, the only page spyur.am's robots.txt allows -
# paginated `-2`/`-3` suffixes are disallowed). '2570' (Handmade Jewelry, a
# tiny 8-company leaf) was dropped in favor of the much larger general '282'
# Jewelry Items Shop, which covers the same ground. Override via
# SOCIAL_COMMERCE_CATEGORY_IDS ("id:label,id:label,...") if a host wants a
# different seed list.
DEFAULT_CATEGORY_IDS = {
    '299': 'Flowers',
    '269': 'Confectionery/Bakery',
    '253': "Women's Clothing/Boutique",
    '1935': 'Cosmetics',
    '274': 'Souvenirs/Gifts',
    '282': 'Jewelry',
    '278': 'Clothing',
    '287': 'Shoes/Footwear',
    '252': "Kids' Clothing",
    '286': 'Perfumes',
    '305': 'Pet Supplies',
    '753': 'Bags',
    '1169': 'Wedding Dresses',
    '256': 'Fashion Jewelry',
}

# 20 companies/category page (fixed, see above) x len(DEFAULT_CATEGORY_IDS)
# categories can exceed a small cap well before the last category is ever
# reached - fetch_social_commerce_candidates stops accumulating the moment
# this cap is hit, in category-dict order, so a cap too close to (or below)
# one category's own page size would starve every category after the first
# few, every single day, regardless of cooldown. 100 comfortably covers
# every configured category's full page-1 output in one run; the rotation
# in fetch_social_commerce_candidates (see _rotated_categories) still
# spreads load fairly if the seed list grows further.
SOCIAL_COMMERCE_MAX_CANDIDATES_PER_RUN = int(
    os.environ.get('SOCIAL_COMMERCE_MAX_CANDIDATES_PER_RUN', '100')
)


def _parse_category_ids_env(raw: str) -> Dict[str, str]:
    """Pure. Parses SOCIAL_COMMERCE_CATEGORY_IDS' "id:label,id:label" format
    into the same {category_id: label} shape as DEFAULT_CATEGORY_IDS. A
    malformed entry (no ":", empty id) is skipped and logged rather than
    raising - one bad entry in a hand-edited env var shouldn't take down
    every other configured category.
    """
    parsed: Dict[str, str] = {}
    for entry in raw.split(','):
        entry = entry.strip()
        if not entry:
            continue
        if ':' not in entry:
            LOGGER.warning('Skipping malformed SOCIAL_COMMERCE_CATEGORY_IDS entry (missing ":"): %r', entry)
            continue
        category_id, label = entry.split(':', 1)
        category_id, label = category_id.strip(), label.strip()
        if not category_id or not label:
            LOGGER.warning('Skipping malformed SOCIAL_COMMERCE_CATEGORY_IDS entry: %r', entry)
            continue
        parsed[category_id] = label
    return parsed


def _default_category_ids() -> Dict[str, str]:
    """SOCIAL_COMMERCE_CATEGORY_IDS overrides DEFAULT_CATEGORY_IDS entirely
    when set and non-empty; an unset or empty env var (the common case)
    keeps the verified defaults untouched. Read lazily (not at import time)
    so tests/callers can set the env var and still get the override.
    """
    raw = os.environ.get('SOCIAL_COMMERCE_CATEGORY_IDS', '').strip()
    if not raw:
        return DEFAULT_CATEGORY_IDS
    parsed = _parse_category_ids_env(raw)
    return parsed or DEFAULT_CATEGORY_IDS


def _rotated_categories(category_ids: Dict[str, str]) -> List[Tuple[str, str]]:
    """Returns `category_ids` as a (category_id, label) list, rotated by a
    deterministic day-of-year offset so a DIFFERENT category gets first
    priority each day. fetch_social_commerce_candidates stops accumulating
    once max_candidates is hit, in list order - without rotation, the same
    early categories in dict order would win that race every single day,
    permanently starving whichever categories happen to sort last (real risk
    once the seed list is bigger than max_candidates // ~20-per-page).
    Deterministic (no persisted state needed) and stable within a day, so a
    retried run doesn't reshuffle mid-way.
    """
    items = list(category_ids.items())
    if not items:
        return items
    offset = date.today().toordinal() % len(items)
    return items[offset:] + items[:offset]


# A company page lists /en/companies/<slug>/<id> (and occasionally the bare
# /en/companies/<id> form before redirect) - matched against the category
# page's own links, not the whole page, so navigation/footer noise is never
# picked up as a candidate.
_COMPANY_LINK_RE = re.compile(r'href="(/en/companies/[a-zA-Z0-9_\-/]+)"')

_H1_RE = re.compile(r'<h1[^>]*>(.*?)</h1>', re.DOTALL)
_WEBSITE_LINK_RE = re.compile(
    r'class="web_link"[^>]*href="([^"]+)"|href="([^"]+)"[^>]*class="web_link"'
)
_FACEBOOK_LINK_RE = re.compile(
    r'class="web_link facebook_link"[^>]*href="([^"]+)"|href="([^"]+)"[^>]*class="web_link facebook_link"'
)
_INSTAGRAM_LINK_RE = re.compile(
    r'class="web_link instagram_link"[^>]*href="([^"]+)"|href="([^"]+)"[^>]*class="web_link instagram_link"'
)
_PHONE_RE = re.compile(r'href="tel:([^"]+)"')

# spyur.am's own company-page template renders a short internal
# call-center/support-line link (e.g. "tel:113") BEFORE a business's real
# phone number(s) - confirmed live. A real Armenian phone number always has
# well over 5 digits, so this threshold cleanly separates the two without
# needing to hardcode "113" itself (which could plausibly change).
_MIN_PHONE_DIGITS = 6

# Cloudflare's email-obfuscation attribute - see _decode_cfemail. Matches the
# hex payload regardless of which element carries it (span/a/link alike).
_CFEMAIL_RE = re.compile(r'data-cfemail="([a-f0-9]+)"')

# A plain-text email anywhere in the page. Filtered post-match (see
# _looks_like_asset_filename/_is_unusable_local_part) rather than
# over-constrained in the regex itself, which would risk missing a real
# address more than it would gain precision.
_PLAIN_EMAIL_RE = re.compile(r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}')

# An input field's placeholder attribute - matched and stripped BEFORE the
# plain-email scan (see _extract_email). Confirmed live: a newsletter
# signup form's <input placeholder="Jane.Doe@gmail.com"/> was extracted as
# if it were the business's real contact address - a placeholder is example
# text shown when a field is empty, never a value the page is asserting is
# real, so it must never satisfy gate 3.
_PLACEHOLDER_ATTR_RE = re.compile(r'placeholder=["\'][^"\']*["\']', re.IGNORECASE)

_ASSET_EXTENSIONS = ('png', 'jpg', 'jpeg', 'gif', 'svg', 'webp', 'ico', 'css', 'js', 'woff', 'woff2', 'ttf')
_UNUSABLE_LOCAL_PARTS = ('noreply', 'no-reply', 'donotreply', 'webmaster', 'postmaster', 'abuse')

_CONTACT_PAGE_GUESSES = ('/contact', '/en/contact', '/about', '/en/about', '/pages/contact')

# A single flat 15s timeout on an arbitrary small-business site (as opposed
# to spyur.am itself, one known-reliable host) turned out live to mean up to
# 15s to CONNECT *and*, separately, up to 15s to READ on a single request -
# a worst-case 30s per attempt. With up to 8 attempts per candidate (1
# homepage + up to len(_CONTACT_PAGE_GUESSES) guesses) and up to ~100
# candidates/run, a run with many slow/dead candidate sites was observed
# live to stretch well past an hour. Tighter, split timeouts bound the
# worst case per attempt to ~13s instead of ~30s.
SITE_CONNECT_TIMEOUT_SECONDS = 5
SITE_READ_TIMEOUT_SECONDS = 8


def _spyur_get(path: str) -> Optional[str]:
    """GET a spyur.am path, honoring Crawl-delay via a blocking sleep AFTER
    the request (so the delay is paid once per call, not doubled by a
    pre-sleep + this one). Returns None (never raises) on any request
    failure or non-200 - callers log and skip, matching this module's
    per-item degrade-and-continue philosophy; only the top-level orchestrator
    decides when enough failures mean the whole run should raise.
    """
    url = urljoin(SPYUR_BASE_URL, path)
    try:
        response = requests.get(url, headers=REQUEST_HEADERS, timeout=REQUEST_TIMEOUT_SECONDS)
        response.raise_for_status()
        return response.text
    except requests.RequestException as exc:
        LOGGER.warning('spyur.am fetch failed for %s: %s', url, exc)
        return None
    finally:
        time.sleep(SPYUR_REQUEST_DELAY_SECONDS)


def _site_get(url: str) -> Tuple[Optional[str], bool]:
    """GET an arbitrary candidate website URL. Never raises - a candidate's
    site being unreachable just means no email is discoverable there, which
    correctly fails gate 3 for that candidate rather than aborting the run.

    Returns (html_or_None, host_unreachable). host_unreachable is True only
    for a connection-level failure (DNS failure, connection refused, a
    connect-phase timeout) - a genuine "this host is down" signal, as
    opposed to a 404 or a slow-but-working response. _discover_email uses
    this to stop trying further guessed paths on a confirmed-dead host
    rather than burning the same timeout on each one in turn.
    """
    try:
        response = requests.get(
            url, headers=REQUEST_HEADERS,
            timeout=(SITE_CONNECT_TIMEOUT_SECONDS, SITE_READ_TIMEOUT_SECONDS),
        )
        if response.status_code >= 400:
            return None, False
        return response.text, False
    except (requests.exceptions.ConnectionError, requests.exceptions.ConnectTimeout):
        return None, True
    except requests.RequestException:
        return None, False
    finally:
        time.sleep(SITE_REQUEST_DELAY_SECONDS)


def _fetch_category_page(category_id: str) -> List[str]:
    """Returns absolute company-page URLs found on a category's page-1
    listing. Page 1 only - spyur.am's robots.txt disallows the paginated
    `-2`/`-3`... suffixes, and its own comment says category pages are
    "alternate views of content already covered completely by sitemap.xml",
    so we simply don't crawl past what's respectfully allowed.
    """
    html = _spyur_get(f'/en/yellow_pages/yp/{category_id}/')
    if not html:
        return []
    seen = set()
    urls = []
    for match in _COMPANY_LINK_RE.finditer(html):
        href = match.group(1)
        if href not in seen:
            seen.add(href)
            urls.append(urljoin(SPYUR_BASE_URL, href))
    return urls


def _pick_phone(html: str) -> Optional[str]:
    """Returns the first tel: href in `html` with at least _MIN_PHONE_DIGITS
    digits - skips spyur.am's own short internal call-center/support-line
    link (e.g. "tel:113") that precedes a business's real number(s) on every
    company page (confirmed live), without hardcoding that exact value.
    """
    for raw in _PHONE_RE.findall(html):
        if sum(c.isdigit() for c in raw) >= _MIN_PHONE_DIGITS:
            return raw
    return None


def _first_group(match: 're.Match') -> Optional[str]:
    """The link-extraction regexes above each have two alternative capture
    groups (attribute order on spyur.am's own markup isn't guaranteed
    consistent across pages) - exactly one is populated per match.
    """
    return match.group(1) or match.group(2)


def _fetch_company_page(company_url: str) -> Optional[Dict[str, Any]]:
    """Returns a raw candidate dict (business, website, instagram_url,
    facebook_url, other_contact, country) from one spyur.am company page, or
    None if the page couldn't be fetched or has no business name. Anchored
    on stable CSS classes (web_link / web_link facebook_link / web_link
    instagram_link / tel: hrefs), verified live against a real company page
    during planning - not loose "any href on the page" scanning.

    Deliberately does NOT look for an email here - spyur.am's own "E-mail"
    field is an on-site contact-form proxy (/en/company_free_message/...),
    never a real address, confirmed live. See _discover_email.
    """
    html = _spyur_get(company_url)
    if not html:
        return None

    h1_match = _H1_RE.search(html)
    business = re.sub(r'<[^>]+>', '', h1_match.group(1)).strip() if h1_match else None
    if not business:
        return None

    website_match = _WEBSITE_LINK_RE.search(html)
    facebook_match = _FACEBOOK_LINK_RE.search(html)
    instagram_match = _INSTAGRAM_LINK_RE.search(html)

    website = _first_group(website_match) if website_match else None
    if website and 'spyur.am' in urlsplit(website if '://' in website else f'//{website}').netloc.lower():
        # Some listings' "website" link is actually a spyur.am-hosted
        # mini-page (spyur.am/<slug>), not an independent business domain -
        # confirmed live. Fetching it for email discovery would surface
        # spyur.am's OWN generic contact address (inform@spyur.am, observed
        # live) and silently misattribute it to the business - worse than no
        # website at all, since gate 3 requires a genuinely discoverable
        # address for THIS business.
        website = None

    return {
        'business': business,
        'website': website,
        'facebook_url': _first_group(facebook_match) if facebook_match else None,
        'instagram_url': _first_group(instagram_match) if instagram_match else None,
        'other_contact': _pick_phone(html),
        'country': 'Armenia',
    }


def _decode_cfemail(hex_string: str) -> Optional[str]:
    """Pure. Decodes Cloudflare's email-obfuscation encoding: the first byte
    of the hex-decoded payload is an XOR key applied to every remaining
    byte. Verified live during planning against a real data-cfemail value on
    monamie.am's own homepage, which decoded to info@monamie.am - the exact
    plaintext also present elsewhere on that same page.

    Returns None (never raises) on malformed hex or a non-UTF-8 result, so a
    single bad attribute never breaks email discovery for the rest of the
    page.
    """
    try:
        raw = bytes.fromhex(hex_string)
    except ValueError:
        return None
    if len(raw) < 2:
        return None
    key = raw[0]
    try:
        return bytes(b ^ key for b in raw[1:]).decode('utf-8')
    except UnicodeDecodeError:
        return None


def _looks_like_asset_filename(candidate: str) -> bool:
    """True when `candidate` (the text right after the "@") ends in a common
    asset extension - the plain-email regex happily matches things like
    "logo@2x.png" (2x.png satisfies "alnum.alnum{2,}" same as a real TLD).
    """
    return candidate.rsplit('.', 1)[-1].lower() in _ASSET_EXTENSIONS


def _is_unusable_local_part(email: str) -> bool:
    """True for technically-real addresses that are useless for outreach
    (noreply@, webmaster@, ...) - gate 3's actual intent is a real human/team
    inbox that outreach can land in, not just the literal presence of an
    "@".
    """
    local_part = email.split('@', 1)[0].lower()
    return local_part in _UNUSABLE_LOCAL_PARTS


def _extract_email(html: str) -> Optional[str]:
    """Returns the first usable email found in `html`, or None. Checks
    Cloudflare's data-cfemail obfuscation first (a page that bothers to
    obfuscate an address is telling us that's THE contact address), falling
    back to a plain-text regex scan of the html with every placeholder="..."
    attribute stripped first - confirmed live necessary: a newsletter
    signup form's placeholder text ("Jane.Doe@gmail.com", example text
    shown in an empty field, never a real value) was otherwise extracted as
    if it were the business's genuine contact address. Both paths run every
    match through the same asset-filename/unusable-local-part filters so a
    page with several matches (an obfuscated real contact address plus a
    stray "logo@2x.png") doesn't return the wrong one.
    """
    for hex_string in _CFEMAIL_RE.findall(html):
        decoded = _decode_cfemail(hex_string)
        if decoded and '@' in decoded:
            domain_part = decoded.split('@', 1)[1]
            if not _looks_like_asset_filename(domain_part) and not _is_unusable_local_part(decoded):
                return decoded

    html_without_placeholders = _PLACEHOLDER_ATTR_RE.sub('', html)
    for match in _PLAIN_EMAIL_RE.finditer(html_without_placeholders):
        email = match.group(0)
        domain_part = email.split('@', 1)[1]
        if _looks_like_asset_filename(domain_part) or _is_unusable_local_part(email):
            continue
        return email

    return None


def _discover_email(website: str) -> Optional[str]:
    """Fetches `website`'s homepage, then a short list of common
    contact/about page guesses, stopping at the first usable email found.
    Never raises - an unreachable or email-less site just means this
    candidate fails gate 3, handled by the caller.

    Stops immediately (skipping the remaining guesses) the moment ANY fetch
    on this host reports host_unreachable (see _site_get) - a connection-
    level failure on one path means every other path on the SAME host will
    almost certainly fail the same way, so trying them just burns the same
    timeout again for nothing. A 404 or an empty-but-reachable page does NOT
    stop the loop - only a confirmed-dead host does.
    """
    if not website:
        return None
    homepage_html, unreachable = _site_get(website)
    if unreachable:
        return None
    if homepage_html:
        email = _extract_email(homepage_html)
        if email:
            return email

    parsed = urlsplit(website if '://' in website else f'//{website}')
    if not parsed.netloc:
        return None
    base = f'{parsed.scheme or "https"}://{parsed.netloc}'
    for path in _CONTACT_PAGE_GUESSES:
        html, unreachable = _site_get(base + path)
        if unreachable:
            break
        if not html:
            continue
        email = _extract_email(html)
        if email:
            return email
    return None


def normalize_social_key(instagram_url: Optional[str], facebook_url: Optional[str]) -> Optional[str]:
    """Pure. Fallback dedup key for a candidate with no website, in the
    exact form the original social_commerce research brief and
    sql/schema.sql's domain column comment specify: "instagram.com/<handle>"
    or "facebook.com/<handle>" WITH the handle included - never bare
    "instagram.com", which would collide every Instagram-sourced prospect
    onto one key. Prefers Instagram over Facebook when both are present,
    matching the original research brief's own source-reliability ordering.
    Returns None if neither URL yields a usable handle.
    """
    for url in (instagram_url, facebook_url):
        if not url:
            continue
        path = urlsplit(url if '://' in url else f'//{url}').path.strip('/')
        handle = path.split('/', 1)[0] if path else ''
        if handle:
            host = 'instagram.com' if 'instagram' in url.lower() else 'facebook.com'
            return f'{host}/{handle}'
    return None


def fetch_social_commerce_candidates(
    category_ids: Optional[Dict[str, str]] = None, max_candidates: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Orchestrator: walks `category_ids`' (default DEFAULT_CATEGORY_IDS,
    overridable via SOCIAL_COMMERCE_CATEGORY_IDS) page-1 listings, fetches
    each company page, then each candidate's own website for a real email.

    Returns only candidates passing the two hard gates computable without an
    LLM: a resolvable real email (never fabricated/pattern-guessed - gate 3)
    and a non-myshopify.com domain (gate 5 - this segment must not overlap
    shopify_smb). Each candidate dict carries a pre-computed 'domain' (never
    left for the LLM to invent - normalize_prospect_domain first, falling
    back to normalize_social_key) and 'platform', mechanically derived from
    which of website/instagram_url/facebook_url are present.

    A single bad category, company page, or site fetch is caught inside its
    own helper and skipped (see _spyur_get/_site_get) - this function raises
    RuntimeError only when EVERY configured category yields zero company
    links, a systemic-outage signal distinct from "today's businesses just
    didn't have websites." Callers (app/scheduler.py) must catch this and
    degrade gracefully, same contract as apify_prospecting.fetch_shopify_prospects.

    Categories are walked in a day-rotated order (see _rotated_categories),
    not dict-definition order - once `max_candidates` is hit, remaining
    categories are skipped for THIS run, so a fixed order would let the same
    early categories starve every later one, every single day.
    """
    if category_ids is None:
        category_ids = _default_category_ids()
    if max_candidates is None:
        max_candidates = SOCIAL_COMMERCE_MAX_CANDIDATES_PER_RUN

    candidates: List[Dict[str, Any]] = []
    categories_with_links = 0

    for category_id, label in _rotated_categories(category_ids):
        if len(candidates) >= max_candidates:
            break
        try:
            company_urls = _fetch_category_page(category_id)
        except Exception:
            LOGGER.exception('Social commerce category %s (%s) fetch failed - skipping', category_id, label)
            continue
        if company_urls:
            categories_with_links += 1

        for company_url in company_urls:
            if len(candidates) >= max_candidates:
                break
            try:
                candidate = _fetch_company_page(company_url)
            except Exception:
                LOGGER.exception('Social commerce company page fetch failed for %s - skipping', company_url)
                continue
            if not candidate:
                continue

            website = candidate.get('website')
            domain = normalize_prospect_domain(website)
            if domain and domain.endswith('.myshopify.com'):
                continue  # gate 5: this segment must not overlap shopify_smb

            try:
                email = _discover_email(website) if website else None
            except Exception:
                LOGGER.exception('Email discovery failed for %s - treating as no email found', website)
                email = None
            if not email:
                continue  # gate 3: a real email is a hard requirement, never fabricated

            candidate['email'] = email
            # The category label is a discovery HINT for the LLM qualification
            # step (app/social_commerce_qualification.py), not the final
            # 'sells' research field itself - that field should only ever
            # hold a grounded description, set by the LLM if it qualifies the
            # candidate, or stay null (surfaced-but-unqualified) otherwise.
            candidate['category_hint'] = label
            candidate['domain'] = domain or normalize_social_key(
                candidate.get('instagram_url'), candidate.get('facebook_url')
            )
            if not candidate['domain']:
                continue  # untrackable - can't be deduped/cooled down, so not worth surfacing
            candidate['platform'] = ' & '.join(
                name for name, present in (
                    ('Website', bool(website)),
                    ('Instagram', bool(candidate.get('instagram_url'))),
                    ('Facebook', bool(candidate.get('facebook_url'))),
                ) if present
            ) or None
            candidates.append(candidate)

    if categories_with_links == 0:
        raise RuntimeError(
            f'Social commerce discovery found zero company links across all {len(category_ids)} '
            f'configured categories - likely a spyur.am outage or markup change, not an empty day'
        )

    return candidates
