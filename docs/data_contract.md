# Data contract

| Dataset | Key | Grain | Contract / use |
|---|---|---|---|
| `orders` | `order_id` | One source order row; duplicate IDs retained in raw | Canonical model keeps first row per ID and reports duplicates. |
| `customers`, `restaurants`, `drivers` | respective `*_id` | One entity | Orders reference these dimensions. |
| `order_events` | `event_id` | One observed lifecycle event | Preserve event type, time, actor and source. |
| support/customer interactions | respective interaction/ticket IDs | One interaction | Keep sources distinct; blank ticket order IDs are unlinked. |
| interventions | `intervention_id` | One intervention | Multiple interventions per order allowed. |
| outcome | `order_id` | One derived outcome | Outcome is calculated from order status/timestamps, not trusted as a hardcoded label. |
| Dispatch API | `(page, order_id)` | One dispatch record | All pages, unique IDs and API-reported total must reconcile. Successful page payloads are preserved separately. |

The late KPI uses unique orders whose normalized status is `delivered` and which have both a promised ETA and actual delivery timestamp. Late means actual delivery is strictly after the promised ETA. Cancelled orders, missing timestamps and duplicate copies are excluded. The >10-minute stakeholder definition is a distinct alternative, not silently substituted.
