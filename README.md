# FlashEats FDE Assessment 2

**Problem:** Late deliveries are rising and customers say ETA is unreliable. Locate observed elapsed time in the order workflow and assess operational interventions before investing in prediction.

**Users:** Operations (primary), Support, Finance, Data. Source owners are not specified in the pack.

**Project KPI:** Late Delivery Rate — delivered orders with actual delivery after promised ETA / delivered orders with both timestamps. Definition sensitivity is in `outputs/kpi_definition_sensitivity.csv`.

## Sources

SQLite orders and entities · paginated Dispatch API · CSV lifecycle, customer/support, restaurant-status, intervention and outcome files · JSON driver events/GPS.

## Run

```bash
python -m pip install -r api/requirements.txt
python -m src.pipeline
python -m unittest discover -s tests -v
```

The pipeline starts the supplied API if needed, preserves raw inputs and overwrites generated outputs on rerun. Port 8000 must be available.

## Evidence

| Metric family | Result |
|---|---:|
| Late Delivery Rate | 843 / 1,495 = **56.39%** |
| Median delay among late orders | **8.03 min** |
| Median order-to-delivery elapsed time | **73.96 min** |
| Support interaction rate | 481 / 1,600 = **30.06%** |
| Intervention rate | 430 / 1,600 = **26.88%** |

Stage medians: creation→pickup **26.07 min**; pickup→delivery **47.28 min**. The late vs. on-time median gap is larger before pickup (**12.04 min**) than after pickup (**3.67 min**). By intervention type, observed late rates range from **51.2%** (priority dispatch) to **66.7%** (customer credit); intervention groups overlap and are not causal comparisons. Details: `outputs/stage_timing.csv`, `outputs/intervention_outcomes.csv`. Alternative KPI definitions: `outputs/kpi_definition_sensitivity.csv`.

**Decision:** The late-rate claim is reproducible, but the KPI owner is undocumented. Confirm the definition and improve stage instrumentation before using this observational evidence to choose interventions or invest in prediction.

## Known / Unknown / Assumption / Limitation

- **Known:** 56.39% of eligible deliveries were late; post-pickup has the larger median elapsed duration.
- **Unknown:** Restaurant arrival time and the causal effect of interventions.
- **Assumption:** Any positive actual-minus-promised delay is late; duplicates are counted once, keeping the first source row.
- **Limitation:** Intervention selection may reflect existing risk; restaurant waiting cannot be isolated without an arrival event.

## Project files

[`docs/source_map.md`](docs/source_map.md) · [`docs/workflow_model.md`](docs/workflow_model.md) · `src/` pipeline · `tests/` · `outputs/` metrics, stage timing, intervention outcomes, validation and manifest.
