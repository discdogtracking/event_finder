#!/usr/bin/env python3
"""
UpDog Challenge scraper (table-based + "When" parsing).
Saves structured events to `data/events.json`.

Dependencies:
  pip install requests beautifulsoup4 python-dateutil
"""

import requests
from bs4 import BeautifulSoup
import time
import json
from urllib.parse import urljoin, urlparse
from dateutil import parser as dateparser
import hashlib
import os
import re
from datetime import timedelta

# Ensure the data folder exists
os.makedirs("data", exist_ok=True)

BASE = "https://updogchallenge.com"
START = BASE + "/events/"
OUTPUT = "data/events.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/121.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

DELAY = 1.0  # seconds between requests (polite)
REQUEST_TIMEOUT = 60
MAX_RETRIES = 4
MIN_EXPECTED_EVENTS = int(os.getenv("MIN_EXPECTED_EVENTS", "80"))


def safe_get(session, url, *, required=False):
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            return BeautifulSoup(resp.text, "html.parser")
        except Exception as e:
            last_error = e
            print(f"[ERROR] GET {url} attempt {attempt}/{MAX_RETRIES} -> {e}")
            if attempt < MAX_RETRIES:
                time.sleep(min(30, attempt * 5))

    if required:
        raise RuntimeError(f"Required page failed after retries: {url}") from last_error
    return None


def normalize_key(text: str) -> str:
    if not text:
        return ""
    text = text.replace("\xa0", " ").strip()
    if text.endswith(":"):
        text = text[:-1]
    return text.strip()


def make_persistent_event_id(details: dict, event_url: str) -> str:
    """
    Deterministic EVENT id based on:
      - event slug
      - club name
      - contact (host) name
    """
    path = urlparse(event_url).path.rstrip("/")
    slug = path.split("/")[-1].lower()

    club = (details.get("club_name") or "").strip().lower()
    host = (details.get("contact_name") or "").strip().lower()

    raw = f"{slug}|{club}|{host}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _format_mdy(dt):
    # Keep output compatible with app parser (M/D/YYYY)
    return f"{dt.month}/{dt.day}/{dt.year}"


def _unique_preserve_order(items):
    seen = set()
    out = []
    for i in items:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


def _extract_when_text(soup):
    # Example:
    # <div class="em-item-meta-line em-event-date em-event-meta-datetime">09/05/2026 - 09/06/2026</div>
    el = soup.select_one(".em-item-meta-line.em-event-date.em-event-meta-datetime")
    if not el:
        return None
    txt = el.get_text(" ", strip=True).replace("\xa0", " ").strip()
    txt = re.sub(r"\s+", " ", txt)
    return txt or None


def _parse_date_token(token, default=None):
    token = (token or "").strip()
    if not token:
        return None
    try:
        return dateparser.parse(token, fuzzy=True, default=default)
    except Exception:
        return None


def _expand_inclusive_days(start_dt, end_dt):
    days = []
    cur = start_dt
    while cur.date() <= end_dt.date():
        days.append(cur)
        cur = cur + timedelta(days=1)
    return days


def _event_dates_from_when_text(when_text):
    """
    Returns a list of datetime objects representing actual event days.

    Rules:
    - "5/1/2026 - 5/4/2026" => expand every day
    - Comma/and separated dates => explicit individual dates
    - Single date => one date
    """
    if not when_text:
        return []

    clean = re.sub(r"\s+", " ", when_text.replace("\xa0", " ")).strip()

    # Normalize common separators
    explicit_parts = re.split(r"\s*(?:,|;|\band\b|\&)\s*", clean, flags=re.IGNORECASE)

    # If we have explicit multiple chunks and each parses, treat as discrete dates
    explicit_dates = []
    for p in explicit_parts:
        if not p:
            continue
        dt = _parse_date_token(p)
        if dt:
            explicit_dates.append(dt)

    if len(explicit_dates) >= 2:
        # Could still include "A - B" as one part; handle that below first.
        pass

    # Range pattern: A - B
    # Supports "-" / "–" / "—"
    range_match = re.match(r"^\s*(.+?)\s*[-–—]\s*(.+?)\s*$", clean)
    if range_match:
        left_raw = range_match.group(1).strip()
        right_raw = range_match.group(2).strip()

        left_dt = _parse_date_token(left_raw)
        right_dt = _parse_date_token(right_raw, default=left_dt or None)

        if left_dt and right_dt:
            # If right side omitted year, parser default above usually helps.
            # If parser still put impossible order, swap.
            if right_dt < left_dt:
                left_dt, right_dt = right_dt, left_dt
            return _expand_inclusive_days(left_dt, right_dt)

    # If not a clean range, fall back to explicit list parsing
    if len(explicit_dates) >= 1:
        return explicit_dates

    # Final fallback: parse as a single fuzzy date
    one = _parse_date_token(clean)
    return [one] if one else []


def _apply_when_dates(detail, when_text):
    """
    Fill:
      - when_text
      - event_dates (exact event days)
      - start/end + iso + notification_date
    """
    if when_text:
        detail["when_text"] = when_text

    days = _event_dates_from_when_text(when_text)
    if not days:
        return

    # Normalize to day precision, de-dupe, sort
    normalized = []
    for dt in days:
        normalized.append(dt.replace(hour=0, minute=0, second=0, microsecond=0))
    normalized.sort()

    # Build string list for app-side exact-day logic
    event_dates = _unique_preserve_order([_format_mdy(d) for d in normalized])
    detail["event_dates"] = event_dates

    start = normalized[0]
    end = normalized[-1]

    # Prefer "When" over table start/end
    detail["start_date"] = _format_mdy(start)
    detail["end_date"] = _format_mdy(end)
    detail["start_date_iso"] = start.isoformat()
    detail["end_date_iso"] = end.isoformat()
    detail["notification_date"] = start.strftime("%Y-%m-%d")


def parse_table_rows_to_dict(soup):
    data = {}
    tables = soup.select("table")

    for table in tables:
        for row in table.select("tr"):
            th = row.select_one("th")
            td = row.select_one("td")
            if not th or not td:
                continue

            key_raw = th.get_text(" ", strip=True)
            key = normalize_key(key_raw)
            val = td.get_text("\n", strip=True)

            if key == "Club Name":
                data["club_name"] = val
            elif key == "Contact Name":
                data["contact_name"] = val
            elif key == "Contact Email":
                data["contact_email"] = val
            elif key == "Event Address":
                data["event_address"] = val
            elif key == "Event Start Date":
                data["start_date"] = val
                try:
                    dt = dateparser.parse(val, fuzzy=True)
                    data["start_date_iso"] = dt.isoformat()
                    data["notification_date"] = dt.strftime("%Y-%m-%d")
                except Exception:
                    pass
            elif key == "Event End Date":
                data["end_date"] = val
                try:
                    data["end_date_iso"] = dateparser.parse(val, fuzzy=True).isoformat()
                except Exception:
                    pass
            elif key == "Event Start Time":
                data["start_time"] = val
            elif key == "Event End Time":
                data["end_time"] = val
            elif key == "Games Being Played":
                data["games"] = [line.strip() for line in val.split("\n") if line.strip()]
            elif key.startswith("Additional event information"):
                data["additional_event_information_for_participants"] = val
            elif key == "Link for pre-registration":
                a_tag = td.select_one("a[href]")
                data["prereg_url"] = a_tag.get("href") if a_tag else val

    return data


def parse_event_detail(session, url):
    soup = safe_get(session, url)
    if not soup:
        return {}

    detail = {}
    title_el = soup.select_one(".entry-title, h1.entry-title, h1")
    detail["title"] = title_el.get_text(strip=True) if title_el else None

    # Table data first (fallback)
    table_data = parse_table_rows_to_dict(soup)
    detail.update(table_data)

    # "When" from page meta is authoritative for actual event days
    when_text = _extract_when_text(soup)
    _apply_when_dates(detail, when_text)

    return detail


def scrape_listing_page(session, url):
    soup = safe_get(session, url, required=True)

    events = []
    cards = soup.select(".em-item-info") or soup.select("article, .event-card")

    for c in cards:
        a = c.select_one(".em-item-title a")
        if a and a.get("href"):
            events.append({
                "title": a.get_text(" ", strip=True),
                "url": urljoin(url, a.get("href"))
            })

    next_el = soup.select_one(".next.page-numbers, a.next, a[rel='next']")
    next_url = urljoin(url, next_el.get("href")) if next_el and next_el.get("href") else None

    return events, next_url


def crawl_all():
    session = requests.Session()
    url = START
    all_events = []
    seen = set()
    page_count = 0

    print("Starting crawl...")

    while url:
        page_count += 1
        print(f"\nListing page: {url}")
        listing_events, next_url = scrape_listing_page(session, url)
        print(f"Found {len(listing_events)} event cards on listing page {page_count}")

        for ev in listing_events:
            ev_url = ev.get("url")
            if not ev_url or ev_url in seen:
                continue

            seen.add(ev_url)
            time.sleep(DELAY)

            details = parse_event_detail(session, ev_url)

            record = {
                "id": make_persistent_event_id(details, ev_url),
                "title": ev.get("title"),
                "url": ev_url,
                "details": details
            }
            all_events.append(record)

        url = next_url

    if len(all_events) < MIN_EXPECTED_EVENTS:
        raise RuntimeError(
            f"Only scraped {len(all_events)} events across {page_count} listing pages. "
            f"Expected at least {MIN_EXPECTED_EVENTS}; refusing to overwrite {OUTPUT}."
        )

    payload = {
        "last_updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "count": len(all_events),
        "events": all_events
    }

    with open(OUTPUT, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print(f"\nDone. Saved {len(all_events)} events to {OUTPUT}")


if __name__ == "__main__":
    crawl_all()
