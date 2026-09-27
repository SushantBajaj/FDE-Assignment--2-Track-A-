from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pandas as pd
import requests

from .ingest import fetch_dispatch, preserve_sources
from .transform import (build_canonical_orders, intervention_outcome_summary,
                        late_rate_definitions, stage_timing_summary)
from .validation import validate

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "outputs"


def _load_sources():
    raw = ROOT / "data" / "raw"
    con = sqlite3.connect(raw / "sqlite" / "flasheats.db")
    try:
        tables = {name: pd.read_sql_query(f'SELECT * FROM "{name}"', con)
                  for name in ("orders", "customers", "drivers", "restaurants")}
    finally:
        con.close()
    for name in ("support_tickets", "restaurant_status", "order_events", "customer_interactions",
                 "customer_app_actions", "order_interventions", "order_outcomes"):
        tables[name] = pd.read_csv(raw / "csv" / f"{name}.csv", keep_default_na=True)
    tables["driver_events"] = json.loads((raw / "json" / "driver_events.json").read_text())
    return tables


def _api_records():
    base_url = "http://127.0.0.1:8000"
    owns_process = False
    process = None
    try:
        requests.get(f"{base_url}/health", timeout=0.3).raise_for_status()
    except requests.RequestException:
        process = subprocess.Popen([sys.executable, str(ROOT / "api" / "mock_dispatch_api.py")],
                                   cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        owns_process = True
        for _ in range(40):
            try:
                requests.get(f"{base_url}/health", timeout=0.3).raise_for_status()
                break
            except requests.RequestException:
                if process.poll() is not None:
                    raise RuntimeError("Mock Dispatch API exited before becoming healthy")
                time.sleep(0.1)
        else:
            process.terminate()
            raise RuntimeError("Mock Dispatch API did not become healthy")
    try:
        records, manifest = fetch_dispatch(base_url, OUT / "raw" / "api", page_size=100)
    finally:
        if owns_process and process is not None:
            process.terminate()
            process.wait(timeout=5)
    return records, manifest


def run() -> dict:
    OUT.mkdir(parents=True, exist_ok=True)
    preserve_sources(ROOT)
    t = _load_sources()
    dispatch, manifest = _api_records()
    orders = t["orders"]
    checks = validate(orders, t["customers"], t["restaurants"], t["drivers"],
                      t["support_tickets"], t["restaurant_status"], dispatch, manifest,
                      t["order_outcomes"], t["order_events"], t["customer_interactions"],
                      t["customer_app_actions"], t["order_interventions"])
    if manifest["status"] != "PASS":
        raise RuntimeError("Dispatch ingestion did not pass completeness verification")

    # A source duplicate is not silently counted twice: first source occurrence is canonical.
    canonical = build_canonical_orders(orders)
    eligible = canonical.metric_eligible

    support_linked = t["customer_interactions"].groupby("order_id").size().add(
        t["support_tickets"].loc[t["support_tickets"].order_id.ne("")].groupby("order_id").size(), fill_value=0)
    app_actions = t["customer_app_actions"].groupby("order_id").size()
    interventions = t["order_interventions"].groupby("order_id").size()
    canonical["support_interaction_count"] = canonical.order_id.map(support_linked).fillna(0).astype(int)
    canonical["app_action_count"] = canonical.order_id.map(app_actions).fillna(0).astype(int)
    canonical["intervention_count"] = canonical.order_id.map(interventions).fillna(0).astype(int)

    dispatch_frame = pd.DataFrame(dispatch).rename(
        columns={column: f"dispatch_{column}" for column in pd.DataFrame(dispatch).columns if column != "order_id"})
    canonical = canonical.merge(dispatch_frame, on="order_id", how="left", validate="one_to_one")

    n = int(eligible.sum())
    late_n = int(canonical.loc[eligible, "late_flag"].sum())
    late_rows = canonical.loc[eligible & canonical.late_flag.eq(1)]
    total_orders = len(canonical)
    late_rate = late_n / n if n else None
    support_rate = int(canonical.support_interaction_count.gt(0).sum()) / total_orders
    intervention_rate = int(canonical.intervention_count.gt(0).sum()) / total_orders
    rate_sensitivity = late_rate_definitions(canonical)
    stage_timing = stage_timing_summary(canonical)
    end_to_end = stage_timing.loc[(stage_timing.stage == "order creation → delivery") &
                                  (stage_timing.cohort == "All eligible")].iloc[0]
    intervention_outcomes = intervention_outcome_summary(canonical, t["order_interventions"])
    metrics = [
        rate_sensitivity[0],
        {"metric": "Median Delay Among Late Eligible Deliveries", "value": float(late_rows.delay_min.median()),
         "numerator": None, "denominator": len(late_rows), "unclassifiable": 0, "unit": "minutes",
         "definition": "Median actual minus promised minutes among metric-eligible deliveries with delay > 0."},
        {"metric": "Median Order-to-Delivery Time", "value": end_to_end.median_minutes,
         "numerator": None, "denominator": int(end_to_end.eligible_orders), "unclassifiable": 0,
         "unit": "minutes", "definition": "Median observed order creation to delivery duration; stage and late/on-time splits are in stage_timing.csv."},
        {"metric": "Support Interaction Rate", "value": support_rate,
         "numerator": int(canonical.support_interaction_count.gt(0).sum()), "denominator": total_orders, "unclassifiable": 0,
         "unit": "rate", "definition": "Canonical orders with at least one linked support ticket or customer interaction / all canonical orders; unlinked tickets excluded."},
        {"metric": "Intervention Rate", "value": intervention_rate,
         "numerator": int(canonical.intervention_count.gt(0).sum()), "denominator": total_orders, "unclassifiable": 0,
         "unit": "rate", "definition": "Canonical orders with one or more recorded interventions / all canonical orders."},
    ]
    pd.DataFrame(metrics).to_csv(OUT / "metrics.csv", index=False)
    pd.DataFrame(rate_sensitivity).to_csv(OUT / "kpi_definition_sensitivity.csv", index=False)
    stage_timing.to_csv(OUT / "stage_timing.csv", index=False)
    intervention_outcomes.to_csv(OUT / "intervention_outcomes.csv", index=False)
    canonical.to_csv(OUT / "canonical_orders.csv", index=False)
    driver_event_rows = []
    for driver in t["driver_events"]:
        for sequence, event in enumerate(driver.get("events", []), start=1):
            driver_event_rows.append({
                "event_id": f"driver:{driver['driver_id']}:{sequence:04d}",
                "order_id": event.get("order_id"), "event_type": event.get("type"),
                "event_time": event.get("timestamp"), "actor_type": "driver",
                "actor_id": driver["driver_id"], "source_system": "driver_events.json",
                "latitude": event.get("lat"), "longitude": event.get("lon"),
            })
    source_events = t["order_events"].copy()
    source_event_rows = source_events.to_dict(orient="records")
    for event in source_event_rows:
        event["latitude"] = None
        event["longitude"] = None
    lifecycle_events = pd.DataFrame(source_event_rows + driver_event_rows)
    lifecycle_events.to_csv(OUT / "canonical_lifecycle_events.csv", index=False)
    t["customer_interactions"].to_csv(OUT / "canonical_customer_interactions.csv", index=False)
    t["customer_app_actions"].to_csv(OUT / "canonical_customer_app_actions.csv", index=False)
    t["order_interventions"].to_csv(OUT / "canonical_interventions.csv", index=False)
    derived_outcomes = canonical[["order_id", "final_status_norm", "delivered_flag", "metric_eligible", "late_flag", "delay_min"]]
    source_outcomes = t["order_outcomes"].rename(columns={
        "final_status_norm": "source_final_status_norm", "delivered_flag": "source_delivered_flag",
        "late_flag": "source_late_flag", "delay_min": "source_delay_min",
        "outcome_bucket": "source_outcome_bucket"})
    derived_outcomes.merge(source_outcomes, on="order_id", how="outer", validate="one_to_one").to_csv(
        OUT / "canonical_outcomes.csv", index=False)
    pd.DataFrame(dispatch).to_csv(OUT / "dispatch_orders.csv", index=False)

    # Compact descriptive comparison only; associations do not identify causes.
    dim = canonical.loc[eligible, ["traffic_bucket", "weather_bucket", "distance_km_estimate", "late_flag", "delay_min"]].copy()
    dim["distance_band"] = pd.cut(dim.distance_km_estimate, bins=[-float("inf"), 5, 10, 15, float("inf")],
                                  labels=["<=5 km", "5-10 km", "10-15 km", ">15 km"])
    comparisons = []
    for column in ("traffic_bucket", "weather_bucket", "distance_band"):
        for key, group in dim.groupby(column, dropna=False, observed=True):
            comparisons.append({"dimension": column, "value": str(key), "eligible_orders": len(group),
                                "late_orders": int(group.late_flag.sum()),
                                "late_rate": float(group.late_flag.mean()),
                                "median_delay_min": float(group.delay_min.median())})
    for label, mask in (("any_linked_support_interaction", canonical.support_interaction_count.gt(0)),
                        ("no_linked_support_interaction", canonical.support_interaction_count.eq(0)),
                        ("any_intervention", canonical.intervention_count.gt(0)),
                        ("no_intervention", canonical.intervention_count.eq(0))):
        group = canonical.loc[eligible & mask]
        comparisons.append({"dimension": "workflow_signal", "value": label, "eligible_orders": len(group),
                            "late_orders": int(group.late_flag.sum()),
                            "late_rate": float(group.late_flag.mean()) if len(group) else None,
                            "median_delay_min": float(group.delay_min.median()) if len(group) else None})
    pd.DataFrame(comparisons).to_csv(OUT / "operational_comparisons.csv", index=False)

    historical = next(item for item in rate_sensitivity if item["metric"] == "Historical-style Late Delivery Rate")
    checks.append({"check": "late_kpi_publishability", "status": "WARN",
        "observed": {"primary_rate_reproduced": late_rate, "historical_style_rate": historical["value"],
                     "historical_denominator": historical["denominator"],
                     "historical_unclassifiable": historical["unclassifiable"],
                     "canonical_owner": "undocumented", "publication": "not approved"},
        "expected": "Business owner confirms canonical definition before publication",
        "affected_records": 1,
        "action": "The 56% claim is reproducible; keep publication at WARN/UNKNOWN until the business owner confirms the KPI definition."})

    validation = {"overall_status": "FAIL" if any(x["status"] == "FAIL" for x in checks) else
                  "WARN" if any(x["status"] in ("WARN", "UNKNOWN") for x in checks) else "PASS",
                  "checks": checks}
    (OUT / "validation_report.json").write_text(json.dumps(validation, indent=2, default=str) + "\n")
    (OUT / "ingestion_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    summary = {"eligible": n, "late": late_n, "late_rate": late_rate,
               "median_late_delay_min": float(late_rows.delay_min.median()),
               "support_rate": support_rate, "intervention_rate": intervention_rate,
               "validation_status": validation["overall_status"], "api_retries": manifest["retry_count"]}
    (OUT / "run_summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))
    return summary


if __name__ == "__main__":
    run()
