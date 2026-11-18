# Ad Library Lead Pipeline

Find small businesses that are running Meta (Facebook/Instagram) ads, filter them by active-ad count and follower range, and collect their public contact emails into a single JSON file.

Built with Python and [Playwright](https://playwright.dev/python/). No API keys required.

## What it does

Give it a search from the [Meta Ad Library](https://www.facebook.com/ads/library/) (a keyword, a country, active ads only) and it runs two workers in parallel:

1. **Ad Library worker** scrolls the results, identifies each advertiser, and counts how many ads that advertiser has active right now in the chosen country.
2. **Email worker** takes each qualifying advertiser, reads their Facebook follower count, and looks for a public email on their Facebook page (and optionally on their own website).

Only brands that pass every filter are saved as leads.

## Features

- **Active-ad filter.** Keep only advertisers with, for example, 1 to 99 active ads. This helps you target small, active advertisers and skip large brands.
- **Follower filter.** Keep only pages inside a follower range (default 1,000 to under 50,000).
- **Email extraction.** Reads emails from the Facebook page (main page, About and contact views), with an optional fallback that checks the brand's own site (`/contact`, `/contact-us`, and so on).
- **Accurate advertiser IDs.** Pairs each ad's Library ID with the advertiser's page ID taken from Meta's own data, so active-ad counts are read for the correct advertiser.
- **Incremental runs.** `--limit N` means N *new* leads. Brands already in your output file are skipped before any slow checks, matched by page ID and URL, and your existing results are kept.
- **Bounded pipeline.** The ad worker runs only a few brands ahead of the email worker, so it doesn't waste page loads on brands you'll never need.
- **Safe to interrupt.** The JSON file is saved atomically after every brand. Ctrl+C keeps everything found so far, and unfinished brands can be completed later.
- **Reusable stages.** Run the scraper and the email extractor separately on any JSON file when you want more control.

## How it works

```
 Ad Library search URL
          |
          v
 +----------------------+      bounded queue      +------------------------+
 |  Ad Library worker   | ----------------------> |      Email worker      |
 |  scroll, find brands |   (pauses when full)    |  followers, emails     |
 |  count active ads    |                         |  Facebook, then website|
 +----------------------+                         +------------------------+
          \                                                /
           \______________  coordinator  _________________/
                    dedupe, apply --limit, save JSON
```

## Requirements

- Python 3.9 or newer
- Playwright with Chromium

```bash
pip install playwright
playwright install chromium
```

## Quick start

1. Open the Ad Library, choose a country, set status to **Active**, search a keyword, and copy the URL.
2. Log in to Facebook once. The session is stored locally in `fb_profile/`:

   ```bash
   python lead_pipeline.py "<ad-library-url>" --login --limit 3
   ```

3. Run a real batch:

   ```bash
   python lead_pipeline.py "<ad-library-url>" --limit 50 --out leads.json --website-fallback
   ```

Always wrap the URL in quotes; it contains `&` characters.

## Common options

| Option | Default | Description |
|---|---|---|
| `--limit N` | 50 | Stop after N **new** leads (0 = no limit) |
| `--out FILE` | `leads.json` | Output file; existing entries are kept and skipped |
| `--min-ads` / `--max-ads` | 1 / 99 | Active-ad range an advertiser must fall in |
| `--no-ad-filter` | off | Skip the active-ad check (much faster) |
| `--min-followers` / `--max-followers` | 1000 / 50000 | Follower range (max is exclusive) |
| `--website-fallback` | off | Check the brand's own site if Facebook shows no email |
| `--require-email` | off | Keep only brands with an email; `--limit` counts only these |
| `--keep-skipped` | off | Keep brands skipped for being outside the follower range |
| `--buffer N` | 5 | How far the ad worker may run ahead of the email worker |
| `--login` | off | Open a Facebook login window first |
| `--headless` | off | Hide the browser windows |
| `--debug` | off | Save raw cards, payloads, and screenshots for troubleshooting |

See [`USAGE_GUIDE.md`](USAGE_GUIDE.md) for every option, the standalone scripts, and troubleshooting.

## Output

A JSON list with one record per brand:

```json
{
  "brand_name": "Example Brand",
  "facebook_page": "https://www.facebook.com/examplebrand",
  "instagram_page": "https://www.instagram.com/examplebrand/",
  "website": "https://example.com",
  "page_id": "1234567890",
  "ad_status": "Active",
  "active_ads": 12,
  "followers": 8400,
  "email": "hello@example.com",
  "email_source": "facebook"
}
```

`email_source` is `facebook`, `website`, `skipped_followers`, `not_checked` (found but not yet checked), or `null` (checked, no email found).

## Project structure

| File | Role |
|---|---|
| `lead_pipeline.py` | Runs both stages in parallel; the main entry point |
| `meta_adlib_scraper.py` | Stage 1: finds advertisers and counts active ads |
| `fb_email_scraper.py` | Stage 2: checks followers and finds emails |

## Limitations

- **It depends on Meta's page layout and data fields**, which change without notice. If results suddenly drop to zero, the selectors or field names have probably changed.
- A keyword search returns advertisers whose ads or page match the words; it doesn't guarantee they are the kind of business you want. Expect some noise and review results by hand.
- Many small businesses don't publish an email, so expect a share of leads with `email: null`.
- Facebook may show login walls, checkpoints, or captchas, especially on large or fast runs.

## Disclaimer

This project is for research and educational purposes. **Automated access to Facebook may violate Meta's Terms of Service**, and accounts used for scraping can be restricted or banned. Use a separate account, keep request rates low, and review the terms yourself before using this tool.

If you contact people using collected emails, you are responsible for complying with the laws that apply to you (for example GDPR, PECR, and CAN-SPAM). A publicly visible email address is not consent to receive marketing.

Do not commit your `fb_profile/` folder (it contains your Facebook session) or your output files (they contain personal data) to a public repository.

## License

Add a license of your choice (for example MIT) before publishing.
