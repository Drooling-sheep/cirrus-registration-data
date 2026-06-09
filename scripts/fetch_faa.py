#!/usr/bin/env python3
"""Download and extract the FAA Releasable Aircraft Database."""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Optional


PROJECT_ROOT = Path(__file__).resolve().parents[1]

# FAA changes public paths occasionally. Keep the default here easy to edit, and
# allow deployments to override it without touching code.
DEFAULT_FAA_DOWNLOAD_URL = os.environ.get(
    "FAA_RELEASABLE_AIRCRAFT_URL",
    "https://registry.faa.gov/database/ReleasableAircraft.zip",
)
DEFAULT_RAW_DIR = Path(os.environ.get("FAA_RAW_DIR", PROJECT_ROOT / "data" / "raw"))
DEFAULT_TIMEOUT_SECONDS = int(os.environ.get("FAA_DOWNLOAD_TIMEOUT", "60"))
DEFAULT_RETRIES = int(os.environ.get("FAA_DOWNLOAD_RETRIES", "3"))
DEFAULT_MIN_DOWNLOAD_BYTES = int(os.environ.get("FAA_MIN_DOWNLOAD_BYTES", "1000000"))

ZIP_NAME = "ReleasableAircraft.zip"
REQUIRED_EXTRACTS = {"MASTER.txt", "ACFTREF.txt"}

USER_AGENT = "Mozilla/5.0"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download FAA ReleasableAircraft.zip and extract required files."
    )
    parser.add_argument("--url", default=DEFAULT_FAA_DOWNLOAD_URL, help="FAA zip URL")
    parser.add_argument(
        "--raw-dir",
        type=Path,
        default=DEFAULT_RAW_DIR,
        help="Directory for the downloaded zip and extracted files",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT_SECONDS,
        help="Download timeout in seconds",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=DEFAULT_RETRIES,
        help="Number of download attempts",
    )
    parser.add_argument(
        "--min-bytes",
        type=int,
        default=DEFAULT_MIN_DOWNLOAD_BYTES,
        help="Minimum acceptable zip size in bytes",
    )
    return parser.parse_args()


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def download_with_urllib(url: str, destination: Path, timeout: int) -> None:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/zip,application/octet-stream,*/*",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        status = getattr(response, "status", None)
        content_type = response.headers.get("Content-Type", "")
        if status and status >= 400:
            raise RuntimeError(f"HTTP {status} while downloading {url}")
        if "html" in content_type.lower():
            raise RuntimeError(f"Expected zip content but received Content-Type {content_type!r}")

        with destination.open("wb") as output:
            shutil.copyfileobj(response, output)


def download_with_curl(url: str, destination: Path, timeout: int) -> None:
    curl = shutil.which("curl")
    if not curl:
        raise RuntimeError("curl fallback is unavailable on this system")

    command = [
        curl,
        "--location",
        "--fail",
        "--show-error",
        "--silent",
        "--connect-timeout",
        str(timeout),
        "--user-agent",
        USER_AGENT,
        "--output",
        str(destination),
        url,
    ]
    subprocess.run(command, check=True)


def download_file(url: str, destination: Path, timeout: int, retries: int) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_destination = destination.with_suffix(destination.suffix + ".tmp")
    last_error: Optional[Exception] = None

    for attempt in range(1, retries + 1):
        try:
            if temp_destination.exists():
                temp_destination.unlink()

            logging.info("Downloading FAA aircraft database (attempt %s/%s)", attempt, retries)
            try:
                download_with_urllib(url, temp_destination, timeout)
            except urllib.error.HTTPError as exc:
                if exc.code != 403:
                    raise
                logging.warning("urllib received HTTP 403; retrying this attempt with curl")
                download_with_curl(url, temp_destination, timeout)

            temp_destination.replace(destination)
            logging.info("Downloaded %s (%s bytes)", destination, destination.stat().st_size)
            return
        except (
            OSError,
            RuntimeError,
            subprocess.CalledProcessError,
            urllib.error.URLError,
            urllib.error.HTTPError,
        ) as exc:
            last_error = exc
            logging.warning("Download attempt %s failed: %s", attempt, exc)
            if attempt < retries:
                time.sleep(min(2**attempt, 10))

    raise RuntimeError(f"Failed to download FAA database after {retries} attempts") from last_error


def validate_zip(zip_path: Path, min_bytes: int) -> None:
    if not zip_path.exists():
        raise FileNotFoundError(f"Downloaded zip not found: {zip_path}")

    size = zip_path.stat().st_size
    if size < min_bytes:
        raise RuntimeError(
            f"Downloaded zip is too small: {size} bytes; expected at least {min_bytes}"
        )

    try:
        with zipfile.ZipFile(zip_path) as archive:
            bad_member = archive.testzip()
            if bad_member:
                raise RuntimeError(f"Zip integrity check failed at member: {bad_member}")
    except zipfile.BadZipFile as exc:
        raise RuntimeError(f"Downloaded file is not a valid zip: {zip_path}") from exc

    logging.info("Zip integrity check passed")


def wanted_member_names(archive: zipfile.ZipFile) -> dict[str, str]:
    available: dict[str, str] = {}
    for member in archive.namelist():
        base_name = Path(member).name
        if not base_name or member.endswith("/"):
            continue
        available[base_name.upper()] = member

    wanted: dict[str, str] = {}
    missing = []
    for required in REQUIRED_EXTRACTS:
        member = available.get(required.upper())
        if member:
            wanted[required] = member
        else:
            missing.append(required)

    pdf_members = {
        Path(member).name: member
        for member in archive.namelist()
        if Path(member).suffix.lower() == ".pdf"
    }
    ardata_member = next(
        (member for name, member in pdf_members.items() if name.lower() == "ardata.pdf"),
        None,
    )
    if ardata_member:
        wanted[Path(ardata_member).name] = ardata_member
    else:
        wanted.update(pdf_members)

    if missing:
        raise RuntimeError(
            "Required FAA files missing from zip: "
            + ", ".join(missing)
            + ". Available files include: "
            + ", ".join(sorted(available)[:20])
        )
    if not any(name.lower().endswith(".pdf") for name in wanted):
        raise RuntimeError("No FAA layout PDF found in zip; expected ardata.pdf or another PDF")

    return wanted


def extract_required_files(zip_path: Path, raw_dir: Path) -> list[Path]:
    extracted: list[Path] = []
    with zipfile.ZipFile(zip_path) as archive:
        wanted = wanted_member_names(archive)
        for output_name, member in wanted.items():
            target = raw_dir / output_name
            logging.info("Extracting %s -> %s", member, target)
            with archive.open(member) as source, target.open("wb") as output:
                shutil.copyfileobj(source, output)
            extracted.append(target)
    return extracted


def count_non_empty_lines(path: Path) -> int:
    count = 0
    with path.open("r", encoding="latin-1", errors="replace", newline="") as handle:
        for line in handle:
            if line.strip():
                count += 1
    return count


def self_test(raw_dir: Path) -> None:
    master_path = raw_dir / "MASTER.txt"
    acftref_path = raw_dir / "ACFTREF.txt"
    pdf_paths = sorted(raw_dir.glob("*.pdf"))

    for path in (master_path, acftref_path):
        if not path.exists() or path.stat().st_size == 0:
            raise RuntimeError(f"Expected extracted file is missing or empty: {path}")

    if not pdf_paths:
        raise RuntimeError("Expected FAA layout PDF is missing")

    master_lines = count_non_empty_lines(master_path)
    if master_lines <= 0:
        raise RuntimeError("MASTER.txt has no non-empty rows")

    logging.info("Self-test passed: MASTER.txt has %s non-empty rows", master_lines)
    logging.info("Layout PDF(s): %s", ", ".join(path.name for path in pdf_paths))


def main() -> int:
    configure_logging()
    args = parse_args()
    raw_dir = args.raw_dir.resolve()
    zip_path = raw_dir / ZIP_NAME

    logging.info("FAA URL: %s", args.url)
    logging.info("Raw data directory: %s", raw_dir)

    try:
        download_file(args.url, zip_path, args.timeout, args.retries)
        validate_zip(zip_path, args.min_bytes)
        extracted = extract_required_files(zip_path, raw_dir)
        logging.info("Extracted %s file(s)", len(extracted))
        self_test(raw_dir)
    except Exception:
        logging.exception("FAA download step failed")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
