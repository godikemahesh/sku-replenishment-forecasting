-- Generated from replenishment/schema.py - do not edit by hand.
PRAGMA foreign_keys = ON;

-- SKU: Product master. One row per SKU.
CREATE TABLE sku (
  sku_id TEXT NOT NULL,
  sku_name TEXT NOT NULL,
  category TEXT NOT NULL CHECK (category IN ('Staples','Dairy','Beverages','Snacks','Personal Care','Household','Packaged Food','Frozen')),
  brand TEXT NOT NULL,
  unit_cost REAL NOT NULL,
  unit_price REAL NOT NULL,
  launch_date TEXT NOT NULL,
  discontinue_date TEXT,
  lifecycle_state TEXT NOT NULL CHECK (lifecycle_state IN ('active','new_launch','discontinued')),
  PRIMARY KEY (sku_id)
);

-- Store: Store / dark-store master.
CREATE TABLE store (
  store_id TEXT NOT NULL,
  store_name TEXT NOT NULL,
  city TEXT NOT NULL,
  region TEXT NOT NULL,
  store_format TEXT NOT NULL CHECK (store_format IN ('hypermarket','supermarket','express')),
  open_date TEXT NOT NULL,
  storage_capacity_units INTEGER NOT NULL,
  lifecycle_state TEXT NOT NULL CHECK (lifecycle_state IN ('active','new_store')),
  PRIMARY KEY (store_id)
);

-- Supplier: Supplier master.
CREATE TABLE supplier (
  supplier_id TEXT NOT NULL,
  supplier_name TEXT NOT NULL,
  region TEXT NOT NULL,
  supplier_type TEXT NOT NULL CHECK (supplier_type IN ('local','regional','national')),
  reliability_tier TEXT NOT NULL CHECK (reliability_tier IN ('A','B','C')),
  lifecycle_state TEXT NOT NULL CHECK (lifecycle_state IN ('active','inactive')),
  PRIMARY KEY (supplier_id)
);

-- SupplierSKU: Bridge SKU <-> Supplier (N:M) with ordering constraints and effective dates.
CREATE TABLE supplier_sku (
  supplier_sku_id TEXT NOT NULL,
  supplier_id TEXT NOT NULL,
  sku_id TEXT NOT NULL,
  priority INTEGER NOT NULL,
  pack_size_units INTEGER NOT NULL,
  moq_units INTEGER NOT NULL,
  unit_cost REAL NOT NULL,
  nominal_lead_time_days INTEGER NOT NULL,
  effective_from TEXT NOT NULL,
  effective_to TEXT,
  is_active INTEGER NOT NULL CHECK (is_active IN (0,1)),
  PRIMARY KEY (supplier_sku_id),
  UNIQUE (supplier_id, sku_id),
  FOREIGN KEY (supplier_id) REFERENCES supplier (supplier_id),
  FOREIGN KEY (sku_id) REFERENCES sku (sku_id)
);

-- SKUStoreMatrix: Bridge SKU <-> Store (N:M) assortment matrix: every SKU x store combination.
CREATE TABLE sku_store_matrix (
  sku_id TEXT NOT NULL,
  store_id TEXT NOT NULL,
  assortment_status TEXT NOT NULL CHECK (assortment_status IN ('ranged','phase_out','delisted','not_ranged')),
  ranged_from TEXT,
  ranged_to TEXT,
  PRIMARY KEY (sku_id, store_id),
  FOREIGN KEY (sku_id) REFERENCES sku (sku_id),
  FOREIGN KEY (store_id) REFERENCES store (store_id)
);

-- SalesHistory: Observed daily sales per SKU-store. Rows exist only on days the SKU was sellable.
CREATE TABLE sales_history (
  sale_id TEXT NOT NULL,
  store_id TEXT NOT NULL,
  sku_id TEXT NOT NULL,
  sale_date TEXT NOT NULL,
  day_of_week TEXT NOT NULL CHECK (day_of_week IN ('Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday')),
  units_sold INTEGER NOT NULL,
  unit_price_paid REAL NOT NULL,
  promotion_flag INTEGER NOT NULL CHECK (promotion_flag IN (0,1)),
  promo_discount_pct REAL NOT NULL,
  availability_status TEXT NOT NULL CHECK (availability_status IN ('in_stock','sold_out_during_day','out_of_stock_all_day')),
  stockout_flag INTEGER NOT NULL CHECK (stockout_flag IN (0,1)),
  PRIMARY KEY (sale_id),
  UNIQUE (store_id, sku_id, sale_date),
  FOREIGN KEY (store_id) REFERENCES store (store_id),
  FOREIGN KEY (sku_id) REFERENCES sku (sku_id),
  FOREIGN KEY (sku_id, store_id) REFERENCES sku_store_matrix (sku_id, store_id)
);

-- InventorySnapshot: End-of-day stock ledger per SKU-store; one row per sellable day.
CREATE TABLE inventory_snapshot (
  inventory_snapshot_id TEXT NOT NULL,
  store_id TEXT NOT NULL,
  sku_id TEXT NOT NULL,
  snapshot_date TEXT NOT NULL,
  opening_on_hand_units INTEGER NOT NULL,
  receipts_units INTEGER NOT NULL,
  units_sold INTEGER NOT NULL,
  adjustment_units INTEGER NOT NULL,
  closing_on_hand_units INTEGER NOT NULL,
  on_order_units INTEGER NOT NULL,
  stockout_flag INTEGER NOT NULL CHECK (stockout_flag IN (0,1)),
  PRIMARY KEY (inventory_snapshot_id),
  UNIQUE (store_id, sku_id, snapshot_date),
  FOREIGN KEY (store_id) REFERENCES store (store_id),
  FOREIGN KEY (sku_id) REFERENCES sku (sku_id),
  FOREIGN KEY (sku_id, store_id) REFERENCES sku_store_matrix (sku_id, store_id)
);

-- PurchaseOrder: Historical replenishment orders placed by the legacy policy (state lives in purchase_order_event).
CREATE TABLE purchase_order (
  po_id TEXT NOT NULL,
  store_id TEXT NOT NULL,
  sku_id TEXT NOT NULL,
  supplier_id TEXT NOT NULL,
  supplier_sku_id TEXT NOT NULL,
  order_reason TEXT NOT NULL CHECK (order_reason IN ('replenishment','launch_allocation')),
  order_date TEXT NOT NULL,
  order_qty_units INTEGER NOT NULL,
  order_value REAL NOT NULL,
  expected_receipt_date TEXT NOT NULL,
  actual_receipt_date TEXT,
  po_status TEXT NOT NULL CHECK (po_status IN ('open','received')),
  PRIMARY KEY (po_id),
  FOREIGN KEY (store_id) REFERENCES store (store_id),
  FOREIGN KEY (sku_id) REFERENCES sku (sku_id),
  FOREIGN KEY (supplier_id) REFERENCES supplier (supplier_id),
  FOREIGN KEY (supplier_sku_id) REFERENCES supplier_sku (supplier_sku_id)
);

-- PurchaseOrderEvent: Append-only PO lifecycle history: CREATED -> [DELAYED] -> RECEIVED.
CREATE TABLE purchase_order_event (
  event_id TEXT NOT NULL,
  po_id TEXT NOT NULL,
  event_type TEXT NOT NULL CHECK (event_type IN ('CREATED','DELAYED','RECEIVED')),
  event_date TEXT NOT NULL,
  quantity_units INTEGER NOT NULL,
  PRIMARY KEY (event_id),
  FOREIGN KEY (po_id) REFERENCES purchase_order (po_id)
);

-- LeadTimeObservation: One observed lead time per received purchase order (1:1 with received POs).
CREATE TABLE lead_time_observation (
  lead_time_observation_id TEXT NOT NULL,
  po_id TEXT NOT NULL,
  supplier_id TEXT NOT NULL,
  sku_id TEXT NOT NULL,
  store_id TEXT NOT NULL,
  order_date TEXT NOT NULL,
  receipt_date TEXT NOT NULL,
  lead_time_days INTEGER NOT NULL,
  nominal_lead_time_days INTEGER NOT NULL,
  delay_days INTEGER NOT NULL,
  PRIMARY KEY (lead_time_observation_id),
  FOREIGN KEY (po_id) REFERENCES purchase_order (po_id),
  FOREIGN KEY (supplier_id) REFERENCES supplier (supplier_id),
  FOREIGN KEY (sku_id) REFERENCES sku (sku_id),
  FOREIGN KEY (store_id) REFERENCES store (store_id)
);

-- Forecast: Demand forecasts. backtest = temporal holdout inside history; production = next month.
CREATE TABLE forecast (
  forecast_id TEXT NOT NULL,
  store_id TEXT NOT NULL,
  sku_id TEXT NOT NULL,
  forecast_type TEXT NOT NULL CHECK (forecast_type IN ('backtest','production')),
  forecast_month TEXT NOT NULL,
  forecast_origin_date TEXT NOT NULL,
  training_window_start_date TEXT NOT NULL,
  training_window_end_date TEXT NOT NULL,
  horizon_start_date TEXT NOT NULL,
  horizon_end_date TEXT NOT NULL,
  horizon_days INTEGER NOT NULL,
  method TEXT NOT NULL CHECK (method IN ('censored_ewma_dow','croston_sba','new_item_shrinkage','censored_prior','zero_demand')),
  demand_class TEXT NOT NULL CHECK (demand_class IN ('smooth','erratic','intermittent','lumpy','new_item','no_clean_history')),
  train_days_used INTEGER NOT NULL,
  stockout_days_excluded INTEGER NOT NULL,
  promo_days_excluded INTEGER NOT NULL,
  seasonal_index REAL NOT NULL,
  predicted_daily_units REAL NOT NULL,
  predicted_daily_sd_units REAL NOT NULL,
  predicted_horizon_units REAL NOT NULL,
  PRIMARY KEY (forecast_id),
  FOREIGN KEY (store_id) REFERENCES store (store_id),
  FOREIGN KEY (sku_id) REFERENCES sku (sku_id)
);

-- ReorderPolicy: Periodic-review order-up-to policy per SKU-store, valid for the forecast month.
CREATE TABLE reorder_policy (
  reorder_policy_id TEXT NOT NULL,
  store_id TEXT NOT NULL,
  sku_id TEXT NOT NULL,
  forecast_id TEXT NOT NULL,
  supplier_sku_id TEXT,
  effective_from TEXT NOT NULL,
  effective_to TEXT NOT NULL,
  abc_class TEXT NOT NULL CHECK (abc_class IN ('A','B','C')),
  service_level_target REAL NOT NULL,
  z_value REAL NOT NULL,
  review_period_days INTEGER NOT NULL,
  lead_time_source TEXT NOT NULL CHECK (lead_time_source IN ('supplier_sku_history','supplier_history','nominal_default','none')),
  lead_time_mean_days REAL NOT NULL,
  lead_time_sd_days REAL NOT NULL,
  lead_time_obs_used INTEGER NOT NULL,
  lead_time_obs_rejected INTEGER NOT NULL,
  demand_daily_units REAL NOT NULL,
  demand_daily_sd_units REAL NOT NULL,
  safety_stock_units INTEGER NOT NULL,
  reorder_point_units INTEGER NOT NULL,
  order_up_to_units INTEGER NOT NULL,
  policy_status TEXT NOT NULL CHECK (policy_status IN ('active','blocked_discontinued','blocked_no_active_supplier')),
  PRIMARY KEY (reorder_policy_id),
  FOREIGN KEY (store_id) REFERENCES store (store_id),
  FOREIGN KEY (sku_id) REFERENCES sku (sku_id),
  FOREIGN KEY (forecast_id) REFERENCES forecast (forecast_id),
  FOREIGN KEY (supplier_sku_id) REFERENCES supplier_sku (supplier_sku_id)
);

-- PurchaseRecommendation: One replenishment decision per sellable SKU-store on the recommendation date; ID is the reserved PO number.
CREATE TABLE purchase_recommendation (
  purchase_recommendation_id TEXT NOT NULL,
  reorder_policy_id TEXT NOT NULL,
  forecast_id TEXT NOT NULL,
  store_id TEXT NOT NULL,
  sku_id TEXT NOT NULL,
  supplier_id TEXT,
  supplier_sku_id TEXT,
  recommendation_date TEXT NOT NULL,
  on_hand_units INTEGER NOT NULL,
  on_order_units INTEGER NOT NULL,
  inventory_position_units INTEGER NOT NULL,
  reorder_point_units INTEGER NOT NULL,
  order_up_to_units INTEGER NOT NULL,
  days_of_cover REAL NOT NULL,
  reorder_triggered INTEGER NOT NULL CHECK (reorder_triggered IN (0,1)),
  raw_need_units INTEGER NOT NULL,
  moq_units INTEGER,
  pack_size_units INTEGER,
  qty_after_moq_pack_units INTEGER NOT NULL,
  capacity_room_units INTEGER NOT NULL,
  recommended_order_qty_units INTEGER NOT NULL,
  order_value REAL NOT NULL,
  expected_receipt_date TEXT,
  recommendation_status TEXT NOT NULL CHECK (recommendation_status IN ('order_recommended','capacity_reduced','capacity_exception','no_order_needed','blocked_discontinued','blocked_no_active_supplier')),
  manual_review_flag INTEGER NOT NULL CHECK (manual_review_flag IN (0,1)),
  rule_trace TEXT NOT NULL,
  PRIMARY KEY (purchase_recommendation_id),
  FOREIGN KEY (store_id) REFERENCES store (store_id),
  FOREIGN KEY (sku_id) REFERENCES sku (sku_id),
  FOREIGN KEY (reorder_policy_id) REFERENCES reorder_policy (reorder_policy_id),
  FOREIGN KEY (forecast_id) REFERENCES forecast (forecast_id),
  FOREIGN KEY (supplier_id) REFERENCES supplier (supplier_id),
  FOREIGN KEY (supplier_sku_id) REFERENCES supplier_sku (supplier_sku_id)
);

-- AuditException: Exceptions raised by the audit gate (rules only - never reads ground truth).
CREATE TABLE audit_exception (
  exception_id TEXT NOT NULL,
  exception_category TEXT NOT NULL CHECK (exception_category IN ('data_defect','operational')),
  rule_id TEXT NOT NULL,
  entity_type TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  store_id TEXT,
  sku_id TEXT,
  severity TEXT NOT NULL CHECK (severity IN ('high','medium','low')),
  action TEXT NOT NULL CHECK (action IN ('quarantine','block','manual_review','expedite')),
  detail TEXT NOT NULL,
  PRIMARY KEY (exception_id)
);

-- GroundTruth: Labels for every known anomaly: natural stockout censoring and injected defects.
CREATE TABLE ground_truth (
  ground_truth_id TEXT NOT NULL,
  entity_type TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  issue_type TEXT NOT NULL CHECK (issue_type IN ('stockout_censored_demand','negative_lead_time','forecast_future_leakage','forecast_stockout_as_zero_demand','reorder_qty_ignores_pack_size','discontinued_sku_recommended')),
  origin TEXT NOT NULL CHECK (origin IN ('natural','injected')),
  expected_behavior TEXT NOT NULL,
  observed_value REAL,
  expected_value REAL,
  severity TEXT NOT NULL CHECK (severity IN ('high','medium','low')),
  PRIMARY KEY (ground_truth_id)
);

-- DemandActual: True (unconstrained) daily demand for the forecast month - used only to score production forecasts.
CREATE TABLE demand_actuals (
  store_id TEXT NOT NULL,
  sku_id TEXT NOT NULL,
  demand_date TEXT NOT NULL,
  true_demand_units INTEGER NOT NULL,
  PRIMARY KEY (store_id, sku_id, demand_date)
);

-- DemandProfile: Hidden generator parameters per SKU-store (for evaluation by true demand class).
CREATE TABLE demand_profile (
  store_id TEXT NOT NULL,
  sku_id TEXT NOT NULL,
  base_daily_rate REAL NOT NULL,
  nb_dispersion_k REAL NOT NULL,
  true_demand_class TEXT NOT NULL CHECK (true_demand_class IN ('smooth','erratic','intermittent','lumpy','no_demand')),
  mean_true_daily_demand REAL NOT NULL,
  PRIMARY KEY (store_id, sku_id)
);

-- SupplierDisruption: Hidden supplier disruption windows that cause clustered delays.
CREATE TABLE supplier_disruption (
  supplier_id TEXT NOT NULL,
  start_date TEXT NOT NULL,
  end_date TEXT NOT NULL,
  PRIMARY KEY (supplier_id, start_date)
);
