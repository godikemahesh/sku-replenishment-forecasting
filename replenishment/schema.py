"""Single source of truth for every table: fields, keys, relationships and rules.

The data dictionary (CSV + Markdown), the SQLite DDL and most validation checks are
all generated from this module, so documentation can never drift from the data.
"""
from __future__ import annotations

from dataclasses import dataclass, field

CATEGORIES = ["Staples", "Dairy", "Beverages", "Snacks", "Personal Care", "Household",
              "Packaged Food", "Frozen"]
SKU_LIFECYCLE = ["active", "new_launch", "discontinued"]
STORE_FORMATS = ["hypermarket", "supermarket", "express"]
STORE_LIFECYCLE = ["active", "new_store"]
SUPPLIER_TYPES = ["local", "regional", "national"]
RELIABILITY_TIERS = ["A", "B", "C"]
ASSORTMENT_STATUS = ["ranged", "phase_out", "delisted", "not_ranged"]
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
AVAILABILITY = ["in_stock", "sold_out_during_day", "out_of_stock_all_day"]
PO_STATUS = ["open", "received"]
PO_REASON = ["replenishment", "launch_allocation"]
PO_EVENTS = ["CREATED", "DELAYED", "RECEIVED"]
FORECAST_TYPES = ["backtest", "production"]
FORECAST_METHODS = ["censored_ewma_dow", "croston_sba", "new_item_shrinkage", "censored_prior", "zero_demand"]
DEMAND_CLASSES = ["smooth", "erratic", "intermittent", "lumpy", "new_item", "no_clean_history"]
ABC = ["A", "B", "C"]
LT_SOURCES = ["supplier_sku_history", "supplier_history", "nominal_default", "none"]
POLICY_STATUS = ["active", "blocked_discontinued", "blocked_no_active_supplier"]
REC_STATUS = ["order_recommended", "capacity_reduced", "capacity_exception", "no_order_needed",
              "blocked_discontinued", "blocked_no_active_supplier"]
ISSUE_TYPES = ["stockout_censored_demand", "negative_lead_time", "forecast_future_leakage",
               "forecast_stockout_as_zero_demand", "reorder_qty_ignores_pack_size",
               "discontinued_sku_recommended"]
EXC_CATEGORY = ["data_defect", "operational"]
SEVERITY = ["high", "medium", "low"]
EXC_ACTIONS = ["quarantine", "block", "manual_review", "expedite"]


@dataclass
class Col:
    name: str
    type: str                       # string | integer | decimal | date | boolean | enum
    required: bool = True
    allowed: list | None = None     # enum values
    unit: str = ""
    unique: bool = False
    rule: str = ""
    desc: str = ""
    pattern: str | None = None      # regex for IDs


@dataclass
class FK:
    cols: tuple
    ref_table: str
    ref_cols: tuple
    cardinality: str                # e.g. "N:1"


@dataclass
class Table:
    name: str
    entity: str
    layer: str                      # input | output | ground_truth
    pk: tuple
    desc: str
    cols: list
    fks: list = field(default_factory=list)
    natural_key: tuple | None = None

    def col(self, name: str) -> Col:
        return next(c for c in self.cols if c.name == name)


def C(name, type_, required=True, **kw) -> Col:
    return Col(name, type_, required, **kw)


SKU_ID = C("sku_id", "string", pattern=r"^SKU-\d{6}$", rule="FK -> sku.sku_id", desc="SKU reference")
STORE_ID = C("store_id", "string", pattern=r"^STORE-\d{6}$", rule="FK -> store.store_id", desc="Store reference")
SUPPLIER_ID = C("supplier_id", "string", pattern=r"^SUP-\d{4}$", rule="FK -> supplier.supplier_id", desc="Supplier reference")

TABLES: dict[str, Table] = {}


def _add(t: Table):
    TABLES[t.name] = t


# ---------------------------------------------------------------- master data
_add(Table("sku", "SKU", "input", ("sku_id",), "Product master. One row per SKU.", [
    C("sku_id", "string", unique=True, pattern=r"^SKU-\d{6}$", rule="unique; SKU-######; immutable", desc="Primary key"),
    C("sku_name", "string", rule="non-empty", desc="Display name (synthetic brand + product + size)"),
    C("category", "enum", allowed=CATEGORIES, desc="Merchandise category; drives seasonality"),
    C("brand", "string", desc="Synthetic brand name"),
    C("unit_cost", "decimal", unit="INR", rule="> 0", desc="Standard cost per unit"),
    C("unit_price", "decimal", unit="INR", rule="> unit_cost", desc="Regular shelf price per unit"),
    C("launch_date", "date", rule="ISO date", desc="First day the SKU may be sold"),
    C("discontinue_date", "date", required=False, rule="null or >= launch_date", desc="Last day replenishment is allowed"),
    C("lifecycle_state", "enum", allowed=SKU_LIFECYCLE,
      rule="discontinued if discontinue_date <= history_end; new_launch if launched in last 90 days", desc="State as of history end"),
]))

_add(Table("store", "Store", "input", ("store_id",), "Store / dark-store master.", [
    C("store_id", "string", unique=True, pattern=r"^STORE-\d{6}$", rule="unique; STORE-######", desc="Primary key"),
    C("store_name", "string", rule="non-empty", desc="Display name"),
    C("city", "string", desc="City"),
    C("region", "string", desc="State / region"),
    C("store_format", "enum", allowed=STORE_FORMATS, desc="Format; drives size and assortment breadth"),
    C("open_date", "date", desc="Store opening date; no sales before it"),
    C("storage_capacity_units", "integer", unit="units", rule="> 0",
      desc="Max units on hand + on order the store can hold (capacity constraint)"),
    C("lifecycle_state", "enum", allowed=STORE_LIFECYCLE, desc="new_store if opened in last 90 days of history"),
]))

_add(Table("supplier", "Supplier", "input", ("supplier_id",), "Supplier master.", [
    C("supplier_id", "string", unique=True, pattern=r"^SUP-\d{4}$", rule="unique; SUP-####", desc="Primary key"),
    C("supplier_name", "string", desc="Synthetic supplier name"),
    C("region", "string", desc="Supplier home region"),
    C("supplier_type", "enum", allowed=SUPPLIER_TYPES, desc="local (2-3d), regional (4-6d), national (7-12d) base lead time"),
    C("reliability_tier", "enum", allowed=RELIABILITY_TIERS, desc="Contracted tier; C suppliers are delayed most often"),
    C("lifecycle_state", "enum", allowed=["active", "inactive"], desc="Supplier status"),
]))

_add(Table("supplier_sku", "SupplierSKU", "input", ("supplier_sku_id",),
           "Bridge SKU <-> Supplier (N:M) with ordering constraints and effective dates.", [
    C("supplier_sku_id", "string", unique=True, pattern=r"^SS-\d{6}$", rule="unique", desc="Primary key"),
    SUPPLIER_ID, SKU_ID,
    C("priority", "integer", allowed=None, rule="1 = primary, 2 = backup", desc="Sourcing priority"),
    C("pack_size_units", "integer", unit="units", rule="> 0", desc="Orders must be a multiple of this"),
    C("moq_units", "integer", unit="units", rule="> 0 and multiple of pack_size_units", desc="Minimum order quantity"),
    C("unit_cost", "decimal", unit="INR", rule="> 0", desc="Purchase cost per unit from this supplier"),
    C("nominal_lead_time_days", "integer", unit="days", rule=">= 1", desc="Contracted lead time"),
    C("effective_from", "date", desc="Contract start"),
    C("effective_to", "date", required=False, rule="null or >= effective_from", desc="Contract end (null = open-ended)"),
    C("is_active", "boolean", desc="Contract active on the recommendation date"),
], [FK(("supplier_id",), "supplier", ("supplier_id",), "N:1"), FK(("sku_id",), "sku", ("sku_id",), "N:1")],
    natural_key=("supplier_id", "sku_id")))

_add(Table("sku_store_matrix", "SKUStoreMatrix", "input", ("sku_id", "store_id"),
           "Bridge SKU <-> Store (N:M) assortment matrix: every SKU x store combination.", [
    SKU_ID, STORE_ID,
    C("assortment_status", "enum", allowed=ASSORTMENT_STATUS,
      rule="ranged -> phase_out (SKU discontinued, stock remains) -> delisted (stock sold through)", desc="Assortment lifecycle"),
    C("ranged_from", "date", required=False, rule="null iff not_ranged; >= launch_date and >= store open_date", desc="First sellable day"),
    C("ranged_to", "date", required=False, rule="set only when delisted", desc="Last sellable day"),
], [FK(("sku_id",), "sku", ("sku_id",), "N:1"), FK(("store_id",), "store", ("store_id",), "N:1")]))

# ------------------------------------------------------------ transactions
_PAIR_FKS = [FK(("store_id",), "store", ("store_id",), "N:1"), FK(("sku_id",), "sku", ("sku_id",), "N:1"),
             FK(("sku_id", "store_id"), "sku_store_matrix", ("sku_id", "store_id"), "N:1")]

_add(Table("sales_history", "SalesHistory", "input", ("sale_id",),
           "Observed daily sales per SKU-store. Rows exist only on days the SKU was sellable.", [
    C("sale_id", "string", unique=True, pattern=r"^SALE-STORE-\d{6}-SKU-\d{6}-\d{8}$",
      rule="SALE-<store_id>-<sku_id>-<yyyymmdd>", desc="Primary key"),
    STORE_ID, SKU_ID,
    C("sale_date", "date", rule="ranged_from <= sale_date <= history_end", desc="Business day"),
    C("day_of_week", "enum", allowed=DAYS, desc="Derived from sale_date"),
    C("units_sold", "integer", unit="units", rule=">= 0; = inventory_snapshot.units_sold",
      desc="OBSERVED sales. Censored (understates demand) when stockout_flag is true"),
    C("unit_price_paid", "decimal", unit="INR", rule="unit_price x (1 - promo_discount_pct)", desc="Average selling price"),
    C("promotion_flag", "boolean", desc="Chain promotion running at this store"),
    C("promo_discount_pct", "decimal", unit="fraction", rule="0 when no promotion, else 0.10-0.30", desc="Promotion discount"),
    C("availability_status", "enum", allowed=AVAILABILITY,
      rule="out_of_stock_all_day if opening+receipts = 0; sold_out_during_day if closing = 0; else in_stock",
      desc="Separates stockouts from genuine zero demand"),
    C("stockout_flag", "boolean", rule="availability_status != in_stock",
      desc="Sales may be censored by availability - do NOT read as zero demand"),
], _PAIR_FKS, natural_key=("store_id", "sku_id", "sale_date")))

_add(Table("inventory_snapshot", "InventorySnapshot", "input", ("inventory_snapshot_id",),
           "End-of-day stock ledger per SKU-store; one row per sellable day.", [
    C("inventory_snapshot_id", "string", unique=True, pattern=r"^INV-STORE-\d{6}-SKU-\d{6}-\d{8}$",
      rule="INV-<store_id>-<sku_id>-<yyyymmdd>", desc="Primary key"),
    STORE_ID, SKU_ID,
    C("snapshot_date", "date", desc="Business day"),
    C("opening_on_hand_units", "integer", unit="units", rule=">= 0; = previous day closing", desc="Start-of-day stock"),
    C("receipts_units", "integer", unit="units", rule=">= 0; = sum(purchase_order received that day)", desc="Goods received"),
    C("units_sold", "integer", unit="units", rule=">= 0", desc="Units sold"),
    C("adjustment_units", "integer", unit="units", rule="<= 0 (shrinkage / damage)", desc="Stock adjustment"),
    C("closing_on_hand_units", "integer", unit="units", rule="= opening + receipts - sold + adjustment; >= 0", desc="End-of-day stock"),
    C("on_order_units", "integer", unit="units", rule="= sum(open purchase_order qty) at end of day", desc="Inbound pipeline"),
    C("stockout_flag", "boolean", rule="closing_on_hand_units = 0", desc="Shelf empty at some point in the day"),
], _PAIR_FKS, natural_key=("store_id", "sku_id", "snapshot_date")))

_add(Table("purchase_order", "PurchaseOrder", "input", ("po_id",),
           "Historical replenishment orders placed by the legacy policy (state lives in purchase_order_event).", [
    C("po_id", "string", unique=True, pattern=r"^PO-\d{4}-\d{6,7}$",
      rule="PO-<order year>-<counter, zero-padded to 6 digits; 7 once a year exceeds 999,999 orders>", desc="Primary key"),
    STORE_ID, SKU_ID, SUPPLIER_ID,
    C("supplier_sku_id", "string", rule="FK -> supplier_sku", desc="Contract used"),
    C("order_reason", "enum", allowed=PO_REASON, desc="launch_allocation = first fill of a new SKU/store"),
    C("order_date", "date", desc="Date the order was placed"),
    C("order_qty_units", "integer", unit="units", rule="> 0; multiple of pack size; >= MOQ", desc="Ordered units"),
    C("order_value", "decimal", unit="INR", rule="order_qty_units x supplier_sku.unit_cost", desc="Order value"),
    C("expected_receipt_date", "date", rule="order_date + nominal_lead_time_days", desc="Promised date"),
    C("actual_receipt_date", "date", required=False, rule="null iff po_status = open; >= order_date", desc="Delivery date"),
    C("po_status", "enum", allowed=PO_STATUS, desc="Current status (derived from latest event)"),
], [FK(("store_id",), "store", ("store_id",), "N:1"), FK(("sku_id",), "sku", ("sku_id",), "N:1"),
    FK(("supplier_id",), "supplier", ("supplier_id",), "N:1"),
    FK(("supplier_sku_id",), "supplier_sku", ("supplier_sku_id",), "N:1")]))

_add(Table("purchase_order_event", "PurchaseOrderEvent", "input", ("event_id",),
           "Append-only PO lifecycle history: CREATED -> [DELAYED] -> RECEIVED.", [
    C("event_id", "string", unique=True, rule="<po_id>-EV<n>", desc="Primary key"),
    C("po_id", "string", rule="FK -> purchase_order.po_id", desc="Order reference"),
    C("event_type", "enum", allowed=PO_EVENTS, desc="Lifecycle transition"),
    C("event_date", "date", rule="non-decreasing within a po_id", desc="When the transition happened"),
    C("quantity_units", "integer", unit="units", rule=">= 0", desc="Units affected"),
], [FK(("po_id",), "purchase_order", ("po_id",), "N:1")]))

_add(Table("lead_time_observation", "LeadTimeObservation", "input", ("lead_time_observation_id",),
           "One observed lead time per received purchase order (1:1 with received POs).", [
    C("lead_time_observation_id", "string", unique=True, pattern=r"^LTO-\d{4}-\d{6,7}$", rule="LTO-<po number>", desc="Primary key"),
    C("po_id", "string", unique=True, rule="FK -> purchase_order.po_id", desc="Order the observation came from"),
    SUPPLIER_ID, SKU_ID, STORE_ID,
    C("order_date", "date", desc="Order date"),
    C("receipt_date", "date", rule="= order_date + lead_time_days", desc="Recorded receipt date"),
    C("lead_time_days", "integer", unit="days", rule=">= 0 (negative values are invalid and must be quarantined)", desc="Observed lead time"),
    C("nominal_lead_time_days", "integer", unit="days", desc="Contracted lead time at order time"),
    C("delay_days", "integer", unit="days", rule="= lead_time_days - nominal_lead_time_days", desc="Lateness (negative = early)"),
], [FK(("po_id",), "purchase_order", ("po_id",), "1:1"), FK(("supplier_id",), "supplier", ("supplier_id",), "N:1"),
    FK(("sku_id",), "sku", ("sku_id",), "N:1"), FK(("store_id",), "store", ("store_id",), "N:1")]))

# ------------------------------------------------------------ engine outputs
_add(Table("forecast", "Forecast", "output", ("forecast_id",),
           "Demand forecasts. backtest = temporal holdout inside history; production = next month.", [
    C("forecast_id", "string", unique=True, pattern=r"^FC-STORE-\d{6}-SKU-\d{6}-\d{4}-\d{2}$",
      rule="FC-<store_id>-<sku_id>-<yyyy-mm of horizon start>", desc="Primary key"),
    STORE_ID, SKU_ID,
    C("forecast_type", "enum", allowed=FORECAST_TYPES, desc="backtest (evaluated on holdout) or production"),
    C("forecast_month", "string", rule="YYYY-MM", desc="Month of horizon start"),
    C("forecast_origin_date", "date", desc="Last date of information available to the model"),
    C("training_window_start_date", "date", desc="First training day"),
    C("training_window_end_date", "date", rule="<= forecast_origin_date (else future leakage)", desc="Last training day"),
    C("horizon_start_date", "date", rule="= origin + 1", desc="First forecast day"),
    C("horizon_end_date", "date", rule=">= horizon_start_date", desc="Last forecast day"),
    C("horizon_days", "integer", unit="days", desc="Horizon length"),
    C("method", "enum", allowed=FORECAST_METHODS, desc="Model chosen by demand class"),
    C("demand_class", "enum", allowed=DEMAND_CLASSES, desc="Syntetos-Boylan class from clean (uncensored) history"),
    C("train_days_used", "integer", unit="days", desc="Clean days used to fit"),
    C("stockout_days_excluded", "integer", unit="days", rule="= stockout days in training window", desc="Censored days excluded"),
    C("promo_days_excluded", "integer", unit="days", desc="Promotion days excluded from the baseline"),
    C("seasonal_index", "decimal", rule="0.6-1.6; 1.0 when target month not in training data", desc="Category month index"),
    C("predicted_daily_units", "decimal", unit="units/day", rule=">= 0", desc="Mean daily demand"),
    C("predicted_daily_sd_units", "decimal", unit="units/day", rule=">= 0", desc="Daily demand standard deviation"),
    C("predicted_horizon_units", "decimal", unit="units", rule=">= 0", desc="Total demand over horizon (day-of-week weighted)"),
], _PAIR_FKS[:2]))

_add(Table("reorder_policy", "ReorderPolicy", "output", ("reorder_policy_id",),
           "Periodic-review order-up-to policy per SKU-store, valid for the forecast month.", [
    C("reorder_policy_id", "string", unique=True, rule="RP-<store_id>-<sku_id>-<yyyy-mm>", desc="Primary key"),
    STORE_ID, SKU_ID,
    C("forecast_id", "string", rule="FK -> forecast (production)", desc="Demand input"),
    C("supplier_sku_id", "string", required=False, rule="FK -> supplier_sku; null if no active contract", desc="Contract chosen"),
    C("effective_from", "date", desc="Policy start"),
    C("effective_to", "date", rule=">= effective_from", desc="Policy end"),
    C("abc_class", "enum", allowed=ABC, desc="Store-level revenue class (80/15/5)"),
    C("service_level_target", "decimal", rule="A 0.98, B 0.95, C 0.90", desc="Cycle service level"),
    C("z_value", "decimal", desc="Normal quantile of service level"),
    C("review_period_days", "integer", unit="days", desc="R"),
    C("lead_time_source", "enum", allowed=LT_SOURCES, desc="Which history the lead-time estimate came from"),
    C("lead_time_mean_days", "decimal", unit="days", rule=">= 0", desc="L"),
    C("lead_time_sd_days", "decimal", unit="days", rule=">= 0", desc="sigma_L"),
    C("lead_time_obs_used", "integer", desc="Valid observations used"),
    C("lead_time_obs_rejected", "integer", desc="Invalid (negative) observations quarantined"),
    C("demand_daily_units", "decimal", unit="units/day", desc="d"),
    C("demand_daily_sd_units", "decimal", unit="units/day", desc="sigma_d"),
    C("safety_stock_units", "integer", unit="units", rule="ceil(z*sqrt((L+R)*sd^2 + d^2*sL^2))", desc="Safety stock"),
    C("reorder_point_units", "integer", unit="units", rule="ceil(d*L) + SS", desc="Order when inventory position <= this"),
    C("order_up_to_units", "integer", unit="units", rule="ceil(d*(L+R)) + SS", desc="Target inventory position"),
    C("policy_status", "enum", allowed=POLICY_STATUS, desc="Whether ordering is allowed"),
], _PAIR_FKS[:2] + [FK(("forecast_id",), "forecast", ("forecast_id",), "1:1"),
                    FK(("supplier_sku_id",), "supplier_sku", ("supplier_sku_id",), "N:1")]))

_add(Table("purchase_recommendation", "PurchaseRecommendation", "output", ("purchase_recommendation_id",),
           "One replenishment decision per sellable SKU-store on the recommendation date; ID is the reserved PO number.", [
    C("purchase_recommendation_id", "string", unique=True, pattern=r"^PO-\d{4}-\d{6}$", rule="PO-2026-######", desc="Primary key"),
    C("reorder_policy_id", "string", rule="FK -> reorder_policy", desc="Policy applied"),
    C("forecast_id", "string", rule="FK -> forecast", desc="Forecast used"),
    STORE_ID, SKU_ID,
    C("supplier_id", "string", required=False, rule="FK -> supplier", desc="Supplier to order from"),
    C("supplier_sku_id", "string", required=False, rule="FK -> supplier_sku", desc="Contract to order on"),
    C("recommendation_date", "date", desc="Decision date"),
    C("on_hand_units", "integer", unit="units", rule="= snapshot closing on history end", desc="Stock on hand"),
    C("on_order_units", "integer", unit="units", rule="= snapshot on_order on history end", desc="Open POs"),
    C("inventory_position_units", "integer", unit="units", rule="on_hand + on_order", desc="IP"),
    C("reorder_point_units", "integer", unit="units", desc="From policy"),
    C("order_up_to_units", "integer", unit="units", desc="From policy"),
    C("days_of_cover", "decimal", unit="days", rule="IP / demand_daily_units", desc="Priority for capacity allocation"),
    C("reorder_triggered", "boolean", rule="IP <= reorder point", desc="Whether the policy fires"),
    C("raw_need_units", "integer", unit="units", rule="max(0, order_up_to - IP) when triggered", desc="Unconstrained need"),
    C("moq_units", "integer", required=False, unit="units", desc="MOQ applied"),
    C("pack_size_units", "integer", required=False, unit="units", desc="Pack size applied"),
    C("qty_after_moq_pack_units", "integer", unit="units", rule="ceil(max(need, MOQ)/pack)*pack", desc="Before capacity"),
    C("capacity_room_units", "integer", unit="units", rule="store room remaining before this line", desc="Capacity check input"),
    C("recommended_order_qty_units", "integer", unit="units",
      rule=">= 0; if > 0: multiple of pack, >= MOQ, fits capacity", desc="Final quantity"),
    C("order_value", "decimal", unit="INR", desc="qty x unit_cost"),
    C("expected_receipt_date", "date", required=False, rule="recommendation_date + ceil(L) when qty > 0", desc="ETA"),
    C("recommendation_status", "enum", allowed=REC_STATUS, desc="Decision"),
    C("manual_review_flag", "boolean", desc="Planner should review (new item, no clean history, LT fallback)"),
    C("rule_trace", "string", desc="Ordered list of rules applied - full decision traceability"),
], _PAIR_FKS[:2] + [FK(("reorder_policy_id",), "reorder_policy", ("reorder_policy_id",), "1:1"),
                    FK(("forecast_id",), "forecast", ("forecast_id",), "1:1"),
                    FK(("supplier_id",), "supplier", ("supplier_id",), "N:1"),
                    FK(("supplier_sku_id",), "supplier_sku", ("supplier_sku_id",), "N:1")]))

_add(Table("audit_exception", "AuditException", "output", ("exception_id",),
           "Exceptions raised by the audit gate (rules only - never reads ground truth).", [
    C("exception_id", "string", unique=True, rule="EXC-########", desc="Primary key"),
    C("exception_category", "enum", allowed=EXC_CATEGORY, desc="data_defect = invalid record; operational = decision needing attention"),
    C("rule_id", "string", desc="Audit rule that fired (see README)"),
    C("entity_type", "string", desc="Table of the offending record"),
    C("entity_id", "string", desc="Primary key of the offending record"),
    C("store_id", "string", required=False, desc="Store, when applicable"),
    C("sku_id", "string", required=False, desc="SKU, when applicable"),
    C("severity", "enum", allowed=SEVERITY, desc="Impact"),
    C("action", "enum", allowed=EXC_ACTIONS, desc="What the workflow does with the record"),
    C("detail", "string", desc="Human-readable explanation with values"),
]))

# ------------------------------------------------------------ ground truth (never an input)
_add(Table("ground_truth", "GroundTruth", "ground_truth", ("ground_truth_id",),
           "Labels for every known anomaly: natural stockout censoring and injected defects.", [
    C("ground_truth_id", "string", unique=True, rule="GT-########", desc="Primary key"),
    C("entity_type", "string", desc="Table containing the labelled record"),
    C("entity_id", "string", desc="Primary key of the labelled record"),
    C("issue_type", "enum", allowed=ISSUE_TYPES, desc="Anomaly label"),
    C("origin", "enum", allowed=["natural", "injected"], desc="natural = arises from simulation; injected = deliberate defect"),
    C("expected_behavior", "string", desc="What a correct system must do"),
    C("observed_value", "decimal", required=False, desc="Value present in the dataset"),
    C("expected_value", "decimal", required=False, desc="Correct value (e.g. latent demand, clean quantity)"),
    C("severity", "enum", allowed=SEVERITY, desc="Impact"),
]))

_add(Table("demand_actuals", "DemandActual", "ground_truth", ("store_id", "sku_id", "demand_date"),
           "True (unconstrained) daily demand for the forecast month - used only to score production forecasts.", [
    STORE_ID, SKU_ID, C("demand_date", "date", desc="Day in forecast month"),
    C("true_demand_units", "integer", unit="units", rule=">= 0", desc="Latent demand"),
]))

_add(Table("demand_profile", "DemandProfile", "ground_truth", ("store_id", "sku_id"),
           "Hidden generator parameters per SKU-store (for evaluation by true demand class).", [
    STORE_ID, SKU_ID,
    C("base_daily_rate", "decimal", unit="units/day", desc="Underlying rate at history end before seasonality"),
    C("nb_dispersion_k", "decimal", desc="Negative-binomial dispersion (low = lumpy)"),
    C("true_demand_class", "enum", allowed=DEMAND_CLASSES[:4] + ["no_demand"], desc="Class from latent history demand"),
    C("mean_true_daily_demand", "decimal", unit="units/day", desc="Mean latent demand over sellable days"),
]))

_add(Table("supplier_disruption", "SupplierDisruption", "ground_truth", ("supplier_id", "start_date"),
           "Hidden supplier disruption windows that cause clustered delays.", [
    SUPPLIER_ID, C("start_date", "date", desc="Start"), C("end_date", "date", desc="End"),
]))


def input_tables():
    return [t for t in TABLES.values() if t.layer == "input"]


# ------------------------------------------------------------ renderers
def dictionary_rows():
    rows = []
    for t in TABLES.values():
        for c in t.cols:
            rows.append({
                "layer": t.layer, "entity": t.entity, "table": t.name, "field": c.name, "type": c.type,
                "required": "required" if c.required else "optional",
                "allowed_values": "|".join(c.allowed) if c.allowed else "",
                "unit": c.unit, "unique": "yes" if (c.unique or t.pk == (c.name,)) else "no",
                "primary_key": "yes" if c.name in t.pk else "no",
                "validation_rule": c.rule, "description": c.desc,
            })
    return rows


def relationships():
    rows = []
    for t in TABLES.values():
        for fk in t.fks:
            optional = any(not t.col(c).required for c in fk.cols)
            rows.append({"child_table": t.name, "child_columns": ",".join(fk.cols), "parent_table": fk.ref_table,
                         "parent_columns": ",".join(fk.ref_cols), "cardinality": fk.cardinality,
                         "optional": "yes" if optional else "no"})
    return rows


_SQL_TYPE = {"string": "TEXT", "date": "TEXT", "enum": "TEXT", "integer": "INTEGER", "boolean": "INTEGER", "decimal": "REAL"}


def ddl() -> str:
    out = ["-- Generated from replenishment/schema.py - do not edit by hand.", "PRAGMA foreign_keys = ON;", ""]
    order = ["sku", "store", "supplier", "supplier_sku", "sku_store_matrix", "sales_history", "inventory_snapshot",
             "purchase_order", "purchase_order_event", "lead_time_observation", "forecast", "reorder_policy",
             "purchase_recommendation", "audit_exception", "ground_truth", "demand_actuals", "demand_profile",
             "supplier_disruption"]
    for name in order:
        t = TABLES[name]
        lines = []
        for c in t.cols:
            s = f"  {c.name} {_SQL_TYPE[c.type]}"
            if c.required:
                s += " NOT NULL"
            if c.allowed:
                vals = ",".join("'" + v + "'" for v in c.allowed)
                s += f" CHECK ({c.name} IN ({vals}))"
            if c.type == "boolean":
                s += f" CHECK ({c.name} IN (0,1))"
            lines.append(s)
        lines.append(f"  PRIMARY KEY ({', '.join(t.pk)})")
        if t.natural_key:
            lines.append(f"  UNIQUE ({', '.join(t.natural_key)})")
        for fk in t.fks:
            lines.append(f"  FOREIGN KEY ({', '.join(fk.cols)}) REFERENCES {fk.ref_table} ({', '.join(fk.ref_cols)})")
        out.append(f"-- {t.entity}: {t.desc}")
        out.append(f"CREATE TABLE {name} (\n" + ",\n".join(lines) + "\n);\n")
    return "\n".join(out)
