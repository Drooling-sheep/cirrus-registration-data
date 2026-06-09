#!/usr/bin/env python3
"""Build dashboard-ready JSON and CSV files from parsed FAA data."""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from calendar import monthrange
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PARSED_INPUT = PROJECT_ROOT / "data" / "cirrus_faa_parsed.json"
DEFAULT_GAMA_INPUT = PROJECT_ROOT / "data" / "gama_deliveries.csv"
DEFAULT_SERIAL_INPUT = PROJECT_ROOT / "data" / "serial_tracking.json"
DEFAULT_FLIGHT_INPUT = PROJECT_ROOT / "data" / "flight_activity.json"
DEFAULT_USED_MARKET_INPUT = PROJECT_ROOT / "data" / "used_market.json"
DEFAULT_JSON_OUTPUT = PROJECT_ROOT / "data" / "cirrus_registrations.json"
DEFAULT_CSV_OUTPUT = PROJECT_ROOT / "data" / "cirrus_registrations.csv"
DEFAULT_SNAPSHOT_OUTPUT = PROJECT_ROOT / "data" / "cirrus_aircraft_snapshot.json"
GAMA_FIELDS = ["year", "model_category", "units"]
SEASONALITY_HISTORY_YEARS = 5


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build final Cirrus registration dashboard data."
    )
    parser.add_argument("--parsed-input", type=Path, default=DEFAULT_PARSED_INPUT)
    parser.add_argument("--gama-input", type=Path, default=DEFAULT_GAMA_INPUT)
    parser.add_argument("--serial-input", type=Path, default=DEFAULT_SERIAL_INPUT)
    parser.add_argument("--flight-input", type=Path, default=DEFAULT_FLIGHT_INPUT)
    parser.add_argument("--used-market-input", type=Path, default=DEFAULT_USED_MARKET_INPUT)
    parser.add_argument("--json-output", type=Path, default=DEFAULT_JSON_OUTPUT)
    parser.add_argument("--csv-output", type=Path, default=DEFAULT_CSV_OUTPUT)
    parser.add_argument("--snapshot-output", type=Path, default=DEFAULT_SNAPSHOT_OUTPUT)
    return parser.parse_args()


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def ensure_gama_csv(path: Path) -> None:
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(GAMA_FIELDS)
    logging.warning("Created empty GAMA input template: %s", path)


def load_json(path: Path) -> Dict[str, object]:
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise RuntimeError(f"Expected JSON object in {path}")
    return payload


def load_gama_deliveries(path: Path) -> List[Dict[str, object]]:
    ensure_gama_csv(path)

    by_year: Dict[int, Dict[str, object]] = {}
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise RuntimeError(f"{path} has no header")

        normalized_fields = [field.strip() for field in reader.fieldnames]
        missing = [field for field in GAMA_FIELDS if field not in normalized_fields]
        if missing:
            raise RuntimeError(
                f"{path.name} is missing required fields: {', '.join(missing)}"
            )

        for line_number, row in enumerate(reader, start=2):
            year_raw = (row.get("year") or "").strip()
            period = (row.get("period") or "annual").strip().lower()
            category = (row.get("model_category") or "").strip().lower()
            units_raw = (row.get("units") or "").strip()
            if not year_raw and not category and not units_raw:
                continue
            if not year_raw.isdigit() or not units_raw.isdigit() or not category or not period:
                raise RuntimeError(f"Invalid GAMA row at line {line_number}: {row}")

            year = int(year_raw)
            units = int(units_raw)
            year_row = by_year.setdefault(year, {"year": year, "period": period})
            if year_row.get("period") != period:
                raise RuntimeError(
                    f"Mixed GAMA periods for {year}: {year_row.get('period')} and {period}"
                )
            year_row[category] = int(year_row.get(category, 0)) + units

    deliveries = [by_year[year] for year in sorted(by_year)]
    if not deliveries:
        logging.warning("GAMA deliveries CSV is empty; final JSON will use an empty list")
    return deliveries


def require_non_empty_list(payload: Dict[str, object], key: str) -> List[Dict[str, object]]:
    value = payload.get(key)
    if not isinstance(value, list) or not value:
        raise RuntimeError(f"Expected non-empty list at parsed payload key: {key}")
    return value


def counter_rows(records: List[Dict[str, object]], key: str) -> List[Dict[str, object]]:
    counts: Dict[str, int] = {}
    for record in records:
        value = str(record.get(key) or "")
        if not value:
            continue
        counts[value] = counts.get(value, 0) + 1
    return [
        {key: value, "count": count}
        for value, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    ]


def snapshot_diff(
    previous_snapshot: List[Dict[str, object]],
    current_snapshot: List[Dict[str, object]],
) -> Dict[str, object]:
    previous_has_hex = any(str(record.get("mode_s_code_hex") or "").strip() for record in previous_snapshot)
    current_has_hex = any(str(record.get("mode_s_code_hex") or "").strip() for record in current_snapshot)
    prefer_hex = previous_has_hex and current_has_hex

    def record_key(record: Dict[str, object]) -> str:
        mode_s_hex = str(record.get("mode_s_code_hex") or "").strip().upper()
        if prefer_hex and mode_s_hex:
            return f"hex:{mode_s_hex}"
        unique_id = str(record.get("unique_id") or "").strip()
        if unique_id:
            return f"uid:{unique_id}"
        if mode_s_hex:
            return f"hex:{mode_s_hex}"
        return str(record.get("key") or "")

    previous_by_key = {
        record_key(record): record
        for record in previous_snapshot
        if record_key(record)
    }
    current_by_key = {
        record_key(record): record
        for record in current_snapshot
        if record_key(record)
    }

    added_keys = sorted(set(current_by_key) - set(previous_by_key))
    removed_keys = sorted(set(previous_by_key) - set(current_by_key))
    common_keys = sorted(set(current_by_key) & set(previous_by_key))
    compare_fields = [
        "n_number",
        "serial_number",
        "mfr_mdl_code",
        "model",
        "year_mfr",
        "state",
        "cert_issue_date",
        "status_code",
        "estimated_new_aircraft",
        "estimated_new_aircraft_lagged",
    ]
    changed_keys = [
        key
        for key in common_keys
        if any(previous_by_key[key].get(field) != current_by_key[key].get(field) for field in compare_fields)
    ]

    added_records = [current_by_key[key] for key in added_keys]
    removed_records = [previous_by_key[key] for key in removed_keys]
    added_estimated_new = [
        record for record in added_records if bool(record.get("estimated_new_aircraft"))
    ]
    added_estimated_new_lagged = [
        record for record in added_records if bool(record.get("estimated_new_aircraft_lagged"))
    ]

    return {
        "previous_snapshot_available": bool(previous_snapshot),
        "previous_count": len(previous_snapshot),
        "current_count": len(current_snapshot),
        "added_count": len(added_records),
        "removed_count": len(removed_records),
        "changed_count": len(changed_keys),
        "added_estimated_new_aircraft_count": len(added_estimated_new),
        "added_estimated_new_aircraft_lagged_count": len(added_estimated_new_lagged),
        "added_by_model": counter_rows(added_records, "model"),
        "added_estimated_new_aircraft_by_model": counter_rows(added_estimated_new, "model"),
        "added_estimated_new_aircraft_lagged_by_model": counter_rows(
            added_estimated_new_lagged, "model"
        ),
        "removed_by_model": counter_rows(removed_records, "model"),
        "added_sample": added_records[:20],
        "removed_sample": removed_records[:20],
        "changed_sample": [
            {"before": previous_by_key[key], "after": current_by_key[key]}
            for key in changed_keys[:20]
        ],
    }


def load_previous_snapshot(path: Path) -> List[Dict[str, object]]:
    if not path.exists():
        return []
    payload = load_json(path)
    snapshot = payload.get("aircraft_snapshot", [])
    if not isinstance(snapshot, list):
        raise RuntimeError(f"Expected aircraft_snapshot list in {path}")
    return snapshot


def parse_cert_date(record: Dict[str, object]) -> Optional[date]:
    text = str(record.get("cert_issue_date") or "").strip()
    if len(text) == 8 and text.isdigit():
        try:
            return date(int(text[:4]), int(text[4:6]), int(text[6:8]))
        except ValueError:
            return None
    if len(text) >= 10:
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            return None
    return None


def same_month_day(year: int, month: int, day: int) -> date:
    return date(year, month, min(day, monthrange(year, month)[1]))


def lagged_records_with_dates(
    aircraft_snapshot: List[Dict[str, object]],
) -> List[tuple[Dict[str, object], date]]:
    records = []
    for record in aircraft_snapshot:
        if not record.get("estimated_new_aircraft_lagged"):
            continue
        cert_date = parse_cert_date(record)
        if cert_date is None:
            continue
        records.append((record, cert_date))
    return records


def count_ytd(records: List[tuple[Dict[str, object], date]], year: int, cutoff: date) -> int:
    return sum(1 for _, cert_date in records if cert_date.year == year and cert_date <= cutoff)


def count_year(records: List[tuple[Dict[str, object], date]], year: int) -> int:
    return sum(1 for _, cert_date in records if cert_date.year == year)


def total_gama_units(row: Dict[str, object]) -> int:
    return sum(
        int(value or 0)
        for key, value in row.items()
        if key not in {"year", "period"}
    )


def build_dashboard_metrics(
    aircraft_snapshot: List[Dict[str, object]],
    gama_deliveries: List[Dict[str, object]],
) -> Dict[str, object]:
    dated_records = lagged_records_with_dates(aircraft_snapshot)
    if not dated_records:
        return {
            "metric_note": "No dated lagged estimated new-aircraft records are available.",
        }

    as_of_date = max(cert_date for _, cert_date in dated_records)
    current_year = as_of_date.year
    previous_year = current_year - 1
    previous_cutoff = same_month_day(previous_year, as_of_date.month, as_of_date.day)
    current_ytd = count_ytd(dated_records, current_year, as_of_date)
    previous_ytd = count_ytd(dated_records, previous_year, previous_cutoff)
    yoy_pct = (
        round((current_ytd - previous_ytd) / previous_ytd * 100, 1)
        if previous_ytd
        else None
    )

    faa_q1 = sum(
        1
        for _, cert_date in dated_records
        if cert_date.year == current_year and 1 <= cert_date.month <= 3
    )
    gama_q1_row = next(
        (
            row
            for row in gama_deliveries
            if int(row.get("year", 0)) == current_year
            and str(row.get("period") or "").lower().startswith("q1")
        ),
        None,
    )
    gama_q1 = total_gama_units(gama_q1_row) if gama_q1_row else None
    q1_delta = faa_q1 - gama_q1 if gama_q1 is not None else None
    q1_delta_pct = round(q1_delta / gama_q1 * 100, 1) if gama_q1 else None

    complete_years = [
        year
        for year in sorted({cert_date.year for _, cert_date in dated_records})
        if year < current_year and count_year(dated_records, year) > 0
    ][-SEASONALITY_HISTORY_YEARS:]
    seasonal_rows = []
    for year in complete_years:
        cutoff = same_month_day(year, as_of_date.month, as_of_date.day)
        annual = count_year(dated_records, year)
        ytd = count_ytd(dated_records, year, cutoff)
        share = ytd / annual if annual else 0.0
        if share > 0:
            seasonal_rows.append(
                {
                    "year": year,
                    "cutoff_date": cutoff.isoformat(),
                    "ytd_count": ytd,
                    "annual_count": annual,
                    "ytd_share_pct": round(share * 100, 1),
                    "ytd_share": share,
                }
            )
    average_share = (
        sum(float(row["ytd_share"]) for row in seasonal_rows) / len(seasonal_rows)
        if seasonal_rows
        else None
    )
    projected_full_year = round(current_ytd / average_share) if average_share else None

    return {
        "metric_note": (
            "Default FAA metric is lagged estimated new-aircraft registrations: "
            "valid Cirrus records where YEAR MFR is the CERT ISSUE DATE year or prior year."
        ),
        "as_of_cert_issue_date": as_of_date.isoformat(),
        "ytd_same_date": {
            "current_year": current_year,
            "comparison_year": previous_year,
            "current_cutoff_date": as_of_date.isoformat(),
            "comparison_cutoff_date": previous_cutoff.isoformat(),
            "current_ytd": current_ytd,
            "comparison_ytd": previous_ytd,
            "delta": current_ytd - previous_ytd,
            "yoy_pct": yoy_pct,
        },
        "faa_q1_vs_gama": {
            "year": current_year,
            "faa_jan_mar_lagged_estimated_new": faa_q1,
            "gama_q1_units": gama_q1,
            "gama_period": gama_q1_row.get("period") if gama_q1_row else None,
            "delta": q1_delta,
            "delta_pct": q1_delta_pct,
        },
        "seasonality_projection": {
            "year": current_year,
            "current_ytd": current_ytd,
            "as_of_cert_issue_date": as_of_date.isoformat(),
            "history_years_used": [row["year"] for row in seasonal_rows],
            "average_ytd_share_pct": round(average_share * 100, 1) if average_share else None,
            "projected_full_year_faa_lagged_estimated_new": projected_full_year,
            "history": [
                {key: value for key, value in row.items() if key != "ytd_share"}
                for row in seasonal_rows
            ],
        },
    }


def build_final_payload(
    parsed: Dict[str, object],
    gama_deliveries: List[Dict[str, object]],
    previous_snapshot: List[Dict[str, object]],
    serial_tracking: Dict[str, object],
    flight_activity: Dict[str, object],
    used_market: Dict[str, object],
) -> Dict[str, object]:
    monthly_new = require_non_empty_list(parsed, "monthly_new")
    monthly_estimated_new_aircraft = require_non_empty_list(parsed, "monthly_estimated_new_aircraft")
    monthly_estimated_new_aircraft_lagged = require_non_empty_list(
        parsed, "monthly_estimated_new_aircraft_lagged"
    )
    monthly_estimated_new_by_model = require_non_empty_list(parsed, "monthly_estimated_new_by_model")
    monthly_estimated_new_lagged_by_model = require_non_empty_list(
        parsed, "monthly_estimated_new_lagged_by_model"
    )
    by_model = require_non_empty_list(parsed, "by_model")
    by_model_estimated_new_aircraft = require_non_empty_list(parsed, "by_model_estimated_new_aircraft")
    by_model_estimated_new_aircraft_lagged = require_non_empty_list(
        parsed, "by_model_estimated_new_aircraft_lagged"
    )
    by_year = require_non_empty_list(parsed, "by_year")
    by_state = require_non_empty_list(parsed, "by_state")
    aircraft_snapshot = require_non_empty_list(parsed, "aircraft_snapshot")

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_note": (
            "FAA Releasable Aircraft Database; data represents U.S. registrations "
            "and estimated new-aircraft registrations use valid Cirrus records where "
            "YEAR MFR equals the CERT ISSUE DATE year. The lagged estimate also allows "
            "the prior model year to absorb registration timing delays. These remain "
            "U.S. registration proxies, not official global Cirrus deliveries."
        ),
        "valid_registration_status_codes": parsed.get("valid_registration_status_codes", []),
        "monthly_new": monthly_new,
        "monthly_estimated_new_aircraft": monthly_estimated_new_aircraft,
        "monthly_estimated_new_aircraft_lagged": monthly_estimated_new_aircraft_lagged,
        "monthly_estimated_new_by_model": monthly_estimated_new_by_model,
        "monthly_estimated_new_lagged_by_model": monthly_estimated_new_lagged_by_model,
        "by_model": by_model,
        "by_model_estimated_new_aircraft": by_model_estimated_new_aircraft,
        "by_model_estimated_new_aircraft_lagged": by_model_estimated_new_aircraft_lagged,
        "by_year": by_year,
        "gama_deliveries": gama_deliveries,
        "by_state": by_state,
        "snapshot_diff": snapshot_diff(previous_snapshot, aircraft_snapshot),
        "dashboard_metrics": build_dashboard_metrics(aircraft_snapshot, gama_deliveries),
        "serial_tracking": serial_tracking,
        "flight_activity": flight_activity,
        "used_market": used_market,
        "metadata": {
            "faa_generated_at": parsed.get("generated_at"),
            "cirrus_total_all_statuses": parsed.get("cirrus_total_all_statuses"),
            "cirrus_total_valid_statuses": parsed.get("cirrus_total_valid_statuses"),
            "aircraft_snapshot_count": len(aircraft_snapshot),
            "skipped_counts": parsed.get("skipped_counts", {}),
            "source_files": parsed.get("source_files", {}),
        },
    }


def snapshot_payload(parsed: Dict[str, object]) -> Dict[str, object]:
    aircraft_snapshot = require_non_empty_list(parsed, "aircraft_snapshot")
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_note": "Compact valid Cirrus FAA registration snapshot for period-over-period diffs.",
        "aircraft_snapshot": aircraft_snapshot,
    }


def write_json(path: Path, payload: Dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
        handle.write("\n")


def write_csv(path: Path, payload: Dict[str, object]) -> None:
    rows: List[Dict[str, object]] = []

    for item in payload["monthly_new"]:
        rows.append(
            {
                "section": "monthly_new",
                "period": item["month"],
                "dimension": "",
                "count": item["count"],
                "year_built_matches": "",
            }
        )

    for item in payload["monthly_estimated_new_aircraft"]:
        rows.append(
            {
                "section": "monthly_estimated_new_aircraft",
                "period": item["month"],
                "dimension": "",
                "count": item["count"],
                "year_built_matches": item["count"],
            }
        )

    for item in payload["monthly_estimated_new_aircraft_lagged"]:
        rows.append(
            {
                "section": "monthly_estimated_new_aircraft_lagged",
                "period": item["month"],
                "dimension": "",
                "count": item["count"],
                "year_built_matches": "",
            }
        )

    for item in payload["monthly_estimated_new_by_model"]:
        rows.append(
            {
                "section": "monthly_estimated_new_by_model",
                "period": item["month"],
                "dimension": item["model"],
                "count": item["count"],
                "year_built_matches": item["count"],
            }
        )

    for item in payload["monthly_estimated_new_lagged_by_model"]:
        rows.append(
            {
                "section": "monthly_estimated_new_lagged_by_model",
                "period": item["month"],
                "dimension": item["model"],
                "count": item["count"],
                "year_built_matches": "",
            }
        )

    for item in payload["by_year"]:
        rows.append(
            {
                "section": "by_year",
                "period": item["year"],
                "dimension": "",
                "count": item["registrations"],
                "year_built_matches": item["year_built_matches"],
            }
        )

    for item in payload["by_model"]:
        rows.append(
            {
                "section": "by_model",
                "period": "",
                "dimension": item["model"],
                "count": item["count"],
                "year_built_matches": "",
            }
        )

    for item in payload["by_model_estimated_new_aircraft"]:
        rows.append(
            {
                "section": "by_model_estimated_new_aircraft",
                "period": "",
                "dimension": item["model"],
                "count": item["count"],
                "year_built_matches": item["count"],
            }
        )

    for item in payload["by_model_estimated_new_aircraft_lagged"]:
        rows.append(
            {
                "section": "by_model_estimated_new_aircraft_lagged",
                "period": "",
                "dimension": item["model"],
                "count": item["count"],
                "year_built_matches": "",
            }
        )

    for item in payload["by_state"]:
        rows.append(
            {
                "section": "by_state",
                "period": "",
                "dimension": item["state"],
                "count": item["count"],
                "year_built_matches": "",
            }
        )

    fieldnames = ["section", "period", "dimension", "count", "year_built_matches"]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def self_test(json_path: Path, payload: Dict[str, object]) -> None:
    loaded = load_json(json_path)
    for key in (
        "monthly_new",
        "monthly_estimated_new_aircraft",
        "monthly_estimated_new_aircraft_lagged",
        "monthly_estimated_new_by_model",
        "monthly_estimated_new_lagged_by_model",
        "by_model",
        "by_model_estimated_new_aircraft",
        "by_model_estimated_new_aircraft_lagged",
        "by_year",
        "by_state",
    ):
        require_non_empty_list(loaded, key)
    serial_tracking = loaded.get("serial_tracking")
    if not isinstance(serial_tracking, dict) or not serial_tracking.get("by_line"):
        raise RuntimeError("Expected serial_tracking.by_line in final JSON")
    flight_activity = loaded.get("flight_activity")
    if not isinstance(flight_activity, dict) or "fleet_hex_count" not in flight_activity:
        raise RuntimeError("Expected flight_activity.fleet_hex_count in final JSON")
    used_market = loaded.get("used_market")
    if not isinstance(used_market, dict) or "transfer_proxy" not in used_market:
        raise RuntimeError("Expected used_market.transfer_proxy in final JSON")
    dashboard_metrics = loaded.get("dashboard_metrics")
    if not isinstance(dashboard_metrics, dict) or "ytd_same_date" not in dashboard_metrics:
        raise RuntimeError("Expected dashboard_metrics.ytd_same_date in final JSON")

    monthly_total = sum(int(item["count"]) for item in payload["monthly_new"])
    yearly_total = sum(int(item["registrations"]) for item in payload["by_year"])
    if monthly_total != yearly_total:
        raise RuntimeError(
            f"Monthly total {monthly_total} does not match yearly total {yearly_total}"
        )

    logging.info("Self-test passed: monthly/yearly FAA totals both equal %s", monthly_total)
    estimated_monthly_total = sum(
        int(item["count"]) for item in payload["monthly_estimated_new_aircraft"]
    )
    estimated_yearly_total = sum(
        int(item["estimated_new_aircraft"]) for item in payload["by_year"]
    )
    if estimated_monthly_total != estimated_yearly_total:
        raise RuntimeError(
            "Estimated monthly total "
            f"{estimated_monthly_total} does not match estimated yearly total "
            f"{estimated_yearly_total}"
        )
    logging.info(
        "Self-test passed: estimated new-aircraft monthly/yearly totals both equal %s",
        estimated_monthly_total,
    )
    estimated_lagged_monthly_total = sum(
        int(item["count"]) for item in payload["monthly_estimated_new_aircraft_lagged"]
    )
    estimated_lagged_yearly_total = sum(
        int(item["estimated_new_aircraft_lagged"]) for item in payload["by_year"]
    )
    if estimated_lagged_monthly_total != estimated_lagged_yearly_total:
        raise RuntimeError(
            "Lagged estimated monthly total "
            f"{estimated_lagged_monthly_total} does not match lagged estimated yearly total "
            f"{estimated_lagged_yearly_total}"
        )
    logging.info(
        "Self-test passed: lagged estimated new-aircraft monthly/yearly totals both equal %s",
        estimated_lagged_monthly_total,
    )


def print_summary(payload: Dict[str, object]) -> None:
    monthly_total = sum(int(item["count"]) for item in payload["monthly_new"])
    estimated_monthly_total = sum(
        int(item["count"]) for item in payload["monthly_estimated_new_aircraft"]
    )
    estimated_lagged_monthly_total = sum(
        int(item["count"]) for item in payload["monthly_estimated_new_aircraft_lagged"]
    )
    by_year = payload["by_year"]
    latest_year = by_year[-1]
    print(f"Final monthly registration total: {monthly_total}")
    print(f"Estimated new-aircraft registration total: {estimated_monthly_total}")
    print(f"Lagged estimated new-aircraft registration total: {estimated_lagged_monthly_total}")
    print(
        "Latest year: "
        f"{latest_year['year']} registrations={latest_year['registrations']} "
        f"estimated_new_aircraft={latest_year['estimated_new_aircraft']} "
        f"lagged={latest_year['estimated_new_aircraft_lagged']}"
    )
    diff = payload["snapshot_diff"]
    print(
        "Snapshot diff: "
        f"added={diff['added_count']} removed={diff['removed_count']} "
        f"changed={diff['changed_count']}"
    )
    print(f"GAMA delivery years loaded: {len(payload['gama_deliveries'])}")
    serial_tracking = payload["serial_tracking"]
    by_line = serial_tracking.get("by_line", {})
    if isinstance(by_line, dict):
        for line in ("SR", "SF50"):
            metrics = by_line.get(line, {})
            if isinstance(metrics, dict):
                print(
                    f"Serial {line}: max={metrics.get('max_serial_us')} "
                    f"count={metrics.get('count_registered')} "
                    f"rate/mo={metrics.get('build_rate_est_per_month')}"
                )
    flight_activity = payload["flight_activity"]
    print(
        "Flight activity: "
        f"fleet_hex_count={flight_activity.get('fleet_hex_count')} "
        f"seen={flight_activity.get('distinct_aircraft_seen')}"
    )
    used_market = payload["used_market"]
    latest_transfer = used_market["transfer_proxy"]["by_month_snapshot"][-1]
    print(
        "Used market: "
        f"transfers={latest_transfer.get('transfers')} "
        f"listings={used_market['listings'].get('active_listing_count')}"
    )
    metrics = payload["dashboard_metrics"]
    ytd = metrics["ytd_same_date"]
    q1 = metrics["faa_q1_vs_gama"]
    projection = metrics["seasonality_projection"]
    print(
        "Same-date YTD: "
        f"{ytd['current_year']}={ytd['current_ytd']} "
        f"{ytd['comparison_year']}={ytd['comparison_ytd']} "
        f"yoy={ytd['yoy_pct']}%"
    )
    print(
        "FAA Q1 vs GAMA Q1: "
        f"faa={q1['faa_jan_mar_lagged_estimated_new']} "
        f"gama={q1['gama_q1_units']} "
        f"delta={q1['delta']}"
    )
    print(
        "Seasonality projection: "
        f"{projection['year']}={projection['projected_full_year_faa_lagged_estimated_new']} "
        f"avg_share={projection['average_ytd_share_pct']}%"
    )


def main() -> int:
    configure_logging()
    args = parse_args()

    try:
        parsed = load_json(args.parsed_input.resolve())
        gama_deliveries = load_gama_deliveries(args.gama_input.resolve())
        previous_snapshot = load_previous_snapshot(args.snapshot_output.resolve())
        serial_tracking = load_json(args.serial_input.resolve())
        flight_activity = load_json(args.flight_input.resolve())
        used_market = load_json(args.used_market_input.resolve())
        payload = build_final_payload(
            parsed,
            gama_deliveries,
            previous_snapshot,
            serial_tracking,
            flight_activity,
            used_market,
        )
        write_json(args.json_output.resolve(), payload)
        write_csv(args.csv_output.resolve(), payload)
        write_json(args.snapshot_output.resolve(), snapshot_payload(parsed))
        self_test(args.json_output.resolve(), payload)
        logging.info("Wrote dashboard JSON: %s", args.json_output.resolve())
        logging.info("Wrote audit CSV: %s", args.csv_output.resolve())
        logging.info("Wrote aircraft snapshot: %s", args.snapshot_output.resolve())
        print_summary(payload)
    except Exception:
        logging.exception("Final data build step failed")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
