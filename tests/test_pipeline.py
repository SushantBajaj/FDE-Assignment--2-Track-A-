import tempfile
import unittest
from pathlib import Path

import pandas as pd

from src.ingest import fetch_dispatch
from src.transform import (build_canonical_orders, intervention_outcome_summary,
                           late_rate_definitions, stage_timing_summary)
from src.validation import validate


class Response:
    def __init__(self, status, payload, headers=None):
        self.status_code, self._payload = status, payload
        self.headers = headers or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def get(self, url, params, timeout):
        self.calls.append(params["page"])
        return self.responses[params["page"]].pop(0)


class PipelineLogicTests(unittest.TestCase):
    def test_order_deduplication_and_late_eligibility(self):
        df = pd.DataFrame([
            {"order_id": "a", "status": "delivered", "eta": "2026-01-01T10:00:00", "actual": "2026-01-01T10:01:00"},
            {"order_id": "a", "status": "delivered", "eta": "2026-01-01T10:00:00", "actual": "2026-01-01T10:01:00"},
            {"order_id": "b", "status": "cancelled", "eta": "2026-01-01T10:00:00", "actual": None},
            {"order_id": "c", "status": "delivered", "eta": "2026-01-01T10:00:00", "actual": None},
            {"order_id": "d", "status": "Delivered", "eta": "2026-01-01T10:00:00", "actual": "2026-01-01T09:59:00"},
        ]).rename(columns={"status": "final_status", "eta": "promised_eta", "actual": "actual_delivery_at"})
        df["created_at"] = "2026-01-01T09:00:00"
        df["pickup_at"] = "2026-01-01T09:30:00"
        canonical = build_canonical_orders(df)
        self.assertEqual(len(canonical), 4)
        self.assertEqual(int(canonical.metric_eligible.sum()), 2)
        self.assertEqual(int(canonical.late_flag.fillna(0).sum()), 1)
        self.assertEqual(canonical.loc[canonical.order_id.eq("b"), "late_flag"].isna().all(), True)
        self.assertEqual(canonical.loc[canonical.order_id.eq("d"), "final_status_norm"].iloc[0], "delivered")

    def test_three_late_definitions_use_their_own_populations(self):
        orders = pd.DataFrame([
            {"order_id": "a", "final_status": "delivered", "promised_eta": "2026-01-01T10:00:00",
             "actual_delivery_at": "2026-01-01T10:01:00"},
            {"order_id": "b", "final_status": "delivered", "promised_eta": None,
             "actual_delivery_at": "2026-01-01T10:01:00"},
            {"order_id": "c", "final_status": "cancelled", "promised_eta": "2026-01-01T10:00:00",
             "actual_delivery_at": None},
        ])
        definitions = {row["metric"]: row for row in late_rate_definitions(build_canonical_orders(orders))}
        self.assertEqual(definitions["Late Delivery Rate"]["numerator"], 1)
        self.assertEqual(definitions["Late Delivery Rate"]["denominator"], 1)
        self.assertEqual(definitions["Late Delivery Rate (>10 minutes; alternative)"]["numerator"], 0)
        historical = definitions["Historical-style Late Delivery Rate"]
        self.assertEqual(historical["numerator"], 1)
        self.assertEqual(historical["denominator"], 2)
        self.assertEqual(historical["unclassifiable"], 1)

    def test_stage_timing_uses_eligible_population_and_rejects_reverse_stage(self):
        orders = pd.DataFrame([
            {"order_id": "late", "final_status": "delivered", "created_at": "2026-01-01T10:00:00",
             "pickup_at": "2026-01-01T10:20:00", "promised_eta": "2026-01-01T11:00:00",
             "actual_delivery_at": "2026-01-01T11:20:00"},
            {"order_id": "ontime", "final_status": "delivered", "created_at": "2026-01-01T10:00:00",
             "pickup_at": "2026-01-01T10:30:00", "promised_eta": "2026-01-01T11:15:00",
             "actual_delivery_at": "2026-01-01T11:00:00"},
            {"order_id": "reverse", "final_status": "delivered", "created_at": "2026-01-01T10:00:00",
             "pickup_at": "2026-01-01T10:50:00", "promised_eta": "2026-01-01T11:00:00",
             "actual_delivery_at": "2026-01-01T10:45:00"},
            {"order_id": "missing_pickup", "final_status": "delivered", "created_at": "2026-01-01T10:00:00",
             "pickup_at": None, "promised_eta": "2026-01-01T11:00:00",
             "actual_delivery_at": "2026-01-01T11:05:00"},
            {"order_id": "cancelled", "final_status": "cancelled", "created_at": "2026-01-01T10:00:00",
             "pickup_at": None, "promised_eta": "2026-01-01T11:00:00", "actual_delivery_at": None},
        ])
        result = stage_timing_summary(build_canonical_orders(orders))
        all_rows = result.loc[result.cohort.eq("All eligible")].set_index("stage")
        self.assertEqual(all_rows.loc["order creation → pickup", "eligible_orders"], 3)
        self.assertEqual(all_rows.loc["pickup → delivery", "eligible_orders"], 2)
        self.assertEqual(all_rows.loc["order creation → delivery", "eligible_orders"], 4)
        late_pickup = result.loc[(result.stage == "order creation → pickup") & result.cohort.eq("Late")].iloc[0]
        self.assertEqual(late_pickup.median_minutes, 20)

    def test_intervention_outcomes_deduplicate_orders_and_use_eligible_denominator(self):
        canonical = build_canonical_orders(pd.DataFrame([
            {"order_id": "late", "final_status": "delivered", "promised_eta": "2026-01-01T10:00:00",
             "actual_delivery_at": "2026-01-01T10:05:00"},
            {"order_id": "ontime", "final_status": "delivered", "promised_eta": "2026-01-01T10:00:00",
             "actual_delivery_at": "2026-01-01T09:55:00"},
            {"order_id": "missing", "final_status": "delivered", "promised_eta": "2026-01-01T10:00:00",
             "actual_delivery_at": None},
            {"order_id": "cancel", "final_status": "cancelled", "promised_eta": "2026-01-01T10:00:00",
             "actual_delivery_at": None},
        ]))
        interventions = pd.DataFrame([
            {"order_id": "late", "intervention_type": "PRIORITY_DISPATCH"},
            {"order_id": "late", "intervention_type": "PRIORITY_DISPATCH"},
            {"order_id": "ontime", "intervention_type": "PRIORITY_DISPATCH"},
            {"order_id": "missing", "intervention_type": "PRIORITY_DISPATCH"},
            {"order_id": "ontime", "intervention_type": "CUSTOMER_CREDIT"},
        ])
        summary = intervention_outcome_summary(canonical, interventions).set_index("intervention_type")
        priority = summary.loc["PRIORITY_DISPATCH"]
        self.assertEqual(priority.eligible_orders, 2)
        self.assertEqual(priority.intervention_count, 4)
        self.assertEqual(priority.late_orders, 1)
        self.assertEqual(priority.late_rate, 0.5)
        self.assertEqual(summary.loc["CUSTOMER_CREDIT", "eligible_orders"], 1)

    def test_api_pagination_retry_and_raw_pages(self):
        def ok(page, data, more):
            return Response(200, {"data": data, "page": page, "page_size": 1,
                                  "has_more": more, "total_records": 2})
        session = FakeSession({1: [Response(500, {}), ok(1, [{"order_id": "a"}], True)],
                               2: [ok(2, [{"order_id": "b"}], False)]})
        with tempfile.TemporaryDirectory() as d:
            records, manifest = fetch_dispatch("http://test", Path(d), page_size=1,
                                                session=session, sleep=lambda _: None)
            self.assertEqual([r["order_id"] for r in records], ["a", "b"])
            self.assertEqual(manifest["retry_count"], 1)
            self.assertEqual(manifest["records_received"], 2)
            self.assertTrue((Path(d) / "page_0001.json").exists())
            self.assertEqual(session.calls, [1, 1, 2])

    def test_api_incomplete_retrieval_fails(self):
        session = FakeSession({1: [Response(200, {"data": [{"order_id": "a"}], "page": 1,
            "page_size": 2, "has_more": False, "total_records": 2})]})
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaisesRegex(RuntimeError, "Incomplete Dispatch retrieval"):
                fetch_dispatch("http://test", Path(d), page_size=2, session=session)

    def test_validation_flags_duplicate_missing_reference_and_ambiguous_state(self):
        orders = pd.DataFrame([
            {"order_id": "a", "customer_id": "c", "restaurant_id": None, "driver_id": "d",
             "final_status": "Delivered", "promised_eta": "2026-01-01T10:00:00",
             "created_at": "2026-01-01T09:00:00", "pickup_at": "2026-01-01T09:30:00", "actual_delivery_at": None,
             "traffic_bucket": "high"},
            {"order_id": "a", "customer_id": "c", "restaurant_id": None, "driver_id": "d",
             "final_status": "Delivered", "promised_eta": "2026-01-01T10:00:00",
             "created_at": "2026-01-01T09:00:00", "pickup_at": "2026-01-01T09:30:00", "actual_delivery_at": None,
             "traffic_bucket": "HIGH"},
        ])
        checks = validate(orders, pd.DataFrame({"customer_id": ["c"]}),
                          pd.DataFrame({"restaurant_id": ["r"]}), pd.DataFrame({"driver_id": ["d"]}),
                          pd.DataFrame({"order_id": [""], "category": ["ETA issue"]}),
                          pd.DataFrame({"order_id": ["a", "a"], "restaurant_id": ["r", "r"],
                                        "last_updated_at": ["2026-01-01T09:20:00", "2026-01-01T09:25:00"],
                                        "status": ["handoff", "handed_off"]}),
                          [{"order_id": "a"}],
                          {"status": "PASS", "records_received": 1, "api_reported_total": 1,
                           "unique_order_ids": 1, "retry_count": 0, "duplicate_order_ids": 0},
                          pd.DataFrame([{"order_id": "a", "final_status_norm": "delivered",
                                         "delivered_flag": 1, "late_flag": None, "delay_min": None,
                                         "outcome_bucket": "unknown"}]),
                          pd.DataFrame({"order_id": ["a"]}), pd.DataFrame({"order_id": ["a"]}),
                          pd.DataFrame({"order_id": ["a"]}), pd.DataFrame({"order_id": ["a"]}))
        by_name = {item["check"]: item for item in checks}
        self.assertEqual(by_name["unique_order_grain"]["status"], "WARN")
        self.assertEqual(by_name["order_restaurant_reference"]["status"], "WARN")
        self.assertEqual(by_name["restaurant_status_categories"]["status"], "WARN")
        self.assertEqual(by_name["traffic_bucket_categories"]["status"], "WARN")
        self.assertEqual(by_name["support_ticket_categories"]["status"], "WARN")
        self.assertEqual(by_name["late_kpi_definition_owner"]["status"], "UNKNOWN")

    def test_api_rerun_overwrites_raw_page_deterministically(self):
        payload = {"data": [{"order_id": "a"}], "page": 1, "page_size": 1,
                   "has_more": False, "total_records": 1}
        with tempfile.TemporaryDirectory() as d:
            for _ in range(2):
                records, manifest = fetch_dispatch("http://test", Path(d), page_size=1,
                    session=FakeSession({1: [Response(200, payload)]}), sleep=lambda _: None)
            self.assertEqual(records, [{"order_id": "a"}])
            self.assertEqual(manifest["records_received"], 1)
            self.assertEqual((Path(d) / "page_0001.json").read_text(),
                             '{\n  "data": [\n    {\n      "order_id": "a"\n    }\n  ],\n  "page": 1,\n  "page_size": 1,\n  "has_more": false,\n  "total_records": 1\n}\n')


if __name__ == "__main__":
    unittest.main()
