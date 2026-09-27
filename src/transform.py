from __future__ import annotations

import pandas as pd


def build_canonical_orders(orders: pd.DataFrame) -> pd.DataFrame:
    """Deduplicate by business key and derive status, timing and KPI eligibility."""
    canonical = orders.drop_duplicates("order_id", keep="first").copy()
    canonical["final_status_norm"] = canonical.final_status.astype("string").str.strip().str.lower()
    promised = pd.to_datetime(canonical.promised_eta, format="mixed", errors="coerce")
    actual = pd.to_datetime(canonical.actual_delivery_at, format="mixed", errors="coerce")
    canonical["delay_min"] = (actual - promised).dt.total_seconds() / 60
    canonical["delivered_flag"] = canonical.final_status_norm.eq("delivered")
    canonical["metric_eligible"] = canonical.delivered_flag & promised.notna() & actual.notna()
    canonical["late_flag"] = pd.Series(pd.NA, index=canonical.index, dtype="Int64")
    eligible = canonical.metric_eligible
    canonical.loc[eligible, "late_flag"] = canonical.loc[eligible, "delay_min"].gt(0).astype(int)
    return canonical


def late_rate_definitions(canonical: pd.DataFrame) -> list[dict]:
    """Calculate the three Class 6 definitions from canonical outcomes."""
    eligible = canonical.metric_eligible
    delay = canonical.loc[eligible, "delay_min"]
    primary_late = delay.gt(0)
    over_ten = delay.gt(10)
    historical_population = canonical.delivered_flag & canonical.actual_delivery_at.notna()
    historical_classifiable = historical_population & canonical.promised_eta.notna()
    historical_late = canonical.loc[historical_classifiable, "delay_min"].gt(0)
    historical_denominator = int(historical_population.sum())
    return [
        {"metric": "Late Delivery Rate", "value": float(primary_late.mean()),
         "numerator": int(primary_late.sum()), "denominator": int(eligible.sum()),
         "unclassifiable": 0, "unit": "rate", "definition": "VP Operations: delay > 0 among delivered orders with promised ETA and actual delivery time."},
        {"metric": "Late Delivery Rate (>10 minutes; alternative)", "value": float(over_ten.mean()),
         "numerator": int(over_ten.sum()), "denominator": int(eligible.sum()),
         "unclassifiable": 0, "unit": "rate", "definition": "Support Lead: delay > 10 minutes among the same calculable delivered population."},
        {"metric": "Historical-style Late Delivery Rate",
         "value": float(historical_late.sum() / historical_denominator) if historical_denominator else None,
         "numerator": int(historical_late.sum()), "denominator": historical_denominator,
         "unclassifiable": int((historical_population & canonical.promised_eta.isna()).sum()),
         "unit": "rate", "definition": "Delivered orders with non-null actual delivery time. Rows without promised ETA remain in this denominator and are counted as unclassifiable."},
    ]


def stage_timing_summary(canonical: pd.DataFrame) -> pd.DataFrame:
    """Summarize observed stage durations within the canonical KPI population."""
    created = pd.to_datetime(canonical.created_at, format="mixed", errors="coerce")
    pickup = pd.to_datetime(canonical.pickup_at, format="mixed", errors="coerce")
    delivered = pd.to_datetime(canonical.actual_delivery_at, format="mixed", errors="coerce")
    base = canonical.metric_eligible
    definitions = (
        ("order creation → pickup", created, pickup),
        ("pickup → delivery", pickup, delivered),
        ("order creation → delivery", created, delivered),
    )
    rows = []
    for stage, start, end in definitions:
        duration = (end - start).dt.total_seconds() / 60
        valid = base & start.notna() & end.notna() & end.ge(start)
        for cohort, cohort_mask in (("All eligible", pd.Series(True, index=canonical.index)),
                                    ("Late", canonical.late_flag.eq(1)),
                                    ("On time", canonical.late_flag.eq(0))):
            values = duration[valid & cohort_mask].dropna()
            rows.append({
                "stage": stage, "cohort": cohort, "eligible_orders": len(values),
                "median_minutes": float(values.median()) if len(values) else None,
                "mean_minutes": float(values.mean()) if len(values) else None,
                "p90_minutes": float(values.quantile(0.9)) if len(values) else None,
                "population": "Canonical delivered orders with promised ETA and actual delivery; stage timestamps present and chronological.",
            })
    return pd.DataFrame(rows)


def intervention_outcome_summary(canonical: pd.DataFrame, interventions: pd.DataFrame) -> pd.DataFrame:
    """Compare unique eligible orders by intervention type; retain raw type counts."""
    per_type_order = interventions[["order_id", "intervention_type"]].drop_duplicates()
    outcomes = canonical[["order_id", "metric_eligible", "late_flag", "delay_min"]]
    joined = per_type_order.merge(outcomes, on="order_id", how="left", validate="many_to_one")
    joined = joined.loc[joined.metric_eligible.fillna(False)]
    raw_counts = interventions.groupby("intervention_type", dropna=False).size()
    rows = []
    for intervention_type, raw_count in raw_counts.items():
        group = joined.loc[joined.intervention_type.eq(intervention_type)]
        rows.append({
            "intervention_type": intervention_type,
            "eligible_orders": int(group.order_id.nunique()),
            "intervention_count": int(raw_count),
            "late_orders": int(group.late_flag.sum()),
            "late_rate": float(group.late_flag.mean()) if len(group) else None,
            "median_delay_minutes": float(group.delay_min.median()) if len(group) else None,
            "grain_note": "One row per distinct eligible order within type; an order may appear under multiple types.",
        })
    return pd.DataFrame(rows)

