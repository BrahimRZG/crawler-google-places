#!/usr/bin/env python3
"""
audit_leads.py — Lead Filter & Audit (VPS sharded edition, self-contained)
--------------------------------------------------------------------------
Input:  JSON export (raw Apify Google Maps OR pre-cleaned dataset)
Output: results_shard_<N>.csv  (one row per lead, written incrementally)

Run (8 parallel shards):
    for i in 0 1 2 3 4 5 6 7; do
      SHARD_INDEX=$i SHARD_TOTAL=8 nohup python audit_leads.py > shard_$i.log 2>&1 &
    done

Resume: rerun the same command — rows already in results_shard_N.csv are skipped.

pip install requests tldextract
export PSI_API_KEY=...
"""

import csv
import json
import os
import re
import sys
import time
from datetime import datetime
from urllib.parse import urlparse, parse_qs

import requests
import tldextract

# ------------------------------------------------------------------ config
INPUT_FILE      = os.environ.get("INPUT_FILE", "dataset_crawler-google-places.json")
PSI_API_KEY     = os.environ.get("PSI_API_KEY", "")
SHARD_INDEX     = int(os.environ.get("SHARD_INDEX", 0))
SHARD_TOTAL     = int(os.environ.get("SHARD_TOTAL", 1))
OUT_FILE        = f"results_shard_{SHARD_INDEX}.csv"

QUALIFY_MAX_PSI = 55        # PSI mobile varies ±5-8 between runs; 60 flips borderline sites
MIN_REVIEWS     = 10
MAX_REVIEWS     = 500
MIN_RATING      = 4.0
PSI_TIMEOUT_S   = 60
PSI_RETRIES     = 3
STATES          = {"TX", "CA", "NY", "FL"}

HVAC_PATTERN = re.compile(
    r"HVAC|Heating|Air conditioning|Furnace|air duct|Mechanical contractor", re.I
)

# National chains / franchises / suppliers whose per-location listings slip
# under the review filter. Never migration candidates.
CHAIN_DOMAINS = {
    "searshomeservices.com", "homedepot.com", "lowes.com",
    "ferguson.com", "johnstonesupply.com", "johnstonewaregroup.com",
    "sunbeltrentals.com", "unitedrentals.com",
    "carrierenterprise.com", "bakerdist.com", "gemaire.com",
    "acpro.com", "aireserv.com", "onehourheatandair.com",
    "serviceexperts.com", "us-ac.com", "sonsrayfleetservices.com",
}
# Hosted site builders: "has website" == True but nothing to migrate.
SITEBUILDER_PATTERNS = (
    "ueniweb.com", "jobbersites.com", "base44.app", "canva.site",
    "business.site", "godaddysites.com", "square.site", "wixsite.com",
    "weebly.com", "duda.co", "facebook.com", "instagram.com", "yelp.com",
)

STATE_MAP = {"Texas": "TX", "California": "CA", "New York": "NY", "Florida": "FL",
             "TX": "TX", "CA": "CA", "NY": "NY", "FL": "FL"}

FIELDNAMES = ["title", "phone", "website", "domain", "city", "state",
              "reviewsCount", "totalScore", "is_wordpress", "psi_score",
              "status", "reason"]

# ------------------------------------------------------------------ helpers
def registered_domain(url: str) -> str:
    if not url:
        return ""
    ext = tldextract.extract(url)
    return f"{ext.domain}.{ext.suffix}".lower() if ext.suffix else ext.domain.lower()


def norm_state(lead: dict) -> str:
    s = STATE_MAP.get(str(lead.get("state") or "").strip(), "")
    if s:
        return s
    m = re.search(r",\s*([A-Z]{2})\s+\d{5}", str(lead.get("address") or ""))
    if m and m.group(1) in STATES:
        return m.group(1)
    return ""


def place_id(lead: dict) -> str:
    if lead.get("place_id"):
        return lead["place_id"]
    q = parse_qs(urlparse(lead.get("url", "") or "").query)
    return q.get("query_place_id", [""])[0]


def load_done() -> set:
    done = set()
    if os.path.exists(OUT_FILE):
        with open(OUT_FILE, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                key = row.get("website") or row.get("title")
                if key:
                    done.add(key)
    return done


def append_result(lead, status, reason="", psi_score=None, is_wordpress=None):
    """Write ONE row immediately — a killed process keeps everything written."""
    new = not os.path.exists(OUT_FILE)
    with open(OUT_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDNAMES)
        if new:
            w.writeheader()
        w.writerow({
            "title": lead.get("title", ""),
            "phone": lead.get("phone", ""),
            "website": lead.get("website", ""),
            "domain": registered_domain(lead.get("website", "")),
            "city": lead.get("city", ""),
            "state": norm_state(lead),
            "reviewsCount": lead.get("reviewsCount", ""),
            "totalScore": lead.get("totalScore", ""),
            "is_wordpress": is_wordpress,
            "psi_score": psi_score,
            "status": status,      # qualified | rejected | needs_recheck | skipped
            "reason": reason,
        })


def check_wordpress(url: str):
    """Returns (is_wp: bool|None, note). None = unreachable."""
    try:
        r = requests.get(url, timeout=20, allow_redirects=True,
                         headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64)"})
        html = r.text[:200_000].lower()
        return ("wp-content" in html or "wp-includes" in html), "reachable"
    except requests.exceptions.SSLError:
        return None, "ssl_error"
    except requests.exceptions.Timeout:
        return None, "timeout"
    except requests.exceptions.RequestException as e:
        return None, f"connection_error"


def psi_mobile_score(url: str):
    """Returns int score, or an error string. NEVER raises on missing score."""
    endpoint = ("https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
                f"?url={url}&strategy=mobile&key={PSI_API_KEY}")
    last_err = "psi_timeout"
    for attempt in range(1, PSI_RETRIES + 1):
        try:
            r = requests.get(endpoint, timeout=PSI_TIMEOUT_S)
            data = r.json()
            if "error" in data:
                msg = data["error"].get("message", "unknown")[:80]
                return f"api_error: {msg}"
            raw = (data.get("lighthouseResult", {})
                       .get("categories", {})
                       .get("performance", {})
                       .get("score"))
            if raw is None:                       # <- the None*100 crash, fixed
                return "psi_no_score"
            return int(round(raw * 100))
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
            last_err = "psi_timeout"
            if attempt < PSI_RETRIES:
                print(f"  ⏳ PSI timeout, retrying ({attempt+1}/{PSI_RETRIES})...", flush=True)
                time.sleep(3)
        except (ValueError, KeyError):
            return "psi_bad_response"
    return last_err


def classify(psi_result):
    """API failure is NOT evidence a site is slow — never qualify errors."""
    if not isinstance(psi_result, int):
        return "needs_recheck", str(psi_result)
    if psi_result <= QUALIFY_MAX_PSI:
        return "qualified", f"psi_{psi_result}"
    return "rejected", f"fast_{psi_result}"


# ------------------------------------------------------------------ main
def main():
    if not PSI_API_KEY:
        sys.exit("PSI_API_KEY not set")

    print("=" * 60)
    print(f"  Lead Audit — shard {SHARD_INDEX + 1}/{SHARD_TOTAL}")
    print(f"  {datetime.now():%Y-%m-%d %H:%M:%S}")
    print("=" * 60, flush=True)

    with open(INPUT_FILE, encoding="utf-8") as f:
        raw = json.load(f)
    print(f"📥 Loaded {len(raw)} raw records", flush=True)

    # ---- clean: HVAC-only, in-scope state, dedupe by place_id then domain
    seen_pid, seen_dom, leads = set(), set(), []
    for lead in raw:
        if not HVAC_PATTERN.search(str(lead.get("categoryName") or "")):
            continue
        if not norm_state(lead):
            continue
        pid = place_id(lead)
        if pid and pid in seen_pid:
            continue
        seen_pid.add(pid)
        dom = registered_domain(lead.get("website", "") or "")
        if dom:
            if dom in seen_dom:
                continue                     # multi-location listings, one audit
            seen_dom.add(dom)
        leads.append(lead)
    print(f"🧹 After clean/dedupe: {len(leads)}", flush=True)

    # ---- shard split + resume
    shard = [l for i, l in enumerate(leads) if i % SHARD_TOTAL == SHARD_INDEX]
    done = load_done()
    print(f"🔢 Shard size: {len(shard)}  |  already done: {len(done)}", flush=True)

    for n, lead in enumerate(shard, 1):
        title = lead.get("title", "?")
        url = (lead.get("website") or "").strip()
        key = url or title
        print(f"\n[{n}/{len(shard)}] {title}", flush=True)

        if key in done:
            print("  ↩ already processed — skipped", flush=True)
            continue

        # cheap filters first — zero network cost
        reviews = float(lead.get("reviewsCount") or 0)
        rating = float(lead.get("totalScore") or 0)
        if reviews < MIN_REVIEWS:
            print(f"  ✗ Too few reviews ({reviews:g})", flush=True)
            append_result(lead, "skipped", "too_few_reviews"); continue
        if reviews > MAX_REVIEWS:
            print(f"  ✗ Too many reviews ({reviews:g}) — large company", flush=True)
            append_result(lead, "skipped", "too_many_reviews"); continue
        if rating and rating < MIN_RATING:
            print(f"  ✗ Rating too low ({rating})", flush=True)
            append_result(lead, "skipped", "low_rating"); continue
        if not url:
            append_result(lead, "skipped", "no_website"); continue

        dom = registered_domain(url)
        if dom in CHAIN_DOMAINS:
            print("  ✗ National chain — skipped", flush=True)
            append_result(lead, "skipped", "national_chain"); continue
        if any(p in url.lower() for p in SITEBUILDER_PATTERNS):
            print("  ✗ Site builder / social — skipped", flush=True)
            append_result(lead, "skipped", "site_builder"); continue

        # network: WordPress check
        print(f"  🌐 {url}", flush=True)
        is_wp, note = check_wordpress(url)
        if is_wp is None:
            print(f"  ✗ Site unreachable ({note})", flush=True)
            append_result(lead, "needs_recheck", f"unreachable_{note}"); continue
        print(f"  {'✅ WordPress' if is_wp else '○  Not WordPress'} ({note})", flush=True)

        # network: PSI
        psi = psi_mobile_score(url)
        status, reason = classify(psi)
        score = psi if isinstance(psi, int) else None
        if score is not None:
            print(f"  📊 Mobile PSI score: {score}/100", flush=True)
        else:
            print(f"  📊 PSI: {psi}", flush=True)
        icon = {"qualified": "✅ QUALIFIED LEAD",
                "rejected": "✗ Site already fast — not a good lead",
                "needs_recheck": "⚠ needs recheck"}[status]
        print(f"  {icon}", flush=True)
        append_result(lead, status, reason, psi_score=score, is_wordpress=is_wp)

    print(f"\n✔ Shard {SHARD_INDEX} complete → {OUT_FILE}", flush=True)


if __name__ == "__main__":
    main()
