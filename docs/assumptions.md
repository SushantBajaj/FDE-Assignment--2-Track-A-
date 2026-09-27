# Assumptions and unresolved decisions

- The order source is deduplicated by `order_id`, keeping its first occurrence for analysis; all original rows remain preserved. Duplicate rows are reported, not deleted from source data.
- `delivered` and `Delivered` are treated as case variants. The raw values remain available. Restaurant `handoff` is not mapped to `handed_off`, and `unknown` remains unresolved.
- The project KPI follows the VP Operations definition: any strictly positive delay versus promised ETA among delivered orders with both timestamps. The Support Lead's >10-minute rule is a separate stakeholder metric proposal. The classroom data names no canonical KPI owner; publish approval remains UNKNOWN.
- Missing actual delivery timestamps and cancelled orders are ineligible for lateness metrics. They are not assigned fabricated timestamps or treated as on time.
- First occurrence of duplicate order ID is used only to avoid double counting; the three duplicates should be investigated upstream.
- Support tickets without an order ID remain customer signals but cannot join to an order. Driver GPS does not directly record restaurant arrival. Restaurant status is a snapshot with ambiguous values and uneven freshness.
- Traffic, weather, distance, support and intervention comparisons are associations. They do not prove causes or intervention effectiveness; order mix and intervention selection may explain differences.
- Sources are a fixed classroom snapshot from August 2026. No freshness SLA is supplied, so freshness suitability for live ETA or accountability is UNKNOWN.
