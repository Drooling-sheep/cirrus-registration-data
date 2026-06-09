#!/usr/bin/env python3
"""Parse FAA aircraft registry files and aggregate Cirrus registrations."""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RAW_DIR = PROJECT_ROOT / "data" / "raw"
DEFAULT_OUTPUT = PROJECT_ROOT / "data" / "cirrus_faa_parsed.json"

ACFTREF_REQUIRED_FIELDS = {"CODE", "MFR", "MODEL"}
MASTER_REQUIRED_FIELDS = {
    "N-NUMBER",
    "SERIAL NUMBER",
    "MFR MDL CODE",
    "YEAR MFR",
    "STATE",
    "CERT ISSUE DATE",
    "STATUS CODE",
    "TYPE AIRCRAFT",
    "UNIQUE ID",
    "NAME",
    "MODE S CODE HEX",
}

# Defined by ardata.pdf in the FAA archive:
# T - Valid Registration from a Trainee; V - Valid Registration.
VALID_REGISTRATION_STATUS_CODES = {"T", "V"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Parse FAA MASTER/ACFTREF files and summarize Cirrus registrations."
    )
    parser.add_argument(
        "--raw-dir",
        type=Path,
        default=DEFAULT_RAW_DIR,
        help="Directory containing MASTER.txt, ACFTREF.txt, and ardata.pdf",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="Intermediate JSON output path",
    )
    parser.add_argument(
        "--sample-size",
        type=int,
        default=8,
        help="Number of matched records to include for manual field alignment checks",
    )
    return parser.parse_args()


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def clean_header(value: str) -> str:
    return value.replace("\ufeff", "").strip()


def clean_value(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()


def dict_reader(path: Path) -> Iterable[Dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise RuntimeError(f"{path} has no CSV header")

        fieldnames = [clean_header(name) for name in reader.fieldnames]
        reader.fieldnames = fieldnames
        for row in reader:
            yield {
                clean_header(key): clean_value(value)
                for key, value in row.items()
                if key is not None
            }


def read_header(path: Path) -> List[str]:
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        reader = csv.reader(handle)
        try:
            return [clean_header(value) for value in next(reader)]
        except StopIteration as exc:
            raise RuntimeError(f"{path} is empty") from exc


def require_fields(path: Path, required_fields: set[str]) -> None:
    header = set(read_header(path))
    missing = sorted(required_fields - header)
    if missing:
        raise RuntimeError(
            f"{path.name} is missing required fields: {', '.join(missing)}. "
            f"Actual fields: {', '.join(sorted(header))}"
        )


def parse_cert_date(value: str) -> Optional[datetime]:
    value = value.strip()
    if not value or len(value) != 8 or not value.isdigit():
        return None
    try:
        return datetime.strptime(value, "%Y%m%d")
    except ValueError:
        return None


def parse_year(value: str) -> Optional[int]:
    value = value.strip()
    if len(value) != 4 or not value.isdigit():
        return None
    return int(value)


def month_key(date_value: datetime) -> str:
    return date_value.strftime("%Y-%m")


def iter_months(start: str, end: str) -> Iterable[str]:
    year, month = [int(part) for part in start.split("-")]
    end_year, end_month = [int(part) for part in end.split("-")]
    while (year, month) <= (end_year, end_month):
        yield f"{year:04d}-{month:02d}"
        month += 1
        if month == 13:
            year += 1
            month = 1


def shift_month(month: str, offset: int) -> str:
    year, month_number = [int(part) for part in month.split("-")]
    month_index = year * 12 + (month_number - 1) + offset
    shifted_year, shifted_month_index = divmod(month_index, 12)
    return f"{shifted_year:04d}-{shifted_month_index + 1:02d}"


def discover_cirrus_models(acftref_path: Path) -> Dict[str, Dict[str, str]]:
    require_fields(acftref_path, ACFTREF_REQUIRED_FIELDS)
    models: Dict[str, Dict[str, str]] = {}
    for row in dict_reader(acftref_path):
        manufacturer = row["MFR"]
        code = row["CODE"]
        if "CIRRUS" not in manufacturer.upper() or not code:
            continue

        models[code] = {
            "code": code,
            "manufacturer": manufacturer,
            "model": row["MODEL"],
            "type_aircraft": row.get("TYPE-ACFT", ""),
            "type_engine": row.get("TYPE-ENG", ""),
            "no_seats": row.get("NO-SEATS", ""),
        }
    return models


def sample_record(row: Dict[str, str], model_info: Dict[str, str]) -> Dict[str, str]:
    return {
        "n_number": row["N-NUMBER"],
        "serial_number": row["SERIAL NUMBER"],
        "mfr_mdl_code": row["MFR MDL CODE"],
        "manufacturer": model_info["manufacturer"],
        "model": model_info["model"],
        "year_mfr": row["YEAR MFR"],
        "state": row["STATE"],
        "cert_issue_date": row["CERT ISSUE DATE"],
        "status_code": row["STATUS CODE"],
        "type_aircraft": row["TYPE AIRCRAFT"],
        "unique_id": row["UNIQUE ID"],
        "name": row["NAME"],
        "mode_s_code_hex": row["MODE S CODE HEX"],
    }


def snapshot_key(row: Dict[str, str]) -> str:
    mode_s_hex = row["MODE S CODE HEX"].strip().upper()
    if mode_s_hex:
        return f"hex:{mode_s_hex}"
    unique_id = row["UNIQUE ID"]
    if unique_id:
        return f"uid:{unique_id}"
    return f"nserial:{row['N-NUMBER']}:{row['SERIAL NUMBER']}"


def snapshot_record(
    row: Dict[str, str],
    model_info: Dict[str, str],
    cert_date: Optional[datetime],
) -> Dict[str, object]:
    cert_year = cert_date.year if cert_date else None
    year_mfr = parse_year(row["YEAR MFR"])
    estimated_new_aircraft = bool(cert_year and year_mfr == cert_year)
    estimated_new_aircraft_lagged = bool(
        cert_year and year_mfr is not None and cert_year - 1 <= year_mfr <= cert_year
    )
    return {
        "key": snapshot_key(row),
        "unique_id": row["UNIQUE ID"],
        "n_number": row["N-NUMBER"],
        "serial_number": row["SERIAL NUMBER"],
        "mfr_mdl_code": row["MFR MDL CODE"],
        "manufacturer": model_info["manufacturer"],
        "model": model_info["model"],
        "year_mfr": year_mfr,
        "state": row["STATE"],
        "cert_issue_date": row["CERT ISSUE DATE"],
        "cert_issue_month": month_key(cert_date) if cert_date else "",
        "status_code": row["STATUS CODE"],
        "unique_id": row["UNIQUE ID"],
        "name": row["NAME"],
        "mode_s_code_hex": row["MODE S CODE HEX"].strip().upper(),
        "estimated_new_aircraft": estimated_new_aircraft,
        "estimated_new_aircraft_lagged": estimated_new_aircraft_lagged,
    }


def summarize_master(
    master_path: Path,
    cirrus_models: Dict[str, Dict[str, str]],
    sample_size: int,
) -> Dict[str, object]:
    require_fields(master_path, MASTER_REQUIRED_FIELDS)

    monthly_counts: Counter[str] = Counter()
    monthly_estimated_new_counts: Counter[str] = Counter()
    monthly_estimated_new_lagged_counts: Counter[str] = Counter()
    monthly_estimated_new_by_model: Counter[tuple[str, str]] = Counter()
    monthly_estimated_new_lagged_by_model: Counter[tuple[str, str]] = Counter()
    model_counts: Counter[str] = Counter()
    estimated_new_by_model_counts: Counter[str] = Counter()
    estimated_new_lagged_by_model_counts: Counter[str] = Counter()
    state_counts: Counter[str] = Counter()
    yearly_counts: Counter[int] = Counter()
    yearly_built_matches: Counter[int] = Counter()
    yearly_lagged_matches: Counter[int] = Counter()
    status_counts: Counter[str] = Counter()
    skipped_counts: Counter[str] = Counter()
    samples: List[Dict[str, str]] = []
    aircraft_snapshot: List[Dict[str, object]] = []

    cirrus_total = 0
    valid_cirrus_total = 0

    for row in dict_reader(master_path):
        code = row["MFR MDL CODE"]
        model_info = cirrus_models.get(code)
        if not model_info:
            continue

        cirrus_total += 1
        status_code = row["STATUS CODE"]
        status_counts[status_code] += 1
        if status_code not in VALID_REGISTRATION_STATUS_CODES:
            skipped_counts["invalid_status"] += 1
            continue

        valid_cirrus_total += 1
        cert_date = parse_cert_date(row["CERT ISSUE DATE"])
        year_mfr = parse_year(row["YEAR MFR"])
        if cert_date is None:
            skipped_counts["missing_or_invalid_cert_issue_date"] += 1
        else:
            month = month_key(cert_date)
            monthly_counts[month] += 1
            yearly_counts[cert_date.year] += 1
            if year_mfr == cert_date.year:
                yearly_built_matches[cert_date.year] += 1
                monthly_estimated_new_counts[month] += 1
                monthly_estimated_new_by_model[(month, model_info["model"])] += 1
                estimated_new_by_model_counts[model_info["model"]] += 1
            if year_mfr is not None and cert_date.year - 1 <= year_mfr <= cert_date.year:
                yearly_lagged_matches[cert_date.year] += 1
                monthly_estimated_new_lagged_counts[month] += 1
                monthly_estimated_new_lagged_by_model[(month, model_info["model"])] += 1
                estimated_new_lagged_by_model_counts[model_info["model"]] += 1

        model_counts[model_info["model"]] += 1
        if row["STATE"]:
            state_counts[row["STATE"]] += 1

        aircraft_snapshot.append(snapshot_record(row, model_info, cert_date))

        if len(samples) < sample_size:
            samples.append(sample_record(row, model_info))

    monthly_new = [
        {"month": month, "count": monthly_counts[month]}
        for month in sorted(monthly_counts)
    ]
    by_year = [
        {
            "year": year,
            "registrations": yearly_counts[year],
            "estimated_new_aircraft": yearly_built_matches[year],
            "estimated_new_aircraft_lagged": yearly_lagged_matches[year],
            "year_built_matches": yearly_built_matches[year],
        }
        for year in sorted(yearly_counts)
    ]

    return {
        "cirrus_total_all_statuses": cirrus_total,
        "cirrus_total_valid_statuses": valid_cirrus_total,
        "monthly_new": monthly_new,
        "monthly_estimated_new_aircraft": [
            {"month": month, "count": monthly_estimated_new_counts[month]}
            for month in sorted(monthly_estimated_new_counts)
        ],
        "monthly_estimated_new_aircraft_lagged": [
            {"month": month, "count": monthly_estimated_new_lagged_counts[month]}
            for month in sorted(monthly_estimated_new_lagged_counts)
        ],
        "monthly_estimated_new_by_model": [
            {"month": month, "model": model, "count": count}
            for (month, model), count in sorted(monthly_estimated_new_by_model.items())
        ],
        "monthly_estimated_new_lagged_by_model": [
            {"month": month, "model": model, "count": count}
            for (month, model), count in sorted(monthly_estimated_new_lagged_by_model.items())
        ],
        "by_model": [
            {"model": model, "count": count}
            for model, count in model_counts.most_common()
        ],
        "by_model_estimated_new_aircraft": [
            {"model": model, "count": count}
            for model, count in estimated_new_by_model_counts.most_common()
        ],
        "by_model_estimated_new_aircraft_lagged": [
            {"model": model, "count": count}
            for model, count in estimated_new_lagged_by_model_counts.most_common()
        ],
        "by_year": by_year,
        "by_state": [
            {"state": state, "count": count}
            for state, count in state_counts.most_common()
        ],
        "status_counts_for_cirrus": dict(sorted(status_counts.items())),
        "skipped_counts": dict(sorted(skipped_counts.items())),
        "sample_records": samples,
        "aircraft_snapshot": sorted(aircraft_snapshot, key=lambda item: str(item["key"])),
    }


def recent_months(monthly_new: List[Dict[str, object]], count: int = 24) -> List[Dict[str, object]]:
    if not monthly_new:
        return []
    monthly_lookup = {str(item["month"]): int(item["count"]) for item in monthly_new}
    end_month = str(monthly_new[-1]["month"])
    start_month = shift_month(end_month, -(count - 1))
    return [
        {"month": month, "count": monthly_lookup.get(month, 0)}
        for month in iter_months(start_month, end_month)
    ]


def build_summary(raw_dir: Path, sample_size: int) -> Dict[str, object]:
    master_path = raw_dir / "MASTER.txt"
    acftref_path = raw_dir / "ACFTREF.txt"
    layout_pdf_path = raw_dir / "ardata.pdf"

    for path in (master_path, acftref_path, layout_pdf_path):
        if not path.exists():
            raise FileNotFoundError(f"Required FAA source file not found: {path}")

    cirrus_models = discover_cirrus_models(acftref_path)
    if not cirrus_models:
        raise RuntimeError("No ACFTREF rows found with MFR containing CIRRUS")

    summary = summarize_master(master_path, cirrus_models, sample_size)
    monthly_new = summary["monthly_new"]
    assert isinstance(monthly_new, list)
    monthly_estimated_new = summary["monthly_estimated_new_aircraft"]
    assert isinstance(monthly_estimated_new, list)
    monthly_estimated_new_lagged = summary["monthly_estimated_new_aircraft_lagged"]
    assert isinstance(monthly_estimated_new_lagged, list)

    model_rows = sorted(
        cirrus_models.values(),
        key=lambda item: (item["manufacturer"], item["model"], item["code"]),
    )

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_note": (
            "FAA Releasable Aircraft Database. Parsed fields use the CSV headers "
            "from MASTER.txt/ACFTREF.txt and status meanings from ardata.pdf."
        ),
        "source_files": {
            "master": str(master_path),
            "acftref": str(acftref_path),
            "layout_pdf": str(layout_pdf_path),
        },
        "valid_registration_status_codes": sorted(VALID_REGISTRATION_STATUS_CODES),
        "cirrus_models": model_rows,
        "recent_24_months": recent_months(monthly_new),
        "recent_24_months_estimated_new_aircraft": recent_months(monthly_estimated_new),
        "recent_24_months_estimated_new_aircraft_lagged": recent_months(
            monthly_estimated_new_lagged
        ),
        **summary,
    }


def write_json(path: Path, payload: Dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
        handle.write("\n")


def print_summary(payload: Dict[str, object]) -> None:
    print(f"Cirrus aircraft reference rows: {len(payload['cirrus_models'])}")
    print(f"Cirrus registrations, all statuses: {payload['cirrus_total_all_statuses']}")
    print(f"Cirrus registrations, valid statuses: {payload['cirrus_total_valid_statuses']}")
    print("Valid status codes:", ", ".join(payload["valid_registration_status_codes"]))

    by_model = payload["by_model"]
    assert isinstance(by_model, list)
    print("\nModels discovered in valid registrations:")
    for item in by_model:
        print(f"  {item['model']}: {item['count']}")

    recent = payload["recent_24_months"]
    assert isinstance(recent, list)
    print("\nRecent 24 months new registrations:")
    for item in recent:
        print(f"  {item['month']}: {item['count']}")

    recent_estimated = payload["recent_24_months_estimated_new_aircraft"]
    assert isinstance(recent_estimated, list)
    print("\nRecent 24 months estimated new-aircraft registrations:")
    for item in recent_estimated:
        print(f"  {item['month']}: {item['count']}")

    recent_estimated_lagged = payload["recent_24_months_estimated_new_aircraft_lagged"]
    assert isinstance(recent_estimated_lagged, list)
    print("\nRecent 24 months estimated new-aircraft registrations (current/prior model year):")
    for item in recent_estimated_lagged:
        print(f"  {item['month']}: {item['count']}")

    samples = payload["sample_records"]
    assert isinstance(samples, list)
    print("\nSample matched records for manual field alignment:")
    for item in samples[:5]:
        print(
            "  "
            f"N{item['n_number']} | {item['manufacturer']} {item['model']} | "
            f"code={item['mfr_mdl_code']} | cert={item['cert_issue_date']} | "
            f"year_mfr={item['year_mfr']} | status={item['status_code']} | "
            f"state={item['state']}"
        )


def main() -> int:
    configure_logging()
    args = parse_args()
    raw_dir = args.raw_dir.resolve()

    try:
        payload = build_summary(raw_dir, args.sample_size)
        write_json(args.output.resolve(), payload)
        logging.info("Wrote intermediate JSON: %s", args.output.resolve())
        print_summary(payload)
    except Exception:
        logging.exception("FAA parse step failed")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
