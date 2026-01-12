#!/usr/bin/env python3
"""
UpDog Challenge scraper (table-based, focused fields, limited events for testing).
Saves structured events to `data/events.json`.


Dependencies:
 pip install requests beautifulsoup4 python-dateutil tqdm
"""


import requests
from bs4 import BeautifulSoup
import time
import json
from urllib.parse import urljoin, urlparse
from dateutil import parser as dateparser
import hashlib
import os  # added to create data folder

# Ensure the data folder exists
os.makedirs("data", exist_ok=True)

BASE = "https://updogchallenge.com"
START = BASE + "/events/"
OUTPUT = "data/events.json"  # changed to save inside data folder


HEADERS = {
   "User-Agent": (
       "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
       "AppleWebKit/537.36 (KHTML, like Gecko) "
       "Chrome/121.0 Safari/537.36"
   ),
   "Accept-Language": "en-US,en;q=0.9",
}


DELAY = 1.0  # seconds between requests (polite)


def safe_get(session, url):
   try:
       resp = session.get(url, headers=HEADERS, timeout=30)
       resp.raise_for_status()
       return BeautifulSoup(resp.text, "html.parser")
   except Exception as e:
       print(f"[ERROR] GET {url} -> {e}")
       return None


def normalize_key(text: str) -> str:
   if not text:
       return ""
   text = text.replace("\xa0", " ")
   text = text.strip()
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
   # Extract slug from URL
   path = urlparse(event_url).path.rstrip("/")
   slug = path.split("/")[-1].lower()

   club = (details.get("club_name") or "").strip().lower()
   host = (details.get("contact_name") or "").strip().lower()

   raw = f"{slug}|{club}|{host}"
   return hashlib.sha256(raw.encode("utf-8")).hexdigest()


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
                   # NEW: add notification_date as YYYY-MM-DD
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

   table_data = parse_table_rows_to_dict(soup)
   detail.update(table_data)

   return detail


def scrape_listing_page(session, url):
   soup = safe_get(session, url)
   if not soup:
       return [], None

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

   print("Starting crawl...")

   while url:
       print(f"\nListing page: {url}")
       listing_events, next_url = scrape_listing_page(session, url)

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
