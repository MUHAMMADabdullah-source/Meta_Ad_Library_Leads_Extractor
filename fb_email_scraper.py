"""
Facebook page email extractor (with follower filter)
----------------------------------------------------
Reads the JSON produced by meta_adlib_scraper.py, visits each advertiser's
Facebook page, checks the follower count, and ONLY if the page has fewer than
--max-followers (default 50,000) looks for a public email address.
Results are written back into the SAME json file:

    "followers":    12400 / null (null = couldn't read it)
    "email":        "hello@brand.com" / null
    "email_source": "facebook" / "website" / "skipped_followers" / null

Usage:
    python fb_email_scraper.py advertisers.json
    python fb_email_scraper.py advertisers.json --website-fallback
    python fb_email_scraper.py advertisers.json --max-followers 20000
    python fb_email_scraper.py advertisers.json --min-followers 1000 --max-followers 50000
    python fb_email_scraper.py advertisers.json --login

Setup (same as the first scraper):
    pip install playwright
    playwright install chromium
"""

import argparse
import json
import random
import re
import sys
import time
from urllib.parse import unquote, urlparse

from playwright.sync_api import sync_playwright

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}")
FOLLOWERS_RE = re.compile(r"(\d[\d.,]*)\s*([KkMmBb]?)\s+followers", re.I)

BAD_DOMAINS = (
    "facebook.com", "fb.com", "fbcdn.net", "instagram.com", "sentry.io",
    "wixpress.com", "example.com", "domain.com", "email.com", "yourdomain.com",
    "schema.org", "w3.org", "googleapis.com",
)
BAD_ENDINGS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".css", ".js")

CLOSE_SELECTORS = [
    '[aria-label="Close"]',
    '[aria-label="Decline optional cookies"]',
    'div[role="dialog"] div[role="button"]:has-text("Not now")',
]


# ---------- parsing helpers ----------

def parse_followers(text: str):
    """Find the first 'N followers' in the page text. Handles 12,345 / 12.4K / 1.2M."""
    m = FOLLOWERS_RE.search(text)
    if not m:
        return None
    num, suffix = m.group(1), m.group(2).upper()
    try:
        value = float(num.replace(",", ""))
    except ValueError:
        return None
    mult = {"": 1, "K": 1_000, "M": 1_000_000, "B": 1_000_000_000}[suffix]
    return int(value * mult)


def valid_email(e: str) -> bool:
    e = e.lower().strip(".")
    if e.endswith(BAD_ENDINGS):
        return False
    domain = e.split("@")[-1]
    return not any(domain == d or domain.endswith("." + d) for d in BAD_DOMAINS)


def find_emails(text: str, hrefs=()):
    found = []
    for h in hrefs:
        if h.lower().startswith("mailto:"):
            found += EMAIL_RE.findall(unquote(h[7:].split("?")[0]))
    found += EMAIL_RE.findall(unquote(text))
    seen, result = set(), []
    for e in found:
        k = e.lower().strip(".")
        if k not in seen and valid_email(k):
            seen.add(k)
            result.append(k)
    return result


# ---------- browser helpers ----------

def dismiss_popups(page):
    for sel in CLOSE_SELECTORS:
        try:
            el = page.query_selector(sel)
            if el:
                el.click(timeout=1500)
                time.sleep(0.5)
        except Exception:
            pass
    try:
        page.keyboard.press("Escape")
    except Exception:
        pass


def page_candidates(fb_url: str):
    """Main page first, then the About / contact info views."""
    if "profile.php" in fb_url:
        return [fb_url, fb_url + "&sk=about"]
    base = fb_url.rstrip("/")
    return [base, base + "/about", base + "/about_contact_and_basic_info"]


def process_facebook(page, fb_url: str, min_followers: int, max_followers: int, strict: bool):
    """
    Returns (followers, email, status) where status is:
      None                 -> normal result (email may or may not be found)
      "skipped_followers"  -> followers outside the min/max range (or unknown in --strict mode)
    """
    first_loaded = True
    followers = None

    for url in page_candidates(fb_url):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            time.sleep(2.5)
            dismiss_popups(page)
            text = page.inner_text("body")
            hrefs = page.eval_on_selector_all("a[href]", "els => els.map(e => e.href)")
        except Exception:
            continue

        if followers is None:
            followers = parse_followers(text)

        # follower filter runs as soon as we know the count
        if followers is not None and not (min_followers <= followers < max_followers):
            return followers, None, "skipped_followers"
        if first_loaded:
            first_loaded = False
            if followers is None and strict:
                return None, None, "skipped_followers"

        emails = find_emails(text, hrefs)
        if emails:
            return followers, emails[0], None

        time.sleep(random.uniform(1, 2))

    return followers, None, None


def email_from_website(context, website: str):
    """Free fallback: check the business's own site (home + common contact pages)."""
    if not website:
        return None
    p = urlparse(website)
    if not p.scheme:
        return None
    root = f"{p.scheme}://{p.netloc}"
    for path in ("", "/contact", "/contact-us", "/pages/contact", "/about"):
        try:
            r = context.request.get(root + path, timeout=15000)
            if r.ok:
                body = r.text()
                emails = find_emails(body, re.findall(r'href=["\'](mailto:[^"\']+)', body))
                if emails:
                    return emails[0]
        except Exception:
            continue
    return None


def save(path, records):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(records, f, indent=2, ensure_ascii=False)


# ---------- main ----------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("json_file", help="JSON file made by meta_adlib_scraper.py")
    ap.add_argument("--max-followers", type=int, default=50000,
                    help="only look for emails on pages with FEWER followers than this (default 50000)")
    ap.add_argument("--min-followers", type=int, default=0,
                    help="only look for emails on pages with AT LEAST this many followers (default 0)")
    ap.add_argument("--strict", action="store_true",
                    help="skip pages whose follower count can't be read (default: still look for an email)")
    ap.add_argument("--website-fallback", action="store_true",
                    help="if no email on Facebook, check the business website")
    ap.add_argument("--login", action="store_true",
                    help="open Facebook login first; your session is saved for next runs")
    ap.add_argument("--profile-dir", default="fb_profile",
                    help="folder where the browser session is stored")
    ap.add_argument("--retry", action="store_true",
                    help="also retry advertisers that previously got no email")
    ap.add_argument("--redo", action="store_true",
                    help="ignore earlier results and process every advertiser again "
                         "(use this for files processed by the older version of this script)")
    ap.add_argument("--headless", action="store_true")
    args = ap.parse_args()

    try:
        with open(args.json_file, encoding="utf-8") as f:
            records = json.load(f)
    except Exception as e:
        sys.exit(f"Could not read {args.json_file}: {e}")

    def needs_work(r):
        if not r.get("facebook_page"):
            return False
        if args.redo:
            return True
        if "email" not in r or r.get("email_source") == "not_checked":
            return True
        return (args.retry and not r.get("email")
                and r.get("email_source") != "skipped_followers")

    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(
            args.profile_dir,
            headless=args.headless,
            viewport={"width": 1366, "height": 900},
            locale="en-US",
        )
        page = context.pages[0] if context.pages else context.new_page()

        if args.login:
            page.goto("https://www.facebook.com/login")
            input("Log in in the browser window, then press Enter here to continue...")

        todo = [r for r in records if needs_work(r)]
        print(f"{len(todo)} advertisers to check ({len(records)} total in file). "
              f"Follower range: {args.min_followers:,} to under {args.max_followers:,}.")

        for i, rec in enumerate(todo, 1):
            name = rec.get("brand_name") or rec["facebook_page"]
            print(f"[{i}/{len(todo)}] {name} ... ", end="", flush=True)

            followers, email, status = process_facebook(
                page, rec["facebook_page"], args.min_followers, args.max_followers, args.strict
            )
            source = "facebook" if email else None

            if status is None and not email and args.website_fallback:
                email = email_from_website(context, rec.get("website"))
                source = "website" if email else None

            rec["followers"] = followers
            rec["email"] = email
            rec["email_source"] = status or source

            fol = f"{followers:,} followers" if followers is not None else "followers unknown"
            if status == "skipped_followers":
                print(f"skipped ({fol})")
            else:
                print(f"{email or 'no email found'} ({fol})")

            save(args.json_file, records)  # saved after every advertiser; safe to stop anytime
            time.sleep(random.uniform(3, 6))

        context.close()

    found = sum(1 for r in records if r.get("email"))
    skipped = sum(1 for r in records if r.get("email_source") == "skipped_followers")
    print(f"Done. {found} emails found, {skipped} skipped for follower count. "
          f"Saved to {args.json_file}")


if __name__ == "__main__":
    main()
