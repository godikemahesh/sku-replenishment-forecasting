# Data dictionary

Generated from `replenishment/schema.py`. Layers: **input** (what planners and models see), **output** (engine decisions), **ground_truth** (labels - never used as features).

## Layer: input

### SKU (`sku`)

Product master. One row per SKU.

Primary key: `sku_id`

| Field | Type | Req. | Allowed / unit | Rule | Description |
|---|---|---|---|---|---|
| `sku_id` | string | Y |  | unique; SKU-######; immutable | Primary key |
| `sku_name` | string | Y |  | non-empty | Display name (synthetic brand + product + size) |
| `category` | enum | Y | Staples \| Dairy \| Beverages \| Snacks \| Personal Care \| Household \| Packaged Food \| Frozen |  | Merchandise category; drives seasonality |
| `brand` | string | Y |  |  | Synthetic brand name |
| `unit_cost` | decimal | Y | INR | > 0 | Standard cost per unit |
| `unit_price` | decimal | Y | INR | > unit_cost | Regular shelf price per unit |
| `launch_date` | date | Y |  | ISO date | First day the SKU may be sold |
| `discontinue_date` | date | N |  | null or >= launch_date | Last day replenishment is allowed |
| `lifecycle_state` | enum | Y | active \| new_launch \| discontinued | discontinued if discontinue_date <= history_end; new_launch if launched in last 90 days | State as of history end |

### Store (`store`)

Store / dark-store master.

Primary key: `store_id`

| Field | Type | Req. | Allowed / unit | Rule | Description |
|---|---|---|---|---|---|
| `store_id` | string | Y |  | unique; STORE-###### | Primary key |
| `store_name` | string | Y |  | non-empty | Display name |
| `city` | string | Y |  |  | City |
| `region` | string | Y |  |  | State / region |
| `store_format` | enum | Y | hypermarket \| supermarket \| express |  | Format; drives size and assortment breadth |
| `open_date` | date | Y |  |  | Store opening date; no sales before it |
| `storage_capacity_units` | integer | Y | units | > 0 | Max units on hand + on order the store can hold (capacity constraint) |
| `lifecycle_state` | enum | Y | active \| new_store |  | new_store if opened in last 90 days of history |

### Supplier (`supplier`)

Supplier master.

Primary key: `supplier_id`

| Field | Type | Req. | Allowed / unit | Rule | Description |
|---|---|---|---|---|---|
| `supplier_id` | string | Y |  | unique; SUP-#### | Primary key |
| `supplier_name` | string | Y |  |  | Synthetic supplier name |
| `region` | string | Y |  |  | Supplier home region |
| `supplier_type` | enum | Y | local \| regional \| national |  | local (2-3d), regional (4-6d), national (7-12d) base lead time |
| `reliability_tier` | enum | Y | A \| B \| C |  | Contracted tier; C suppliers are delayed most often |
| `lifecycle_state` | enum | Y | active \| inactive |  | Supplier status |

### SupplierSKU (`supplier_sku`)

Bridge SKU <-> Supplier (N:M) with ordering constraints and effective dates.

Primary key: `supplier_sku_id` | Natural key: `supplier_id, sku_id`
Relationships: `supplier_id` -> `supplier` (N:1); `sku_id` -> `sku` (N:1)

| Field | Type | Req. | Allowed / unit | Rule | Description |
|---|---|---|---|---|---|
| `supplier_sku_id` | string | Y |  | unique | Primary key |
| `supplier_id` | string | Y |  | FK -> supplier.supplier_id | Supplier reference |
| `sku_id` | string | Y |  | FK -> sku.sku_id | SKU reference |
| `priority` | integer | Y |  | 1 = primary, 2 = backup | Sourcing priority |
| `pack_size_units` | integer | Y | units | > 0 | Orders must be a multiple of this |
| `moq_units` | integer | Y | units | > 0 and multiple of pack_size_units | Minimum order quantity |
| `unit_cost` | decimal | Y | INR | > 0 | Purchase cost per unit from this supplier |
| `nominal_lead_time_days` | integer | Y | days | >= 1 | Contracted lead time |
| `effective_from` | date | Y |  |  | Contract start |
| `effective_to` | date | N |  | null or >= effective_from | Contract end (null = open-ended) |
| `is_active` | boolean | Y |  |  | Contract active on the recommendation date |

### SKUStoreMatrix (`sku_store_matrix`)

Bridge SKU <-> Store (N:M) assortment matrix: every SKU x store combination.

Primary key: `sku_id, store_id`
Relationships: `sku_id` -> `sku` (N:1); `store_id` -> `store` (N:1)

| Field | Type | Req. | Allowed / unit | Rule | Description |
|---|---|---|---|---|---|
| `sku_id` | string | Y |  | FK -> sku.sku_id | SKU reference |
| `store_id` | string | Y |  | FK -> store.store_id | Store reference |
| `assortment_status` | enum | Y | ranged \| phase_out \| delisted \| not_ranged | ranged -> phase_out (SKU discontinued, stock remains) -> delisted (stock sold through) | Assortment lifecycle |
| `ranged_from` | date | N |  | null iff not_ranged; >= launch_date and >= store open_date | First sellable day |
| `ranged_to` | date | N |  | set only when delisted | Last sellable day |

### SalesHistory (`sales_history`)

Observed daily sales per SKU-store. Rows exist only on days the SKU was sellable.

Primary key: `sale_id` | Natural key: `store_id, sku_id, sale_date`
Relationships: `store_id` -> `store` (N:1); `sku_id` -> `sku` (N:1); `sku_id,store_id` -> `sku_store_matrix` (N:1)

| Field | Type | Req. | Allowed / unit | Rule | Description |
|---|---|---|---|---|---|
| `sale_id` | string | Y |  | SALE-<store_id>-<sku_id>-<yyyymmdd> | Primary key |
| `store_id` | string | Y |  | FK -> store.store_id | Store reference |
| `sku_id` | string | Y |  | FK -> sku.sku_id | SKU reference |
| `sale_date` | date | Y |  | ranged_from <= sale_date <= history_end | Business day |
| `day_of_week` | enum | Y | Monday \| Tuesday \| Wednesday \| Thursday \| Friday \| Saturday \| Sunday |  | Derived from sale_date |
| `units_sold` | integer | Y | units | >= 0; = inventory_snapshot.units_sold | OBSERVED sales. Censored (understates demand) when stockout_flag is true |
| `unit_price_paid` | decimal | Y | INR | unit_price x (1 - promo_discount_pct) | Average selling price |
| `promotion_flag` | boolean | Y |  |  | Chain promotion running at this store |
| `promo_discount_pct` | decimal | Y | fraction | 0 when no promotion, else 0.10-0.30 | Promotion discount |
| `availability_status` | enum | Y | in_stock \| sold_out_during_day \| out_of_stock_all_day | out_of_stock_all_day if opening+receipts = 0; sold_out_during_day if closing = 0; else in_stock | Separates stockouts from genuine zero demand |
| `stockout_flag` | boolean | Y |  | availability_status != in_stock | Sales may be censored by availability - do NOT read as zero demand |

### InventorySnapshot (`inventory_snapshot`)

End-of-day stock ledger per SKU-store; one row per sellable day.

Primary key: `inventory_snapshot_id` | Natural key: `store_id, sku_id, snapshot_date`
Relationships: `store_id` -> `store` (N:1); `sku_id` -> `sku` (N:1); `sku_id,store_id` -> `sku_store_matrix` (N:1)

| Field | Type | Req. | Allowed / unit | Rule | Description |
|---|---|---|---|---|---|
| `inventory_snapshot_id` | string | Y |  | INV-<store_id>-<sku_id>-<yyyymmdd> | Primary key |
| `store_id` | string | Y |  | FK -> store.store_id | Store reference |
| `sku_id` | string | Y |  | FK -> sku.sku_id | SKU reference |
| `snapshot_date` | date | Y |  |  | Business day |
| `opening_on_hand_units` | integer | Y | units | >= 0; = previous day closing | Start-of-day stock |
| `receipts_units` | integer | Y | units | >= 0; = sum(purchase_order received that day) | Goods received |
| `units_sold` | integer | Y | units | >= 0 | Units sold |
| `adjustment_units` | integer | Y | units | <= 0 (shrinkage / damage) | Stock adjustment |
| `closing_on_hand_units` | integer | Y | units | = opening + receipts - sold + adjustment; >= 0 | End-of-day stock |
| `on_order_units` | integer | Y | units | = sum(open purchase_order qty) at end of day | Inbound pipeline |
| `stockout_flag` | boolean | Y |  | closing_on_hand_units = 0 | Shelf empty at some point in the day |

### PurchaseOrder (`purchase_order`)

Historical replenishment orders placed by the legacy policy (state lives in purchase_order_event).

Primary key: `po_id`
Relationships: `store_id` -> `store` (N:1); `sku_id` -> `sku` (N:1); `supplier_id` -> `supplier` (N:1); `supplier_sku_id` -> `supplier_sku` (N:1)

| Field | Type | Req. | Allowed / unit | Rule | Description |
|---|---|---|---|---|---|
| `po_id` | string | Y |  | PO-<order year>-<counter, zero-padded to 6 digits; 7 once a year exceeds 999,999 orders> | Primary key |
| `store_id` | string | Y |  | FK -> store.store_id | Store reference |
| `sku_id` | string | Y |  | FK -> sku.sku_id | SKU reference |
| `supplier_id` | string | Y |  | FK -> supplier.supplier_id | Supplier reference |
| `supplier_sku_id` | string | Y |  | FK -> supplier_sku | Contract used |
| `order_reason` | enum | Y | replenishment \| launch_allocation |  | launch_allocation = first fill of a new SKU/store |
| `order_date` | date | Y |  |  | Date the order was placed |
| `order_qty_units` | integer | Y | units | > 0; multiple of pack size; >= MOQ | Ordered units |
| `order_value` | decimal | Y | INR | order_qty_units x supplier_sku.unit_cost | Order value |
| `expected_receipt_date` | date | Y |  | order_date + nominal_lead_time_days | Promised date |
| `actual_receipt_date` | date | N |  | null iff po_status = open; >= order_date | Delivery date |
| `po_status` | enum | Y | open \| received |  | Current status (derived from latest event) |

### PurchaseOrderEvent (`purchase_order_event`)

Append-only PO lifecycle history: CREATED -> [DELAYED] -> RECEIVED.

Primary key: `event_id`
Relationships: `po_id` -> `purchase_order` (N:1)

| Field | Type | Req. | Allowed / unit | Rule | Description |
|---|---|---|---|---|---|
| `event_id` | string | Y |  | <po_id>-EV<n> | Primary key |
| `po_id` | string | Y |  | FK -> purchase_order.po_id | Order reference |
| `event_type` | enum | Y | CREATED \| DELAYED \| RECEIVED |  | Lifecycle transition |
| `event_date` | date | Y |  | non-decreasing within a po_id | When the transition happened |
| `quantity_units` | integer | Y | units | >= 0 | Units affected |

### LeadTimeObservation (`lead_time_observation`)

One observed lead time per received purchase order (1:1 with received POs).

Primary key: `lead_time_observation_id`
Relationships: `po_id` -> `purchase_order` (1:1); `supplier_id` -> `supplier` (N:1); `sku_id` -> `sku` (N:1); `store_id` -> `store` (N:1)

| Field | Type | Req. | Allowed / unit | Rule | Description |
|---|---|---|---|---|---|
| `lead_time_observation_id` | string | Y |  | LTO-<po number> | Primary key |
| `po_id` | string | Y |  | FK -> purchase_order.po_id | Order the observation came from |
| `supplier_id` | string | Y |  | FK -> supplier.supplier_id | Supplier reference |
| `sku_id` | string | Y |  | FK -> sku.sku_id | SKU reference |
| `store_id` | string | Y |  | FK -> store.store_id | Store reference |
| `order_date` | date | Y |  |  | Order date |
| `receipt_date` | date | Y |  | = order_date + lead_time_days | Recorded receipt date |
| `lead_time_days` | integer | Y | days | >= 0 (negative values are invalid and must be quarantined) | Observed lead time |
| `nominal_lead_time_days` | integer | Y | days |  | Contracted lead time at order time |
| `delay_days` | integer | Y | days | = lead_time_days - nominal_lead_time_days | Lateness (negative = early) |

## Layer: output

### Forecast (`forecast`)

Demand forecasts. backtest = temporal holdout inside history; production = next month.

Primary key: `forecast_id`
Relationships: `store_id` -> `store` (N:1); `sku_id` -> `sku` (N:1)

| Field | Type | Req. | Allowed / unit | Rule | Description |
|---|---|---|---|---|---|
| `forecast_id` | string | Y |  | FC-<store_id>-<sku_id>-<yyyy-mm of horizon start> | Primary key |
| `store_id` | string | Y |  | FK -> store.store_id | Store reference |
| `sku_id` | string | Y |  | FK -> sku.sku_id | SKU reference |
| `forecast_type` | enum | Y | backtest \| production |  | backtest (evaluated on holdout) or production |
| `forecast_month` | string | Y |  | YYYY-MM | Month of horizon start |
| `forecast_origin_date` | date | Y |  |  | Last date of information available to the model |
| `training_window_start_date` | date | Y |  |  | First training day |
| `training_window_end_date` | date | Y |  | <= forecast_origin_date (else future leakage) | Last training day |
| `horizon_start_date` | date | Y |  | = origin + 1 | First forecast day |
| `horizon_end_date` | date | Y |  | >= horizon_start_date | Last forecast day |
| `horizon_days` | integer | Y | days |  | Horizon length |
| `method` | enum | Y | censored_ewma_dow \| croston_sba \| new_item_shrinkage \| censored_prior \| zero_demand |  | Model chosen by demand class |
| `demand_class` | enum | Y | smooth \| erratic \| intermittent \| lumpy \| new_item \| no_clean_history |  | Syntetos-Boylan class from clean (uncensored) history |
| `train_days_used` | integer | Y | days |  | Clean days used to fit |
| `stockout_days_excluded` | integer | Y | days | = stockout days in training window | Censored days excluded |
| `promo_days_excluded` | integer | Y | days |  | Promotion days excluded from the baseline |
| `seasonal_index` | decimal | Y |  | 0.6-1.6; 1.0 when target month not in training data | Category month index |
| `predicted_daily_units` | decimal | Y | units/day | >= 0 | Mean daily demand |
| `predicted_daily_sd_units` | decimal | Y | units/day | >= 0 | Daily demand standard deviation |
| `predicted_horizon_units` | decimal | Y | units | >= 0 | Total demand over horizon (day-of-week weighted) |

### ReorderPolicy (`reorder_policy`)

Periodic-review order-up-to policy per SKU-store, valid for the forecast month.

Primary key: `reorder_policy_id`
Relationships: `store_id` -> `store` (N:1); `sku_id` -> `sku` (N:1); `forecast_id` -> `forecast` (1:1); `supplier_sku_id` -> `supplier_sku` (N:1)

| Field | Type | Req. | Allowed / unit | Rule | Description |
|---|---|---|---|---|---|
| `reorder_policy_id` | string | Y |  | RP-<store_id>-<sku_id>-<yyyy-mm> | Primary key |
| `store_id` | string | Y |  | FK -> store.store_id | Store reference |
| `sku_id` | string | Y |  | FK -> sku.sku_id | SKU reference |
| `forecast_id` | string | Y |  | FK -> forecast (production) | Demand input |
| `supplier_sku_id` | string | N |  | FK -> supplier_sku; null if no active contract | Contract chosen |
| `effective_from` | date | Y |  |  | Policy start |
| `effective_to` | date | Y |  | >= effective_from | Policy end |
| `abc_class` | enum | Y | A \| B \| C |  | Store-level revenue class (80/15/5) |
| `service_level_target` | decimal | Y |  | A 0.98, B 0.95, C 0.90 | Cycle service level |
| `z_value` | decimal | Y |  |  | Normal quantile of service level |
| `review_period_days` | integer | Y | days |  | R |
| `lead_time_source` | enum | Y | supplier_sku_history \| supplier_history \| nominal_default \| none |  | Which history the lead-time estimate came from |
| `lead_time_mean_days` | decimal | Y | days | >= 0 | L |
| `lead_time_sd_days` | decimal | Y | days | >= 0 | sigma_L |
| `lead_time_obs_used` | integer | Y |  |  | Valid observations used |
| `lead_time_obs_rejected` | integer | Y |  |  | Invalid (negative) observations quarantined |
| `demand_daily_units` | decimal | Y | units/day |  | d |
| `demand_daily_sd_units` | decimal | Y | units/day |  | sigma_d |
| `safety_stock_units` | integer | Y | units | ceil(z*sqrt((L+R)*sd^2 + d^2*sL^2)) | Safety stock |
| `reorder_point_units` | integer | Y | units | ceil(d*L) + SS | Order when inventory position <= this |
| `order_up_to_units` | integer | Y | units | ceil(d*(L+R)) + SS | Target inventory position |
| `policy_status` | enum | Y | active \| blocked_discontinued \| blocked_no_active_supplier |  | Whether ordering is allowed |

### PurchaseRecommendation (`purchase_recommendation`)

One replenishment decision per sellable SKU-store on the recommendation date; ID is the reserved PO number.

Primary key: `purchase_recommendation_id`
Relationships: `store_id` -> `store` (N:1); `sku_id` -> `sku` (N:1); `reorder_policy_id` -> `reorder_policy` (1:1); `forecast_id` -> `forecast` (1:1); `supplier_id` -> `supplier` (N:1); `supplier_sku_id` -> `supplier_sku` (N:1)

| Field | Type | Req. | Allowed / unit | Rule | Description |
|---|---|---|---|---|---|
| `purchase_recommendation_id` | string | Y |  | PO-2026-###### | Primary key |
| `reorder_policy_id` | string | Y |  | FK -> reorder_policy | Policy applied |
| `forecast_id` | string | Y |  | FK -> forecast | Forecast used |
| `store_id` | string | Y |  | FK -> store.store_id | Store reference |
| `sku_id` | string | Y |  | FK -> sku.sku_id | SKU reference |
| `supplier_id` | string | N |  | FK -> supplier | Supplier to order from |
| `supplier_sku_id` | string | N |  | FK -> supplier_sku | Contract to order on |
| `recommendation_date` | date | Y |  |  | Decision date |
| `on_hand_units` | integer | Y | units | = snapshot closing on history end | Stock on hand |
| `on_order_units` | integer | Y | units | = snapshot on_order on history end | Open POs |
| `inventory_position_units` | integer | Y | units | on_hand + on_order | IP |
| `reorder_point_units` | integer | Y | units |  | From policy |
| `order_up_to_units` | integer | Y | units |  | From policy |
| `days_of_cover` | decimal | Y | days | IP / demand_daily_units | Priority for capacity allocation |
| `reorder_triggered` | boolean | Y |  | IP <= reorder point | Whether the policy fires |
| `raw_need_units` | integer | Y | units | max(0, order_up_to - IP) when triggered | Unconstrained need |
| `moq_units` | integer | N | units |  | MOQ applied |
| `pack_size_units` | integer | N | units |  | Pack size applied |
| `qty_after_moq_pack_units` | integer | Y | units | ceil(max(need, MOQ)/pack)*pack | Before capacity |
| `capacity_room_units` | integer | Y | units | store room remaining before this line | Capacity check input |
| `recommended_order_qty_units` | integer | Y | units | >= 0; if > 0: multiple of pack, >= MOQ, fits capacity | Final quantity |
| `order_value` | decimal | Y | INR |  | qty x unit_cost |
| `expected_receipt_date` | date | N |  | recommendation_date + ceil(L) when qty > 0 | ETA |
| `recommendation_status` | enum | Y | order_recommended \| capacity_reduced \| capacity_exception \| no_order_needed \| blocked_discontinued \| blocked_no_active_supplier |  | Decision |
| `manual_review_flag` | boolean | Y |  |  | Planner should review (new item, no clean history, LT fallback) |
| `rule_trace` | string | Y |  |  | Ordered list of rules applied - full decision traceability |

### AuditException (`audit_exception`)

Exceptions raised by the audit gate (rules only - never reads ground truth).

Primary key: `exception_id`

| Field | Type | Req. | Allowed / unit | Rule | Description |
|---|---|---|---|---|---|
| `exception_id` | string | Y |  | EXC-######## | Primary key |
| `exception_category` | enum | Y | data_defect \| operational |  | data_defect = invalid record; operational = decision needing attention |
| `rule_id` | string | Y |  |  | Audit rule that fired (see README) |
| `entity_type` | string | Y |  |  | Table of the offending record |
| `entity_id` | string | Y |  |  | Primary key of the offending record |
| `store_id` | string | N |  |  | Store, when applicable |
| `sku_id` | string | N |  |  | SKU, when applicable |
| `severity` | enum | Y | high \| medium \| low |  | Impact |
| `action` | enum | Y | quarantine \| block \| manual_review \| expedite |  | What the workflow does with the record |
| `detail` | string | Y |  |  | Human-readable explanation with values |

## Layer: ground_truth

### GroundTruth (`ground_truth`)

Labels for every known anomaly: natural stockout censoring and injected defects.

Primary key: `ground_truth_id`

| Field | Type | Req. | Allowed / unit | Rule | Description |
|---|---|---|---|---|---|
| `ground_truth_id` | string | Y |  | GT-######## | Primary key |
| `entity_type` | string | Y |  |  | Table containing the labelled record |
| `entity_id` | string | Y |  |  | Primary key of the labelled record |
| `issue_type` | enum | Y | stockout_censored_demand \| negative_lead_time \| forecast_future_leakage \| forecast_stockout_as_zero_demand \| reorder_qty_ignores_pack_size \| discontinued_sku_recommended |  | Anomaly label |
| `origin` | enum | Y | natural \| injected |  | natural = arises from simulation; injected = deliberate defect |
| `expected_behavior` | string | Y |  |  | What a correct system must do |
| `observed_value` | decimal | N |  |  | Value present in the dataset |
| `expected_value` | decimal | N |  |  | Correct value (e.g. latent demand, clean quantity) |
| `severity` | enum | Y | high \| medium \| low |  | Impact |

### DemandActual (`demand_actuals`)

True (unconstrained) daily demand for the forecast month - used only to score production forecasts.

Primary key: `store_id, sku_id, demand_date`

| Field | Type | Req. | Allowed / unit | Rule | Description |
|---|---|---|---|---|---|
| `store_id` | string | Y |  | FK -> store.store_id | Store reference |
| `sku_id` | string | Y |  | FK -> sku.sku_id | SKU reference |
| `demand_date` | date | Y |  |  | Day in forecast month |
| `true_demand_units` | integer | Y | units | >= 0 | Latent demand |

### DemandProfile (`demand_profile`)

Hidden generator parameters per SKU-store (for evaluation by true demand class).

Primary key: `store_id, sku_id`

| Field | Type | Req. | Allowed / unit | Rule | Description |
|---|---|---|---|---|---|
| `store_id` | string | Y |  | FK -> store.store_id | Store reference |
| `sku_id` | string | Y |  | FK -> sku.sku_id | SKU reference |
| `base_daily_rate` | decimal | Y | units/day |  | Underlying rate at history end before seasonality |
| `nb_dispersion_k` | decimal | Y |  |  | Negative-binomial dispersion (low = lumpy) |
| `true_demand_class` | enum | Y | smooth \| erratic \| intermittent \| lumpy \| no_demand |  | Class from latent history demand |
| `mean_true_daily_demand` | decimal | Y | units/day |  | Mean latent demand over sellable days |

### SupplierDisruption (`supplier_disruption`)

Hidden supplier disruption windows that cause clustered delays.

Primary key: `supplier_id, start_date`

| Field | Type | Req. | Allowed / unit | Rule | Description |
|---|---|---|---|---|---|
| `supplier_id` | string | Y |  | FK -> supplier.supplier_id | Supplier reference |
| `start_date` | date | Y |  |  | Start |
| `end_date` | date | Y |  |  | End |
