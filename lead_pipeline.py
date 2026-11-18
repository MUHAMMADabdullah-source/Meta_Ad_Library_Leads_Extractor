"""
Lead pipeline: Ad Library scraper + Facebook email scraper running AT THE SAME TIME
-----------------------------------------------------------------------------------
Worker 1 scrolls the Ad Library and finds brands (with the active-ad-count filter).
Worker 2 receives each brand the moment it is found, checks its Facebook follower
count, and looks for an email. The main process saves everything into one JSON file.

Needs meta_adlib_scraper.py and fb_email_scraper.py in the same folder.

Usage:
    python lead_pipeline.py "<ad library url>" --limit 100 --out perfumes.json --website-fallback
    python lead_pipeline.py "<url>" --min-followers 1000 --max-followers 50000
    python lead_pipeline.py "<url>" --min-ads 1 --max-ads 99
    python lead_pipeline.py "<url>" --login           (log in to Facebook once, session is remembered)
    python lead_pipeline.py "<url>" --limit 20 --require-email --website-fallback --out leads.json

--limit counts FINAL leads: brands that pass the active-ad filter AND the follower range.
Press Ctrl+C any time; everything found so far is saved. Brands not yet checked are kept
with email_source = 'not_checked'; brands outside the follower range are dropped.
"""

import argparse
import copy
import json
import multiprocessing as mp
import os
import queue
import random
import sys
import time
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import sync_playwright

import fb_email_scraper as fbem
import meta_adlib_scraper as adlib

VIEWPORT = {"width": 1366, "height": 900}


# ---------------------------------------------------------------- workers

def put_or_stop(q, item, stop_event, announce=False):
    """Put on a bounded queue; wait while it is full, but give up as soon as stop is set."""
    told = False
    while not stop_event.is_set():
        try:
            q.put(item, timeout=1)
            return True
        except queue.Full:
            if announce and not told:
                told = True
                print("[ADS] buffer full, pausing until the email worker catches up...",
                      flush=True)
    return False


def producer(args, work_q, event_q, stop_event):
    """Worker 1: finds brands on the Ad Library and hands each one over immediately."""
    try:
        country = parse_qs(urlparse(args.url).query).get("country", ["ALL"])[0]
        p_args = copy.copy(args)
        p_args.limit = 0  # the coordinator decides when we have enough final leads

        def on_lead(rec):
            event_q.put(("lead", rec["facebook_page"], rec))
            put_or_stop(work_q, rec, stop_event, announce=True)  # blocks while buffer is full

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=args.headless)
            context = browser.new_context(viewport=VIEWPORT, locale="en-US")
            page = context.new_page()
            checker = context.new_page()  # second tab for per-advertiser ad counts

            print("[ADS] Opening Ad Library...", flush=True)
            page.goto(args.url, wait_until="domcontentloaded", timeout=60000)
            try:
                page.wait_for_selector("text=Library ID:", timeout=30000)
            except Exception:
                print("[ADS] No ads detected yet. If a popup is showing, close it in the "
                      "browser window (waiting up to 90 more seconds)...", flush=True)
                page.wait_for_selector("text=Library ID:", timeout=90000)

            if not args.no_ad_filter:
                print(f"[ADS] Filter on: {args.min_ads} to {args.max_ads} active ads "
                      f"(country: {country}).", flush=True)
            adlib.scroll_and_collect(page, checker, p_args, country,
                                     on_lead=on_lead, stop_event=stop_event)
            browser.close()
    except Exception as e:
        event_q.put(("log", f"[ADS] worker stopped: {e.__class__.__name__}: {e}"))
    finally:
        put_or_stop(work_q, None, stop_event)  # tells the email worker nothing more is coming
        event_q.put(("producer_done",))


def consumer(args, work_q, event_q, stop_event):
    """Worker 2: for each brand, checks followers and looks for an email."""
    try:
        with sync_playwright() as pw:
            context = pw.chromium.launch_persistent_context(
                args.profile_dir,
                headless=args.headless,
                viewport=VIEWPORT,
                locale="en-US",
            )
            page = context.pages[0] if context.pages else context.new_page()

            while not stop_event.is_set():
                try:
                    rec = work_q.get(timeout=2)
                except queue.Empty:
                    continue
                if rec is None:
                    break

                followers, email, status = fbem.process_facebook(
                    page, rec["facebook_page"], args.min_followers, args.max_followers, args.strict
                )
                source = "facebook" if email else None
                if status is None and not email and args.website_fallback:
                    email = fbem.email_from_website(context, rec.get("website"))
                    source = "website" if email else None

                event_q.put(("email", rec["facebook_page"], {
                    "followers": followers,
                    "email": email,
                    "email_source": status or source,
                }))
                time.sleep(random.uniform(3, 6))

            context.close()
    except Exception as e:
        event_q.put(("log", f"[EMAIL] worker stopped: {e.__class__.__name__}: {e}"))
    finally:
        event_q.put(("consumer_done",))


# ---------------------------------------------------------------- coordinator

def save(path, records):
    """Write atomically so the JSON file is never left half-written."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(list(records.values()), f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def is_final(rec, require_email=False):
    """Fully checked, inside the follower range (and, with --require-email, has an email)."""
    if "email_source" not in rec or rec["email_source"] == "skipped_followers":
        return False
    return bool(rec.get("email")) if require_email else True


def coordinate(args, event_q, stop_event, procs=(), existing=()):
    records = {}   # NEW leads from this run only: --limit, filters and finalising apply to these
    done = set()
    # Leads already in the file are carried through untouched on every save.
    existing_map = {(r.get("facebook_page") or f"_existing_{i}"): r
                    for i, r in enumerate(existing)}

    def snapshot():
        return {**existing_map, **records}
    try:
        while len(done) < 2:
            try:
                msg = event_q.get(timeout=1)
            except queue.Empty:
                if procs and not any(p.is_alive() for p in procs):
                    break  # both workers are gone and nothing is left to read
                continue

            kind = msg[0]
            if kind == "log":
                print(msg[1], flush=True)

            elif kind == "lead":
                _, key, rec = msg
                records.setdefault(key, rec)
                save(args.out, snapshot())
                name = rec.get("brand_name") or key
                ads = rec.get("active_ads")
                print(f"[ADS] passed ad filter: {name}" + (f" ({ads} active ads)" if ads is not None else ""),
                      flush=True)

            elif kind == "email":
                _, key, fields = msg
                rec = records.get(key)
                if rec is None:
                    continue
                rec.update(fields)
                name = rec.get("brand_name") or key
                fol = (f"{fields['followers']:,} followers"
                       if fields["followers"] is not None else "followers unknown")
                if fields["email_source"] == "skipped_followers":
                    print(f"[EMAIL] {name}: skipped ({fol})", flush=True)
                else:
                    print(f"[EMAIL] {name}: {fields['email'] or 'no email found'} ({fol})",
                          flush=True)
                save(args.out, snapshot())

                finals = sum(1 for r in records.values() if is_final(r, args.require_email))
                if args.limit and finals >= args.limit and not stop_event.is_set():
                    print(f"[DONE] reached {args.limit} leads, stopping workers...", flush=True)
                    stop_event.set()

            elif kind in ("producer_done", "consumer_done"):
                done.add(kind)
    except KeyboardInterrupt:
        print("\nStopping (Ctrl+C)... saving what's finished so far.", flush=True)
        stop_event.set()

    # ---- finalise
    # 0. --require-email: only brands with an email address go into the final file.
    #    Brands that were found but never checked (run stopped early) are saved to a
    #    side file instead of being lost.
    if args.require_email:
        with_email = sum(1 for r in records.values() if is_final(r, True))
        enough = bool(args.limit and with_email >= args.limit)
        pending = [r for r in records.values() if "email_source" not in r]
        if pending and not enough:
            side = os.path.splitext(args.out)[0] + "_unchecked.json"
            with open(side, "w", encoding="utf-8") as f:
                json.dump(pending, f, indent=2, ensure_ascii=False)
            print(f"[NOTE] {len(pending)} brands were found but not checked yet. Saved to "
                  f"{side}. Run fb_email_scraper.py on that file to check them.", flush=True)
        records = {k: r for k, r in records.items() if r.get("email")}

    # 1. brands outside the follower range are removed (unless --keep-skipped)
    if not args.keep_skipped:
        records = {k: r for k, r in records.items()
                   if r.get("email_source") != "skipped_followers"}

    # 2. stop at --limit final leads
    finals = sum(1 for r in records.values() if is_final(r, args.require_email))
    limit_reached = bool(args.limit and finals >= args.limit)
    if args.limit:
        kept, count = {}, 0
        for k, r in records.items():
            if is_final(r, args.require_email):
                if count >= args.limit:
                    continue
                count += 1
            kept[k] = r
        records = kept

    # 3. brands found but never checked (run stopped early, or the email worker failed):
    #    drop the extras if we already hit the limit, otherwise KEEP them, marked clearly.
    unchecked = [k for k, r in records.items() if "email_source" not in r]
    if limit_reached:
        for k in unchecked:
            del records[k]
    else:
        for k in unchecked:
            records[k].update({"followers": None, "email": None, "email_source": "not_checked"})
        if unchecked:
            print(f"[NOTE] {len(unchecked)} brands were found but not checked for "
                  f"followers/email (run stopped early or the email worker failed). "
                  f"They are kept in the file with email_source = 'not_checked'. "
                  f"Run fb_email_scraper.py on the file to finish them.", flush=True)

    save(args.out, snapshot())
    return records


# ---------------------------------------------------------------- main

def build_parser():
    ap = argparse.ArgumentParser()
    ap.add_argument("url", nargs="?", help="Ad Library search results URL")
    ap.add_argument("--out", default="leads.json")
    ap.add_argument("--limit", type=int, default=50,
                    help="stop after this many final leads (0 = no limit)")
    # Ad Library side
    ap.add_argument("--min-ads", type=int, default=1)
    ap.add_argument("--max-ads", type=int, default=99)
    ap.add_argument("--no-ad-filter", action="store_true")
    ap.add_argument("--debug", action="store_true")
    ap.add_argument("--max-scrolls", type=int, default=300)
    ap.add_argument("--pause", type=float, default=2.0)
    # Email side
    ap.add_argument("--min-followers", type=int, default=1000)
    ap.add_argument("--max-followers", type=int, default=50000)
    ap.add_argument("--strict", action="store_true")
    ap.add_argument("--website-fallback", action="store_true")
    ap.add_argument("--login", action="store_true")
    ap.add_argument("--profile-dir", default="fb_profile")
    # General
    ap.add_argument("--require-email", action="store_true",
                    help="only keep brands where an email was found; --limit then counts only these")
    ap.add_argument("--keep-skipped", action="store_true",
                    help="also keep brands that were skipped for being outside the follower range")
    ap.add_argument("--buffer", type=int, default=5,
                    help="max brands waiting for the email check; the Ad Library worker pauses "
                         "when this many are queued (default 5)")
    ap.add_argument("--headless", action="store_true")
    return ap


def main():
    args = build_parser().parse_args()
    args.url = args.url or input("Paste the Ad Library URL: ").strip().strip('"')
    if "facebook.com/ads/library" not in args.url:
        sys.exit("That doesn't look like an Ad Library URL.")

    existing, args.known = adlib.load_existing(args.out)  # args.known goes to the workers

    if args.login:  # done here because worker processes can't ask for keyboard input
        with sync_playwright() as pw:
            ctx = pw.chromium.launch_persistent_context(args.profile_dir, headless=False)
            pg = ctx.pages[0] if ctx.pages else ctx.new_page()
            pg.goto("https://www.facebook.com/login")
            input("Log in in the browser window, then press Enter here to continue...")
            ctx.close()

    if existing:
        print(f"{len(existing)} advertisers already in {args.out} will be skipped and kept; "
              f"--limit counts NEW leads only.")
    print(f"Starting. Final leads need {args.min_followers:,} to under {args.max_followers:,} "
          f"followers" + (" and an email address" if args.require_email else "") + ("" if args.no_ad_filter else
                          f" and {args.min_ads} to {args.max_ads} active ads") + ".")

    ctx = mp.get_context("spawn")
    work_q = ctx.Queue(maxsize=max(1, args.buffer))  # bounded: the ad worker can only run
    event_q, stop_event = ctx.Queue(), ctx.Event()   # this far ahead of the email worker
    procs = [
        ctx.Process(target=producer, args=(args, work_q, event_q, stop_event), name="ads"),
        ctx.Process(target=consumer, args=(args, work_q, event_q, stop_event), name="email"),
    ]
    for p in procs:
        p.start()

    records = coordinate(args, event_q, stop_event, procs, existing)

    for p in procs:
        p.join(timeout=15)
        if p.is_alive():
            p.terminate()

    emails = sum(1 for r in records.values() if r.get("email"))
    print(f"Done. {len(records)} new leads added ({emails} with an email). "
          f"{args.out} now has {len(existing) + len(records)} in total.")


if __name__ == "__main__":
    main()
