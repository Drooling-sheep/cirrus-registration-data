#!/usr/bin/env python3
"""Sample ADS-B activity for the Cirrus fleet using FAA Mode S hex addresses."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SNAPSHOT = PROJECT_ROOT / "data" / "cirrus_faa_parsed.json"
DEFAULT_HISTORY = PROJECT_ROOT / "data" / "flight_activity_history.json"
DEFAULT_OUTPUT = PROJECT_ROOT / "data" / "flight_activity.json"

DEFAULT_PROVIDER = os.environ.get("ADSB_PROVIDER", "airplanes_live")
AIRPLANES_LIVE_BASE_URL = os.environ.get(
    "AIRPLANES_LIVE_BASE_URL", "https://api.airplanes.live/v2"
)
OPEN_SKY_BASE_URL = os.environ.get("OPENSKY_BASE_URL", "https://opensky-network.org/api")
DEFAULT_BATCH_SIZE = int(os.environ.get("ADSB_BATCH_SIZE", "1000"))
DEFAULT_MAX_BATCHES = int(os.environ.get("ADSB_MAX_BATCHES", "10"))
DEFAULT_SLEEP_SECONDS = float(os.environ.get("ADSB_SLEEP_SECONDS", "1.1"))
USER_AGENT = os.environ.get(
    "ADSB_USER_AGENT", "cirrus-registration-tracker/1.0 personal research"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sample Cirrus ADS-B activity.")
    parser.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT)
    parser.add_argument("--history", type=Path, default=DEFAULT_HISTORY)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--provider", default=DEFAULT_PROVIDER, choices=["airplanes_live", "opensky", "none"])
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--max-batches", type=int, default=DEFAULT_MAX_BATCHES)
    parser.add_argument("--sleep-seconds", type=float, default=DEFAULT_SLEEP_SECONDS)
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


def load_snapshot(path: Path) -> List[Dict[str, object]]:
    payload = load_json(path)
    if not isinstance(payload, dict):
        raise RuntimeError(f"Expected object in {path}")
    rows = payload.get("aircraft_snapshot")
    if not isinstance(rows, list) or not rows:
        raise RuntimeError(f"Expected non-empty aircraft_snapshot in {path}")
    return rows


def load_history(path: Path) -> List[Dict[str, object]]:
    if not path.exists():
        return []
    payload = load_json(path)
    if not isinstance(payload, list):
        raise RuntimeError(f"Expected list in {path}")
    return payload


def classify_line(model: object) -> str:
    model_value = str(model or "").upper()
    if model_value == "SF50":
        return "SF50"
    if model_value.startswith("SR20") or model_value.startswith("SR22"):
        return "SR"
    return "other"


def normalize_hex(value: object) -> str:
    text = str(value or "").strip().lower()
    if len(text) != 6:
        return ""
    if any(ch not in "0123456789abcdef" for ch in text):
        return ""
    return text


def fleet_from_snapshot(snapshot: List[Dict[str, object]]) -> Dict[str, Dict[str, object]]:
    fleet: Dict[str, Dict[str, object]] = {}
    for row in snapshot:
        hex_code = normalize_hex(row.get("mode_s_code_hex"))
        if not hex_code:
            continue
        fleet[hex_code] = {
            "hex": hex_code,
            "model": str(row.get("model") or ""),
            "line": classify_line(row.get("model")),
            "n_number": str(row.get("n_number") or ""),
            "serial_number": str(row.get("serial_number") or ""),
        }
    return fleet


def chunks(values: List[str], size: int) -> Iterable[List[str]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def fetch_json(url: str, timeout: int = 30) -> Dict[str, object]:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", errors="replace"))


def query_airplanes_live(hex_batch: List[str]) -> List[Dict[str, object]]:
    # The public guide documents /hex/[hex] with up to 1000 Mode S hex ids.
    url = f"{AIRPLANES_LIVE_BASE_URL.rstrip('/')}/hex/{','.join(hex_batch)}"
    payload = fetch_json(url)
    aircraft = payload.get("ac", [])
    if not isinstance(aircraft, list):
        return []
    return [item for item in aircraft if isinstance(item, dict)]


def query_opensky(hex_batch: List[str]) -> List[Dict[str, object]]:
    # OpenSky state vectors accept a comma-separated icao24 filter.
    url = f"{OPEN_SKY_BASE_URL.rstrip('/')}/states/all?icao24={','.join(hex_batch)}"
    payload = fetch_json(url)
    states = payload.get("states", [])
    if not isinstance(states, list):
        return []
    results = []
    for state in states:
        if not isinstance(state, list) or len(state) < 17:
            continue
        results.append(
            {
                "hex": str(state[0] or "").lower(),
                "callsign": str(state[1] or "").strip(),
                "on_ground": bool(state[8]),
                "last_contact": state[4],
                "source": "opensky",
            }
        )
    return results


def sample_activity(
    fleet: Dict[str, Dict[str, object]],
    provider: str,
    batch_size: int,
    max_batches: int,
    sleep_seconds: float,
) -> tuple[List[Dict[str, object]], List[str]]:
    if provider == "none":
        return [], ["provider set to none; no ADS-B API queried"]

    query_func = query_airplanes_live if provider == "airplanes_live" else query_opensky
    hexes = sorted(fleet)
    seen: Dict[str, Dict[str, object]] = {}
    errors: List[str] = []
    batches = list(chunks(hexes, batch_size))[:max_batches]

    for index, batch in enumerate(batches, start=1):
        try:
            logging.info("Querying ADS-B provider %s batch %s/%s", provider, index, len(batches))
            for aircraft in query_func(batch):
                hex_code = normalize_hex(aircraft.get("hex") or aircraft.get("icao24"))
                if hex_code in fleet:
                    seen[hex_code] = aircraft
        except (OSError, urllib.error.URLError, RuntimeError, json.JSONDecodeError) as exc:
            errors.append(str(exc))
            logging.warning("ADS-B query failed for batch %s: %s", index, exc)
        if index < len(batches):
            time.sleep(sleep_seconds)

    return list(seen.values()), errors


def update_history(history: List[Dict[str, object]], row: Dict[str, object]) -> List[Dict[str, object]]:
    preserved = [item for item in history if str(item.get("date")) != str(row["date"])]
    preserved.append(row)
    return sorted(preserved, key=lambda item: str(item["date"]))


def build_payload(
    fleet: Dict[str, Dict[str, object]],
    provider: str,
    as_of: str,
    active_aircraft: List[Dict[str, object]],
    errors: List[str],
    history: List[Dict[str, object]],
) -> tuple[Dict[str, object], List[Dict[str, object]]]:
    active_hexes = {
        normalize_hex(item.get("hex") or item.get("icao24"))
        for item in active_aircraft
    }
    active_hexes = {hex_code for hex_code in active_hexes if hex_code in fleet}
    line_counts = Counter(fleet[hex_code]["line"] for hex_code in active_hexes)
    fleet_line_counts = Counter(item["line"] for item in fleet.values())
    fleet_hex_count = len(fleet)
    distinct_seen = len(active_hexes)
    utilization = round((distinct_seen / fleet_hex_count * 100) if fleet_hex_count else 0.0, 3)

    period_row = {
        "date": as_of,
        "distinct_aircraft_seen": distinct_seen,
        "flights": 0,
        "utilization_pct": utilization,
        "source": provider,
    }
    updated_history = update_history(history, period_row)

    payload = {
        "note": (
            "ADS-B 覆盖有盲区，PIA/LADD 会屏蔽部分飞机；这是机队利用率代理，"
            "不是精确飞行统计。"
        ),
        "source": provider,
        "as_of": datetime.now(timezone.utc).isoformat(),
        "window": "current_state_sample",
        "fleet_hex_count": fleet_hex_count,
        "tracked_lines": dict(fleet_line_counts),
        "distinct_aircraft_seen": distinct_seen,
        "flights": 0,
        "utilization_pct": utilization,
        "by_period": updated_history,
        "by_line": [
            {
                "line": line,
                "fleet_hex_count": fleet_line_counts[line],
                "distinct_aircraft_seen": line_counts[line],
                "utilization_pct": round(
                    (line_counts[line] / fleet_line_counts[line] * 100)
                    if fleet_line_counts[line]
                    else 0.0,
                    3,
                ),
            }
            for line in sorted(fleet_line_counts)
        ],
        "active_sample": [
            {
                "hex": hex_code,
                "model": fleet[hex_code]["model"],
                "line": fleet[hex_code]["line"],
                "n_number": fleet[hex_code]["n_number"],
            }
            for hex_code in sorted(active_hexes)[:20]
        ],
        "errors": errors[:10],
    }
    return payload, updated_history


def sanity_check(payload: Dict[str, object]) -> None:
    fleet_hex_count = int(payload["fleet_hex_count"])
    if fleet_hex_count < 5000:
        raise RuntimeError(f"Fleet hex count is unexpectedly low: {fleet_hex_count}")
    distinct_seen = int(payload["distinct_aircraft_seen"])
    if distinct_seen > fleet_hex_count:
        raise RuntimeError("distinct_aircraft_seen cannot exceed fleet_hex_count")


def main() -> int:
    configure_logging()
    args = parse_args()
    try:
        snapshot = load_snapshot(args.snapshot.resolve())
        history = load_history(args.history.resolve())
        fleet = fleet_from_snapshot(snapshot)
        active_aircraft, errors = sample_activity(
            fleet,
            args.provider,
            max(1, args.batch_size),
            max(0, args.max_batches),
            max(0.0, args.sleep_seconds),
        )
        payload, updated_history = build_payload(
            fleet, args.provider, args.as_of, active_aircraft, errors, history
        )
        sanity_check(payload)
        write_json(args.history.resolve(), updated_history)
        write_json(args.output.resolve(), payload)
        logging.info("Wrote flight activity history: %s", args.history.resolve())
        logging.info("Wrote flight activity output: %s", args.output.resolve())
        print(
            "Flight activity: "
            f"fleet_hex_count={payload['fleet_hex_count']} "
            f"seen={payload['distinct_aircraft_seen']} "
            f"utilization={payload['utilization_pct']}%"
        )
        if payload["errors"]:
            print("ADS-B query warnings:", payload["errors"])
    except Exception:
        logging.exception("Flight activity step failed")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
