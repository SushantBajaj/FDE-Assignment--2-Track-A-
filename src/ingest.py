from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import requests


def preserve_sources(root: Path) -> None:
    """Fail clearly if any canonical raw source needed by the pipeline is absent."""
    raw = root / "data" / "raw"
    required = [raw / "sqlite" / "flasheats.db"]
    required.extend((raw / "csv" / name) for name in (
        "customer_app_actions.csv", "customer_interactions.csv", "order_events.csv",
        "order_interventions.csv", "order_outcomes.csv", "restaurant_status.csv",
        "restaurants.csv", "support_tickets.csv",
    ))
    required.extend((raw / "json" / name) for name in ("driver_events.json", "dispatch_data.json"))
    missing = [path.relative_to(root).as_posix() for path in required if not path.is_file()]
    if missing:
        raise FileNotFoundError("Required raw source file(s) missing: " + ", ".join(missing))


def fetch_dispatch(
    base_url: str,
    raw_dir: Path,
    *,
    page_size: int = 100,
    max_retries: int = 4,
    timeout: float = 5,
    session: requests.Session | Any | None = None,
    sleep=time.sleep,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Fetch every API page, persist each successful payload, and verify completeness."""
    client = session or requests.Session()
    raw_dir.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, Any]] = []
    pages: list[dict[str, Any]] = []
    retries = 0
    expected_total: int | None = None
    page = 1
    while True:
        response = None
        attempts = 0
        while attempts <= max_retries:
            attempts += 1
            response = client.get(
                f"{base_url.rstrip('/')}/dispatch/orders",
                params={"page": page, "page_size": page_size}, timeout=timeout,
            )
            if response.status_code == 200:
                break
            if response.status_code not in (429, 500, 502, 503, 504) or attempts > max_retries:
                response.raise_for_status()
                raise RuntimeError(f"Unexpected HTTP {response.status_code} on page {page}")
            retries += 1
            delay = response.headers.get("Retry-After")
            try:
                wait = min(float(delay), 10) if delay else min(0.25 * (2 ** (attempts - 1)), 4)
            except ValueError:
                wait = min(0.25 * (2 ** (attempts - 1)), 4)
            sleep(wait)
        assert response is not None
        payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise RuntimeError(f"Invalid Dispatch API response structure on page {page}")
        if payload.get("page") != page or payload.get("page_size") != page_size:
            raise RuntimeError(f"Dispatch API page metadata mismatch on page {page}")
        total = payload.get("total_records")
        if not isinstance(total, int):
            raise RuntimeError("Dispatch API omitted integer total_records")
        if expected_total is None:
            expected_total = total
        elif expected_total != total:
            raise RuntimeError("Dispatch API total_records changed during retrieval")
        page_path = raw_dir / f"page_{page:04d}.json"
        page_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        items = payload["data"]
        records.extend(items)
        pages.append({"page": page, "records": len(items), "http_status": response.status_code,
                      "attempts": attempts, "raw_file": page_path.name})
        has_more = payload.get("has_more")
        if not isinstance(has_more, bool):
            raise RuntimeError(f"Dispatch API omitted boolean has_more on page {page}")
        if has_more:
            if not items:
                raise RuntimeError(f"Dispatch API returned empty page {page} with has_more=true")
            page += 1
            continue
        break
    ids = [row.get("order_id") for row in records]
    duplicate_count = len(ids) - len(set(ids))
    if expected_total is None or len(records) != expected_total or duplicate_count:
        raise RuntimeError(
            f"Incomplete Dispatch retrieval: received={len(records)}, expected={expected_total}, "
            f"duplicate_order_ids={duplicate_count}"
        )
    manifest = {
        "status": "PASS", "endpoint": f"{base_url.rstrip('/')}/dispatch/orders",
        "page_size": page_size, "pages": pages, "page_count": len(pages),
        "records_received": len(records), "api_reported_total": expected_total,
        "unique_order_ids": len(set(ids)), "duplicate_order_ids": duplicate_count,
        "retry_count": retries, "complete": True,
    }
    return records, manifest
