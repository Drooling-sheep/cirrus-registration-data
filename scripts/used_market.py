#!/usr/bin/env python3
"""Build Cirrus used-market proxies from FAA snapshots and optional listings."""

from __future__ import annotations

import argparse
import csv
import json
import logging
import statistics
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PARSED = PROJECT_ROOT / "data" / "cirrus_faa_parsed.json"
DEFAULT_PREVIOUS_SNAPSHOT = PROJECT_ROOT / "data" / "cirrus_aircraft_snapshot.json"
DEFAULT_TRANSFER_HISTORY = PROJECT_ROOT / "data" / "transfer_history.json"
DEFAULT_LISTINGS_INPUT = PROJECT_ROOT / "data" / "listings_manual.csv"
DEFAULT_ASO_LISTINGS_INPUT = PROJECT_ROOT / "data" / "listings_aso.csv"
DEFAULT_LISTING_HISTORY = PROJECT_ROOT / "data" / "listing_history.json"
DEFAULT_OUTPUT = PROJECT_ROOT / "data" / "used_market.json"
LISTING_FIELDS = [
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
    parser = argparse.ArgumentParser(description="Build Cirrus used-market proxy data.")
    parser.add_argument("--parsed-input", type=Path, default=DEFAULT_PARSED)
    parser.add_argument("--previous-snapshot", type=Path, default=DEFAULT_PREVIOUS_SNAPSHOT)
    parser.add_argument("--transfer-history", type=Path, default=DEFAULT_TRANSFER_HISTORY)
    parser.add_argument("--listings-input", type=Path, default=DEFAULT_LISTINGS_INPUT)
    parser.add_argument("--aso-listings-input", type=Path, default=DEFAULT_ASO_LISTINGS_INPUT)
    parser.add_argument("--listing-history", type=Path, default=DEFAULT_LISTING_HISTORY)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--as-of", default=date.today().isoformat())
    return parser.parse_args()


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def load_json(path: Path) -> object:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
        handle.write("\n")


def load_history(path: Path) -> List[Dict[str, object]]:
    if not path.exists():
        return []
    payload = load_json(path)
    if not isinstance(payload, list):
        raise RuntimeError(f"Expected list in {path}")
    return payload


def load_current_snapshot(parsed_path: Path) -> List[Dict[str, object]]:
    payload = load_json(parsed_path)
    if not isinstance(payload, dict):
        raise RuntimeError(f"Expected object in {parsed_path}")
    snapshot = payload.get("aircraft_snapshot")
    if not isinstance(snapshot, list) or not snapshot:
        raise RuntimeError(f"Expected non-empty aircraft_snapshot in {parsed_path}")
    return snapshot


def load_previous_snapshot(path: Path) -> List[Dict[str, object]]:
    if not path.exists():
        return []
    payload = load_json(path)
    if not isinstance(payload, dict):
        raise RuntimeError(f"Expected object in {path}")
    snapshot = payload.get("aircraft_snapshot")
    if not isinstance(snapshot, list):
        return []
    comparable = any(
        str(record.get("mode_s_code_hex") or "").strip()
        or str(record.get("unique_id") or "").strip()
        or str(record.get("name") or "").strip()
        for record in snapshot[:20]
        if isinstance(record, dict)
    )
    if snapshot and not comparable:
        logging.warning(
            "Previous snapshot lacks transfer-comparison fields; treating it as unavailable"
        )
        return []
    return snapshot


def stable_key(record: Dict[str, object], prefer_hex: bool = True) -> str:
    mode_s_hex = str(record.get("mode_s_code_hex") or "").strip().upper()
    if prefer_hex and mode_s_hex:
        return f"hex:{mode_s_hex}"
    unique_id = str(record.get("unique_id") or "").strip()
    if unique_id:
        return f"uid:{unique_id}"
    if mode_s_hex:
        return f"hex:{mode_s_hex}"
    serial = str(record.get("serial_number") or "").strip()
    model = str(record.get("model") or "").strip()
    if serial or model:
        return f"serial:{model}:{serial}"
    return str(record.get("key") or "")


def cert_year(record: Dict[str, object]) -> Optional[int]:
    cert = str(record.get("cert_issue_date") or "")
    if len(cert) >= 4 and cert[:4].isdigit():
        return int(cert[:4])
    return None


def year_mfr(record: Dict[str, object]) -> Optional[int]:
    value = record.get("year_mfr")
    if isinstance(value, int):
        return value
    text = str(value or "")
    if len(text) == 4 and text.isdigit():
        return int(text)
    return None


def year_gap_proxy(snapshot: List[Dict[str, object]]) -> List[Dict[str, object]]:
    rows: Dict[int, Counter[str]] = defaultdict(Counter)
    for record in snapshot:
        cyear = cert_year(record)
        if cyear is None:
            continue
        y_mfr = year_mfr(record)
        if y_mfr is None:
            rows[cyear]["unknown_year_mfr"] += 1
        elif cyear - y_mfr >= 2:
            rows[cyear]["used_like_incl_renewals"] += 1
        else:
            rows[cyear]["new_like"] += 1
        rows[cyear]["registrations"] += 1

    return [
        {
            "cert_year": year,
            "registrations": counts["registrations"],
            "new_like": counts["new_like"],
            "used_like_incl_renewals": counts["used_like_incl_renewals"],
            "unknown_year_mfr": counts["unknown_year_mfr"],
        }
        for year, counts in sorted(rows.items())
    ]


def detect_transfer_events(
    previous_snapshot: List[Dict[str, object]],
    current_snapshot: List[Dict[str, object]],
    as_of: str,
) -> Dict[str, object]:
    previous_has_hex = any(str(record.get("mode_s_code_hex") or "").strip() for record in previous_snapshot)
    current_has_hex = any(str(record.get("mode_s_code_hex") or "").strip() for record in current_snapshot)
    prefer_hex = previous_has_hex and current_has_hex
    previous_by_key = {
        stable_key(record, prefer_hex): record
        for record in previous_snapshot
        if stable_key(record, prefer_hex)
    }
    current_by_key = {
        stable_key(record, prefer_hex): record
        for record in current_snapshot
        if stable_key(record, prefer_hex)
    }
    if not previous_by_key:
        return {
            "date": as_of,
            "transfers": 0,
            "new": 0,
            "deregistered": 0,
            "added": 0,
            "transfer_sample": [],
            "previous_snapshot_available": False,
        }
    added_keys = set(current_by_key) - set(previous_by_key)
    removed_keys = set(previous_by_key) - set(current_by_key)
    common_keys = set(previous_by_key) & set(current_by_key)

    transfer_events = []
    for key in sorted(common_keys):
        previous = previous_by_key[key]
        current = current_by_key[key]
        previous_name = str(previous.get("name") or "").strip()
        current_name = str(current.get("name") or "").strip()
        previous_cert = str(previous.get("cert_issue_date") or "")
        current_cert = str(current.get("cert_issue_date") or "")
        name_changed = bool(previous_name and current_name and previous_name != current_name)
        cert_changed = bool(previous_cert and current_cert and previous_cert != current_cert)
        if name_changed or cert_changed:
            transfer_events.append(
                {
                    "key": key,
                    "n_number": current.get("n_number"),
                    "model": current.get("model"),
                    "previous_name": previous_name,
                    "current_name": current_name,
                    "previous_cert_issue_date": previous_cert,
                    "current_cert_issue_date": current_cert,
                    "state": current.get("state"),
                }
            )

    new_like = 0
    for key in added_keys:
        record = current_by_key[key]
        cyear = cert_year(record)
        y_mfr = year_mfr(record)
        if cyear is not None and y_mfr is not None and cyear - y_mfr <= 1:
            new_like += 1

    return {
        "date": as_of,
        "transfers": len(transfer_events),
        "new": new_like,
        "deregistered": len(removed_keys),
        "added": len(added_keys),
        "transfer_sample": transfer_events[:20],
        "previous_snapshot_available": bool(previous_snapshot),
    }


def ensure_listing_template(path: Path) -> None:
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        csv.writer(handle).writerow(LISTING_FIELDS)


def parse_price(value: str) -> Optional[int]:
    text = (value or "").strip().lower()
    if not text or "call" in text:
        return None
    digits = "".join(ch for ch in text if ch.isdigit())
    if not digits:
        return None
    return int(digits)


def parse_int(value: str) -> Optional[int]:
    text = (value or "").strip()
    if not text:
        return None
    return int(text) if text.isdigit() else None


def percentile(values: List[int], pct: float) -> Optional[int]:
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    ordered = sorted(values)
    index = (len(ordered) - 1) * pct
    lower = int(index)
    upper = min(lower + 1, len(ordered) - 1)
    weight = index - lower
    return round(ordered[lower] * (1 - weight) + ordered[upper] * weight)


def dedupe_key(row: Dict[str, str]) -> str:
    registration = row.get("registration", "").strip().upper()
    serial = row.get("serial", "").strip().upper()
    if registration:
        return f"n:{registration}"
    if serial:
        return f"serial:{serial}"
    return "|".join(
        [
            row.get("model", "").strip().upper(),
            row.get("year", "").strip(),
            str(parse_price(row.get("asking_price", "")) or ""),
            row.get("location", "").strip().upper(),
        ]
    )


def load_listings(path: Path, create_template: bool = False) -> List[Dict[str, object]]:
    if create_template:
        ensure_listing_template(path)
    elif not path.exists():
        return []
    by_key: Dict[str, Dict[str, object]] = {}
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            return []
        for raw in reader:
            row = {key: (value or "").strip() for key, value in raw.items() if key}
            if not any(row.values()):
                continue
            key = dedupe_key(row)
            by_key[key] = {
                "key": key,
                "source_site": row.get("source_site", ""),
                "model": row.get("model", ""),
                "year": parse_int(row.get("year", "")),
                "asking_price": parse_price(row.get("asking_price", "")),
                "registration": row.get("registration", ""),
                "serial": row.get("serial", ""),
                "location": row.get("location", ""),
                "listing_date": row.get("listing_date", ""),
                "days_on_market": parse_int(row.get("days_on_market", "")),
                "dealer": row.get("dealer", ""),
                "url": row.get("url", ""),
            }
    return list(by_key.values())


def merge_listings(*listing_groups: List[Dict[str, object]]) -> List[Dict[str, object]]:
    by_key: Dict[str, Dict[str, object]] = {}
    for listings in listing_groups:
        for listing in listings:
            key = str(listing.get("key") or "")
            if key:
                by_key[key] = listing
    return sorted(
        by_key.values(),
        key=lambda item: (
            str(item.get("model") or ""),
            int(item.get("year") or 0),
            str(item.get("registration") or ""),
            str(item.get("serial") or ""),
        ),
    )


def listing_stats(listings: List[Dict[str, object]], as_of: str) -> Dict[str, object]:
    by_model: Dict[str, List[Dict[str, object]]] = defaultdict(list)
    for listing in listings:
        model = str(listing.get("model") or "UNKNOWN").upper()
        by_model[model].append(listing)

    rows = []
    for model, model_listings in sorted(by_model.items()):
        prices = [
            int(item["asking_price"])
            for item in model_listings
            if isinstance(item.get("asking_price"), int)
        ]
        days = [
            int(item["days_on_market"])
            for item in model_listings
            if isinstance(item.get("days_on_market"), int)
        ]
        rows.append(
            {
                "model": model,
                "active_listings": len(model_listings),
                "median_ask": percentile(prices, 0.5),
                "p25_ask": percentile(prices, 0.25),
                "p75_ask": percentile(prices, 0.75),
                "price_coverage_pct": round(
                    (len(prices) / len(model_listings) * 100) if model_listings else 0.0,
                    1,
                ),
                "median_days_on_market": percentile(days, 0.5),
            }
        )
    return {
        "as_of": datetime.now(timezone.utc).isoformat(),
        "sources": sorted({str(item.get("source_site") or "") for item in listings if item.get("source_site")}),
        "active_listing_count": len(listings),
        "by_model": rows,
        "sample": listings[:20],
        "input_note": (
            "Listings are loaded from data/listings_manual.csv and the low-frequency "
            "ASO public listing summary scraper after robots.txt review. Asking prices "
            "are not closing prices."
        ),
    }


def update_listing_history(
    history: List[Dict[str, object]],
    as_of: str,
    listings_payload: Dict[str, object],
) -> List[Dict[str, object]]:
    row = {
        "date": as_of,
        "active_listing_count": listings_payload["active_listing_count"],
        "by_model": listings_payload["by_model"],
    }
    preserved = [item for item in history if str(item.get("date")) != as_of]
    preserved.append(row)
    return sorted(preserved, key=lambda item: str(item["date"]))


def update_transfer_history(history: List[Dict[str, object]], row: Dict[str, object]) -> List[Dict[str, object]]:
    preserved = [item for item in history if str(item.get("date")) != str(row["date"])]
    preserved.append(row)
    return sorted(preserved, key=lambda item: str(item["date"]))


def build_payload(
    current_snapshot: List[Dict[str, object]],
    previous_snapshot: List[Dict[str, object]],
    transfer_history: List[Dict[str, object]],
    listing_history: List[Dict[str, object]],
    listings: List[Dict[str, object]],
    as_of: str,
) -> tuple[Dict[str, object], List[Dict[str, object]], List[Dict[str, object]]]:
    transfer_row = detect_transfer_events(previous_snapshot, current_snapshot, as_of)
    updated_transfer_history = update_transfer_history(transfer_history, transfer_row)
    listings_payload = listing_stats(listings, as_of)
    updated_listing_history = update_listing_history(listing_history, as_of, listings_payload)

    payload = {
        "note": "要价不等于成交价；过户量代理含续期；下架约等于成交只是弱代理。",
        "transfer_proxy": {
            "by_month_snapshot": updated_transfer_history,
            "by_year_yearmfr_gap": year_gap_proxy(current_snapshot),
        },
        "listings": {
            **listings_payload,
            "history": updated_listing_history,
        },
    }
    return payload, updated_transfer_history, updated_listing_history


def sanity_check(payload: Dict[str, object]) -> None:
    yearly = payload["transfer_proxy"]["by_year_yearmfr_gap"]
    for row in yearly:
        total = row["new_like"] + row["used_like_incl_renewals"] + row["unknown_year_mfr"]
        if total != row["registrations"]:
            raise RuntimeError(f"Used-market year gap totals do not add up: {row}")


def main() -> int:
    configure_logging()
    args = parse_args()
    try:
        current_snapshot = load_current_snapshot(args.parsed_input.resolve())
        previous_snapshot = load_previous_snapshot(args.previous_snapshot.resolve())
        transfer_history = load_history(args.transfer_history.resolve())
        listing_history = load_history(args.listing_history.resolve())
        manual_listings = load_listings(args.listings_input.resolve(), create_template=True)
        aso_listings = load_listings(args.aso_listings_input.resolve())
        listings = merge_listings(manual_listings, aso_listings)
        payload, updated_transfer_history, updated_listing_history = build_payload(
            current_snapshot,
            previous_snapshot,
            transfer_history,
            listing_history,
            listings,
            args.as_of,
        )
        sanity_check(payload)
        write_json(args.transfer_history.resolve(), updated_transfer_history)
        write_json(args.listing_history.resolve(), updated_listing_history)
        write_json(args.output.resolve(), payload)
        logging.info("Wrote transfer history: %s", args.transfer_history.resolve())
        logging.info("Wrote listing history: %s", args.listing_history.resolve())
        logging.info("Wrote used-market output: %s", args.output.resolve())
        latest = payload["transfer_proxy"]["by_month_snapshot"][-1]
        print(
            "Used market: "
            f"transfers={latest['transfers']} new={latest['new']} "
            f"deregistered={latest['deregistered']} listings={payload['listings']['active_listing_count']}"
        )
    except Exception:
        logging.exception("Used-market step failed")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
