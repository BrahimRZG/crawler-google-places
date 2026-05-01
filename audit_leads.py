"""
Lead Filter & Audit Script
--------------------------
Input:  raw Apify Google Maps JSON export
Output: audited_leads.csv — only qualified targets

Requirements:
    pip install requests tldextract
    
Set your Google PSI API key below or via environment variable:
    export PSI_API_KEY=your_key_here
"""

import json
import csv
import time
import os
import re
import requests
import tldextract
from datetime import datetime

# ── CONFIG ────────────────────────────────────────────────────────────────────

PSI_API_KEY = os.getenv("PSI_API_KEY", "YOUR_PSI_API_KEY_HERE")

INPUT_FILE   = "dataset_crawler-google-places.json"
OUTPUT_FILE  = "audited_leads.csv"
RETRY_FILE   = "audited_leads.csv"   # set to CSV to retry only timed-out rows

# Filter thresholds
MIN_REVIEWS  = 10
MAX_REVIEWS  = 500   # exclude large companies
MIN_RATING   = 3.5
MAX_PSI_SCORE = 59   # only keep sites scoring below 60 on mobile

# Domains that indicate it's not a real website
EXCLUDED_DOMAINS = {
    "facebook.com", "instagram.com", "yelp.com",
    "yellowpages.com", "twitter.com", "linkedin.com",
    "localo.site", "business.site", "wixsite.com",
    "squarespace.com", "godaddysites.com", "weebly.com",
}

# ── HELPERS ───────────────────────────────────────────────────────────────────

def clean_url(url: str) -> str:
    """Strip UTM parameters and trailing slashes for cleaner auditing."""
    if not url:
        return ""
    # Remove UTM params
    url = re.sub(r'\?utm_.*$', '', url)
    url = re.sub(r'&utm_.*$', '', url)
    return url.rstrip("/")


def is_excluded_domain(url: str) -> bool:
    """Return True if the URL belongs to a platform we don't want."""
    ext = tldextract.extract(url)
    domain = f"{ext.domain}.{ext.suffix}"
    return domain in EXCLUDED_DOMAINS


def is_wordpress(url: str) -> tuple[bool, str]:
    """
    Attempt to detect WordPress by checking for wp-content or wp-includes.
    Returns (is_wp: bool, status: str)
    """
    try:
        headers = {"User-Agent": "Mozilla/5.0 (compatible; AuditBot/1.0)"}
        r = requests.get(url, headers=headers, timeout=12)
        html = r.text.lower()

        wp_signatures = [
            "wp-content",
            "wp-includes",
            "wp-json",
            'id="wp-block-library',
            "wordpress",
        ]

        found = any(sig in html for sig in wp_signatures)
        return found, "reachable"

    except requests.exceptions.SSLError:
        return False, "ssl_error"
    except requests.exceptions.ConnectionError:
        return False, "connection_error"
    except requests.exceptions.Timeout:
        return False, "timeout"
    except Exception as e:
        return False, f"error: {str(e)[:40]}"


def get_psi_score(url: str) -> tuple[int | None, str]:
    """
    Call Google PageSpeed Insights API for mobile score.
    Returns (score: int|None, status: str)
    Free tier: 25,000 requests/day
    """
    if PSI_API_KEY == "YOUR_PSI_API_KEY_HERE":
        print("  ⚠️  PSI API key not set — skipping speed check")
        return None, "no_api_key"

    endpoint = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
    params = {
        "url": url,
        "strategy": "mobile",
        "key": PSI_API_KEY,
        "category": "performance",
    }

    for attempt in range(3):  # retry up to 3 times
        try:
            r = requests.get(endpoint, params=params, timeout=60)
            data = r.json()

            if "error" in data:
                return None, f"api_error: {data['error'].get('message', '')[:50]}"

            score = data["lighthouseResult"]["categories"]["performance"]["score"]
            return int(score * 100), "ok"

        except requests.exceptions.Timeout:
            if attempt < 2:
                print(f"  ⏳ PSI timeout, retrying ({attempt+2}/3)...")
                time.sleep(3)
                continue
            return None, "psi_timeout"
        except (KeyError, json.JSONDecodeError) as e:
            return None, f"parse_error: {str(e)[:40]}"
        except Exception as e:
            return None, f"error: {str(e)[:40]}"
    return None, "psi_timeout"


# ── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    print(f"\n{'='*60}")
    print(f"  Lead Filter & Audit Script")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}\n")

    # Load raw data
    with open(INPUT_FILE, "r", encoding="utf-8") as f:
        raw = json.load(f)

    print(f"📥 Loaded {len(raw)} raw records\n")

    results = []
    skipped = 0
    errors  = 0

    for i, biz in enumerate(raw):
        name    = biz.get("title", "Unknown")
        website = biz.get("website", "") or ""
        reviews = biz.get("reviewsCount", 0) or 0
        rating  = biz.get("totalScore", 0) or 0
        phone   = biz.get("phone", "") or ""
        city    = biz.get("city", "") or ""
        state   = biz.get("state", "") or ""
        category = biz.get("categoryName", "") or ""

        print(f"[{i+1}/{len(raw)}] {name}")

        # ── Pre-flight filters (no network calls) ──────────────────────────

        if not website:
            print(f"  ✗ No website — skipped\n")
            skipped += 1
            continue

        if is_excluded_domain(website):
            print(f"  ✗ Platform/directory URL — skipped\n")
            skipped += 1
            continue

        if reviews < MIN_REVIEWS:
            print(f"  ✗ Too few reviews ({reviews}) — skipped\n")
            skipped += 1
            continue

        if reviews > MAX_REVIEWS:
            print(f"  ✗ Too many reviews ({reviews}) — large company, skipped\n")
            skipped += 1
            continue

        if rating < MIN_RATING:
            print(f"  ✗ Rating too low ({rating}) — skipped\n")
            skipped += 1
            continue

        # Clean URL before network calls
        clean = clean_url(website)
        print(f"  🌐 {clean}")

        # ── WordPress check ────────────────────────────────────────────────

        is_wp, wp_status = is_wordpress(clean)
        print(f"  {'✅ WordPress' if is_wp else '○  Not WordPress'} ({wp_status})")

        # We audit ALL reachable sites, not just WordPress
        # WordPress flag is just extra data — a slow non-WP site is still a lead
        if wp_status in ("connection_error", "timeout", "ssl_error"):
            print(f"  ✗ Site unreachable — skipped\n")
            skipped += 1
            continue

        # ── PageSpeed audit ────────────────────────────────────────────────

        psi_score, psi_status = get_psi_score(clean)

        if psi_score is not None:
            print(f"  📊 Mobile PSI score: {psi_score}/100")
        else:
            print(f"  📊 PSI: {psi_status}")

        # If we got a score and it's too high, skip (site is already fast)
        if psi_score is not None and psi_score > MAX_PSI_SCORE:
            print(f"  ✗ Site already fast ({psi_score}/100) — not a good lead\n")
            skipped += 1
            continue

        # ── Qualified lead — save it ───────────────────────────────────────

        print(f"  ✅ QUALIFIED LEAD\n")

        results.append({
            "business_name": name,
            "website":       clean,
            "phone":         phone,
            "city":          city,
            "state":         state,
            "category":      category,
            "reviews":       reviews,
            "rating":        rating,
            "is_wordpress":  is_wp,
            "psi_mobile":    psi_score if psi_score is not None else "not_checked",
            "wp_status":     wp_status,
            "psi_status":    psi_status,
            "scraped_at":    datetime.now().strftime("%Y-%m-%d"),
        })

        # Polite delay between PSI calls to avoid rate limiting
        time.sleep(1.5)

    # ── Write output CSV ───────────────────────────────────────────────────

    if results:
        fieldnames = [
            "business_name", "website", "phone", "city", "state",
            "category", "reviews", "rating", "is_wordpress",
            "psi_mobile", "wp_status", "psi_status", "scraped_at",
        ]

        with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(results)

    # ── Summary ────────────────────────────────────────────────────────────

    print(f"\n{'='*60}")
    print(f"  RESULTS SUMMARY")
    print(f"{'='*60}")
    print(f"  Total input:      {len(raw)}")
    print(f"  Qualified leads:  {len(results)}")
    print(f"  Skipped:          {skipped}")
    print(f"  Errors:           {errors}")
    print(f"  Output file:      {OUTPUT_FILE}")

    if results:
        wp_count  = sum(1 for r in results if r["is_wordpress"])
        psi_vals  = [r["psi_mobile"] for r in results if isinstance(r["psi_mobile"], int)]
        avg_psi   = int(sum(psi_vals) / len(psi_vals)) if psi_vals else "N/A"

        print(f"\n  WordPress sites:  {wp_count}/{len(results)}")
        print(f"  Avg mobile score: {avg_psi}/100")

        print(f"\n  Top leads by lowest PSI score:")
        scored = sorted(
            [r for r in results if isinstance(r["psi_mobile"], int)],
            key=lambda x: x["psi_mobile"]
        )
        for r in scored[:5]:
            print(f"    {r['psi_mobile']:3}/100  {r['business_name']} — {r['website']}")

    print(f"\n{'='*60}\n")


def retry_timeouts(csv_path: str):
    """
    Re-run PSI checks only on rows where psi_status = psi_timeout.
    Updates the CSV in place.
    """
    import csv as csv_module

    with open(csv_path, "r", encoding="utf-8") as f:
        rows = list(csv_module.DictReader(f))

    fieldnames = list(rows[0].keys()) if rows else []
    pending = [r for r in rows if r.get("psi_status") == "psi_timeout"]

    if not pending:
        print("No timed-out rows to retry.")
        return

    print(f"\n🔄 Retrying PSI for {len(pending)} timed-out rows...\n")

    for row in pending:
        name = row["business_name"]
        url  = row["website"]
        print(f"  {name}")
        score, status = get_psi_score(url)
        row["psi_mobile"] = score if score is not None else "not_checked"
        row["psi_status"] = status
        if score is not None:
            print(f"  📊 {score}/100\n")
        else:
            print(f"  📊 {status}\n")
        time.sleep(2)

    # Remove rows where site turned out to be fast (score > MAX_PSI_SCORE)
    kept = []
    removed = 0
    for row in rows:
        score = row.get("psi_mobile")
        if isinstance(score, int) and score > MAX_PSI_SCORE:
            removed += 1
            continue
        try:
            if int(score) > MAX_PSI_SCORE:
                removed += 1
                continue
        except (ValueError, TypeError):
            pass
        kept.append(row)

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv_module.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(kept)

    print(f"✅ Done. {len(kept)} leads kept, {removed} removed (sites too fast).")


# ── ENTRY POINT ───────────────────────────────────────────────────────────────


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--retry":
        target = sys.argv[2] if len(sys.argv) > 2 else RETRY_FILE
        retry_timeouts(target)
    else:
        main()
