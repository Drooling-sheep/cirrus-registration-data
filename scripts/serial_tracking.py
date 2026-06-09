#!/usr/bin/env python3
"""Track Cirrus serial-number progress as a U.S.-visible production proxy."""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SNAPSHOT = PROJECT_ROOT / "data" / "cirrus_aircraft_snapshot.json"
DEFAULT_HISTORY = PROJECT_ROOT / "data" / "serial_history.json"
DEFAULT_OUTPUT = PROJECT_ROOT / "data" / "serial_tracking.json"

SERIAL_DIGITS = re.compile(r"\d+")
RECENT_SERIAL_COUNT = 20


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build Cirrus serial-number progress metrics from the FAA snapshot."
    )
    parser.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT)
    parser.add_argument("--history", type=Path, default=DEFAULT_HISTORY)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--as-of",
        default=date.today().isoformat(),
        help="Snapshot date to write into serial history, YYYY-MM-DD",
    )
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
        raise RuntimeError(f"Expected object in snapshot file: {path}")
    snapshot = payload.get("aircraft_snapshot")
    if not isinstance(snapshot, list) or not snapshot:
        raise RuntimeError(f"Expected non-empty aircraft_snapshot list in {path}")
    return snapshot


def load_history(path: Path) -> List[Dict[str, object]]:
    if not path.exists():
        return []
    payload = load_json(path)
    if not isinstance(payload, list):
        raise RuntimeError(f"Expected list in serial history: {path}")
    return payload


def classify_model_line(model: object) -> str:
    normalized = str(model or "").strip().upper()
    if normalized == "SF50":
        return "SF50"
    if normalized.startswith("SR20") or normalized.startswith("SR22"):
        return "SR"
    return "other"


def parse_serial_number(raw_serial: object) -> Optional[int]:
    serial = str(raw_serial or "").strip().upper()
    if not serial:
        return None

    matches = SERIAL_DIGITS.findall(serial)
    if not matches:
        return None

    # FAA Cirrus serials are currently numeric strings with optional leading
    # zeroes. If a prefix/suffix appears, use the longest digit group as the
    # comparable serial body and record parse failures separately.
    body = max(matches, key=len)
    return int(body.lstrip("0") or "0")


def parse_history_date(value: object) -> date:
    return datetime.strptime(str(value), "%Y-%m-%d").date()


def months_between(start: date, end: date) -> float:
    days = (end - start).days
    if days <= 0:
        return 0.0
    return days / 30.4375


def monthly_deltas(history_rows: List[Dict[str, object]]) -> List[Dict[str, object]]:
    rows = sorted(history_rows, key=lambda item: str(item["date"]))
    deltas: List[Dict[str, object]] = []
    for previous, current in zip(rows, rows[1:]):
        previous_date = parse_history_date(previous["date"])
        current_date = parse_history_date(current["date"])
        months = months_between(previous_date, current_date)
        if months <= 0:
            continue
        delta = int(current["max_serial"]) - int(previous["max_serial"])
        deltas.append(
            {
                "date": current_date.isoformat(),
                "delta_serial": delta,
                "months_elapsed": round(months, 3),
                "rate_per_month": round(delta / months, 2),
            }
        )
    return deltas


def trailing_rate(deltas: List[Dict[str, object]], window: int = 3) -> float:
    if not deltas:
        return 0.0
    selected = deltas[-window:]
    total_delta = sum(float(item["delta_serial"]) for item in selected)
    total_months = sum(float(item["months_elapsed"]) for item in selected)
    if total_months <= 0:
        return 0.0
    return round(total_delta / total_months, 2)


def update_history(
    history: List[Dict[str, object]],
    as_of: str,
    current_metrics: Dict[str, Dict[str, object]],
) -> List[Dict[str, object]]:
    preserved = [
        row
        for row in history
        if not (str(row.get("date")) == as_of and str(row.get("line")) in current_metrics)
    ]
    for line, metrics in current_metrics.items():
        if line == "other":
            continue
        preserved.append(
            {
                "date": as_of,
                "line": line,
                "max_serial": metrics["max_serial_us"],
                "count": metrics["count_registered"],
            }
        )
    return sorted(preserved, key=lambda row: (str(row["date"]), str(row["line"])))


def history_for_line(history: List[Dict[str, object]], line: str) -> List[Dict[str, object]]:
    return [
        {
            "date": str(row["date"]),
            "max_serial": int(row["max_serial"]),
            "count": int(row.get("count", 0)),
        }
        for row in history
        if str(row.get("line")) == line
    ]


def summarize_line(line: str, records: List[Dict[str, object]]) -> Dict[str, object]:
    parsed_rows = []
    parse_failures = 0
    serial_samples: List[Dict[str, object]] = []
    by_year: Dict[int, Dict[str, int]] = defaultdict(lambda: {"max_serial": 0, "count": 0})

    for record in records:
        raw_serial = record.get("serial_number", "")
        serial = parse_serial_number(raw_serial)
        if serial is None:
            parse_failures += 1
            continue

        parsed = {
            "serial": serial,
            "raw_serial": str(raw_serial),
            "model": str(record.get("model") or ""),
            "n_number": str(record.get("n_number") or ""),
            "year_mfr": record.get("year_mfr"),
            "cert_issue_date": str(record.get("cert_issue_date") or ""),
            "state": str(record.get("state") or ""),
        }
        parsed_rows.append(parsed)
        if len(serial_samples) < 12:
            serial_samples.append(parsed)

        year_mfr = record.get("year_mfr")
        if isinstance(year_mfr, int):
            by_year[year_mfr]["count"] += 1
            by_year[year_mfr]["max_serial"] = max(by_year[year_mfr]["max_serial"], serial)

    recent_serials = sorted(parsed_rows, key=lambda item: item["serial"], reverse=True)[
        :RECENT_SERIAL_COUNT
    ]
    serials_by_year = [
        {"year": year, **values}
        for year, values in sorted(by_year.items())
        if values["count"] > 0
    ]

    max_serial = recent_serials[0]["serial"] if recent_serials else 0
    return {
        "line": line,
        "max_serial_us": max_serial,
        "count_registered": len(records),
        "parsed_serial_count": len(parsed_rows),
        "parse_failures": parse_failures,
        "raw_serial_samples": serial_samples,
        "recent_serials": recent_serials,
        "serials_by_year": serials_by_year,
        "max_serial_history": [],
        "monthly_serial_deltas": [],
        "build_rate_est_per_month": 0.0,
    }


def build_serial_tracking(
    snapshot: List[Dict[str, object]],
    history: List[Dict[str, object]],
    as_of: str,
) -> tuple[Dict[str, object], List[Dict[str, object]]]:
    try:
        datetime.strptime(as_of, "%Y-%m-%d")
    except ValueError as exc:
        raise RuntimeError("--as-of must be YYYY-MM-DD") from exc

    models = sorted({str(record.get("model") or "") for record in snapshot})
    by_line_records: Dict[str, List[Dict[str, object]]] = defaultdict(list)
    unclassified_models = Counter()
    for record in snapshot:
        line = classify_model_line(record.get("model"))
        by_line_records[line].append(record)
        if line == "other":
            unclassified_models[str(record.get("model") or "")] += 1

    by_line = {
        line: summarize_line(line, records)
        for line, records in sorted(by_line_records.items())
    }
    updated_history = update_history(history, as_of, by_line)

    for line, metrics in by_line.items():
        line_history = history_for_line(updated_history, line)
        deltas = monthly_deltas(line_history)
        metrics["max_serial_history"] = line_history
        metrics["monthly_serial_deltas"] = deltas
        metrics["build_rate_est_per_month"] = trailing_rate(deltas)

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "note": (
            "基于 FAA 当前有效 Cirrus 注册记录中的序列号。该指标是美国可见的"
            "最大序列号进度，是全球真实产量的下限代理，非精确产量。"
        ),
        "serial_parse_rule": (
            "SERIAL NUMBER is parsed by extracting the longest digit group and "
            "removing leading zeroes; current Cirrus samples are mostly numeric strings."
        ),
        "model_line_rule": {
            "SR": "MODEL starts with SR20 or SR22",
            "SF50": "MODEL equals SF50",
            "other": "unclassified models kept separate for review",
        },
        "models_seen": models,
        "unclassified_models": dict(unclassified_models),
        "by_line": by_line,
    }
    return payload, updated_history


def sanity_check(payload: Dict[str, object]) -> None:
    by_line = payload["by_line"]
    assert isinstance(by_line, dict)

    sr = by_line.get("SR")
    sf50 = by_line.get("SF50")
    if not isinstance(sr, dict) or not isinstance(sf50, dict):
        raise RuntimeError("Expected SR and SF50 serial tracking lines")

    sr_max = int(sr["max_serial_us"])
    sf50_max = int(sf50["max_serial_us"])
    if not (9000 <= sr_max <= 12000):
        raise RuntimeError(f"SR max serial sanity check failed: {sr_max}")
    if not (600 <= sf50_max <= 1000):
        raise RuntimeError(f"SF50 max serial sanity check failed: {sf50_max}")

    for line, metrics in by_line.items():
        if not isinstance(metrics, dict):
            continue
        if int(metrics["parsed_serial_count"]) == 0:
            raise RuntimeError(f"No parsed serials for line {line}")
        if int(metrics["parse_failures"]) > 0:
            logging.warning("%s serial parse failures: %s", line, metrics["parse_failures"])


def print_summary(payload: Dict[str, object]) -> None:
    print("Serial tracking summary")
    print("Models seen:", ", ".join(payload["models_seen"]))
    if payload["unclassified_models"]:
        print("Unclassified models:", payload["unclassified_models"])
    by_line = payload["by_line"]
    assert isinstance(by_line, dict)
    for line, metrics in by_line.items():
        if not isinstance(metrics, dict):
            continue
        print(
            f"{line}: max_serial_us={metrics['max_serial_us']} "
            f"count={metrics['count_registered']} "
            f"parsed={metrics['parsed_serial_count']} "
            f"failures={metrics['parse_failures']} "
            f"rate/mo={metrics['build_rate_est_per_month']}"
        )
        print("  Recent serials:")
        for item in metrics["recent_serials"][:5]:
            print(
                "   "
                f"{item['serial']} raw={item['raw_serial']} "
                f"model={item['model']} N{item['n_number']} "
                f"year={item['year_mfr']}"
            )


def main() -> int:
    configure_logging()
    args = parse_args()

    try:
        snapshot = load_snapshot(args.snapshot.resolve())
        history = load_history(args.history.resolve())
        payload, updated_history = build_serial_tracking(snapshot, history, args.as_of)
        sanity_check(payload)
        write_json(args.history.resolve(), updated_history)
        write_json(args.output.resolve(), payload)
        logging.info("Wrote serial history: %s", args.history.resolve())
        logging.info("Wrote serial tracking output: %s", args.output.resolve())
        print_summary(payload)
    except Exception:
        logging.exception("Serial tracking step failed")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
