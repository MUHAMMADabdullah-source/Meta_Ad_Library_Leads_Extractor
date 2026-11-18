"""
Meta Ad Library scraper
-----------------------
Usage:
    python meta_adlib_scraper.py                      (asks you to paste the URL)
    python meta_adlib_scraper.py "<ad library url>"
    python meta_adlib_scraper.py "<url>" --limit 100 --out perfumes.json
    python meta_adlib_scraper.py "<url>" --min-ads 1 --max-ads 99
    python meta_adlib_scraper.py "<url>" --no-ad-filter        (skip the active-ad-count filter)
    python meta_adlib_scraper.py "<url>" --limit 0              (0 = no limit)
    python meta_adlib_scraper.py --check-page "https://www.facebook.com/somebrand"
                                                           (test ONE brand's ad count)

Setup (one time):
    pip install playwright
    playwright install chromium

How the active-ad filter works:
    For every advertiser found, the script opens that advertiser's own Ad Library
    page (active ads only) and reads the "~N results" number. Only advertisers
    with min-ads <= N <= max-ads are kept (default: more than 0 and fewer than 100).
    --limit counts advertisers that PASS the filter.
"""

import argparse
import json
import os
import random
import re
import sys
import time
from urllib.parse import urlparse, parse_qs, unquote

from playwright.sync_api import sync_playwright

# Runs inside the browser. Finds every ad card (anchored on the "Library ID:" text)
# and pulls out status, page name, page link, and any outbound links.
EXTRACT_JS = r"""
() => {
  const out = [];
  const leaves = [...document.querySelectorAll('span, div')].filter(
    el => el.children.length === 0 && /^Library ID:\s*\d+/.test(el.textContent.trim())
  );
  const countIds = el => (el.textContent.match(/Library ID:/g) || []).length;

  for (const leaf of leaves) {
    // climb to the largest ancestor that still contains exactly one ad
    let card = leaf;
    while (card.parentElement && countIds(card.parentElement) === 1) {
      card = card.parentElement;
    }

    const libraryId = (leaf.textContent.match(/\d+/) || [''])[0];

    // status
    let status = null;
    for (const el of card.querySelectorAll('span, div')) {
      if (el.children.length) continue;
      const t = el.textContent.trim();
      if (t === 'Active' || t === 'Inactive') { status = t; break; }
    }

    const anchors = [...card.querySelectorAll('a[href]')].map(a => ({
      href: a.href,
      text: (a.innerText || '').trim(),
    }));

    const idMatch = card.outerHTML.match(/view_all_page_id=(\d+)/);
    out.push({ libraryId, status, anchors, pageIdHtml: idMatch ? idMatch[1] : null });
  }
  return out;
}
"""

CARD_HTML_JS = r"""
(n) => {
  const leaves = [...document.querySelectorAll('span, div')].filter(
    el => el.children.length === 0 && /^Library ID:\s*\d+/.test(el.textContent.trim())
  );
  const countIds = el => (el.textContent.match(/Library ID:/g) || []).length;
  return leaves.slice(0, n).map(leaf => {
    let card = leaf;
    while (card.parentElement && countIds(card.parentElement) === 1) card = card.parentElement;
    return card.outerHTML;
  });
}
"""

FB_RESERVED = {
    "ads", "l.php", "login", "help", "policies", "privacy", "business",
    "photo", "photo.php", "watch", "reel", "share", "sharer", "dialog", "plugins",
}

RESULTS_RE = re.compile(r"~?\s*([\d,]+)\s+results?\b", re.I)
PAGE_ID_ANCHOR_RE = re.compile(r"view_all_page_id=(\d+)|facebook\.com/(\d{8,})(?:[/?]|$)")
PAGE_ID_PROFILE_RE = re.compile(r"profile\.php\?id=(\d+)")
PAGE_ID_HTML_RE = re.compile(
    r'fb://page/\?id=(\d+)|"pageID":"(\d+)"|"page_id":"(\d+)"|"delegate_page_id":"(\d+)"'
)


# ---------- link helpers ----------

def decode_redirect(href: str) -> str:
    """Facebook wraps outbound links in l.facebook.com/l.php?u=<real url>."""
    p = urlparse(href)
    if p.netloc.endswith("facebook.com") and p.path.startswith("/l.php"):
        u = parse_qs(p.query).get("u", [None])[0]
        if u:
            return unquote(u)
    return href


def clean_fb_page_url(href: str):
    p = urlparse(href)
    if not (p.netloc.endswith("facebook.com") or p.netloc.endswith("fb.com")):
        return None
    if p.netloc.startswith("l."):
        return None
    parts = [x for x in p.path.split("/") if x]
    if not parts or parts[0] in FB_RESERVED:
        return None
    if parts[0] == "profile.php":
        id_ = parse_qs(p.query).get("id", [None])[0]
        return f"https://www.facebook.com/profile.php?id={id_}" if id_ else None
    return f"https://www.facebook.com/{parts[0]}"


def clean_ig_url(href: str):
    p = urlparse(href)
    if not p.netloc.endswith("instagram.com"):
        return None
    parts = [x for x in p.path.split("/") if x]
    if not parts or parts[0] in {"p", "reel", "explore", "accounts"}:
        return None
    return f"https://www.instagram.com/{parts[0]}/"


def is_external(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return bool(host) and not any(
        host.endswith(d)
        for d in ("facebook.com", "fb.com", "instagram.com", "fb.me", "meta.com")
    )


def page_id_from_href(href: str):
    m = PAGE_ID_ANCHOR_RE.search(href)
    if m:
        return m.group(1) or m.group(2)
    return None


def page_id_from_fb_url(fb_url: str):
    m = PAGE_ID_PROFILE_RE.search(fb_url)
    if m:
        return m.group(1)
    tail = fb_url.rstrip("/").split("/")[-1]
    return tail if tail.isdigit() and len(tail) >= 8 else None


# ---------- duplicates: what is already in the output file ----------

def rec_keys(rec):
    """Every identity an advertiser can be known by: its page ID, and its Facebook URL
    (lowercased). Two records sharing ANY key are the same advertiser."""
    keys = set()
    pid = rec.get("page_id")
    url = (rec.get("facebook_page") or "").strip().lower().rstrip("/")
    if url:
        keys.add("url:" + url)
        pid = pid or page_id_from_fb_url(url)
    if pid:
        keys.add("id:" + str(pid))
    return keys


def load_existing(path):
    """Returns (records already in the file, set of their keys). Missing file = empty.
    An unreadable file stops the run rather than being silently overwritten."""
    if not os.path.exists(path):
        return [], set()
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            raise ValueError("top level is not a list")
    except Exception as e:
        sys.exit(f"Could not read existing {path} ({e}). Fix or move it first, "
                 f"so it isn't overwritten.")
    known = set()
    for r in data:
        if isinstance(r, dict):
            known |= rec_keys(r)
    return data, known


# ---------- Library ID -> advertiser page ID (from Meta's own data) ----------
# The Ad Library ships, for every ad, the ad's Library ID together with the advertiser's
# page ID inside its JSON payloads (embedded <script type="application/json"> tags for the
# first results, GraphQL responses for everything loaded by scrolling). Pairing the two
# gives the exact ID the Ad Library uses, so we never have to guess it from links or from
# the advertiser's Facebook page.

LIB_TO_PAGE = {}                       # "Library ID" (ad_archive_id) -> page ID
AD_KEYS = ("ad_archive_id", "adArchiveID")
PAGE_KEYS = ("page_id", "pageID")
_payload_dumped = [False]


def _first(d, keys):
    for k in keys:
        v = d.get(k)
        if v:
            return str(v)
    return None


def _walk_ids(node):
    if isinstance(node, dict):
        ad = _first(node, AD_KEYS)
        if ad:
            pid = _first(node, PAGE_KEYS)
            if not pid and isinstance(node.get("snapshot"), dict):
                pid = _first(node["snapshot"], PAGE_KEYS)
            if pid and pid.isdigit():
                LIB_TO_PAGE.setdefault(ad, pid)
        for v in node.values():
            _walk_ids(v)
    elif isinstance(node, list):
        for v in node:
            _walk_ids(v)


def _strip_prefix(t: str) -> str:
    t = t.strip()
    return t[len("for (;;);"):] if t.startswith("for (;;);") else t


def ingest_payload_text(text: str):
    """Parse a response body: one JSON document, or newline-delimited JSON."""
    text = _strip_prefix(text)
    try:
        _walk_ids(json.loads(text))
        return
    except ValueError:
        pass
    for line in text.splitlines():
        line = _strip_prefix(line)
        if "ad_archive_id" in line or "adArchiveID" in line:
            try:
                _walk_ids(json.loads(line))
            except ValueError:
                pass


def attach_graphql_listener(page):
    """Captures ID pairs from ads that load while scrolling (pagination)."""
    def handle(response):
        if "graphql" not in response.url:
            return
        try:
            text = response.text()
        except Exception:
            return
        if "ad_archive_id" in text or "adArchiveID" in text:
            ingest_payload_text(text)
    page.on("response", handle)


def harvest_ids_from_dom(page, debug=False):
    """Captures ID pairs embedded in the page itself (the first batch of ads)."""
    try:
        blobs = page.eval_on_selector_all(
            'script[type="application/json"]', "els => els.map(e => e.textContent)"
        )
    except Exception:
        return
    for b in blobs:
        if "ad_archive_id" in b or "adArchiveID" in b:
            try:
                _walk_ids(json.loads(b))
            except ValueError:
                pass
            if debug and not _payload_dumped[0]:
                _payload_dumped[0] = True
                with open("debug_payload.json", "w", encoding="utf-8") as f:
                    f.write(b[:200000])
                print("  [debug] saved one embedded payload to debug_payload.json", flush=True)


# ---------- card parsing ----------

def parse_cards(raw_cards, lib_map=None):
    """Turn raw card data into one record per advertiser (page)."""
    advertisers = {}
    for card in raw_cards:
        fb_url, brand, ig_url, website = None, None, None, None
        # Best source: Meta's own Library ID -> page ID pairing. Then the card's own
        # view_all_page_id link. Anything else is a last-resort guess.
        page_id = (lib_map or {}).get(card.get("libraryId")) or card.get("pageIdHtml")

        for a in card["anchors"]:
            href = decode_redirect(a["href"])

            if not ig_url:
                ig_url = clean_ig_url(href)
            if not page_id:
                page_id = page_id_from_href(href)

            page = clean_fb_page_url(href)
            if page and not fb_url:
                fb_url = page
            if page and not brand and a["text"]:
                brand = a["text"].split("\n")[0].strip()

            if not website and is_external(href):
                website = href

        if not fb_url:
            continue

        rec = advertisers.setdefault(
            fb_url,
            {
                "ad_status": card["status"],
                "brand_name": brand,
                "instagram_page": ig_url,
                "facebook_page": fb_url,
                "website": website,
                "page_id": page_id or page_id_from_fb_url(fb_url),
                "active_ads": None,
            },
        )
        # merge: an advertiser is "Active" if ANY of its ads is active
        if card["status"] == "Active":
            rec["ad_status"] = "Active"
        rec["brand_name"] = rec["brand_name"] or brand
        rec["instagram_page"] = rec["instagram_page"] or ig_url
        rec["website"] = rec["website"] or website
        rec["page_id"] = rec["page_id"] or page_id

    return list(advertisers.values())


# ---------- active ad count filter ----------

PAGE_ID_HTML_PATTERNS = [
    re.compile(r"fb://page/\?id=(\d+)"),   # most reliable: the page's own app link
    re.compile(r'"pageID":"(\d+)"'),
]


def get_page_id_from_facebook(page, fb_url: str):
    """Fallback: open the Facebook page and look for its numeric page ID in the source.
    Patterns are tried in order of reliability, not by position in the page."""
    try:
        page.goto(fb_url, wait_until="domcontentloaded", timeout=30000)
        time.sleep(2)
        html = page.content()
        for pat in PAGE_ID_HTML_PATTERNS:
            m = pat.search(html)
            if m:
                return m.group(1)
    except Exception:
        pass
    return None


_DEBUG_PRINTS_LEFT = [10]  # --debug prints details for the first 10 checks only


def _read_count(text: str):
    m = RESULTS_RE.search(text)
    if m:
        return int(m.group(1).replace(",", "")), m.group(0)
    if re.search(r"no ads (match|found)|isn't running ads", text, re.I):
        return 0, "no-ads message"
    return None, None


def count_active_ads(page, page_id: str, country: str, debug: bool = False):
    """Open this advertiser's own Ad Library page (active ads only) and read '~N results'."""
    url = (
        "https://www.facebook.com/ads/library/?active_status=active&ad_type=all"
        f"&country={country}&media_type=all&search_type=page&view_all_page_id={page_id}"
    )
    text, matched, result = "", None, None
    try:
        page.bring_to_front()  # background tabs are throttled and may not render the count
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        try:
            page.wait_for_function(
                "() => /\\d[\\d,]*\\s+results?\\b|no ads (match|found)|isn't running ads/i"
                ".test(document.body.innerText)",
                timeout=25000,
            )
        except Exception:
            pass
        time.sleep(1.5)
        text = page.inner_text("body")
        result, matched = _read_count(text)

        # A 0 (or nothing) may just be a placeholder shown while the real number loads:
        # wait a little and read again before believing it.
        if not result:
            time.sleep(3)
            text = page.inner_text("body")
            result2, matched2 = _read_count(text)
            if result2:
                result, matched = result2, matched2
            elif result is None:
                result, matched = result2, matched2
    except Exception:
        pass

    if debug and _DEBUG_PRINTS_LEFT[0] > 0:
        _DEBUG_PRINTS_LEFT[0] -= 1
        snippet = " ".join(text.split())[:250]
        print(f"    [debug] page ID {page_id} -> read {result!r} from {matched!r}\n"
              f"            URL: {url}\n"
              f"            page text starts: {snippet!r}", flush=True)
        mm = re.search(r"no ads (match|found)|isn't running ads", text, re.I)
        if matched == "no-ads message" and mm:
            around = " ".join(text[max(0, mm.start() - 120): mm.end() + 120].split())
            print(f"            text around the 'no ads' match: {around!r}", flush=True)
        try:
            with open(f"debug_{page_id}.txt", "w", encoding="utf-8") as f:
                f.write(url + "\n\n" + text)
            print(f"            full page text saved to debug_{page_id}.txt", flush=True)
        except Exception:
            pass
        try:
            page.screenshot(path=f"debug_{page_id}.png")
        except Exception:
            pass
    return result


def passes_filter(checker, rec, country, min_ads, max_ads, debug=False):
    """Fills rec['active_ads'] / rec['page_id'] and returns True if it fits the range."""
    if not rec.get("page_id"):
        rec["page_id"] = get_page_id_from_facebook(checker, rec["facebook_page"])
    if not rec["page_id"]:
        rec["active_ads"] = None
        return False, "page ID not found"

    count = count_active_ads(checker, rec["page_id"], country, debug)

    # Sanity check: the ad card we found this brand on said "Active", so a count of 0
    # means we are almost certainly looking at the wrong page ID. Try the ID from the
    # Facebook page itself, and if that doesn't help, don't trust the 0.
    if count == 0 and rec.get("ad_status") == "Active":
        alt = get_page_id_from_facebook(checker, rec["facebook_page"])
        if alt and alt != rec["page_id"]:
            count2 = count_active_ads(checker, alt, country, debug)
            if count2:
                rec["page_id"], count = alt, count2
        if count == 0:
            rec["active_ads"] = None
            return False, ("count was 0 but this brand's ad is Active, so the page ID is "
                           f"probably wrong (ID tried: {rec['page_id']})")

    rec["active_ads"] = count
    if count is None:
        return False, "couldn't read ad count"
    if count > 0:
        rec["ad_status"] = "Active"
    ok = min_ads <= count <= max_ads
    return ok, f"{count} active ads (page ID {rec['page_id']})"


# ---------- scrolling / collecting ----------

def scroll_and_collect(page, checker, args, country, on_lead=None, stop_event=None):
    """
    Scroll the results page and collect advertisers until:
      - we have `--limit` advertisers (that pass the ad filter, if it's on), or
      - the results run out, or
      - --max-scrolls is reached, or
      - stop_event is set (used by lead_pipeline.py).
    If on_lead is given, it is called with a copy of each advertiser the moment it qualifies.
    """
    use_filter = not args.no_ad_filter
    last_raw, stale = 0, 0
    all_recs = {}        # facebook_page -> record (every advertiser seen)
    qualified = {}       # facebook_page -> record (passed the filter)
    unknown = 0
    stats = {"too_many": 0, "zero": 0, "unreadable": 0, "no_id": 0, "known": 0}
    skip_keys = set(getattr(args, "known", None) or ())  # advertisers already in the file
    debug_cards_saved = False
    attach_graphql_listener(page)

    def done():
        return bool(args.limit and len(qualified) >= args.limit) or (
            stop_event is not None and stop_event.is_set()
        )

    for i in range(args.max_scrolls + 1):
        raw = page.evaluate(EXTRACT_JS)
        harvest_ids_from_dom(page, getattr(args, "debug", False))
        recs = parse_cards(raw, LIB_TO_PAGE)

        if getattr(args, "debug", False) and raw and not debug_cards_saved:
            debug_cards_saved = True
            try:
                for n, html in enumerate(page.evaluate(CARD_HTML_JS, 2), 1):
                    with open(f"debug_card_{n}.html", "w", encoding="utf-8") as f:
                        f.write(html)
                print("  [debug] saved the raw HTML of the first 2 ad cards to "
                      "debug_card_1.html and debug_card_2.html", flush=True)
            except Exception:
                pass

        for rec in recs:
            key = rec["facebook_page"]
            if key in all_recs:
                if rec["ad_status"] == "Active":
                    all_recs[key]["ad_status"] = "Active"
                continue
            all_recs[key] = rec

            # Already in the output file (or the same advertiser under another URL form):
            # skip BEFORE the slow ad-count check.
            if rec_keys(rec) & skip_keys:
                stats["known"] += 1
                continue
            skip_keys |= rec_keys(rec)

            if not use_filter:
                qualified[key] = rec
                if on_lead:
                    on_lead(dict(rec))
            else:
                want_debug = getattr(args, "debug", False)
                ok, note = passes_filter(checker, rec, country, args.min_ads, args.max_ads,
                                         want_debug)
                if rec["active_ads"] is None:
                    unknown += 1
                if not rec.get("page_id"):
                    stats["no_id"] += 1
                elif rec["active_ads"] is None:
                    stats["unreadable"] += 1
                elif rec["active_ads"] > args.max_ads:
                    stats["too_many"] += 1
                elif rec["active_ads"] < args.min_ads:
                    stats["zero"] += 1
                skip_keys |= rec_keys(rec)  # page_id may have just been resolved
                name = rec.get("brand_name") or key
                print(f"    {'KEEP' if ok else 'skip'}  {name}: {note}")
                if ok:
                    qualified[key] = rec
                    if on_lead:
                        on_lead(dict(rec))
                page.bring_to_front()
                time.sleep(random.uniform(1.0, 2.5))

            if done():
                break

        breakdown = ""
        if use_filter:
            breakdown = (f" | skipped: {stats['known']} already in file, {stats['too_many']} too many ads, {stats['zero']} below min, "
                         f"{stats['unreadable']} unreadable count, {stats['no_id']} no page ID")
        if not use_filter and stats["known"]:
            breakdown = f" | skipped: {stats['known']} already in file"
        print(f"  {len(raw)} ads loaded | {len(all_recs)} advertisers seen | "
              f"{len(qualified)} new passed ad filter | {len(LIB_TO_PAGE)} Library-ID->page-ID pairs{breakdown}")

        if done():
            break
        if len(raw) == last_raw:
            stale += 1
            if stale >= 4:  # nothing new after 4 checks -> end of results
                break
        else:
            stale, last_raw = 0, len(raw)
        if i == args.max_scrolls:
            break

        page.mouse.wheel(0, 6000)
        page.keyboard.press("End")
        page.wait_for_timeout(int(args.pause * 1000))  # (not time.sleep: it blocks response events)

    if use_filter and unknown:
        print(f"  Note: {unknown} advertisers were excluded because their ad count "
              f"or page ID couldn't be read.")
    return list(qualified.values())


def enrich_from_facebook(context, records):
    """
    Optional: open each advertiser's Facebook page and look for an Instagram
    link / website that the Ad Library card didn't expose. Slower, and Facebook
    may show a login wall for some pages, so treat as best-effort.
    """
    page = context.new_page()
    for i, rec in enumerate(records, 1):
        if rec["instagram_page"] and rec["website"]:
            continue
        print(f"  enriching {i}/{len(records)}: {rec['brand_name']}")
        try:
            page.goto(rec["facebook_page"], wait_until="domcontentloaded", timeout=30000)
            time.sleep(2.5)
            hrefs = page.eval_on_selector_all("a[href]", "els => els.map(e => e.href)")
            for h in hrefs:
                h = decode_redirect(h)
                if not rec["instagram_page"]:
                    rec["instagram_page"] = clean_ig_url(h)
                if not rec["website"] and is_external(h):
                    rec["website"] = h
        except Exception as e:
            print(f"    skipped ({e.__class__.__name__})")
    page.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("url", nargs="?", help="Ad Library search results URL")
    ap.add_argument("--out", default="advertisers.json")
    ap.add_argument("--limit", type=int, default=50,
                    help="stop after this many advertisers that pass the filters (0 = no limit)")
    ap.add_argument("--min-ads", type=int, default=1,
                    help="minimum active ads an advertiser must have (default 1, i.e. more than 0)")
    ap.add_argument("--max-ads", type=int, default=99,
                    help="maximum active ads an advertiser may have (default 99, i.e. fewer than 100)")
    ap.add_argument("--no-ad-filter", action="store_true",
                    help="turn off the active-ad-count filter (faster, no extra page visits)")
    ap.add_argument("--debug", action="store_true",
                    help="print the URL, the number read and the page text for the first 10 checks, "
                         "and save a screenshot of each")
    ap.add_argument("--max-scrolls", type=int, default=300,
                    help="safety cap on scroll attempts")
    ap.add_argument("--pause", type=float, default=2.0, help="seconds between scrolls")
    ap.add_argument("--enrich", action="store_true",
                    help="visit each Facebook page to find missing Instagram/website")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--check-page", metavar="FACEBOOK_PAGE_URL",
                    help="test mode: check ONE Facebook page and print its page ID and active ad "
                         "count (add your search URL as the first argument to use its country)")
    args = ap.parse_args()

    if args.check_page:
        country = "ALL"
        if args.url:
            country = parse_qs(urlparse(args.url).query).get("country", ["ALL"])[0]
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=args.headless)
            pg = browser.new_context(
                viewport={"width": 1366, "height": 900}, locale="en-US"
            ).new_page()
            pid = page_id_from_fb_url(args.check_page) or get_page_id_from_facebook(
                pg, args.check_page
            )
            print(f"Page ID found: {pid}   (country used: {country})")
            if pid:
                n = count_active_ads(pg, pid, country, debug=True)
                print(f"Active ads read: {n}")
            browser.close()
        return

    url = args.url or input("Paste the Ad Library URL: ").strip().strip('"')
    if "facebook.com/ads/library" not in url:
        sys.exit("That doesn't look like an Ad Library URL.")

    country = parse_qs(urlparse(url).query).get("country", ["ALL"])[0]

    existing, args.known = load_existing(args.out)
    if existing:
        print(f"{len(existing)} advertisers already in {args.out}; they will be skipped "
              f"and kept. --limit counts NEW ones only.")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=args.headless)
        context = browser.new_context(
            viewport={"width": 1366, "height": 900}, locale="en-US"
        )
        page = context.new_page()
        checker = context.new_page()   # second tab used for per-advertiser ad counts

        print("Opening Ad Library...")
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        try:
            page.wait_for_selector("text=Library ID:", timeout=30000)
        except Exception:
            print("No ads detected yet. If a popup/cookie banner is showing, "
                  "close it in the browser window, then press Enter here.")
            input()

        if not args.no_ad_filter:
            print(f"Filter on: keeping advertisers with {args.min_ads} to {args.max_ads} "
                  f"active ads (country: {country}).")
        records = scroll_and_collect(page, checker, args, country)
        if args.limit:
            records = records[: args.limit]
        print(f"Collected {len(records)} new advertisers.")

        if args.enrich:
            enrich_from_facebook(context, records)

        browser.close()

    tmp = args.out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(existing + records, f, indent=2, ensure_ascii=False)
    os.replace(tmp, args.out)
    print(f"Added {len(records)} new advertisers; {args.out} now has {len(existing) + len(records)}.")


if __name__ == "__main__":
    main()
