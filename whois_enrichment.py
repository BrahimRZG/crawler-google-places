"""
WHOIS Enrichment Script
------------------------
Input:  audited_leads.csv (from audit_leads.py)
Output: enriched_leads.csv — same data + WHOIS fields + platform detection

Adds these columns:
    domain_registered   — date the domain was first registered
    domain_expires      — expiration date
    domain_age_years    — how old the domain is (older = more stuck in old stack)
    days_until_expiry   — urgency signal (< 90 days = renewal conversation)
    registrar           — registrar name
    name_servers        — raw nameserver list
    platform_detected   — Wix / Squarespace / GoDaddy / Cloudflare / Other
    platform_note       — human-readable note for email copy

Requirements:
    pip install python-whois requests tldextract
"""

import csv
import time
import os
import re
from datetime import datetime, date

# ── PLATFORM DETECTION ────────────────────────────────────────────────────────
# Maps nameserver keywords → platform name + email copy note

PLATFORM_MAP = [
    (["wixdns.net", "wix.com"],
     "Wix",
     "built on Wix — monthly subscription with limited SEO control"),

    (["squarespace.com"],
     "Squarespace",
     "on Squarespace — recurring fees and restricted performance options"),

    (["godaddy.com", "domaincontrol.com", "secureserver.net"],
     "GoDaddy",
     "hosted on GoDaddy — typically slow shared hosting"),

    (["hostgator.com", "hostgator"],
     "HostGator",
     "on HostGator shared hosting — known for slow load times"),

    (["bluehost.com"],
     "Bluehost",
     "on Bluehost shared hosting — common source of WordPress slowdowns"),

    (["siteground"],
     "SiteGround",
     "on SiteGround — better than average but still paying monthly fees"),

    (["cloudflare.com"],
     "Cloudflare",
     "already using Cloudflare DNS — good foundation, site performance still improvable"),

    (["wordpress.com", "automattic"],
     "WordPress.com",
     "on WordPress.com — heavily restricted, no plugin control"),

    (["netlify.com"],
     "Netlify",
     "on Netlify — modern stack, likely already a developer-maintained site"),

    (["vercel.com"],
     "Vercel",
     "on Vercel — modern stack, already developer-maintained"),

    (["ns1.digitalocean", "ns2.digitalocean", "ns3.digitalocean"],
     "DigitalOcean",
     "self-hosted on DigitalOcean — developer-managed, may still be slow"),
]

def detect_platform(name_servers: list[str]) -> tuple[str, str]:
    """
    Match nameservers against known platform signatures.
    Returns (platform_name, copy_note)
    """
    ns_str = " ".join(name_servers).lower()

    for keywords, platform, note in PLATFORM_MAP:
        if any(kw in ns_str for kw in keywords):
            return platform, note

    return "Unknown", "on an unidentified hosting platform"


# ── URGENCY CLASSIFIER ────────────────────────────────────────────────────────

def expiry_urgency(days: int | None) -> str:
    """
    Returns a copy-ready urgency note based on days until domain expiry.
    """
    if days is None:
        return ""
    if days < 0:
        return "EXPIRED — domain may already be lost"
    if days <= 30:
        return f"domain expires in {days} days — URGENT renewal needed"
    if days <= 90:
        return f"domain expires in {days} days — renewal coming up soon"
    if days <= 180:
        return f"domain renews in about {days // 30} months"
    return ""


# ── WHOIS LOOKUP ──────────────────────────────────────────────────────────────

def lookup_whois(domain: str) -> dict:
    """
    Perform WHOIS lookup and return structured data.
    Falls back gracefully on any failure.
    """
    try:
        import whois
        w = whois.whois(domain)

        # creation_date can be a list or a single datetime
        def extract_date(val) -> date | None:
            if val is None:
                return None
            if isinstance(val, list):
                val = val[0]
            if isinstance(val, datetime):
                return val.date()
            if isinstance(val, date):
                return val
            return None

        registered = extract_date(w.creation_date)
        expires    = extract_date(w.expiration_date)
        registrar  = (w.registrar or "").strip()

        # Nameservers — normalize to list of lowercase strings
        ns_raw = w.name_servers or []
        if isinstance(ns_raw, str):
            ns_raw = [ns_raw]
        name_servers = sorted({ns.lower().strip() for ns in ns_raw})

        # Derived fields
        today = date.today()
        domain_age_years = None
        days_until_expiry = None

        if registered:
            delta = today - registered
            domain_age_years = round(delta.days / 365.25, 1)

        if expires:
            days_until_expiry = (expires - today).days

        platform, platform_note = detect_platform(name_servers)
        urgency = expiry_urgency(days_until_expiry)

        return {
            "domain_registered":  registered.isoformat() if registered else "",
            "domain_expires":     expires.isoformat() if expires else "",
            "domain_age_years":   domain_age_years or "",
            "days_until_expiry":  days_until_expiry if days_until_expiry is not None else "",
            "registrar":          registrar,
            "name_servers":       " | ".join(name_servers),
            "platform_detected":  platform,
            "platform_note":      platform_note,
            "expiry_urgency":     urgency,
            "whois_status":       "ok",
        }

    except Exception as e:
        error_msg = str(e)[:60]
        return {
            "domain_registered":  "",
            "domain_expires":     "",
            "domain_age_years":   "",
            "days_until_expiry":  "",
            "registrar":          "",
            "name_servers":       "",
            "platform_detected":  "",
            "platform_note":      "",
            "expiry_urgency":     "",
            "whois_status":       f"error: {error_msg}",
        }


# ── DOMAIN EXTRACTOR ──────────────────────────────────────────────────────────

def extract_domain(url: str) -> str:
    """Strip protocol and path, return bare domain for WHOIS query."""
    url = url.replace("https://", "").replace("http://", "")
    url = url.replace("www.", "")
    return url.split("/")[0].split("?")[0].strip()


# ── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    INPUT_FILE  = "audited_leads.csv"
    OUTPUT_FILE = "enriched_leads.csv"

    print(f"\n{'='*60}")
    print(f"  WHOIS Enrichment")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}\n")

    with open(INPUT_FILE, "r", encoding="utf-8") as f:
        leads = list(csv.DictReader(f))

    print(f"📥 Leads to enrich: {len(leads)}\n")

    whois_fields = [
        "domain_registered", "domain_expires", "domain_age_years",
        "days_until_expiry", "registrar", "name_servers",
        "platform_detected", "platform_note", "expiry_urgency", "whois_status",
    ]

    enriched = []

    for i, lead in enumerate(leads):
        name = lead["business_name"]
        url  = lead["website"]
        domain = extract_domain(url)

        print(f"[{i+1}/{len(leads)}] {name}")
        print(f"  🔍 WHOIS: {domain}")

        data = lookup_whois(domain)

        print(f"  📅 Registered: {data['domain_registered'] or 'unknown'}")
        print(f"  ⏳ Expires:    {data['domain_expires'] or 'unknown'}")
        print(f"  🏗️  Platform:   {data['platform_detected']}")
        if data['expiry_urgency']:
            print(f"  🚨 Urgency:    {data['expiry_urgency']}")
        print(f"  ✅ Status:     {data['whois_status']}\n")

        enriched.append({**lead, **data})

        # WHOIS servers rate-limit aggressively — wait between queries
        time.sleep(2)

    # Write output
    if enriched:
        original_fields = list(leads[0].keys())
        all_fields = original_fields + whois_fields

        with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=all_fields)
            writer.writeheader()
            writer.writerows(enriched)

    # Summary
    print(f"{'='*60}")
    print(f"  SUMMARY")
    print(f"{'='*60}")
    print(f"  Enriched: {len(enriched)} leads")
    print(f"  Output:   {OUTPUT_FILE}")

    platforms = {}
    for r in enriched:
        p = r.get("platform_detected") or "Unknown"
        platforms[p] = platforms.get(p, 0) + 1

    print(f"\n  Platform breakdown:")
    for p, count in sorted(platforms.items(), key=lambda x: -x[1]):
        print(f"    {p:20} {count}")

    urgent = [r for r in enriched if r.get("days_until_expiry") and
              isinstance(r["days_until_expiry"], int) and r["days_until_expiry"] < 90]
    if urgent:
        print(f"\n  ⚠️  Domains expiring within 90 days: {len(urgent)}")
        for r in urgent:
            print(f"    {r['business_name']} — {r['days_until_expiry']} days")

    print(f"\n{'='*60}\n")


if __name__ == "__main__":
    main()
