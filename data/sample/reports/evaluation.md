# Evaluation report - profile `sample` (seed 20261022)

## 1. Forecast accuracy on the temporal holdout

Holdout: 2025-12-25..2025-12-31 (7 days, promotion days excluded). Target: true (unconstrained) demand = observed sales on in-stock days + latent demand on stockout days.

| Model | Series | WAPE | Bias |
|---|---:|---:|---:|
| Engine (censoring-aware) | 241 | 31.1% | -3.8% |
| Naive 28-day mean (stockout zeros counted as demand) | 241 | 28.6% | -6.0% |
| Engine - series with >=3 stockout days in the 28 days before origin | 27 | 50.4% | 17.6% |
| Naive - same series | 27 | 36.3% | -6.1% |

Injected defective forecasts versus the clean engine forecast on the same series:

| Defect | Series | Defective WAPE / bias | Clean WAPE / bias |
|---|---:|---:|---:|
| forecast_future_leakage | 3 | 25.8% / -25.8% | 33.5% / -33.5% |
| forecast_stockout_as_zero_demand | 3 | 9.3% / -8.1% | 25.8% / 5.6% |

Engine accuracy by demand class (engine's classification):

| Class | Series | WAPE | Bias |
|---|---:|---:|---:|
| erratic | 25 | 28.8% | -0.9% |
| intermittent | 116 | 56.4% | -4.0% |
| lumpy | 13 | 37.7% | -26.8% |
| new_item | 14 | 78.6% | -26.0% |
| smooth | 73 | 20.0% | -1.1% |

## 2. Production forecast (2026-01) vs true demand

| Model | Series | WAPE | Bias |
|---|---:|---:|---:|
| Engine | 252 | 23.6% | 1.2% |
| Naive 28-day mean (stockout zeros counted as demand) | 252 | 21.6% | -1.8% |
| - method `censored_ewma_dow` | 104 | 22.0% | 4.8% |
| - method `croston_sba` | 142 | 27.5% | -8.9% |
| - method `new_item_shrinkage` | 5 | 57.1% | -52.4% |
| - method `zero_demand` | 1 | 100.0% | -100.0% |

Seasonal (year-over-year) index applied: False

## 3. Stockout censoring identification

- Sales rows: 7,403; stockout-flagged: 314 (4.2%)
- Truly censored rows (demand > available): 181 - recall **100.0%**, precision 57.6%
- Zero-sales days in stock (genuine zero demand): 2,426
- Zero-sales days during a stockout (censored, NOT zero demand): 230
- Lost sales on censored days: 523 units

## 4. Replenishment recommendations (2026-01-01)

- Lines: 252; units recommended: 1,641; value: INR 82,803; manual review: 7
- Status: no_order_needed 188, order_recommended 48, blocked_discontinued 10, blocked_no_active_supplier 4, capacity_exception 2
- Compliance: pack size 100.0%, MOQ 100.0%, ordering stores within capacity 100.0%, discontinued blocked: True
- Stores already over capacity before ordering (orders withheld, raised as exceptions): 1
- Store capacity utilisation after orders: median 73%, max 110%

Forward check against true demand over each series' protection period (L + R):

| Policy | Series | Coverage | Shortfall units | Units ordered |
|---|---:|---:|---:|---:|
| Engine (censoring-aware forecast) | 238 | 88.7% | 342 | 1,641 |
| Same policy, naive forecast | 238 | 85.7% | 430 | 772 |
| No order placed | 238 | 78.1% | - | 0 |

## 5. Audit gate vs ground truth (injected defects)

| Issue | Rule | Labelled | Flagged | Precision | Recall |
|---|---|---:|---:|---:|---:|
| negative_lead_time | LT01 | 3 | 3 | 100.0% | 100.0% |
| forecast_future_leakage | FC01 | 3 | 3 | 100.0% | 100.0% |
| forecast_stockout_as_zero_demand | FC02 | 3 | 3 | 100.0% | 100.0% |
| reorder_qty_ignores_pack_size | PR01 | 3 | 3 | 100.0% | 100.0% |
| discontinued_sku_recommended | PR03 | 2 | 2 | 100.0% | 100.0% |

Unexpected data-defect exceptions: 0. Operational exceptions: OP05 16, OP04 5, OP03 4, OP01 2
## 6. Validation summary

131 of 131 checks passed (6 found only the labelled anomalies).

| Check | Category | Status | Rows | Violations |
|---|---|---|---:|---:|
| sku: required fields not null | completeness | PASS | 100 | 0 |
| sku: enum values allowed | domain | PASS | 100 | 0 |
| sku: ID pattern | identity | PASS | 100 | 0 |
| sku: primary key unique | identity | PASS | 100 | 0 |
| store: required fields not null | completeness | PASS | 10 | 0 |
| store: enum values allowed | domain | PASS | 10 | 0 |
| store: ID pattern | identity | PASS | 10 | 0 |
| store: primary key unique | identity | PASS | 10 | 0 |
| supplier: required fields not null | completeness | PASS | 12 | 0 |
| supplier: enum values allowed | domain | PASS | 12 | 0 |
| supplier: ID pattern | identity | PASS | 12 | 0 |
| supplier: primary key unique | identity | PASS | 12 | 0 |
| supplier_sku: required fields not null | completeness | PASS | 123 | 0 |
| supplier_sku: enum values allowed | domain | PASS | 123 | 0 |
| supplier_sku: ID pattern | identity | PASS | 123 | 0 |
| supplier_sku: primary key unique | identity | PASS | 123 | 0 |
| supplier_sku: foreign keys resolve | referential | PASS | 123 | 0 |
| sku_store_matrix: required fields not null | completeness | PASS | 1,000 | 0 |
| sku_store_matrix: enum values allowed | domain | PASS | 1,000 | 0 |
| sku_store_matrix: ID pattern | identity | PASS | 1,000 | 0 |
| sku_store_matrix: primary key unique | identity | PASS | 1,000 | 0 |
| sku_store_matrix: foreign keys resolve | referential | PASS | 1,000 | 0 |
| sales_history: required fields not null | completeness | PASS | 7,403 | 0 |
| sales_history: enum values allowed | domain | PASS | 7,403 | 0 |
| sales_history: ID pattern | identity | PASS | 7,403 | 0 |
| sales_history: primary key unique | identity | PASS | 7,403 | 0 |
| sales_history: foreign keys resolve | referential | PASS | 7,403 | 0 |
| inventory_snapshot: required fields not null | completeness | PASS | 7,403 | 0 |
| inventory_snapshot: enum values allowed | domain | PASS | 7,403 | 0 |
| inventory_snapshot: ID pattern | identity | PASS | 7,403 | 0 |
| inventory_snapshot: primary key unique | identity | PASS | 7,403 | 0 |
| inventory_snapshot: foreign keys resolve | referential | PASS | 7,403 | 0 |
| purchase_order: required fields not null | completeness | PASS | 756 | 0 |
| purchase_order: enum values allowed | domain | PASS | 756 | 0 |
| purchase_order: ID pattern | identity | PASS | 756 | 0 |
| purchase_order: primary key unique | identity | PASS | 756 | 0 |
| purchase_order: foreign keys resolve | referential | PASS | 756 | 0 |
| purchase_order_event: required fields not null | completeness | PASS | 1,589 | 0 |
| purchase_order_event: enum values allowed | domain | PASS | 1,589 | 0 |
| purchase_order_event: ID pattern | identity | PASS | 1,589 | 0 |
| purchase_order_event: primary key unique | identity | PASS | 1,589 | 0 |
| purchase_order_event: foreign keys resolve | referential | PASS | 1,589 | 0 |
| lead_time_observation: required fields not null | completeness | PASS | 626 | 0 |
| lead_time_observation: enum values allowed | domain | PASS | 626 | 0 |
| lead_time_observation: ID pattern | identity | PASS | 626 | 0 |
| lead_time_observation: primary key unique | identity | PASS | 626 | 0 |
| lead_time_observation: foreign keys resolve | referential | PASS | 626 | 0 |
| forecast: required fields not null | completeness | PASS | 504 | 0 |
| forecast: enum values allowed | domain | PASS | 504 | 0 |
| forecast: ID pattern | identity | PASS | 504 | 0 |
| forecast: primary key unique | identity | PASS | 504 | 0 |
| forecast: foreign keys resolve | referential | PASS | 504 | 0 |
| reorder_policy: required fields not null | completeness | PASS | 252 | 0 |
| reorder_policy: enum values allowed | domain | PASS | 252 | 0 |
| reorder_policy: ID pattern | identity | PASS | 252 | 0 |
| reorder_policy: primary key unique | identity | PASS | 252 | 0 |
| reorder_policy: foreign keys resolve | referential | PASS | 252 | 0 |
| purchase_recommendation: required fields not null | completeness | PASS | 252 | 0 |
| purchase_recommendation: enum values allowed | domain | PASS | 252 | 0 |
| purchase_recommendation: ID pattern | identity | PASS | 252 | 0 |
| purchase_recommendation: primary key unique | identity | PASS | 252 | 0 |
| purchase_recommendation: foreign keys resolve | referential | PASS | 252 | 0 |
| audit_exception: required fields not null | completeness | PASS | 41 | 0 |
| audit_exception: enum values allowed | domain | PASS | 41 | 0 |
| audit_exception: ID pattern | identity | PASS | 41 | 0 |
| audit_exception: primary key unique | identity | PASS | 41 | 0 |
| ground_truth: required fields not null | completeness | PASS | 195 | 0 |
| ground_truth: enum values allowed | domain | PASS | 195 | 0 |
| ground_truth: ID pattern | identity | PASS | 195 | 0 |
| ground_truth: primary key unique | identity | PASS | 195 | 0 |
| demand_actuals: required fields not null | completeness | PASS | 7,812 | 0 |
| demand_actuals: enum values allowed | domain | PASS | 7,812 | 0 |
| demand_actuals: ID pattern | identity | PASS | 7,812 | 0 |
| demand_actuals: primary key unique | identity | PASS | 7,812 | 0 |
| demand_profile: required fields not null | completeness | PASS | 252 | 0 |
| demand_profile: enum values allowed | domain | PASS | 252 | 0 |
| demand_profile: ID pattern | identity | PASS | 252 | 0 |
| demand_profile: primary key unique | identity | PASS | 252 | 0 |
| supplier_disruption: required fields not null | completeness | PASS | 1 | 0 |
| supplier_disruption: enum values allowed | domain | PASS | 1 | 0 |
| supplier_disruption: ID pattern | identity | PASS | 1 | 0 |
| supplier_disruption: primary key unique | identity | PASS | 1 | 0 |
| sku: unit_price > unit_cost | domain | PASS | 100 | 0 |
| sku: discontinue_date >= launch_date | date_order | PASS | 100 | 0 |
| sku: lifecycle_state consistent with dates | lifecycle | PASS | 100 | 0 |
| supplier_sku: MOQ is a positive multiple of pack size | domain | PASS | 123 | 0 |
| supplier_sku: effective_to >= effective_from | date_order | PASS | 123 | 0 |
| supplier_sku: one contract per supplier x SKU | duplicates | PASS | 123 | 0 |
| sku_store_matrix: complete SKU x store grid | completeness | PASS | 1,000 | 0 |
| sku_store_matrix: ranged_from >= launch and store open; null iff not_ranged | date_order | PASS | 1,000 | 0 |
| sku_store_matrix: phase_out/delisted only for discontinued SKUs | lifecycle | PASS | 1,000 | 0 |
| purchase_order: qty multiple of pack and >= MOQ | constraint | PASS | 756 | 0 |
| purchase_order: order_value = qty x unit_cost | arithmetic | PASS | 756 | 0 |
| purchase_order: expected_receipt = order_date + nominal lead time | arithmetic | PASS | 756 | 0 |
| purchase_order: status/actual date consistent and receipt >= order | lifecycle | PASS | 756 | 0 |
| purchase_order: ordered only while SKU orderable and contract effective | lifecycle | PASS | 742 | 0 |
| purchase_order_event: CREATED -> [DELAYED] -> RECEIVED sequence and dates | lifecycle | PASS | 1,589 | 0 |
| lead_time_observation: 1:1 with received POs | referential | PASS | 626 | 0 |
| lead_time_observation: lead_time = receipt - order; delay = lead_time - nominal | arithmetic | PASS | 626 | 0 |
| lead_time_observation: lead_time_days >= 0 | anomaly_isolation | PASS_LABELLED | 626 | 3 |
| lead_time_observation: receipt_date matches purchase_order | anomaly_isolation | PASS_LABELLED | 626 | 3 |
| sales_history/inventory_snapshot: IDs derived from natural key | identity | PASS | 7,403 | 0 |
| sales_history = inventory_snapshot (same SKU-store-day, units_sold) | arithmetic | PASS | 7,403 | 0 |
| inventory_snapshot: closing = opening + receipts - sold + adjustment | arithmetic | PASS | 7,403 | 0 |
| inventory_snapshot: opening = previous closing | arithmetic | PASS | 7,403 | 0 |
| inventory_snapshot: quantities non-negative, adjustments <= 0 | domain | PASS | 7,403 | 0 |
| inventory_snapshot: receipts = POs received that day | arithmetic | PASS | 7,403 | 0 |
| inventory_snapshot: on_order = open PO quantity | arithmetic | PASS | 7,403 | 0 |
| sales_history: availability_status / stockout_flag derived from ledger | derived | PASS | 7,403 | 0 |
| sales_history: every truly censored day is flagged as stockout | censoring | PASS | 7,403 | 0 |
| sales_history: day_of_week matches sale_date | derived | PASS | 7,403 | 0 |
| sales_history: price paid = price x (1 - discount); promo flag consistent | arithmetic | PASS | 7,403 | 0 |
| sales_history: sales only while ranged and launched | lifecycle | PASS | 7,403 | 0 |
| forecast: horizon starts day after origin; horizon_days consistent | date_order | PASS | 504 | 0 |
| forecast: ID month = horizon start month | identity | PASS | 504 | 0 |
| forecast: horizon units = daily x days | arithmetic | PASS | 504 | 0 |
| forecast: no future data (training end <= origin) | anomaly_isolation | PASS_LABELLED | 504 | 3 |
| forecast: all stockout days in training window excluded (censoring) | anomaly_isolation | PASS_LABELLED | 504 | 3 |
| reorder_policy: SS / ROP / order-up-to recompute exactly | arithmetic | PASS | 252 | 0 |
| reorder_policy: demand equals linked production forecast | referential | PASS | 252 | 0 |
| purchase_recommendation: IP = on_hand + on_order; trigger = IP <= ROP | arithmetic | PASS | 252 | 0 |
| purchase_recommendation: qty_after_moq_pack = ceil(max(need, MOQ)/pack)*pack | arithmetic | PASS | 252 | 0 |
| purchase_recommendation: positive qty is a multiple of pack size | anomaly_isolation | PASS_LABELLED | 50 | 3 |
| purchase_recommendation: positive qty >= MOQ | constraint | PASS | 50 | 0 |
| purchase_recommendation: no positive qty for discontinued SKUs | anomaly_isolation | PASS_LABELLED | 252 | 2 |
| purchase_recommendation: store IP + orders <= storage capacity | constraint | PASS | 10 | 0 |
| purchase_recommendation: final qty <= qty after MOQ/pack (capacity only cuts) | constraint | PASS | 252 | 0 |
| purchase_recommendation: expected_receipt_date present iff qty > 0 | lifecycle | PASS | 252 | 0 |
| purchase_recommendation: ID sequence PO-<year>-###### contiguous | identity | PASS | 252 | 0 |
| ground truth not present in input/output tables | separation | PASS | 18 | 0 |
| ground_truth: labelled entities exist | referential | PASS | 14 | 0 |
