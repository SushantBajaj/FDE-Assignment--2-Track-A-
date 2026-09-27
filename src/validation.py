from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

import pandas as pd


def check(name: str, status: str, observed: Any, expected: Any, affected: int, action: str) -> dict[str, Any]:
    return {"check": name, "status": status, "observed": observed, "expected": expected,
            "affected_records": int(affected), "action": action}


def _category_profile(series: pd.Series, expected: set[str]) -> dict[str, Any]:
    raw_values = series.fillna("<NULL>").astype(str)
    folded = raw_values.str.strip().str.casefold()
    groups: dict[str, set[str]] = defaultdict(set)
    for raw, normalized in zip(raw_values, folded):
        groups[normalized].add(raw)
    variants = {key: sorted(values) for key, values in groups.items() if len(values) > 1}
    tokenized = raw_values.map(lambda value: "_".join(value.strip().casefold().split()))
    spacing_candidates: dict[str, set[str]] = defaultdict(set)
    for raw, token in zip(raw_values, tokenized):
        if token in expected and raw.strip().casefold() not in expected:
            spacing_candidates[token].add(raw)
    candidate_folded = {raw.strip().casefold() for values in spacing_candidates.values() for raw in values}
    return {
        "observed_values": dict(Counter(raw_values)),
        "safe_case_whitespace_normalization": dict(Counter(folded)),
        "representation_variants": variants,
        "unexpected_normalized_values": sorted(set(folded) - expected),
        "spacing_to_underscore_candidates_pending_confirmation": {
            key: sorted(values) for key, values in spacing_candidates.items()},
        "unmapped_values_requiring_semantic_review": sorted((set(folded) - expected) - candidate_folded),
    }


def validate(orders: pd.DataFrame, customers: pd.DataFrame, restaurants: pd.DataFrame,
             drivers: pd.DataFrame, tickets: pd.DataFrame, restaurant_status: pd.DataFrame,
             dispatch: list[dict[str, Any]], manifest: dict[str, Any],
             outcomes: pd.DataFrame, order_events: pd.DataFrame,
             customer_interactions: pd.DataFrame, customer_app_actions: pd.DataFrame,
             interventions: pd.DataFrame) -> list[dict[str, Any]]:
    unique = orders.drop_duplicates("order_id", keep="first")
    duplicate_rows = len(orders) - orders.order_id.nunique()
    restaurant_unmatched = ~unique.restaurant_id.isin(restaurants.restaurant_id)
    driver_unmatched = ~unique.driver_id.isin(drivers.driver_id)
    traffic_expected = {"low", "medium", "high", "severe"}
    traffic_profile = _category_profile(orders.traffic_bucket, traffic_expected)
    traffic_affected = sum(max(0, len(values) - 1) for values in traffic_profile["representation_variants"].values())
    traffic_affected += int(orders.traffic_bucket.astype("string").str.strip().str.casefold().isin(
        traffic_profile["unexpected_normalized_values"]).sum())
    ticket_expected = {"late_delivery", "eta_changed", "restaurant_delay", "ready_but_waiting",
                       "status_mismatch", "driver_not_moving"}
    ticket_profile = _category_profile(tickets.category, ticket_expected)
    ticket_affected = sum(max(0, len(values) - 1) for values in ticket_profile["representation_variants"].values())
    ticket_affected += int(tickets.category.astype("string").str.strip().str.casefold().isin(
        ticket_profile["unexpected_normalized_values"]).sum())
    checks = [check("unique_order_grain", "WARN" if duplicate_rows else "PASS",
                    {"rows": len(orders), "unique_order_ids": int(orders.order_id.nunique())},
                    "one canonical row per order", duplicate_rows,
                    "Keep first source row for canonical analysis; retain source and flag duplicates."),
             check("delivered_completion_timestamp", "WARN",
                   int(((unique.final_status.str.lower() == "delivered") & unique.actual_delivery_at.isna()).sum()),
                   0, int(((unique.final_status.str.lower() == "delivered") & unique.actual_delivery_at.isna()).sum()),
                   "Exclude from timing metrics; investigate missing completion events."),
             check("promised_eta_present", "PASS" if unique.promised_eta.notna().all() else "WARN",
                   int(unique.promised_eta.notna().sum()), len(unique), int(unique.promised_eta.isna().sum()),
                   "Rows without ETA are ineligible for lateness metrics."),
             check("status_representation", "WARN" if orders.final_status.nunique() > 2 else "PASS",
                   dict(Counter(orders.final_status.fillna("<NULL>"))),
                   "Delivered/cancelled; casing may be normalized", int((orders.final_status != orders.final_status.str.lower()).sum()),
                   "Normalize case for analysis while retaining original values."),
             check("traffic_bucket_categories", "WARN" if traffic_affected else "PASS",
                   traffic_profile,
                   {"expected_after_safe_normalization": ["low", "medium", "high", "severe"]},
                   traffic_affected,
                   "Case and surrounding whitespace can be normalized safely; preserve raw values and confirm any unexpected traffic meaning."),
             check("support_ticket_categories", "WARN" if ticket_affected else "PASS",
                   ticket_profile,
                   "Known categories after case/outer-whitespace normalization",
                   ticket_affected,
                   "Case/outer whitespace are safe representation normalization. Keep raw labels; confirm space-to-underscore candidates and review wording such as 'ETA issue' before mapping."),
             check("order_restaurant_reference", "WARN" if restaurant_unmatched.any() else "PASS",
                   {"mapped": int(unique.restaurant_id.isin(restaurants.restaurant_id).sum()), "total": len(unique)}, len(unique),
                   int(restaurant_unmatched.sum()), "Retain rows with missing/unmapped restaurant IDs and investigate enrichment coverage."),
             check("order_customer_reference", "PASS" if unique.customer_id.isin(customers.customer_id).all() else "WARN",
                   {"mapped": int(unique.customer_id.isin(customers.customer_id).sum()), "total": len(unique)}, len(unique),
                   int((~unique.customer_id.isin(customers.customer_id)).sum()), "Investigate missing or unmapped customer identifiers."),
             check("order_driver_reference", "WARN" if driver_unmatched.any() else "PASS",
                   {"mapped": int(unique.driver_id.isin(drivers.driver_id).sum()), "total": len(unique)}, len(unique),
                   int(driver_unmatched.sum()), "Retain rows with missing/unmapped driver IDs and investigate enrichment coverage."),
             check("ticket_order_reference", "WARN" if tickets.order_id.isna().any() or (tickets.order_id == "").any() else "PASS",
                   {"linked": int(tickets.order_id.isin(unique.order_id).sum()), "total": len(tickets)}, len(tickets),
                   int((~tickets.order_id.isin(unique.order_id)).sum()), "Preserve unlinked tickets as customer signals; exclude from order joins."),
             check("restaurant_status_categories", "WARN",
                   dict(Counter(restaurant_status.status.fillna("<NULL>"))),
                   "Preparing, ready, handed_off; handoff/unknown remain ambiguous",
                   int((~restaurant_status.status.str.strip().str.lower().isin(["preparing", "ready", "handed_off"])).sum()),
                   "Do not map handoff to handed_off or unknown to a state without owner confirmation."),
             check("dispatch_api_completeness", manifest["status"],
                   {"received": manifest["records_received"], "reported_total": manifest["api_reported_total"],
                    "unique_ids": manifest["unique_order_ids"], "retries": manifest["retry_count"]},
                   "All API-reported rows, unique order IDs", manifest["duplicate_order_ids"],
                   "Fail pipeline if pagination is incomplete or duplicate API order IDs occur.")]

    source_ids, api_ids = set(unique.order_id), {row.get("order_id") for row in dispatch}
    coverage_missing, coverage_extra = source_ids - api_ids, api_ids - source_ids
    checks.append(check("dispatch_order_coverage", "PASS" if not coverage_missing and not coverage_extra else "FAIL",
                        {"source_orders_missing_from_api": len(coverage_missing),
                         "api_orders_missing_from_source": len(coverage_extra)},
                        "Exact order ID set match", len(coverage_missing) + len(coverage_extra),
                        "Reconcile order IDs across operational and dispatch sources."))

    outcome_compare = unique[["order_id", "final_status", "promised_eta", "actual_delivery_at"]].merge(
        outcomes, on="order_id", how="outer", indicator=True, suffixes=("", "_source"))
    eta = pd.to_datetime(outcome_compare.promised_eta, format="mixed", errors="coerce")
    actual = pd.to_datetime(outcome_compare.actual_delivery_at, format="mixed", errors="coerce")
    delay = (actual - eta).dt.total_seconds() / 60
    delivered = outcome_compare.final_status.astype("string").str.strip().str.lower().eq("delivered")
    expected_late = delay.gt(0).astype("Int64")
    source_late = pd.to_numeric(outcome_compare.late_flag, errors="coerce").astype("Int64")
    comparable = delivered & eta.notna() & actual.notna() & outcome_compare._merge.eq("both")
    status_mismatch = outcome_compare.final_status.astype("string").str.strip().str.lower().ne(
        outcome_compare.final_status_norm.astype("string").str.strip().str.lower())
    late_mismatch = comparable & expected_late.ne(source_late)
    delay_source = pd.to_numeric(outcome_compare.delay_min, errors="coerce")
    delay_mismatch = comparable & ((delay - delay_source).abs() > 0.011)
    outcome_issues = int((status_mismatch.fillna(True) | late_mismatch.fillna(True) |
                          delay_mismatch.fillna(True) | outcome_compare._merge.ne("both")).sum())
    checks.append(check("supplied_outcome_consistency", "PASS" if outcome_issues == 0 else "WARN",
                        {"source_outcome_rows": len(outcomes), "mismatched_or_unmatched": outcome_issues},
                        "Status and late flag agree; delay agrees within 0.011 minutes", outcome_issues,
                        "Use timestamp-derived canonical values and investigate source outcome discrepancies."))

    for check_name, frame in (("order_event_order_reference", order_events),
                              ("customer_interaction_order_reference", customer_interactions),
                              ("customer_action_order_reference", customer_app_actions),
                              ("intervention_order_reference", interventions)):
        missing = int((~frame.order_id.isin(unique.order_id)).sum())
        checks.append(check(check_name, "PASS" if missing == 0 else "WARN",
                            {"linked": len(frame) - missing, "total": len(frame)}, len(frame), missing,
                            "Investigate unlinked order events while preserving their source records."))

    status_order_missing = int((~restaurant_status.order_id.isin(unique.order_id)).sum())
    status_restaurant_missing = int((~restaurant_status.restaurant_id.isin(restaurants.restaurant_id)).sum())
    checks.extend([
        check("restaurant_status_order_reference", "PASS" if not status_order_missing else "WARN",
              {"linked": len(restaurant_status) - status_order_missing, "total": len(restaurant_status)},
              len(restaurant_status), status_order_missing, "Investigate status rows without a known order."),
        check("restaurant_status_restaurant_reference", "PASS" if not status_restaurant_missing else "WARN",
              {"linked": len(restaurant_status) - status_restaurant_missing, "total": len(restaurant_status)},
              len(restaurant_status), status_restaurant_missing, "Investigate status rows without a known restaurant."),
    ])
    freshness = restaurant_status[["order_id", "last_updated_at"]].merge(
        unique[["order_id", "pickup_at"]], on="order_id", how="left")
    updated = pd.to_datetime(freshness.last_updated_at, format="mixed", errors="coerce")
    picked = pd.to_datetime(freshness.pickup_at, format="mixed", errors="coerce")
    offset_min = (updated - picked).dt.total_seconds() / 60
    checks.append(check("restaurant_status_freshness", "UNKNOWN",
                        {"status_rows": len(freshness), "before_pickup": int(offset_min.lt(0).sum()),
                         "at_or_after_pickup": int(offset_min.ge(0).sum()),
                         "median_minutes_from_pickup": float(offset_min.median()),
                         "more_than_60_minutes_before_pickup": int(offset_min.lt(-60).sum())},
                        "Freshness SLA by use case (not supplied)", int(offset_min.isna().sum()),
                        "Set different freshness SLAs for weekly analysis, live ETA and restaurant accountability."))

    p = pd.to_datetime(unique.promised_eta, format="mixed", errors="coerce")
    created = pd.to_datetime(unique.created_at, format="mixed", errors="coerce")
    actual = pd.to_datetime(unique.actual_delivery_at, format="mixed", errors="coerce")
    pickup = pd.to_datetime(unique.pickup_at, format="mixed", errors="coerce")
    chronology = (p < created) | ((actual.notna()) & (actual < pickup))
    chronology_count = int(chronology.fillna(False).sum())
    checks.append(check("timestamp_chronology", "WARN" if chronology_count else "PASS", chronology_count,
                        0, chronology_count, "Review affected rows; do not rewrite timestamps."))
    checks.append(check("late_kpi_definition_owner", "UNKNOWN", "Stakeholder definitions conflict; no owner documented",
                        "Approved canonical definition", 1,
                        "Obtain KPI owner sign-off before external publication."))
    return checks
