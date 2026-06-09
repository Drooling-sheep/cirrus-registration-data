#!/usr/bin/env python3
"""Fetch low-frequency Cirrus listings from ASO public listing pages."""

from __future__ import annotations

import argparse
import csv
import html
import logging
import re
import sys
import time
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = PROJECT_ROOT / "data" / "listings_aso.csv"
USER_AGENT = "cirrus-registration-tracker/1.0 personal research"
REQUEST_SLEEP_SECONDS = 2.0
ASO_SOURCES = [
    ("SR20", "https://www.aso.com/listings/AircraftListings.aspx?mg_id=292"),
    ("SR22", "https://www.aso.com/listings/AircraftListings.aspx?mg_id=293"),
    ("SR22T", "https://www.aso.com/listings/AircraftListings.aspx?act_id=1&mg_id=1122"),
    ("SF50", "https://www.aso.com/listings/AircraftListings.aspx?act_id=4&m_id=124"),
]
OUTPUT_FIELDS = [
    "source_site",
    "model",
    "year",
    "asking_price",
    "registration",
    "serial",
    "location",
    "listing_date",
    "days_on_market",
    "dealer",
    "url",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scrape ASO Cirrus listing summaries.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--sleep-seconds", type=float, default=REQUEST_SLEEP_SECONDS)
    return parser.parse_args()


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def fetch_text(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=45) as response:
        return response.read().decode("utf-8", errors="replace")


def visible_lines(markup: str) -> List[str]:
    text = re.sub(r"<script\b.*?</script>", " ", markup, flags=re.I | re.S)
    text = re.sub(r"<style\b.*?</style>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", "\n", text)
    text = html.unescape(text)
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    return [line for line in lines if line]


def result_urls(markup: str) -> List[str]:
    match = re.search(r"var\s+Results\s*=\s*\[([^\]]*)\]", markup)
    if not match:
        return []
    ids = [item.strip() for item in match.group(1).split(",") if item.strip().isdigit()]
    return [f"https://www.aso.com/listings/spec/ViewAd.aspx?id={listing_id}" for listing_id in ids]


def parse_price(line: str) -> str:
    if "inquire" in line.lower() or "call" in line.lower():
        return ""
    match = re.search(r"\$[\d,]+", line)
    return match.group(0) if match else ""


def model_bucket(title: str, fallback: str) -> str:
    upper = title.upper()
    if "SF50" in upper or "VISION" in upper:
        return "SF50"
    if "SR22T" in upper:
        return "SR22T"
    if "SR20" in upper:
        return "SR20"
    if "SR22" in upper:
        return "SR22"
    return fallback


def parse_source(model_hint: str, url: str) -> List[Dict[str, str]]:
    markup = fetch_text(url)
    lines = visible_lines(markup)
    urls = result_urls(markup)
    listings: List[Dict[str, str]] = []

    index = 0
    while index < len(lines):
        title = lines[index]
        if not re.match(r"^(19|20)\d{2}\s+Cirrus\b", title, flags=re.I):
            index += 1
            continue

        year_match = re.match(r"^((?:19|20)\d{2})\s+", title)
        year = year_match.group(1) if year_match else ""
        registration = ""
        serial = ""
        asking_price = ""
        location = ""
        dealer = ""
        cursor = index + 1

        if cursor < len(lines) and lines[cursor].startswith("Reg#:"):
            registration = lines[cursor].replace("Reg#:", "").strip()
            cursor += 1
        if cursor < len(lines) and lines[cursor].startswith("S/N:"):
            serial = lines[cursor].replace("S/N:", "").strip()
            cursor += 1
        if cursor < len(lines) and lines[cursor].startswith("$"):
            asking_price = parse_price(lines[cursor])
            cursor += 1
        if cursor < len(lines) and lines[cursor].startswith("TTAF:"):
            cursor += 1
        if cursor < len(lines) and lines[cursor] == "Loc:":
            cursor += 1
            if cursor < len(lines) and lines[cursor] != "Image":
                location = lines[cursor]
            cursor += 1
        if cursor < len(lines):
            dealer = lines[cursor]

        detail_url = urls[len(listings)] if len(listings) < len(urls) else url
        listings.append(
            {
                "source_site": "ASO",
                "model": model_bucket(title, model_hint),
                "year": year,
                "asking_price": asking_price,
                "registration": registration,
                "serial": serial,
                "location": location,
                "listing_date": "",
                "days_on_market": "",
                "dealer": dealer,
                "url": detail_url,
            }
        )
        index = max(cursor + 1, index + 1)

    return listings


def dedupe_key(row: Dict[str, str]) -> str:
    if row["registration"]:
        return f"n:{row['registration'].upper()}"
    if row["serial"]:
        return f"serial:{row['model'].upper()}:{row['serial'].upper()}"
    return "|".join([row["model"].upper(), row["year"], row["asking_price"], row["dealer"].upper()])


def write_csv(path: Path, rows: List[Dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=OUTPUT_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    configure_logging()
    args = parse_args()
    try:
        by_key: Dict[str, Dict[str, str]] = {}
        for source_index, (model_hint, url) in enumerate(ASO_SOURCES, start=1):
            logging.info("Fetching ASO listings %s/%s: %s", source_index, len(ASO_SOURCES), url)
            for row in parse_source(model_hint, url):
                by_key[dedupe_key(row)] = row
            if source_index < len(ASO_SOURCES):
                time.sleep(max(0.0, args.sleep_seconds))
        rows = sorted(by_key.values(), key=lambda row: (row["model"], row["year"], row["registration"]))
        write_csv(args.output.resolve(), rows)
        logging.info("Wrote ASO listings: %s", args.output.resolve())
        print(f"ASO listings fetched: {len(rows)}")
    except Exception:
        logging.exception("ASO scrape failed")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
