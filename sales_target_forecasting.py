# ============================================================
# UNIFIED SALES DEMAND CLASSIFICATION + FORECASTING PIPELINE
# SQL Server -> Activity -> Demand Pattern -> Forecast -> Excel
# No intermediate Excel input is required.
# ============================================================

# ============================================================
# SALES DEMAND PATTERN CLASSIFICATION PIPELINE
# ProductionOrderFull -> Activity Status -> SaleInvoice -> Time Series
# -> ADI / CV^2 -> Smooth / Erratic / Intermittent / Lumpy
# -> Forecast Strategy -> Excel Diagnostic Report
# ============================================================

import os
import warnings
from getpass import getpass
from pathlib import Path

import jdatetime
import numpy as np
import pandas as pd
from sqlalchemy import create_engine, text

warnings.filterwarnings("ignore")


# ============================================================
# 1. USER SETTINGS
# ============================================================

# Database connection settings are intentionally externalized for public use.
# Set these as environment variables on your machine / deployment environment.
SERVER = os.getenv("SALES_DB_SERVER", "localhost")
DATABASE = os.getenv("SALES_DB_NAME", "YOUR_DATABASE")
USERNAME = os.getenv("SALES_DB_USERNAME", "YOUR_USERNAME")
PASSWORD = os.getenv("SALES_DB_PASSWORD") or getpass("SQL Server password: ")
DRIVER = os.getenv("SALES_DB_DRIVER", "ODBC Driver 17 for SQL Server")

# Source table names are configurable so the public repository does not depend
# on one organization's database naming convention.
PRODUCTION_ORDER_TABLE = os.getenv("PRODUCTION_ORDER_TABLE", "dbo.ProductionOrderFull")
SALE_INVOICE_TABLE = os.getenv("SALE_INVOICE_TABLE", "dbo.SaleInvoice")
DIM_DATE_TABLE = os.getenv("DIM_DATE_TABLE", "dbo.DimDate")
SALE_TARGET_TABLE = os.getenv("SALE_TARGET_TABLE", "dbo.SaleTarget")

# Preferred sales quantity column.
# If it does not exist, the script automatically checks the fallback list.
PREFERRED_TARGET_COL = "NetSaleCartonQuantity"

TARGET_CANDIDATES = [
    "NetSaleCartonQuantity",
    "SaleCartonQuantity",
    "CartonQuantity",
    "NetSaleQuantity",
    "SaleQuantity",
    "Quantity",
]

# Demand classification is performed only for these activity states.
CLASSIFIABLE_ACTIVITY_STATUSES = ["Active", "Dormant"]

# Business floors for product activity classification.
# Historical P90/P95 gaps are still used, but these floors prevent
# high-frequency products from being marked Inactive after only a few quiet days.
MIN_ACTIVE_DAYS = 14
MIN_DORMANT_DAYS = 30

# Time bucket used to create the demand series.
# "D"  = daily
# "W"  = weekly (recommended default here)
# "MS" = monthly
DEMAND_FREQUENCY = "W"

# Syntetos-Boylan demand classification thresholds.
ADI_THRESHOLD = 1.32
CV2_THRESHOLD = 0.49

# Minimum history required for reliable classification.
MIN_PERIODS = 8
MIN_NONZERO_PERIODS = 2

# If negative sales occur because of returns/adjustments:
# True  -> negative period totals are floored at zero for demand-pattern analysis.
# False -> negatives are retained.
CLIP_NEGATIVE_DEMAND_TO_ZERO = True


# ============================================================
# 2. DATABASE CONNECTION
# ============================================================

connection_string = (
    f"mssql+pyodbc://{USERNAME}:{PASSWORD}@{SERVER}/{DATABASE}"
    f"?driver={DRIVER.replace(' ', '+')}"
)

engine = create_engine(
    connection_string,
    fast_executemany=True,
)

print("✅ Database connection created.")


# ============================================================
# 3. PRODUCTIONORDERFULL -> PRODUCT ACTIVITY -> SALEINVOICE
# ============================================================

sale_invoice_query = f"""
WITH BaseOrders AS
(
    SELECT
        LTRIM(RTRIM(CAST(ProductCode AS varchar(50)))) AS ProductCode,
        ProductName,
        ProductionOrderNumber AS OrderNumber,
        TRY_CAST(ProductionOrderGDate AS date) AS OrderDate
    FROM {PRODUCTION_ORDER_TABLE}
    WHERE
        ProductCode IS NOT NULL
        AND LTRIM(RTRIM(CAST(ProductCode AS varchar(50)))) LIKE '11%'
        AND ISNULL(ProductName, N'') NOT LIKE N'%میکس%'
        AND ISNULL(ProductName, N'') NOT LIKE N'%لفاف%'
        AND ISNULL(ProductName, N'') NOT LIKE N'%فیلم%'
        AND ISNULL(ProductName, N'') NOT LIKE N'%دستکش%'
        AND ISNULL(ProductName, N'') NOT LIKE N'%کیت%'
        AND ISNULL(ProductName, N'') NOT LIKE N'%کیسه%'
        AND ISNULL(ProductName, N'') NOT LIKE N'%پلیمر%'
        AND ISNULL(ProductName, N'') NOT LIKE N'%رول%'
        AND TRY_CAST(ProductionOrderGDate AS date) IS NOT NULL
),

DistinctOrders AS
(
    SELECT DISTINCT
        ProductCode,
        ProductName,
        OrderNumber,
        OrderDate
    FROM BaseOrders
),

OrderSequence AS
(
    SELECT
        ProductCode,
        ProductName,
        OrderNumber,
        OrderDate,
        LAG(OrderDate) OVER
        (
            PARTITION BY ProductCode
            ORDER BY OrderDate, OrderNumber
        ) AS PreviousOrderDate
    FROM DistinctOrders
),

OrderGaps AS
(
    SELECT
        ProductCode,
        ProductName,
        OrderNumber,
        OrderDate,
        PreviousOrderDate,
        DATEDIFF(DAY, PreviousOrderDate, OrderDate) AS GapDays
    FROM OrderSequence
    WHERE PreviousOrderDate IS NOT NULL
),

GapStatistics AS
(
    SELECT DISTINCT
        ProductCode,
        AVG(CAST(GapDays AS decimal(18, 2))) OVER
        (
            PARTITION BY ProductCode
        ) AS AvgGapDays,

        PERCENTILE_CONT(0.50) WITHIN GROUP
        (
            ORDER BY GapDays
        ) OVER
        (
            PARTITION BY ProductCode
        ) AS MedianGapDays,

        PERCENTILE_CONT(0.90) WITHIN GROUP
        (
            ORDER BY GapDays
        ) OVER
        (
            PARTITION BY ProductCode
        ) AS P90GapDays,

        PERCENTILE_CONT(0.95) WITHIN GROUP
        (
            ORDER BY GapDays
        ) OVER
        (
            PARTITION BY ProductCode
        ) AS P95GapDays
    FROM OrderGaps
),

ProductSummary AS
(
    SELECT
        ProductCode,
        MAX(ProductName) AS ProductName,
        MIN(OrderDate) AS FirstOrderDate,
        MAX(OrderDate) AS LastOrderDate,
        COUNT(DISTINCT OrderNumber) AS OrderCount,
        COUNT(DISTINCT OrderDate) AS ActiveOrderDays
    FROM DistinctOrders
    GROUP BY ProductCode
),

ProductActivity AS
(
    SELECT
        P.ProductCode,
        P.ProductName AS OrderProductName,
        P.FirstOrderDate,
        P.LastOrderDate,
        P.OrderCount,
        P.ActiveOrderDays,

        DATEDIFF(DAY, P.LastOrderDate, GETDATE()) AS DaysSinceLastOrder,

        ROUND(G.AvgGapDays, 1) AS AvgGapDays,
        ROUND(G.MedianGapDays, 1) AS MedianGapDays,
        ROUND(G.P90GapDays, 1) AS P90GapDays,
        ROUND(G.P95GapDays, 1) AS P95GapDays,
        CASE WHEN G.P90GapDays > {MIN_ACTIVE_DAYS} THEN G.P90GapDays ELSE {MIN_ACTIVE_DAYS} END AS ActiveThresholdDays,
        CASE WHEN G.P95GapDays > {MIN_DORMANT_DAYS} THEN G.P95GapDays ELSE {MIN_DORMANT_DAYS} END AS DormantThresholdDays,

        CASE
            WHEN G.MedianGapDays IS NULL
                THEN 'Insufficient History'

            WHEN DATEDIFF(DAY, P.LastOrderDate, GETDATE()) <=
                 CASE
                     WHEN G.P90GapDays > {MIN_ACTIVE_DAYS} THEN G.P90GapDays
                     ELSE {MIN_ACTIVE_DAYS}
                 END
                THEN 'Active'

            WHEN DATEDIFF(DAY, P.LastOrderDate, GETDATE()) <=
                 CASE
                     WHEN G.P95GapDays > {MIN_DORMANT_DAYS} THEN G.P95GapDays
                     ELSE {MIN_DORMANT_DAYS}
                 END
                THEN 'Dormant'

            ELSE 'Inactive'
        END AS ActivityStatus

    FROM ProductSummary AS P
    LEFT JOIN GapStatistics AS G
        ON P.ProductCode = G.ProductCode
)

SELECT
    SI.*,
    PA.OrderProductName,
    PA.FirstOrderDate,
    PA.LastOrderDate,
    PA.OrderCount,
    PA.ActiveOrderDays,
    PA.DaysSinceLastOrder,
    PA.AvgGapDays,
    PA.MedianGapDays,
    PA.P90GapDays,
    PA.P95GapDays,
    PA.ActiveThresholdDays,
    PA.DormantThresholdDays,
    PA.ActivityStatus
FROM {SALE_INVOICE_TABLE} AS SI
INNER JOIN ProductActivity AS PA
    ON LTRIM(RTRIM(CAST(SI.ProductCode AS varchar(50)))) = PA.ProductCode
"""


# ============================================================
# 4. LOAD DATA
# ============================================================

print("\n========================================")
print("LOADING DATA FROM SQL SERVER")
print("========================================")

with engine.connect() as connection:
    sale_invoice = pd.read_sql_query(
        text(sale_invoice_query),
        connection,
    )

    dim_date = pd.read_sql_query(
        text(f"SELECT * FROM {DIM_DATE_TABLE}"),
        connection,
    )

    sale_target_history = pd.read_sql_query(
        text(f"""
            SELECT
                [YearMonth], [DateKey], [ChannelRef], [ChannelName],
                [BrandRef], [BrandName], [MainGroupRef], [MainGroupName],
                [SubGroupRef], [SubGroupName], [TargetQuantity], [SaleQuantity]
            FROM {SALE_TARGET_TABLE}
        """),
        connection,
    )

print("✅ SaleInvoice loaded:", sale_invoice.shape)
print("✅ DimDate loaded:", dim_date.shape)
print("✅ SaleTarget history loaded:", sale_target_history.shape)


# ============================================================
# 5. BASIC CLEANING
# ============================================================

sale_invoice["ProductCode"] = (
    sale_invoice["ProductCode"]
    .astype("string")
    .str.strip()
)


def resolve_target_column(df: pd.DataFrame) -> str:
    """Find the sales quantity column used for demand analysis."""

    if PREFERRED_TARGET_COL in df.columns:
        return PREFERRED_TARGET_COL

    for col in TARGET_CANDIDATES:
        if col in df.columns:
            return col

    raise KeyError(
        "No sales quantity column was found. "
        "Set PREFERRED_TARGET_COL to the correct SaleInvoice column.\n"
        f"Available columns:\n{df.columns.tolist()}"
    )


TARGET_COL = resolve_target_column(sale_invoice)

sale_invoice[TARGET_COL] = pd.to_numeric(
    sale_invoice[TARGET_COL],
    errors="coerce",
).fillna(0.0)


print(f"✅ Demand target column: {TARGET_COL}")


# ============================================================
# SALES CHANNEL / BRAND DIMENSION RESOLUTION
# ============================================================

CHANNEL_CODE_CANDIDATES = [
    "SalesChnnelCode",      # spelling used in some source extracts
    "SalesChannelCode",
    "SaleChannelCode",
    "ChannelCode",
    "SalesChCode",
]

CHANNEL_NAME_CANDIDATES = [
    "SalesChnnelName",
    "SalesChannelName",
    "SaleChannelName",
    "ChannelName",
    "SalesChName",
]

BRAND_CANDIDATES = [
    "Brand",
    "BrandName",
    "ProductBrand",
    "BrandTitle",
]


def resolve_dimension_column(df: pd.DataFrame, candidates, required=False, label="dimension"):
    """Resolve a business dimension without silently guessing a nonexistent column."""
    for col in candidates:
        if col in df.columns:
            return col

    # Case-insensitive fallback for harmless capitalization differences.
    lower_map = {str(col).lower(): col for col in df.columns}
    for candidate in candidates:
        if candidate.lower() in lower_map:
            return lower_map[candidate.lower()]

    if required:
        raise KeyError(
            f"No {label} column was found. Checked: {candidates}\n"
            f"Available SaleInvoice columns:\n{df.columns.tolist()}"
        )
    return None


CHANNEL_CODE_COL = resolve_dimension_column(
    sale_invoice,
    CHANNEL_CODE_CANDIDATES,
    required=True,
    label="sales-channel code",
)
CHANNEL_NAME_COL = resolve_dimension_column(
    sale_invoice,
    CHANNEL_NAME_CANDIDATES,
    required=False,
    label="sales-channel name",
)
BRAND_COL = resolve_dimension_column(
    sale_invoice,
    BRAND_CANDIDATES,
    required=False,
    label="brand",
)

sale_invoice[CHANNEL_CODE_COL] = (
    sale_invoice[CHANNEL_CODE_COL]
    .astype("string")
    .str.strip()
)

# Blank/NULL channels cannot receive a scientifically defensible channel target.
sale_invoice[CHANNEL_CODE_COL] = sale_invoice[CHANNEL_CODE_COL].replace("", pd.NA)

if CHANNEL_NAME_COL is not None:
    sale_invoice[CHANNEL_NAME_COL] = (
        sale_invoice[CHANNEL_NAME_COL]
        .astype("string")
        .str.strip()
    )

if BRAND_COL is not None:
    sale_invoice[BRAND_COL] = (
        sale_invoice[BRAND_COL]
        .astype("string")
        .str.strip()
    )

print(f"✅ Sales-channel code column: {CHANNEL_CODE_COL}")
print(f"✅ Sales-channel name column: {CHANNEL_NAME_COL or 'Not found'}")
print(f"✅ Brand column: {BRAND_COL or 'Not found'}")


# ============================================================
# HISTORICAL SaleTarget PREPARATION
# ============================================================

TARGET_DIMENSION_CANDIDATES = {
    "ChannelRef": ["ChannelRef", "SalesChnnelCode", "SalesChannelCode", "SaleChannelCode", "ChannelCode", "SalesChCode"],
    "BrandRef": ["BrandRef", "BrandCode", "ProductBrandRef", "ProductBrandCode"],
    "MainGroupRef": ["MainGroupRef", "MainGroupCode", "ProductMainGroupRef", "ProductMainGroupCode", "GroupRef"],
    "SubGroupRef": ["SubGroupRef", "SubGroupCode", "ProductSubGroupRef", "ProductSubGroupCode"],
}

SALEINVOICE_TARGET_DIM_COLS = {
    target_dim: resolve_dimension_column(
        sale_invoice, candidates, required=False, label=f"{target_dim} for SaleTarget mapping"
    )
    for target_dim, candidates in TARGET_DIMENSION_CANDIDATES.items()
}

def normalize_key(value):
    if pd.isna(value):
        return None
    value = str(value).strip()
    if value.endswith(".0"):
        value = value[:-2]
    return value or None

def normalize_yearmonth(value):
    if pd.isna(value):
        return np.nan
    digits = "".join(ch for ch in str(value) if ch.isdigit())
    if len(digits) < 6:
        return np.nan
    try:
        return int(digits[:6])
    except Exception:
        return np.nan

sale_target_history = sale_target_history.copy()
sale_target_history["YearMonthSort"] = sale_target_history["YearMonth"].apply(normalize_yearmonth)
sale_target_history["TargetQuantity"] = pd.to_numeric(sale_target_history["TargetQuantity"], errors="coerce").fillna(0.0)
sale_target_history["SaleQuantity"] = pd.to_numeric(sale_target_history["SaleQuantity"], errors="coerce").fillna(0.0)
for dim in ["ChannelRef", "BrandRef", "MainGroupRef", "SubGroupRef"]:
    sale_target_history[dim] = sale_target_history[dim].apply(normalize_key)
sale_target_history = sale_target_history.dropna(subset=["YearMonthSort"]).sort_values("YearMonthSort").reset_index(drop=True)
print("✅ SaleTarget mapping columns:", SALEINVOICE_TARGET_DIM_COLS)

# ============================================================
# 6. JALALI DATE HELPERS
# ============================================================


def standardize_jalali(value):
    """Convert a Jalali date to YYYY/MM/DD."""

    if pd.isna(value):
        return None

    try:
        value = str(value).strip()
        year, month, day = value.split("/")
        return f"{int(year):04d}/{int(month):02d}/{int(day):02d}"
    except Exception:
        return None



def jalali_to_gregorian(value):
    """Convert YYYY/MM/DD Jalali date to pandas Timestamp."""

    if value is None or pd.isna(value):
        return pd.NaT

    try:
        year, month, day = map(int, str(value).split("/"))
        gdate = jdatetime.date(year, month, day).togregorian()
        return pd.Timestamp(gdate)
    except Exception:
        return pd.NaT


if "InvoiceSDate" not in sale_invoice.columns:
    raise KeyError(
        "InvoiceSDate was not found in SaleInvoice. "
        "A date column is required for time-series classification."
    )

sale_invoice["InvoiceSDate"] = (
    sale_invoice["InvoiceSDate"]
    .apply(standardize_jalali)
)

sale_invoice["InvoiceGDate_Calc"] = (
    sale_invoice["InvoiceSDate"]
    .apply(jalali_to_gregorian)
)

invalid_date_rows = sale_invoice["InvoiceGDate_Calc"].isna().sum()

print(f"✅ Valid invoice dates: {len(sale_invoice) - invalid_date_rows:,}")
print(f"⚠️ Invalid invoice dates: {invalid_date_rows:,}")


# ============================================================
# 7. IRANIAN OFFICIAL HOLIDAYS
# ============================================================

holidays = {
    1399: {
        (1, 1), (1, 2), (1, 3), (1, 4), (1, 12), (1, 13), (1, 21),
        (2, 26), (3, 4), (3, 5), (3, 14), (3, 15), (3, 28),
        (5, 10), (5, 18), (6, 8), (6, 9), (7, 17), (7, 25), (7, 26),
        (8, 4), (8, 13), (10, 28), (11, 22), (12, 7), (12, 21), (12, 29),
    },
    1400: {
        (1, 1), (1, 2), (1, 3), (1, 4), (1, 9), (1, 12), (1, 13),
        (2, 14), (2, 23), (2, 24), (3, 14), (3, 15), (3, 16),
        (4, 30), (5, 7), (5, 27), (5, 28), (7, 5), (7, 13), (7, 15),
        (7, 22), (8, 2), (10, 16), (11, 22), (11, 26), (12, 10),
        (12, 27), (12, 29),
    },
    1401: {
        (1, 1), (1, 2), (1, 3), (1, 4), (1, 12), (1, 13), (1, 21),
        (2, 3), (2, 12), (2, 13), (3, 5), (3, 14), (3, 15),
        (4, 19), (4, 27), (5, 16), (5, 17), (6, 26), (7, 3), (7, 5),
        (7, 13), (7, 22), (10, 6), (11, 15), (11, 22), (11, 29),
        (12, 17), (12, 29),
    },
    1402: {
        (1, 1), (1, 2), (1, 3), (1, 4), (1, 12), (1, 13), (1, 23),
        (2, 2), (2, 3), (2, 26), (3, 14), (3, 15), (4, 8), (4, 16),
        (5, 5), (5, 6), (6, 15), (6, 23), (6, 25), (7, 2), (7, 11),
        (9, 26), (11, 5), (11, 19), (11, 22), (12, 6), (12, 29),
    },
    1403: {
        (1, 1), (1, 2), (1, 3), (1, 4), (1, 12), (1, 13), (1, 22),
        (1, 23), (2, 15), (3, 14), (3, 15), (3, 28), (4, 5), (4, 25),
        (4, 26), (6, 4), (6, 12), (6, 14), (6, 22), (6, 31), (9, 15),
        (10, 25), (11, 9), (11, 22), (11, 26), (12, 29),
    },
    1404: {
        (1, 1), (1, 2), (1, 3), (1, 4), (1, 11), (1, 12), (1, 13),
        (2, 4), (3, 14), (3, 15), (3, 24), (4, 14), (4, 15), (5, 23),
        (6, 2), (6, 10), (6, 19), (9, 3), (10, 13), (10, 27),
        (11, 15), (11, 22), (12, 20),
    },
    1405: {
        (1, 1), (1, 2), (1, 3), (1, 4), (1, 12), (1, 13), (1, 25),
        (3, 6), (3, 14), (4, 3), (4, 4), (4, 13), (4, 14), (4, 15),
        (4, 16), (4, 17), (4, 18), (5, 13), (5, 21), (5, 22), (6, 8),
        (10, 2), (10, 16), (11, 4), (11, 22), (12, 9), (12, 19),
        (12, 20), (12, 29),
    },
}



def check_holiday(jalali_date):
    """Return 1 for Friday/official holiday, otherwise 0."""

    if jalali_date is None:
        return 0

    try:
        year, month, day = map(int, jalali_date.split("/"))
        gregorian_date = jdatetime.date(year, month, day).togregorian()
        is_friday = gregorian_date.weekday() == 4
        is_official_holiday = year in holidays and (month, day) in holidays[year]
        return int(is_friday or is_official_holiday)
    except Exception:
        return 0


sale_invoice["holiday"] = (
    sale_invoice["InvoiceSDate"]
    .apply(check_holiday)
    .astype(int)
)


# ============================================================
# 8. FINAL RAW DATAFRAMES
# ============================================================

df_1 = sale_invoice.copy().reset_index(drop=True)
df_2 = dim_date.copy().reset_index(drop=True)

activity_columns = [
    "ProductCode",
    "OrderProductName",
    "FirstOrderDate",
    "LastOrderDate",
    "OrderCount",
    "ActiveOrderDays",
    "DaysSinceLastOrder",
    "AvgGapDays",
    "MedianGapDays",
    "P90GapDays",
    "P95GapDays",
    "ActiveThresholdDays",
    "DormantThresholdDays",
    "ActivityStatus",
]

product_activity = (
    df_1[activity_columns]
    .drop_duplicates(subset="ProductCode")
    .reset_index(drop=True)
)


# ============================================================
# 9. FILTER PRODUCTS FOR DEMAND-PATTERN CLASSIFICATION
# ============================================================

classifiable_products = set(
    product_activity.loc[
        product_activity["ActivityStatus"].isin(CLASSIFIABLE_ACTIVITY_STATUSES),
        "ProductCode",
    ]
)

analysis_data = df_1[
    df_1["ProductCode"].isin(classifiable_products)
].copy()

analysis_data = analysis_data.dropna(subset=["InvoiceGDate_Calc"])

print("\n========================================")
print("DEMAND PATTERN ANALYSIS")
print("========================================")
print("Classifiable statuses:", CLASSIFIABLE_ACTIVITY_STATUSES)
print("Products to classify:", len(classifiable_products))
print("Frequency:", DEMAND_FREQUENCY)


# ============================================================
# 10. BUILD COMPLETE TIME SERIES FOR EACH PRODUCT
# ============================================================

# First aggregate invoices to one daily total per product.
daily_sales = (
    analysis_data.groupby(
        ["ProductCode", "InvoiceGDate_Calc"],
        as_index=False,
    )[TARGET_COL]
    .sum()
)

if CLIP_NEGATIVE_DEMAND_TO_ZERO:
    daily_sales[TARGET_COL] = daily_sales[TARGET_COL].clip(lower=0)


# Common analysis window across all selected products.
# Using a common window makes ADI comparable across products.
if not daily_sales.empty:
    GLOBAL_START_DATE = daily_sales["InvoiceGDate_Calc"].min()
    GLOBAL_END_DATE = daily_sales["InvoiceGDate_Calc"].max()
else:
    GLOBAL_START_DATE = pd.NaT
    GLOBAL_END_DATE = pd.NaT


# ============================================================
# 11. DEMAND CLASSIFICATION FUNCTIONS
# ============================================================


def classify_demand_pattern(adi, cv2):
    """Classify demand using ADI and CV^2 thresholds."""

    if pd.isna(adi) or pd.isna(cv2):
        return "Insufficient Demand History"

    if adi <= ADI_THRESHOLD and cv2 <= CV2_THRESHOLD:
        return "Smooth"

    if adi <= ADI_THRESHOLD and cv2 > CV2_THRESHOLD:
        return "Erratic"

    if adi > ADI_THRESHOLD and cv2 <= CV2_THRESHOLD:
        return "Intermittent"

    return "Lumpy"



def forecast_strategy(activity_status, demand_pattern):
    """Map product state and demand pattern to a practical model family."""

    if activity_status == "Inactive":
        return "No Forecast / Product Review"

    if activity_status == "Insufficient History":
        return "Rule-Based / Naive / Manual Review"

    if demand_pattern == "Smooth":
        return "ETS / ARIMA / Seasonal Naive"

    if demand_pattern == "Erratic":
        return "ETS / Robust Regression / ML with Lag Features"

    if demand_pattern == "Intermittent":
        return "Croston / SBA / TSB"

    if demand_pattern == "Lumpy":
        return "TSB / ADIDA / Croston-SBA + Review"

    return "Insufficient History / Manual Review"



def build_product_series(product_df: pd.DataFrame) -> pd.Series:
    """Create a zero-filled demand series for one product."""

    if product_df.empty:
        return pd.Series(dtype=float)

    s = (
        product_df.set_index("InvoiceGDate_Calc")[TARGET_COL]
        .sort_index()
        .resample(DEMAND_FREQUENCY)
        .sum()
    )

    # Explicitly fill missing time buckets with zero demand.
    full_index = pd.date_range(
        start=s.index.min(),
        end=GLOBAL_END_DATE,
        freq=DEMAND_FREQUENCY,
    )

    s = s.reindex(full_index, fill_value=0.0)

    if CLIP_NEGATIVE_DEMAND_TO_ZERO:
        s = s.clip(lower=0)

    return s.astype(float)



def calculate_demand_metrics(product_code, product_df):
    """Calculate ADI, CV^2 and related diagnostics for one product."""

    series = build_product_series(product_df)

    total_periods = len(series)
    nonzero = series[series > 0]
    nonzero_periods = len(nonzero)
    zero_periods = total_periods - nonzero_periods

    total_demand = float(series.sum()) if total_periods else 0.0
    mean_all_periods = float(series.mean()) if total_periods else np.nan
    mean_nonzero = float(nonzero.mean()) if nonzero_periods else np.nan
    std_nonzero = (
        float(nonzero.std(ddof=1))
        if nonzero_periods >= 2
        else np.nan
    )

    demand_probability = (
        nonzero_periods / total_periods
        if total_periods > 0
        else np.nan
    )

    zero_demand_percent = (
        100.0 * zero_periods / total_periods
        if total_periods > 0
        else np.nan
    )

    # ADI = number of periods / number of non-zero demand periods.
    adi = (
        total_periods / nonzero_periods
        if nonzero_periods > 0
        else np.nan
    )

    # CV^2 is calculated only from non-zero demand sizes.
    if nonzero_periods >= 2 and mean_nonzero != 0 and not pd.isna(mean_nonzero):
        cv2 = (std_nonzero / mean_nonzero) ** 2
    else:
        cv2 = np.nan

    if total_periods < MIN_PERIODS or nonzero_periods < MIN_NONZERO_PERIODS:
        demand_pattern = "Insufficient Demand History"
    else:
        demand_pattern = classify_demand_pattern(adi, cv2)

    first_demand_date = series[series > 0].index.min() if nonzero_periods else pd.NaT
    last_demand_date = series[series > 0].index.max() if nonzero_periods else pd.NaT

    return {
        "ProductCode": product_code,
        "AnalysisStart": series.index.min() if total_periods else pd.NaT,
        "AnalysisEnd": series.index.max() if total_periods else pd.NaT,
        "TimeFrequency": DEMAND_FREQUENCY,
        "TotalPeriods": total_periods,
        "NonZeroPeriods": nonzero_periods,
        "ZeroPeriods": zero_periods,
        "ZeroDemandPercent": round(zero_demand_percent, 2) if not pd.isna(zero_demand_percent) else np.nan,
        "DemandProbability": round(demand_probability, 4) if not pd.isna(demand_probability) else np.nan,
        "TotalDemand": round(total_demand, 4),
        "MeanDemandAllPeriods": round(mean_all_periods, 4) if not pd.isna(mean_all_periods) else np.nan,
        "MeanNonZeroDemand": round(mean_nonzero, 4) if not pd.isna(mean_nonzero) else np.nan,
        "StdNonZeroDemand": round(std_nonzero, 4) if not pd.isna(std_nonzero) else np.nan,
        "ADI": round(adi, 4) if not pd.isna(adi) else np.nan,
        "CV2": round(cv2, 4) if not pd.isna(cv2) else np.nan,
        "DemandPattern": demand_pattern,
        "FirstDemandPeriod": first_demand_date,
        "LastDemandPeriod": last_demand_date,
    }


# ============================================================
# 12. CALCULATE PRODUCT-LEVEL DEMAND METRICS
# ============================================================

metric_rows = []
series_rows = []

for product_code, product_df in daily_sales.groupby("ProductCode"):
    metric_rows.append(
        calculate_demand_metrics(product_code, product_df)
    )

    series = build_product_series(product_df)

    if not series.empty:
        temp = pd.DataFrame({
            "ProductCode": product_code,
            "Period": series.index,
            "Demand": series.values,
        })
        series_rows.append(temp)


demand_metrics = pd.DataFrame(metric_rows)

if series_rows:
    demand_time_series = pd.concat(series_rows, ignore_index=True)
else:
    demand_time_series = pd.DataFrame(
        columns=["ProductCode", "Period", "Demand"]
    )


# ============================================================
# 13. MERGE ACTIVITY + DEMAND PATTERN
# ============================================================

product_diagnostic = product_activity.merge(
    demand_metrics,
    on="ProductCode",
    how="left",
)

# Products not classified because they are Inactive / Insufficient History.
not_classified_mask = ~product_diagnostic["ActivityStatus"].isin(
    CLASSIFIABLE_ACTIVITY_STATUSES
)

product_diagnostic.loc[
    not_classified_mask,
    "DemandPattern",
] = "Not Classified"

product_diagnostic.loc[
    not_classified_mask,
    [
        "TimeFrequency",
    ],
] = DEMAND_FREQUENCY

product_diagnostic["ForecastStrategy"] = product_diagnostic.apply(
    lambda row: forecast_strategy(
        row["ActivityStatus"],
        row["DemandPattern"],
    ),
    axis=1,
)

# A simple priority flag for forecasting workflow.
def forecast_priority(row):
    if row["ActivityStatus"] == "Active":
        return "High"
    if row["ActivityStatus"] == "Dormant":
        return "Review"
    return "Exclude"


product_diagnostic["ForecastPriority"] = product_diagnostic.apply(
    forecast_priority,
    axis=1,
)


# ============================================================
# 14. SUMMARIES
# ============================================================

activity_summary = (
    product_diagnostic["ActivityStatus"]
    .value_counts(dropna=False)
    .rename_axis("ActivityStatus")
    .reset_index(name="ProductCount")
)

pattern_summary = (
    product_diagnostic[
        product_diagnostic["ActivityStatus"].isin(CLASSIFIABLE_ACTIVITY_STATUSES)
    ]["DemandPattern"]
    .value_counts(dropna=False)
    .rename_axis("DemandPattern")
    .reset_index(name="ProductCount")
)

activity_pattern_matrix = pd.crosstab(
    product_diagnostic["ActivityStatus"],
    product_diagnostic["DemandPattern"],
    margins=True,
).reset_index()

forecast_strategy_summary = (
    product_diagnostic["ForecastStrategy"]
    .value_counts(dropna=False)
    .rename_axis("ForecastStrategy")
    .reset_index(name="ProductCount")
)

basic_info = pd.DataFrame({
    "Metric": [
        "SaleInvoice Rows",
        "SaleInvoice Columns",
        "Unique ProductCodes",
        "Target Column",
        "Demand Frequency",
        "ADI Threshold",
        "CV2 Threshold",
        "Minimum Periods",
        "Minimum NonZero Periods",
        "Active Products",
        "Dormant Products",
        "Inactive Products",
        "Insufficient Activity History",
        "Products Classified for Demand Pattern",
        "Smooth Products",
        "Erratic Products",
        "Intermittent Products",
        "Lumpy Products",
        "Insufficient Demand History",
    ],
    "Value": [
        len(df_1),
        df_1.shape[1],
        df_1["ProductCode"].nunique(),
        TARGET_COL,
        DEMAND_FREQUENCY,
        ADI_THRESHOLD,
        CV2_THRESHOLD,
        MIN_PERIODS,
        MIN_NONZERO_PERIODS,
        (product_diagnostic["ActivityStatus"] == "Active").sum(),
        (product_diagnostic["ActivityStatus"] == "Dormant").sum(),
        (product_diagnostic["ActivityStatus"] == "Inactive").sum(),
        (product_diagnostic["ActivityStatus"] == "Insufficient History").sum(),
        product_diagnostic["DemandPattern"].isin(
            ["Smooth", "Erratic", "Intermittent", "Lumpy"]
        ).sum(),
        (product_diagnostic["DemandPattern"] == "Smooth").sum(),
        (product_diagnostic["DemandPattern"] == "Erratic").sum(),
        (product_diagnostic["DemandPattern"] == "Intermittent").sum(),
        (product_diagnostic["DemandPattern"] == "Lumpy").sum(),
        (product_diagnostic["DemandPattern"] == "Insufficient Demand History").sum(),
    ],
})


# ============================================================
# 15. DATA QUALITY TABLES
# ============================================================

missing_values = pd.DataFrame({
    "Column": df_1.columns,
    "MissingCount": df_1.isna().sum().values,
    "MissingPercent": (df_1.isna().mean() * 100).round(2).values,
}).sort_values("MissingPercent", ascending=False)


data_types = pd.DataFrame({
    "Column": df_1.columns,
    "DataType": [str(dtype) for dtype in df_1.dtypes],
    "UniqueValues": [df_1[col].nunique(dropna=True) for col in df_1.columns],
})

numeric_columns = df_1.select_dtypes(include=np.number).columns.tolist()

if numeric_columns:
    numerical_profile = (
        df_1[numeric_columns]
        .describe()
        .T
        .reset_index()
        .rename(columns={"index": "Column"})
    )
else:
    numerical_profile = pd.DataFrame()

holiday_summary = (
    df_1["holiday"]
    .value_counts()
    .rename_axis("holiday")
    .reset_index(name="Rows")
)

holiday_summary["Description"] = holiday_summary["holiday"].map({
    0: "Working Day",
    1: "Holiday / Friday",
})


# ============================================================
# 16. CLASSIFICATION RULES SHEET
# ============================================================

classification_rules = pd.DataFrame({
    "Rule": [
        "Activity: Active",
        "Activity: Dormant",
        "Activity: Inactive",
        "Demand: Smooth",
        "Demand: Erratic",
        "Demand: Intermittent",
        "Demand: Lumpy",
        "ADI Formula",
        "CV2 Formula",
        "Classification Scope",
    ],
    "Definition": [
        f"Days since last order <= max(product P90 historical order gap, {MIN_ACTIVE_DAYS} days)",
        f"Active threshold < days since last order <= max(product P95 historical order gap, {MIN_DORMANT_DAYS} days)",
        f"Days since last order > max(product P95 historical order gap, {MIN_DORMANT_DAYS} days)",
        f"ADI <= {ADI_THRESHOLD} and CV2 <= {CV2_THRESHOLD}",
        f"ADI <= {ADI_THRESHOLD} and CV2 > {CV2_THRESHOLD}",
        f"ADI > {ADI_THRESHOLD} and CV2 <= {CV2_THRESHOLD}",
        f"ADI > {ADI_THRESHOLD} and CV2 > {CV2_THRESHOLD}",
        "Total time periods / Non-zero demand periods",
        "(Std of non-zero demand / Mean of non-zero demand)^2",
        "Demand pattern is calculated only for Active and Dormant products",
    ],
})

# ============================================================
# SALES TARGET FORECASTING ENGINE V4 - FORECAST + SMART TARGET + CHANNEL
# Sales-target oriented forecasting
#
# Key changes vs V1:
# 1) Zero is NOT allowed to win merely because many weeks are zero.
# 2) ALL patterns are evaluated primarily on 4-week cumulative demand,
#    matching the monthly sales-target objective.
# 3) Weekly WAPE is retained as a small diagnostic tie-breaker.
# 4) Intermittent/Lumpy use Croston/SBA/TSB/ADIDA plus a two-stage
#    occurrence x demand-size model.
# 5) Champion selection includes forecast bias, not only WAPE.
# 6) Dormant and uncertain products receive management flags.
# 7) Output includes weekly forecast AND 4-week production buckets.
# ============================================================

import warnings
warnings.filterwarnings("ignore")

from pathlib import Path
import numpy as np
import pandas as pd

from statsmodels.tsa.holtwinters import (
    SimpleExpSmoothing,
    Holt,
    ExponentialSmoothing,
)
from statsmodels.tsa.statespace.sarimax import SARIMAX


# ============================================================
# 1. CONFIGURATION
# ============================================================

OUTPUT_DIR = Path(os.getenv("SALES_FORECAST_OUTPUT_DIR", "outputs"))
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

OUTPUT_FILE = OUTPUT_DIR / "Sales_Target_Results_V4.xlsx"
MANAGEMENT_OUTPUT_FILE = OUTPUT_DIR / "Sales_Target_Management_V4.xlsx"

FORECAST_STATUSES = {"Active", "Dormant"}

# Diagnostic file is weekly.
FORECAST_HORIZON = 12          # 12 weeks ahead
TARGET_BUCKET = 4              # 4-week sales-target evaluation bucket

# Backtesting
REGULAR_TEST_HORIZON = 4       # Smooth / Erratic
INTERMITTENT_TEST_HORIZON = 4  # Intermittent / Lumpy
BACKTEST_FOLDS = 6
MIN_TRAIN_PERIODS = 24

# Weekly seasonality
SEASON_LENGTH = 52

# Model parameters
CROSTON_ALPHA_GRID = [0.05, 0.10, 0.20, 0.30, 0.50]
TSB_ALPHA_GRID = [0.05, 0.10, 0.20, 0.30, 0.50]
TSB_BETA_GRID = [0.05, 0.10, 0.20, 0.30, 0.40, 0.50]

# Two-stage model lookback.
# Expanded only for Intermittent/Lumpy so the existing two-stage family can
# adapt to very short and very long occurrence/size regimes.
OCCURRENCE_LOOKBACKS = [4, 6, 8, 13, 26, 52]
SIZE_LOOKBACKS = [2, 4, 6, 8, 13, 26]

USE_ARIMA = True
CLIP_NEGATIVE = True

# Champion selection:
# V4 emphasizes recent backtest folds because the latest demand regime is
# more relevant for sales targeting than equally weighting old folds.
BIAS_WEIGHT = 0.20
RECENCY_DECAY = 0.80          # oldest folds receive progressively less weight
RECENT_ERROR_WEIGHT = 0.65   # recent-weighted error in champion score
OVERALL_ERROR_WEIGHT = 0.35  # full backtest error remains a stability anchor
WEEKLY_ANCHOR_WEIGHT = 0.05 # small weekly tie-breaker; monthly target accuracy remains primary

# Optional, conservative post-selection bias correction.
# It only activates when enough backtest folds exist and systematic bias is material.
APPLY_BIAS_CORRECTION = True
MIN_FOLDS_FOR_BIAS_CORRECTION = 3
BIAS_CORRECTION_TRIGGER = 10.0   # percent
BIAS_CORRECTION_MIN_FACTOR = 0.85
BIAS_CORRECTION_MAX_FACTOR = 1.15

# Reliability thresholds are applied to the blended, recency-aware 4-week
# bucket WAPE for ALL demand patterns, aligned with the monthly sales target.
HIGH_ERROR_THRESHOLD = 25
MEDIUM_ERROR_THRESHOLD = 45

# V4 target-setting policy.
# The statistical forecast and the management target are intentionally separated.
# Historical sales-target data is kept for diagnostics / achievement analysis only and
# no longer pulls the forecast toward old management targets.
TARGET_STRETCH_PCT = 0.00  # legacy fixed stretch; V4 uses SMART STRETCH below

USE_HISTORICAL_TARGET_SIGNAL = True
TARGET_HISTORY_MONTHS = 6
TARGET_HISTORY_RECENT_MONTHS = 3
TARGET_HISTORY_MIN_FACTOR = 0.90
TARGET_HISTORY_MAX_FACTOR = 1.10
MIN_TARGET_HISTORY_ROWS = 3

# Smart stretch: turns an expected-sales forecast into a challenging but
# data-grounded recommended target.  It depends on forecast reliability,
# recent sales momentum, product status and demand pattern -- NOT on old target size.
SMART_STRETCH_ENABLED = True
SMART_STRETCH_BY_RELIABILITY = {
    "High": 0.07,
    "Medium": 0.05,
    "Low": 0.03,
    "Insufficient Backtest": 0.02,
}
SMART_STRETCH_MAX = 0.12
SMART_STRETCH_MIN = 0.00
SMART_STRETCH_MOMENTUM_BONUS_STRONG = 0.03   # last4 >= 110% of previous4
SMART_STRETCH_MOMENTUM_BONUS_MILD = 0.015   # last4 >= 103% of previous4
SMART_STRETCH_MOMENTUM_PENALTY_MILD = 0.01  # last4 <= 97% of previous4
SMART_STRETCH_MOMENTUM_PENALTY_STRONG = 0.02 # last4 <= 90% of previous4
SMART_STRETCH_INTERMITTENT_CAP = 0.06
SMART_STRETCH_LUMPY_CAP = 0.05
SMART_STRETCH_DORMANT_CAP = 0.02

# Recent momentum is used only as a bounded adjustment to avoid chasing spikes.
USE_RECENT_MOMENTUM = True
RECENT_WEEKS_SHORT = 4
RECENT_WEEKS_LONG = 13
MOMENTUM_WEIGHT = 0.25
MOMENTUM_MIN_FACTOR = 0.90
MOMENTUM_MAX_FACTOR = 1.10

# For low-reliability SKUs, blend champion forecast with a robust recent baseline.
LOW_RELIABILITY_BLEND = 0.35

# V6 reliability-aware target calibration.
# Champion forecasts remain the statistical signal, but weak backtests are
# automatically pulled toward a robust recent 4-week baseline.
MEDIUM_RELIABILITY_MODEL_WEIGHT = 0.70
LOW_RELIABILITY_MODEL_WEIGHT = 0.35
INSUFFICIENT_RELIABILITY_MODEL_WEIGHT = 0.20

# Guardrails are intentionally broad: they prevent pathological targets while
# still allowing genuine growth/decline. Bounds are relative to a robust recent
# monthly-like baseline built from recent 4-week blocks.
TARGET_FLOOR_FACTOR = 0.50
TARGET_CAP_FACTOR = 1.60
DORMANT_CAP_FACTOR = 1.00
DORMANT_ZERO_RECENT_CAP_FACTOR = 0.60


# ============================================================
# HIERARCHICAL PRODUCT x CHANNEL SETTINGS
# ============================================================

# The product target remains the coherent top-level target.
# Channel models determine the mix beneath each product; reconciliation guarantees
# that Sum(ChannelTarget) == Product SalesTarget for every product.
CHANNEL_SHARE_LOOKBACK_WEEKS = 13

# Reliability-aware blend between an independently backtested channel model share
# and the recent historical channel share. These are deliberately conservative.
CHANNEL_HIGH_MODEL_WEIGHT = 0.80
CHANNEL_MEDIUM_MODEL_WEIGHT = 0.60
CHANNEL_LOW_MODEL_WEIGHT = 0.30
CHANNEL_INSUFFICIENT_MODEL_WEIGHT = 0.15

# Exporting these sheets makes the channel layer auditable.
EXPORT_CHANNEL_MODEL_LEADERBOARD = True
EXPORT_CHANNEL_BACKTEST_DETAILS = True


# ============================================================
# 2. BASIC HELPERS
# ============================================================

def clean_product_code(value):
    if pd.isna(value):
        return None
    value = str(value).strip()
    if value.endswith(".0"):
        value = value[:-2]
    return value


def safe_series(values):
    s = pd.Series(values, dtype="float64")
    s = s.replace([np.inf, -np.inf], np.nan).fillna(0.0)
    if CLIP_NEGATIVE:
        s = s.clip(lower=0.0)
    return s.reset_index(drop=True)


def clip_forecast(values):
    arr = np.asarray(values, dtype=float).reshape(-1)
    arr = np.where(np.isfinite(arr), arr, 0.0)
    if CLIP_NEGATIVE:
        arr = np.maximum(arr, 0.0)
    return arr


def metrics(actual, forecast):
    actual = np.asarray(actual, dtype=float)
    forecast = np.asarray(forecast, dtype=float)

    mask = np.isfinite(actual) & np.isfinite(forecast)
    actual = actual[mask]
    forecast = forecast[mask]

    if len(actual) == 0:
        return {
            "WAPE": np.nan,
            "MAE": np.nan,
            "RMSE": np.nan,
            "BiasPercent": np.nan,
        }

    error = forecast - actual
    ae = np.abs(error)

    mae = float(np.mean(ae))
    rmse = float(np.sqrt(np.mean(error ** 2)))

    denominator = float(np.sum(np.abs(actual)))

    if denominator > 0:
        wape = float(np.sum(ae) / denominator * 100)
        bias = float(np.sum(error) / denominator * 100)
    else:
        # All actual values are zero.
        # Do not reward a zero forecast with a misleading perfect WAPE.
        wape = np.nan
        bias = np.nan

    return {
        "WAPE": wape,
        "MAE": mae,
        "RMSE": rmse,
        "BiasPercent": bias,
    }


def recency_weighted_metrics(fold_pairs, decay=RECENCY_DECAY):
    """
    WAPE / bias with exponentially larger weight on the latest backtest folds.

    fold_pairs must be ordered from oldest -> newest and contain
    (actual_array, forecast_array) tuples. This avoids allowing an old demand
    regime to dominate champion selection when recent behavior has changed.
    """
    if not fold_pairs:
        return {"WAPE": np.nan, "BiasPercent": np.nan}

    n = len(fold_pairs)
    weights = np.asarray([decay ** (n - 1 - i) for i in range(n)], dtype=float)

    abs_error_num = 0.0
    bias_num = 0.0
    demand_den = 0.0

    for weight, (actual, forecast) in zip(weights, fold_pairs):
        actual = np.asarray(actual, dtype=float)
        forecast = np.asarray(forecast, dtype=float)
        mask = np.isfinite(actual) & np.isfinite(forecast)
        actual = actual[mask]
        forecast = forecast[mask]
        if len(actual) == 0:
            continue

        abs_error_num += weight * float(np.sum(np.abs(forecast - actual)))
        bias_num += weight * float(np.sum(forecast - actual))
        demand_den += weight * float(np.sum(np.abs(actual)))

    if demand_den <= 0:
        return {"WAPE": np.nan, "BiasPercent": np.nan}

    return {
        "WAPE": abs_error_num / demand_den * 100.0,
        "BiasPercent": bias_num / demand_den * 100.0,
    }


def bias_correction_factor(bias_percent, folds):
    """Return a deliberately capped correction for systematic backtest bias."""
    if not APPLY_BIAS_CORRECTION:
        return 1.0
    if folds < MIN_FOLDS_FOR_BIAS_CORRECTION or pd.isna(bias_percent):
        return 1.0
    if abs(float(bias_percent)) < BIAS_CORRECTION_TRIGGER:
        return 1.0

    # Bias > 0 means systematic over-forecasting -> reduce future forecast.
    raw = 1.0 - float(bias_percent) / 100.0
    return float(np.clip(raw, BIAS_CORRECTION_MIN_FACTOR, BIAS_CORRECTION_MAX_FACTOR))


def aggregate_blocks(values, block_size=4):
    """
    Convert weekly values into consecutive sales-target buckets.
    Example with block_size=4:
    weeks 1-4 -> bucket 1
    weeks 5-8 -> bucket 2
    """
    arr = np.asarray(values, dtype=float)
    if len(arr) == 0:
        return np.array([], dtype=float)

    blocks = []
    for start in range(0, len(arr), block_size):
        block = arr[start:start + block_size]
        blocks.append(float(np.sum(block)))
    return np.asarray(blocks, dtype=float)


def occurrence_metrics(actual, forecast):
    """
    Event-occurrence diagnostics.
    A demand event is actual > 0.
    Forecast occurrence probability is approximated from whether
    the model predicts positive weekly demand.
    """
    actual = np.asarray(actual, dtype=float)
    forecast = np.asarray(forecast, dtype=float)

    actual_event = (actual > 0).astype(float)
    forecast_event = (forecast > 1e-9).astype(float)

    accuracy = float(np.mean(actual_event == forecast_event) * 100)

    actual_positive = actual_event.sum()
    if actual_positive > 0:
        recall = float(
            ((actual_event == 1) & (forecast_event == 1)).sum()
            / actual_positive
            * 100
        )
    else:
        recall = np.nan

    return {
        "OccurrenceAccuracy": accuracy,
        "DemandEventRecall": recall,
    }


# ============================================================
# 3. BASELINE / REGULAR-DEMAND MODELS
# ============================================================

def model_naive(train, horizon, **kwargs):
    train = safe_series(train)
    if len(train) == 0:
        return np.zeros(horizon)
    return np.repeat(float(train.iloc[-1]), horizon)


def model_mean(train, horizon, **kwargs):
    train = safe_series(train)
    if len(train) == 0:
        return np.zeros(horizon)
    return np.repeat(float(train.mean()), horizon)


def model_ma(train, horizon, window=4, **kwargs):
    train = safe_series(train)
    if len(train) == 0:
        return np.zeros(horizon)
    window = min(window, len(train))
    return np.repeat(float(train.iloc[-window:].mean()), horizon)


def model_seasonal_naive(train, horizon, season_length=SEASON_LENGTH, **kwargs):
    train = safe_series(train)
    if len(train) < season_length:
        return model_naive(train, horizon)

    season = train.iloc[-season_length:].to_numpy(dtype=float)
    return clip_forecast([
        season[i % season_length]
        for i in range(horizon)
    ])


def model_ses(train, horizon, **kwargs):
    train = safe_series(train)
    if len(train) < 3:
        return model_mean(train, horizon)
    try:
        fit = SimpleExpSmoothing(
            train.to_numpy(),
            initialization_method="estimated",
        ).fit(optimized=True)
        return clip_forecast(fit.forecast(horizon))
    except Exception:
        return model_mean(train, horizon)


def model_holt(train, horizon, **kwargs):
    train = safe_series(train)
    if len(train) < 6:
        return model_ses(train, horizon)
    try:
        fit = Holt(
            train.to_numpy(),
            damped_trend=True,
            initialization_method="estimated",
        ).fit(optimized=True)
        return clip_forecast(fit.forecast(horizon))
    except Exception:
        return model_ses(train, horizon)


def model_ets(train, horizon, season_length=SEASON_LENGTH, **kwargs):
    train = safe_series(train)

    if len(train) < 8:
        return model_ses(train, horizon)

    try:
        if len(train) >= 2 * season_length:
            fit = ExponentialSmoothing(
                train.to_numpy(),
                trend="add",
                damped_trend=True,
                seasonal="add",
                seasonal_periods=season_length,
                initialization_method="estimated",
            ).fit(optimized=True)
        else:
            fit = ExponentialSmoothing(
                train.to_numpy(),
                trend="add",
                damped_trend=True,
                seasonal=None,
                initialization_method="estimated",
            ).fit(optimized=True)

        return clip_forecast(fit.forecast(horizon))

    except Exception:
        return model_holt(train, horizon)


def model_arima_100(train, horizon, **kwargs):
    train = safe_series(train)
    if len(train) < 10:
        return model_ses(train, horizon)
    try:
        fit = SARIMAX(
            train.to_numpy(),
            order=(1, 0, 0),
            trend="c",
            enforce_stationarity=False,
            enforce_invertibility=False,
        ).fit(disp=False, maxiter=100)

        return clip_forecast(fit.forecast(horizon))
    except Exception:
        return model_ses(train, horizon)


def model_arima_011(train, horizon, **kwargs):
    train = safe_series(train)
    if len(train) < 10:
        return model_ses(train, horizon)
    try:
        fit = SARIMAX(
            train.to_numpy(),
            order=(0, 1, 1),
            trend=None,
            enforce_stationarity=False,
            enforce_invertibility=False,
        ).fit(disp=False, maxiter=100)

        return clip_forecast(fit.forecast(horizon))
    except Exception:
        return model_ses(train, horizon)


def model_weighted_mean(train, horizon, half_life=13, **kwargs):
    """Exponentially recency-weighted level forecast."""
    train = safe_series(train)
    if len(train) == 0:
        return np.zeros(horizon)

    ages = np.arange(len(train) - 1, -1, -1, dtype=float)
    half_life = max(float(half_life), 1.0)
    weights = np.power(0.5, ages / half_life)
    level = float(np.average(train.to_numpy(dtype=float), weights=weights))
    return np.repeat(max(level, 0.0), horizon)


def model_ewma(train, horizon, alpha=0.30, **kwargs):
    """Exponentially weighted moving-average level forecast."""
    train = safe_series(train)
    if len(train) == 0:
        return np.zeros(horizon)
    level = float(train.ewm(alpha=alpha, adjust=False).mean().iloc[-1])
    return np.repeat(max(level, 0.0), horizon)


def model_recent_median(train, horizon, window=13, **kwargs):
    """Robust recent-level baseline for erratic series with extreme spikes."""
    train = safe_series(train)
    if len(train) == 0:
        return np.zeros(horizon)
    window = min(int(window), len(train))
    level = float(train.iloc[-window:].median())
    return np.repeat(max(level, 0.0), horizon)


def model_recent_holt(train, horizon, window=26, **kwargs):
    """Damped Holt fitted only on the recent regime."""
    train = safe_series(train)
    if len(train) < 6:
        return model_ses(train, horizon)
    recent = train.iloc[-min(int(window), len(train)):].reset_index(drop=True)
    return model_holt(recent, horizon)


# ============================================================
# 4. INTERMITTENT-DEMAND MODELS
# ============================================================

def croston_rate(train, alpha=0.10):
    train = safe_series(train)
    y = train.to_numpy(dtype=float)

    nz = np.flatnonzero(y > 0)
    if len(nz) == 0:
        return 0.0

    first = int(nz[0])
    z = float(y[first])       # demand size
    p = float(first + 1)      # interval
    interval = 1.0

    for t in range(first + 1, len(y)):
        if y[t] > 0:
            z = z + alpha * (y[t] - z)
            p = p + alpha * (interval - p)
            interval = 1.0
        else:
            interval += 1.0

    return max(z / max(p, 1e-9), 0.0)


def model_croston(train, horizon, alpha=0.10, **kwargs):
    return np.repeat(croston_rate(train, alpha), horizon)


def model_sba(train, horizon, alpha=0.10, **kwargs):
    level = (1 - alpha / 2.0) * croston_rate(train, alpha)
    return np.repeat(max(level, 0.0), horizon)


def tsb_rate(train, alpha=0.10, beta=0.10):
    train = safe_series(train)
    y = train.to_numpy(dtype=float)

    nonzero = y[y > 0]
    if len(nonzero) == 0:
        return 0.0

    z = float(nonzero[0])
    p = float(np.mean(y > 0))

    for value in y:
        event = 1.0 if value > 0 else 0.0
        p = p + beta * (event - p)

        if value > 0:
            z = z + alpha * (value - z)

    return max(p * z, 0.0)


def model_tsb(train, horizon, alpha=0.10, beta=0.10, **kwargs):
    return np.repeat(tsb_rate(train, alpha, beta), horizon)


def model_adida(train, horizon, **kwargs):
    """
    Aggregate -> forecast -> disaggregate.
    Aggregation level is based on ADI.
    """
    train = safe_series(train)

    nz = int((train > 0).sum())
    if nz == 0:
        return np.zeros(horizon)

    adi = len(train) / nz
    k = max(2, int(round(adi)))

    y = train.to_numpy(dtype=float)
    aggregated = []

    for start in range(0, len(y), k):
        aggregated.append(float(np.sum(y[start:start + k])))

    agg_series = pd.Series(aggregated, dtype=float)
    agg_horizon = int(np.ceil(horizon / k))

    agg_fc = model_ses(agg_series, agg_horizon)
    weekly_fc = np.repeat(agg_fc / k, k)

    return clip_forecast(weekly_fc[:horizon])


def model_two_stage(
    train,
    horizon,
    occurrence_lookback=13,
    size_lookback=8,
    **kwargs,
):
    """
    Production-oriented hurdle model.

    Stage 1:
        Estimate probability that demand occurs in a week.

    Stage 2:
        Estimate expected demand size conditional on demand occurring.

    Weekly expected demand:
        P(demand occurs) * E(size | demand occurs)

    This avoids treating every zero week as evidence that future demand
    should simply be forecast as zero.
    """
    train = safe_series(train)

    if len(train) == 0:
        return np.zeros(horizon)

    occ_window = train.iloc[-min(occurrence_lookback, len(train)):]
    probability = float((occ_window > 0).mean())

    nonzero = train[train > 0]

    if len(nonzero) == 0:
        return np.zeros(horizon)

    size_window = nonzero.iloc[-min(size_lookback, len(nonzero)):]
    expected_size = float(size_window.mean())

    weekly_expected = probability * expected_size

    return np.repeat(
        max(weekly_expected, 0.0),
        horizon,
    )


def model_weighted_two_stage(train, horizon, decay=0.92, **kwargs):
    """
    Recency-weighted intermittent-demand model.
    Recent weeks have greater influence on both occurrence probability and
    conditional demand size, useful when an SKU changes velocity.
    """
    train = safe_series(train)
    if len(train) == 0:
        return np.zeros(horizon)

    y = train.to_numpy(dtype=float)
    ages = np.arange(len(y) - 1, -1, -1, dtype=float)
    weights = np.power(float(decay), ages)

    events = (y > 0).astype(float)
    probability = float(np.average(events, weights=weights))

    positive_mask = y > 0
    if not positive_mask.any():
        return np.zeros(horizon)

    size = float(np.average(y[positive_mask], weights=weights[positive_mask]))
    return np.repeat(max(probability * size, 0.0), horizon)


# ============================================================
# 5. MODEL SPECIFICATIONS
# ============================================================

def candidate_specs(pattern):
    """
    V3 candidate library. Recency-aware models are deliberately included
    alongside classical models rather than replacing them; rolling backtesting
    decides whether the recent regime or the longer history is more predictive.
    """

    recency_level_specs = [
        ("WMean_HL8", model_weighted_mean, {"half_life": 8}),
        ("WMean_HL13", model_weighted_mean, {"half_life": 13}),
        ("WMean_HL26", model_weighted_mean, {"half_life": 26}),
        ("EWMA_a0.20", model_ewma, {"alpha": 0.20}),
        ("EWMA_a0.35", model_ewma, {"alpha": 0.35}),
        ("EWMA_a0.50", model_ewma, {"alpha": 0.50}),
    ]

    if pattern == "Smooth":
        specs = [
            ("Naive", model_naive, {}),
            ("MA4", model_ma, {"window": 4}),
            ("MA8", model_ma, {"window": 8}),
            ("SeasonalNaive", model_seasonal_naive, {}),
            ("SES", model_ses, {}),
            ("Holt", model_holt, {}),
            ("RecentHolt26", model_recent_holt, {"window": 26}),
            ("RecentHolt52", model_recent_holt, {"window": 52}),
            ("ETS", model_ets, {}),
            *recency_level_specs,
        ]
        if USE_ARIMA:
            specs += [
                ("ARIMA_100", model_arima_100, {}),
                ("ARIMA_011", model_arima_011, {}),
            ]
        return specs

    if pattern == "Erratic":
        specs = [
            ("Mean", model_mean, {}),
            ("MA4", model_ma, {"window": 4}),
            ("MA8", model_ma, {"window": 8}),
            ("Median8", model_recent_median, {"window": 8}),
            ("Median13", model_recent_median, {"window": 13}),
            ("SES", model_ses, {}),
            ("Holt", model_holt, {}),
            ("RecentHolt26", model_recent_holt, {"window": 26}),
            ("ETS", model_ets, {}),
            *recency_level_specs,
        ]
        if USE_ARIMA:
            specs += [
                ("ARIMA_100", model_arima_100, {}),
                ("ARIMA_011", model_arima_011, {}),
            ]
        return specs

    if pattern in {"Intermittent", "Lumpy"}:
        specs = [
            ("Mean", model_mean, {}),
            ("WMean_HL8", model_weighted_mean, {"half_life": 8}),
            ("WMean_HL13", model_weighted_mean, {"half_life": 13}),
            ("WMean_HL26", model_weighted_mean, {"half_life": 26}),
            ("WMean_HL52", model_weighted_mean, {"half_life": 52}),
            ("ADIDA", model_adida, {}),
            ("WeightedTwoStage_d0.75", model_weighted_two_stage, {"decay": 0.75}),
            ("WeightedTwoStage_d0.85", model_weighted_two_stage, {"decay": 0.85}),
            ("WeightedTwoStage_d0.92", model_weighted_two_stage, {"decay": 0.92}),
            ("WeightedTwoStage_d0.97", model_weighted_two_stage, {"decay": 0.97}),
            ("WeightedTwoStage_d0.99", model_weighted_two_stage, {"decay": 0.99}),
        ]

        for alpha in CROSTON_ALPHA_GRID:
            specs.append((f"Croston_a{alpha}", model_croston, {"alpha": alpha}))
            specs.append((f"SBA_a{alpha}", model_sba, {"alpha": alpha}))

        for alpha in TSB_ALPHA_GRID:
            for beta in TSB_BETA_GRID:
                specs.append((
                    f"TSB_a{alpha}_b{beta}",
                    model_tsb,
                    {"alpha": alpha, "beta": beta},
                ))

        for occ_lb in OCCURRENCE_LOOKBACKS:
            for size_lb in SIZE_LOOKBACKS:
                specs.append((
                    f"TwoStage_o{occ_lb}_s{size_lb}",
                    model_two_stage,
                    {"occurrence_lookback": occ_lb, "size_lookback": size_lb},
                ))
        return specs

    return [
        ("Mean", model_mean, {}),
        ("MA4", model_ma, {"window": 4}),
        ("WMean_HL13", model_weighted_mean, {"half_life": 13}),
        ("SES", model_ses, {}),
    ]


# ============================================================
# 6. ROLLING BACKTEST
# ============================================================

def backtest_splits(n, test_horizon):
    splits = []

    for fold in range(BACKTEST_FOLDS, 0, -1):
        test_start = n - fold * test_horizon
        test_end = test_start + test_horizon

        if test_start < MIN_TRAIN_PERIODS:
            continue

        if test_end <= n:
            splits.append((test_start, test_end))

    return splits


def evaluate_model(series, pattern, model_name, function, kwargs):
    series = safe_series(series)

    test_horizon = (
        INTERMITTENT_TEST_HORIZON
        if pattern in {"Intermittent", "Lumpy"}
        else REGULAR_TEST_HORIZON
    )

    splits = backtest_splits(
        len(series),
        test_horizon,
    )

    if not splits:
        return None, []

    weekly_actual_all = []
    weekly_fc_all = []

    bucket_actual_all = []
    bucket_fc_all = []

    # Oldest -> newest fold pairs for recency-aware evaluation.
    weekly_fold_pairs = []
    bucket_fold_pairs = []

    fold_rows = []

    for fold_id, (test_start, test_end) in enumerate(splits, start=1):
        train = series.iloc[:test_start].copy()
        actual = series.iloc[test_start:test_end].to_numpy(dtype=float)

        try:
            forecast = function(
                train,
                len(actual),
                season_length=SEASON_LENGTH,
                **kwargs,
            )
            forecast = clip_forecast(forecast)

            if len(forecast) != len(actual):
                continue

        except Exception:
            continue

        weekly_m = metrics(actual, forecast)
        occ_m = occurrence_metrics(actual, forecast)

        actual_bucket = aggregate_blocks(actual, TARGET_BUCKET)
        fc_bucket = aggregate_blocks(forecast, TARGET_BUCKET)
        bucket_m = metrics(actual_bucket, fc_bucket)

        weekly_actual_all.extend(actual.tolist())
        weekly_fc_all.extend(forecast.tolist())

        bucket_actual_all.extend(actual_bucket.tolist())
        bucket_fc_all.extend(fc_bucket.tolist())
        weekly_fold_pairs.append((actual.copy(), forecast.copy()))
        bucket_fold_pairs.append((actual_bucket.copy(), fc_bucket.copy()))

        fold_rows.append({
            "Fold": fold_id,
            "TrainPeriods": len(train),
            "TestPeriods": len(actual),
            "ActualTotal": float(np.sum(actual)),
            "ForecastTotal": float(np.sum(forecast)),
            "WeeklyWAPE": weekly_m["WAPE"],
            "WeeklyMAE": weekly_m["MAE"],
            "WeeklyBiasPercent": weekly_m["BiasPercent"],
            "BucketWAPE": bucket_m["WAPE"],
            "BucketMAE": bucket_m["MAE"],
            "BucketBiasPercent": bucket_m["BiasPercent"],
            "OccurrenceAccuracy": occ_m["OccurrenceAccuracy"],
            "DemandEventRecall": occ_m["DemandEventRecall"],
        })

    if not weekly_actual_all:
        return None, []

    weekly_m = metrics(weekly_actual_all, weekly_fc_all)
    bucket_m = metrics(bucket_actual_all, bucket_fc_all)
    occ_m = occurrence_metrics(weekly_actual_all, weekly_fc_all)

    recent_weekly_m = recency_weighted_metrics(weekly_fold_pairs)
    recent_bucket_m = recency_weighted_metrics(bucket_fold_pairs)

    # V5 is explicitly sales-target aligned: the primary objective for EVERY
    # demand pattern is cumulative 4-week accuracy, because the business output
    # is a monthly sales target. Weekly WAPE remains a diagnostic and a very small
    # tie-breaker so that two models with similar monthly accuracy do not choose a
    # model with unnecessarily unstable weekly behavior.
    overall_error = bucket_m["WAPE"]
    recent_error = recent_bucket_m["WAPE"]
    overall_bias = bucket_m["BiasPercent"]
    recent_bias = recent_bucket_m["BiasPercent"]
    evaluation_basis = f"Recency-weighted {TARGET_BUCKET}-Week Bucket WAPE (Monthly Target Aligned)"

    # All-zero holdout protection.
    if pd.isna(overall_error):
        positive = series[series > 0]
        scale = float(positive.mean()) if len(positive) else 1.0
        overall_error = float(bucket_m["MAE"] / max(scale, 1e-9) * 100)

    if pd.isna(recent_error):
        recent_error = overall_error

    # Blend recent predictive quality with full-history stability.
    main_error = float(
        RECENT_ERROR_WEIGHT * recent_error
        + OVERALL_ERROR_WEIGHT * overall_error
    )

    # Weekly accuracy is NOT the primary target metric in V5. It is only a small
    # selection anchor/tie-breaker. This preserves weekly reasonableness without
    # sacrificing the monthly sales-target objective.
    weekly_overall_error = weekly_m["WAPE"]
    weekly_recent_error = recent_weekly_m["WAPE"]
    if pd.isna(weekly_overall_error):
        weekly_overall_error = main_error
    if pd.isna(weekly_recent_error):
        weekly_recent_error = weekly_overall_error
    weekly_anchor_error = float(
        RECENT_ERROR_WEIGHT * weekly_recent_error
        + OVERALL_ERROR_WEIGHT * weekly_overall_error
    )

    main_bias = recent_bias if pd.notna(recent_bias) else overall_bias
    bias_penalty = abs(main_bias) if pd.notna(main_bias) else 0.0
    selection_score = float(
        main_error
        + BIAS_WEIGHT * bias_penalty
        + WEEKLY_ANCHOR_WEIGHT * weekly_anchor_error
    )

    result = {
        "Model": model_name,
        "EvaluationBasis": evaluation_basis,
        "SelectionScore": selection_score,
        "MainError": main_error,
        "OverallMainError": overall_error,
        "RecentWeightedMainError": recent_error,
        "WeeklyWAPE": weekly_m["WAPE"],
        "WeeklyMAE": weekly_m["MAE"],
        "WeeklyRMSE": weekly_m["RMSE"],
        "WeeklyBiasPercent": weekly_m["BiasPercent"],
        "BucketWAPE": bucket_m["WAPE"],
        "BucketMAE": bucket_m["MAE"],
        "BucketRMSE": bucket_m["RMSE"],
        "BucketBiasPercent": bucket_m["BiasPercent"],
        "RecentWeeklyWAPE": recent_weekly_m["WAPE"],
        "RecentWeeklyBiasPercent": recent_weekly_m["BiasPercent"],
        "RecentBucketWAPE": recent_bucket_m["WAPE"],
        "RecentBucketBiasPercent": recent_bucket_m["BiasPercent"],
        "SelectionBiasPercent": main_bias,
        "OccurrenceAccuracy": occ_m["OccurrenceAccuracy"],
        "DemandEventRecall": occ_m["DemandEventRecall"],
        "BacktestFolds": len(fold_rows),
        "BacktestObservations": len(weekly_actual_all),
    }

    return result, fold_rows


# ============================================================
# 7. FUTURE DATES
# ============================================================

def future_periods(last_period, horizon):
    last_period = pd.Timestamp(last_period)
    return pd.date_range(
        start=last_period + pd.Timedelta(weeks=1),
        periods=horizon,
        freq="7D",
    )


def next_jalali_month_bounds(last_gregorian_date):
    """Return Gregorian start/end timestamps and YYYY/MM label for next Jalali month."""
    g = pd.Timestamp(last_gregorian_date).date()
    j = jdatetime.date.fromgregorian(date=g)
    if j.month == 12:
        ny, nm = j.year + 1, 1
    else:
        ny, nm = j.year, j.month + 1
    start_j = jdatetime.date(ny, nm, 1)
    if nm == 12:
        next2 = jdatetime.date(ny + 1, 1, 1)
    else:
        next2 = jdatetime.date(ny, nm + 1, 1)
    start_g = pd.Timestamp(start_j.togregorian())
    end_g = pd.Timestamp(next2.togregorian()) - pd.Timedelta(days=1)
    return start_g, end_g, f"{ny:04d}/{nm:02d}"


def weekly_forecast_to_daily(future_dates, weekly_values):
    """Spread each weekly forecast evenly across the 7 days ending at ForecastPeriod."""
    rows = []
    for period_end, value in zip(pd.to_datetime(future_dates), weekly_values):
        daily_value = float(value) / 7.0
        start = pd.Timestamp(period_end) - pd.Timedelta(days=6)
        for d in pd.date_range(start, pd.Timestamp(period_end), freq="D"):
            rows.append((d.normalize(), daily_value))
    if not rows:
        return pd.Series(dtype=float)
    df = pd.DataFrame(rows, columns=["Date", "Forecast"])
    return df.groupby("Date")["Forecast"].sum().sort_index()


def recent_momentum_factor(series):
    """Bounded short-vs-long recent demand momentum; intentionally conservative."""
    series = safe_series(series)
    if (not USE_RECENT_MOMENTUM) or len(series) < RECENT_WEEKS_LONG:
        return 1.0
    short_mean = float(series.iloc[-RECENT_WEEKS_SHORT:].mean())
    long_mean = float(series.iloc[-RECENT_WEEKS_LONG:].mean())
    if long_mean <= 1e-9:
        return 1.0
    raw_ratio = short_mean / long_mean
    adjusted = 1.0 + MOMENTUM_WEIGHT * (raw_ratio - 1.0)
    return float(np.clip(adjusted, MOMENTUM_MIN_FACTOR, MOMENTUM_MAX_FACTOR))


def robust_monthly_baseline(series):
    """Robust monthly-like baseline from recent non-overlapping 4-week totals."""
    series = safe_series(series)
    if len(series) == 0:
        return 0.0
    recent = series.iloc[-min(len(series), 24):].to_numpy(dtype=float)
    blocks = []
    # Work backward so the newest complete 4-week block is always represented.
    end = len(recent)
    while end >= 4 and len(blocks) < 6:
        start = end - 4
        blocks.append(float(np.sum(recent[start:end])))
        end = start
    if not blocks:
        return float(series.iloc[-min(4, len(series)):].sum())
    return float(np.median(blocks))


def calibrate_monthly_target(raw_target, series, reliability, status):
    """Reliability-aware target calibration with transparent management guardrails."""
    raw_target = max(float(raw_target), 0.0)
    baseline = robust_monthly_baseline(series)
    last4 = float(series.iloc[-4:].sum()) if len(series) else 0.0

    if reliability == "High":
        model_weight = 1.00
    elif reliability == "Medium":
        model_weight = MEDIUM_RELIABILITY_MODEL_WEIGHT
    elif reliability == "Low":
        model_weight = LOW_RELIABILITY_MODEL_WEIGHT
    else:
        model_weight = INSUFFICIENT_RELIABILITY_MODEL_WEIGHT

    if baseline > 1e-9:
        calibrated = model_weight * raw_target + (1.0 - model_weight) * baseline
        floor = baseline * TARGET_FLOOR_FACTOR
        cap = baseline * TARGET_CAP_FACTOR
        if status == "Dormant":
            cap = baseline * (DORMANT_ZERO_RECENT_CAP_FACTOR if last4 <= 1e-9 else DORMANT_CAP_FACTOR)
        calibrated = float(np.clip(calibrated, floor, cap))
    else:
        calibrated = raw_target if reliability in {"High", "Medium"} else model_weight * raw_target

    return max(calibrated, 0.0), baseline, model_weight



# ============================================================
# HIERARCHICAL CHANNEL HELPERS
# ============================================================

def classify_series_pattern(series):
    """
    Recalculate ADI/CV^2 for a Product x Channel series.
    Demand pattern belongs to the time series itself, so a channel does not
    automatically inherit the parent product's demand pattern.
    """
    series = safe_series(series)
    total_periods = len(series)
    nonzero = series[series > 0]
    nonzero_periods = len(nonzero)

    if total_periods < MIN_PERIODS or nonzero_periods < MIN_NONZERO_PERIODS:
        return "Insufficient Demand History", np.nan, np.nan

    adi = total_periods / nonzero_periods if nonzero_periods else np.nan
    mean_nonzero = float(nonzero.mean()) if nonzero_periods else np.nan
    std_nonzero = float(nonzero.std(ddof=1)) if nonzero_periods >= 2 else np.nan

    if (
        nonzero_periods >= 2
        and pd.notna(mean_nonzero)
        and abs(mean_nonzero) > 1e-9
    ):
        cv2 = (std_nonzero / mean_nonzero) ** 2
    else:
        cv2 = np.nan

    pattern = classify_demand_pattern(adi, cv2)
    return pattern, adi, cv2


def reliability_from_error(main_error):
    if pd.isna(main_error):
        return "Insufficient Backtest"
    if main_error <= HIGH_ERROR_THRESHOLD:
        return "High"
    if main_error <= MEDIUM_ERROR_THRESHOLD:
        return "Medium"
    return "Low"


def channel_model_weight(reliability):
    """How much the reconciled channel mix trusts the independently fitted channel model."""
    if reliability == "High":
        return CHANNEL_HIGH_MODEL_WEIGHT
    if reliability == "Medium":
        return CHANNEL_MEDIUM_MODEL_WEIGHT
    if reliability == "Low":
        return CHANNEL_LOW_MODEL_WEIGHT
    return CHANNEL_INSUFFICIENT_MODEL_WEIGHT


def last_nonblank(values):
    """Return the most recent nonblank value from a Series-like object."""
    s = pd.Series(values).dropna()
    if s.empty:
        return None
    s = s.astype("string").str.strip()
    s = s[s != ""]
    return None if s.empty else str(s.iloc[-1])


def build_channel_weekly_series(channel_rows, common_end):
    """Build a zero-filled weekly series for one Product x Channel."""
    if channel_rows.empty:
        return pd.Series(dtype=float), pd.DatetimeIndex([])

    daily = (
        channel_rows.groupby("InvoiceGDate_Calc", as_index=False)[TARGET_COL]
        .sum()
        .sort_values("InvoiceGDate_Calc")
    )

    s = (
        daily.set_index("InvoiceGDate_Calc")[TARGET_COL]
        .resample(DEMAND_FREQUENCY)
        .sum()
    )

    if s.empty:
        return pd.Series(dtype=float), pd.DatetimeIndex([])

    full_index = pd.date_range(
        start=s.index.min(),
        end=pd.Timestamp(common_end),
        freq=DEMAND_FREQUENCY,
    )
    s = s.reindex(full_index, fill_value=0.0)

    if CLIP_NEGATIVE:
        s = s.clip(lower=0.0)

    return safe_series(s.values), full_index


def reconcile_rounded_targets(rows, product_target):
    """
    Round channel targets to two decimals while preserving exact product-level coherence.
    The rounding residual is assigned to the largest allocation.
    """
    if not rows:
        return rows

    raw = np.asarray([float(r["AllocationShare"]) * float(product_target) for r in rows])
    rounded = np.round(raw, 2)
    residual = round(float(product_target) - float(rounded.sum()), 2)

    if len(rounded) and abs(residual) >= 0.005:
        idx = int(np.argmax(raw))
        rounded[idx] = round(float(rounded[idx]) + residual, 2)

    for i, r in enumerate(rows):
        r["ChannelSalesTarget"] = float(rounded[i])

    return rows


# ============================================================
# 8. IN-MEMORY HANDOFF FROM DEMAND CLASSIFICATION
# ============================================================

print("\n========================================")
print("SALES TARGET FORECASTING ENGINE V4 - FORECAST + SMART TARGET + CHANNEL")
print("========================================")
print("Using in-memory diagnostic tables (no Excel input dependency).")

# The classification pipeline above already created these DataFrames.
product_diag = product_diagnostic.copy()
demand_ts = demand_time_series.copy()

required_diag = {
    "ProductCode",
    "ActivityStatus",
    "DemandPattern",
}
required_ts = {
    "ProductCode",
    "Period",
    "Demand",
}

missing_diag = required_diag - set(product_diag.columns)
missing_ts = required_ts - set(demand_ts.columns)

if missing_diag:
    raise KeyError(
        "Missing in-memory Product_Diagnostic columns: "
        + ", ".join(sorted(missing_diag))
    )

if missing_ts:
    raise KeyError(
        "Missing in-memory Demand_Time_Series columns: "
        + ", ".join(sorted(missing_ts))
    )

product_diag["ProductCode"] = (
    product_diag["ProductCode"]
    .apply(clean_product_code)
)

demand_ts["ProductCode"] = (
    demand_ts["ProductCode"]
    .apply(clean_product_code)
)

demand_ts["Period"] = pd.to_datetime(
    demand_ts["Period"],
    errors="coerce",
)

demand_ts["Demand"] = pd.to_numeric(
    demand_ts["Demand"],
    errors="coerce",
).fillna(0.0)

if CLIP_NEGATIVE:
    demand_ts["Demand"] = demand_ts["Demand"].clip(lower=0.0)

demand_ts = (
    demand_ts
    .dropna(subset=["ProductCode", "Period"])
    .sort_values(["ProductCode", "Period"])
    .reset_index(drop=True)
)

eligible = (
    product_diag[
        product_diag["ActivityStatus"].isin(FORECAST_STATUSES)
    ]
    .copy()
)

excluded = (
    product_diag[
        ~product_diag["ActivityStatus"].isin(FORECAST_STATUSES)
    ]
    .copy()
)

print("Eligible products:", eligible["ProductCode"].nunique())
print("\nPatterns:")
print(eligible["DemandPattern"].value_counts(dropna=False))


# ============================================================
# HISTORICAL TARGET SIGNAL FOR EACH PRODUCT
# ============================================================

def historical_target_signal_for_product(product_code):
    """
    Return historical management-target diagnostics for a product hierarchy.

    V4 IMPORTANT:
    Historical TargetQuantity is NOT allowed to change the statistical forecast
    or the recommended target.  Old targets can be budget/management aspirations
    and may have very low achievement; using them as a numeric forecast anchor
    would contaminate a demand forecast.  We therefore export their trend and
    achievement only for management review.
    """
    default = {
        "HistoryRows": 0,
        "TargetTrendFactor": 1.0,
        "AchievementRate": np.nan,
        "AchievementFactor": 1.0,
        "HistoricalTargetFactor": 1.0,
        "MatchedDimensions": "None",
        "HistoryLatestYearMonth": np.nan,
        "RecentAverageTarget": np.nan,
        "RecentAverageSale": np.nan,
    }
    if not USE_HISTORICAL_TARGET_SIGNAL or sale_target_history.empty:
        return default

    product_rows = analysis_data[
        analysis_data["ProductCode"].apply(clean_product_code) == clean_product_code(product_code)
    ].copy()
    if product_rows.empty:
        return default

    available = []
    for target_dim, source_col in SALEINVOICE_TARGET_DIM_COLS.items():
        if source_col is not None and source_col in product_rows.columns:
            product_rows[target_dim + "__key"] = product_rows[source_col].apply(normalize_key)
            if product_rows[target_dim + "__key"].notna().any():
                available.append(target_dim)
    if not available:
        return default

    combos = product_rows[[d + "__key" for d in available]].drop_duplicates()
    matched_parts = []
    for _, combo in combos.iterrows():
        hist = sale_target_history.copy()
        for dim in available:
            val = combo[dim + "__key"]
            if val is not None and not pd.isna(val):
                hist = hist[hist[dim] == val]
        if not hist.empty:
            matched_parts.append(hist)
    if not matched_parts:
        return default

    hist = pd.concat(matched_parts, ignore_index=True).drop_duplicates()
    monthly = (
        hist.groupby("YearMonthSort", as_index=False)[["TargetQuantity", "SaleQuantity"]]
        .sum().sort_values("YearMonthSort").tail(TARGET_HISTORY_MONTHS).reset_index(drop=True)
    )
    default["HistoryRows"] = int(len(monthly))
    default["MatchedDimensions"] = ",".join(available)
    if len(monthly):
        default["HistoryLatestYearMonth"] = int(monthly["YearMonthSort"].max())
    if len(monthly) < MIN_TARGET_HISTORY_ROWS:
        return default

    recent_n = min(TARGET_HISTORY_RECENT_MONTHS, len(monthly))
    recent = monthly.tail(recent_n)
    previous = monthly.iloc[:-recent_n].tail(recent_n)

    recent_target = float(recent["TargetQuantity"].mean()) if len(recent) else np.nan
    recent_sale = float(recent["SaleQuantity"].mean()) if len(recent) else np.nan
    previous_target = float(previous["TargetQuantity"].mean()) if len(previous) else np.nan

    if pd.notna(previous_target) and previous_target > 0 and pd.notna(recent_target):
        trend = recent_target / previous_target
    else:
        y = recent["TargetQuantity"].to_numpy(dtype=float)
        trend = y[-1] / float(np.mean(y[:-1])) if len(y) >= 2 and float(np.mean(y[:-1])) > 0 else 1.0
    trend = float(np.clip(trend, TARGET_HISTORY_MIN_FACTOR, TARGET_HISTORY_MAX_FACTOR))

    target_sum = float(recent["TargetQuantity"].sum())
    sale_sum = float(recent["SaleQuantity"].sum())
    achievement = sale_sum / target_sum if target_sum > 0 else np.nan

    # Diagnostic only. The factor is deliberately fixed at 1.0 in V4.
    achievement_factor = float(np.clip(achievement, 0.0, 2.0)) if pd.notna(achievement) else 1.0

    return {
        "HistoryRows": int(len(monthly)),
        "TargetTrendFactor": trend,
        "AchievementRate": achievement,
        "AchievementFactor": achievement_factor,
        "HistoricalTargetFactor": 1.0,
        "MatchedDimensions": ",".join(available),
        "HistoryLatestYearMonth": int(monthly["YearMonthSort"].max()),
        "RecentAverageTarget": recent_target,
        "RecentAverageSale": recent_sale,
    }


def smart_stretch_percent(reliability, status, pattern, last4, prev4):
    """V4 management stretch based on achievable sales signals, not old target size."""
    if not SMART_STRETCH_ENABLED:
        return 0.0, "Disabled"

    stretch = float(SMART_STRETCH_BY_RELIABILITY.get(str(reliability), 0.02))
    reasons = [f"Reliability={reliability}"]

    if pd.notna(prev4) and float(prev4) > 0:
        ratio = float(last4) / float(prev4)
        if ratio >= 1.10:
            stretch += SMART_STRETCH_MOMENTUM_BONUS_STRONG
            reasons.append("Strong positive 4W momentum")
        elif ratio >= 1.03:
            stretch += SMART_STRETCH_MOMENTUM_BONUS_MILD
            reasons.append("Positive 4W momentum")
        elif ratio <= 0.90:
            stretch -= SMART_STRETCH_MOMENTUM_PENALTY_STRONG
            reasons.append("Strong negative 4W momentum")
        elif ratio <= 0.97:
            stretch -= SMART_STRETCH_MOMENTUM_PENALTY_MILD
            reasons.append("Negative 4W momentum")

    stretch = float(np.clip(stretch, SMART_STRETCH_MIN, SMART_STRETCH_MAX))

    if str(pattern) == "Intermittent":
        stretch = min(stretch, SMART_STRETCH_INTERMITTENT_CAP)
        reasons.append("Intermittent cap")
    elif str(pattern) == "Lumpy":
        stretch = min(stretch, SMART_STRETCH_LUMPY_CAP)
        reasons.append("Lumpy cap")

    if str(status) == "Dormant":
        stretch = min(stretch, SMART_STRETCH_DORMANT_CAP)
        reasons.append("Dormant cap")

    return float(np.clip(stretch, SMART_STRETCH_MIN, SMART_STRETCH_MAX)), "; ".join(reasons)

# ============================================================
# 9. FORECAST LOOP
# ============================================================

leaderboard_rows = []
fold_rows_all = []
champion_rows = []
weekly_future_rows = []
target_bucket_rows = []
monthly_target_rows = []
error_rows = []

codes = (
    eligible["ProductCode"]
    .dropna()
    .astype(str)
    .unique()
    .tolist()
)

for number, product_code in enumerate(codes, start=1):

    info_rows = eligible[
        eligible["ProductCode"] == product_code
    ]

    if info_rows.empty:
        continue

    info = info_rows.iloc[0]

    pattern = str(
        info.get("DemandPattern", "Unknown")
    ).strip()

    status = info.get("ActivityStatus")
    name = info.get("OrderProductName")

    product_ts = (
        demand_ts[
            demand_ts["ProductCode"] == product_code
        ]
        .sort_values("Period")
        .copy()
    )

    if product_ts.empty:
        error_rows.append({
            "ProductCode": product_code,
            "Error": "No Demand_Time_Series rows.",
        })
        continue

    series = safe_series(product_ts["Demand"])
    periods = product_ts["Period"].reset_index(drop=True)

    specs = candidate_specs(pattern)
    product_results = []

    for model_name, function, kwargs in specs:
        try:
            result, fold_rows = evaluate_model(
                series,
                pattern,
                model_name,
                function,
                kwargs,
            )

            if result is None:
                continue

            row = {
                "ProductCode": product_code,
                "ProductName": name,
                "ActivityStatus": status,
                "DemandPattern": pattern,
                **result,
            }

            leaderboard_rows.append(row)
            product_results.append((row, function, kwargs))

            for fr in fold_rows:
                fold_rows_all.append({
                    "ProductCode": product_code,
                    "DemandPattern": pattern,
                    "Model": model_name,
                    **fr,
                })

        except Exception as exc:
            error_rows.append({
                "ProductCode": product_code,
                "Model": model_name,
                "Error": str(exc),
            })

    if not product_results:
        # fallback if backtesting is impossible
        champion_name = "Mean"
        champion_function = model_mean
        champion_kwargs = {}
        champion_result = {
            "EvaluationBasis": "Insufficient Backtest",
            "SelectionScore": np.nan,
            "MainError": np.nan,
            "OverallMainError": np.nan,
            "RecentWeightedMainError": np.nan,
            "WeeklyWAPE": np.nan,
            "WeeklyMAE": np.nan,
            "WeeklyRMSE": np.nan,
            "WeeklyBiasPercent": np.nan,
            "BucketWAPE": np.nan,
            "BucketMAE": np.nan,
            "BucketRMSE": np.nan,
            "BucketBiasPercent": np.nan,
            "RecentWeeklyWAPE": np.nan,
            "RecentWeeklyBiasPercent": np.nan,
            "RecentBucketWAPE": np.nan,
            "RecentBucketBiasPercent": np.nan,
            "SelectionBiasPercent": np.nan,
            "OccurrenceAccuracy": np.nan,
            "DemandEventRecall": np.nan,
            "BacktestFolds": 0,
            "BacktestObservations": 0,
        }
    else:
        product_results.sort(
            key=lambda item: (
                np.inf
                if pd.isna(item[0]["SelectionScore"])
                else item[0]["SelectionScore"]
            )
        )

        champion_row, champion_function, champion_kwargs = product_results[0]
        champion_name = champion_row["Model"]
        champion_result = champion_row

    try:
        future_fc = champion_function(
            series,
            FORECAST_HORIZON,
            season_length=SEASON_LENGTH,
            **champion_kwargs,
        )
        future_fc = clip_forecast(future_fc)

    except Exception as exc:
        error_rows.append({
            "ProductCode": product_code,
            "Model": champion_name,
            "Error": "Final fit failed: " + str(exc),
        })

        champion_name = "Mean"
        future_fc = model_mean(series, FORECAST_HORIZON)

    # Conservative correction for persistent recent backtest bias.
    selection_bias = champion_result.get("SelectionBiasPercent", np.nan)
    correction_factor = bias_correction_factor(
        selection_bias,
        int(champion_result.get("BacktestFolds", 0) or 0),
    )
    planning_fc = clip_forecast(future_fc * correction_factor)

    future_dates = future_periods(
        periods.max(),
        FORECAST_HORIZON,
    )

    main_error = champion_result.get("MainError", np.nan)

    reliability = reliability_from_error(main_error)

    if status == "Dormant":
        flag = "Review - Dormant"
    elif pattern == "Lumpy":
        flag = "Review - Lumpy / High Uncertainty"
    elif reliability == "Low":
        flag = "Review - Low Accuracy"
    else:
        flag = "Normal"

    # Management summary
    champion_rows.append({
        "ProductCode": product_code,
        "ProductName": name,
        "ActivityStatus": status,
        "DemandPattern": pattern,
        "HistoryPeriods": len(series),
        "NonZeroPeriods": int((series > 0).sum()),
        "ZeroDemandPercent": round(float((series == 0).mean() * 100), 2),
        "ChampionModel": champion_name,
        "EvaluationBasis": champion_result.get("EvaluationBasis"),
        "SelectionScore": champion_result.get("SelectionScore"),
        "MainBacktestError": main_error,
        "OverallMainBacktestError": champion_result.get("OverallMainError"),
        "RecentWeightedMainError": champion_result.get("RecentWeightedMainError"),
        "WeeklyWAPE": champion_result.get("WeeklyWAPE"),
        "FourWeekWAPE": champion_result.get("BucketWAPE"),
        "WeeklyBiasPercent": champion_result.get("WeeklyBiasPercent"),
        "FourWeekBiasPercent": champion_result.get("BucketBiasPercent"),
        "RecentWeeklyWAPE": champion_result.get("RecentWeeklyWAPE"),
        "RecentFourWeekWAPE": champion_result.get("RecentBucketWAPE"),
        "SelectionBiasPercent": selection_bias,
        "BiasCorrectionFactor": correction_factor,
        "OccurrenceAccuracy": champion_result.get("OccurrenceAccuracy"),
        "DemandEventRecall": champion_result.get("DemandEventRecall"),
        "BacktestFolds": champion_result.get("BacktestFolds"),
        "ForecastHorizonWeeks": FORECAST_HORIZON,
        "BaseForecastTotal12Weeks": round(float(np.sum(future_fc)), 2),
        "ForecastTotal12Weeks": round(float(np.sum(planning_fc)), 2),
        "BaseForecastAverageWeek": round(float(np.mean(future_fc)), 2),
        "ForecastAverageWeek": round(float(np.mean(planning_fc)), 2),
        "ForecastReliability": reliability,
        "ManagementFlag": flag,
    })

    # Weekly forecast
    for step, (date, value) in enumerate(
        zip(future_dates, future_fc),
        start=1,
    ):
        weekly_future_rows.append({
            "ProductCode": product_code,
            "ProductName": name,
            "ActivityStatus": status,
            "DemandPattern": pattern,
            "ChampionModel": champion_name,
            "ForecastWeek": step,
            "ForecastPeriod": date,
            "BaseForecastDemand": round(float(value), 4),
            "ForecastDemand": round(float(planning_fc[step - 1]), 4),
            "BiasCorrectionFactor": correction_factor,
            "ForecastReliability": reliability,
            "ManagementFlag": flag,
        })

    # Next Jalali-month sales target.
    # For low-reliability products, do not trust the champion alone: blend it with
    # a robust recency-weighted baseline before applying a small bounded momentum factor.
    target_fc = planning_fc.copy()
    momentum_factor = recent_momentum_factor(series)
    target_fc = clip_forecast(target_fc * momentum_factor)

    month_start, month_end, target_month = next_jalali_month_bounds(periods.max())
    daily_target = weekly_forecast_to_daily(future_dates, target_fc)
    monthly_base_daily = weekly_forecast_to_daily(future_dates, future_fc)

    month_mask = (daily_target.index >= month_start) & (daily_target.index <= month_end)
    base_month_mask = (monthly_base_daily.index >= month_start) & (monthly_base_daily.index <= month_end)
    statistical_month_forecast = float(daily_target.loc[month_mask].sum()) if len(daily_target) else 0.0
    base_month_forecast = float(monthly_base_daily.loc[base_month_mask].sum()) if len(monthly_base_daily) else 0.0

    # V4 separates expected sales from the management target.
    # 1) Expected sales = statistical forecast after reliability calibration.
    # 2) Recommended target = expected sales + bounded smart stretch.
    # 3) Historical management targets are diagnostics only.
    raw_sales_target = max(statistical_month_forecast * (1.0 + TARGET_STRETCH_PCT), 0.0)
    sales_forecast, robust_baseline, target_model_weight = calibrate_monthly_target(
        raw_sales_target, series, reliability, status
    )

    # Recent monthly-like references for management sanity-checking.
    last4 = float(series.iloc[-4:].sum()) if len(series) else 0.0
    prev4 = float(series.iloc[-8:-4].sum()) if len(series) >= 8 else np.nan
    recent13_avg4 = float(series.iloc[-13:].mean() * 4.0) if len(series) else 0.0

    smart_stretch, smart_stretch_reason = smart_stretch_percent(
        reliability, status, pattern, last4, prev4
    )
    recommended_sales_target = max(float(sales_forecast) * (1.0 + smart_stretch), 0.0)

    # Preserve existing broad safety guardrails after smart stretch.
    if robust_baseline > 0:
        lower_bound = robust_baseline * TARGET_FLOOR_FACTOR
        upper_bound = robust_baseline * TARGET_CAP_FACTOR
        if status == "Dormant":
            upper_bound = min(upper_bound, robust_baseline * DORMANT_CAP_FACTOR)
        recommended_sales_target = float(
            np.clip(recommended_sales_target, lower_bound, upper_bound)
        )

    target_history = historical_target_signal_for_product(product_code)

    # Backward-compatible alias used by the hierarchical channel layer.
    # In V4 SalesTarget means the RECOMMENDED management target.
    sales_target = float(recommended_sales_target)

    monthly_target_rows.append({
        "ProductCode": product_code,
        "ProductName": name,
        "ActivityStatus": status,
        "DemandPattern": pattern,
        "TargetMonthJalali": target_month,
        "TargetMonthStartG": month_start,
        "TargetMonthEndG": month_end,
        "ChampionModel": champion_name,
        "BaseMonthlyForecast": round(base_month_forecast, 2),
        "BiasAdjustedMonthlyForecast": round(statistical_month_forecast / max(momentum_factor, 1e-9), 2),
        "MomentumFactor": round(momentum_factor, 4),
        "StatisticalMonthlyForecast": round(statistical_month_forecast, 2),
        "RawSalesTarget": round(raw_sales_target, 2),
        "RobustRecent4WBaseline": round(robust_baseline, 2),
        "TargetModelWeight": round(target_model_weight, 2),
        "SalesForecast": round(sales_forecast, 2),
        "SmartStretchPercent": round(smart_stretch * 100.0, 2),
        "SmartStretchReason": smart_stretch_reason,
        "RecommendedSalesTarget": round(recommended_sales_target, 2),
        "SalesTarget": round(sales_target, 2),
        "TargetHistoryRows": target_history["HistoryRows"],
        "TargetHistoryLatestYearMonth": target_history["HistoryLatestYearMonth"],
        "TargetHistoryMatchedDimensions": target_history["MatchedDimensions"],
        "HistoricalTargetTrendFactor": round(target_history["TargetTrendFactor"], 4),
        "HistoricalTargetAchievementRate": round(target_history["AchievementRate"], 4) if pd.notna(target_history["AchievementRate"]) else np.nan,
        "HistoricalTargetRecentAvgTarget": round(target_history["RecentAverageTarget"], 2) if pd.notna(target_history["RecentAverageTarget"]) else np.nan,
        "HistoricalTargetRecentAvgSale": round(target_history["RecentAverageSale"], 2) if pd.notna(target_history["RecentAverageSale"]) else np.nan,
        "HistoricalTargetCalibrationFactor": 1.0,
        "HistoricalTargetUsage": "Diagnostic only - not used to calculate V4 target",
        "Last4WeeksActual": round(last4, 2),
        "Previous4WeeksActual": round(prev4, 2) if pd.notna(prev4) else np.nan,
        "Recent13WeekEquivalent4W": round(recent13_avg4, 2),
        "MainBacktestError": main_error,
        "ForecastReliability": reliability,
        "ManagementFlag": flag,
    })

    # 4-week sales-target diagnostic buckets
    for bucket_no, start in enumerate(
        range(0, FORECAST_HORIZON, TARGET_BUCKET),
        start=1,
    ):
        end = min(start + TARGET_BUCKET, FORECAST_HORIZON)

        base_bucket_values = future_fc[start:end]
        bucket_values = planning_fc[start:end]
        bucket_dates = future_dates[start:end]

        target_bucket_rows.append({
            "ProductCode": product_code,
            "ProductName": name,
            "ActivityStatus": status,
            "DemandPattern": pattern,
            "ChampionModel": champion_name,
            "TargetBucket": bucket_no,
            "FromDate": bucket_dates.min(),
            "ToDate": bucket_dates.max(),
            "WeeksInBucket": len(bucket_values),
            "BaseForecastDemand": round(float(np.sum(base_bucket_values)), 2),
            "ForecastDemand": round(float(np.sum(bucket_values)), 2),
            "BiasCorrectionFactor": correction_factor,
            "ForecastReliability": reliability,
            "ManagementFlag": flag,
        })

    if number == 1 or number % 20 == 0 or number == len(codes):
        print(f"Processed {number}/{len(codes)} products")


# ============================================================
# 10. OUTPUT TABLES
# ============================================================

leaderboard = pd.DataFrame(leaderboard_rows)
backtest_details = pd.DataFrame(fold_rows_all)
champions = pd.DataFrame(champion_rows)
weekly_forecast = pd.DataFrame(weekly_future_rows)
target_bucket_plan = pd.DataFrame(target_bucket_rows)
monthly_sales_target = pd.DataFrame(monthly_target_rows)
errors = pd.DataFrame(error_rows)

if not leaderboard.empty:
    leaderboard = (
        leaderboard
        .sort_values(
            ["ProductCode", "SelectionScore"],
            na_position="last",
        )
        .reset_index(drop=True)
    )

if not champions.empty:
    champions = (
        champions
        .sort_values(
            ["ActivityStatus", "DemandPattern", "ProductCode"]
        )
        .reset_index(drop=True)
    )

if not weekly_forecast.empty:
    weekly_forecast = (
        weekly_forecast
        .sort_values(["ProductCode", "ForecastWeek"])
        .reset_index(drop=True)
    )

if not target_bucket_plan.empty:
    target_bucket_plan = (
        target_bucket_plan
        .sort_values(["ProductCode", "TargetBucket"])
        .reset_index(drop=True)
    )

if not monthly_sales_target.empty:
    monthly_sales_target = (
        monthly_sales_target
        .sort_values(["TargetMonthJalali", "ProductCode"])
        .reset_index(drop=True)
    )


# ============================================================
# 10B. HIERARCHICAL PRODUCT x SALES-CHANNEL FORECASTING
# ============================================================

channel_target_rows = []
channel_champion_rows = []
channel_leaderboard_rows = []
channel_backtest_rows = []
channel_reconciliation_rows = []
channel_error_rows = []

# Use the same filtered sales universe as the product model, but keep channel detail.
channel_source = analysis_data.dropna(
    subset=["ProductCode", CHANNEL_CODE_COL, "InvoiceGDate_Calc"]
).copy()

channel_source["ProductCode"] = (
    channel_source["ProductCode"].apply(clean_product_code)
)
channel_source[CHANNEL_CODE_COL] = (
    channel_source[CHANNEL_CODE_COL].astype("string").str.strip()
)

# Fast lookup of final coherent parent-product targets.
product_target_lookup = {}
if not monthly_sales_target.empty:
    product_target_lookup = (
        monthly_sales_target
        .drop_duplicates("ProductCode")
        .set_index("ProductCode")
        .to_dict("index")
    )

eligible_lookup = (
    eligible
    .drop_duplicates("ProductCode")
    .set_index("ProductCode")
    .to_dict("index")
)

for product_no, (product_code, product_target_info) in enumerate(
    product_target_lookup.items(),
    start=1,
):
    product_rows = channel_source[
        channel_source["ProductCode"] == product_code
    ].copy()

    if product_rows.empty:
        channel_error_rows.append({
            "ProductCode": product_code,
            "Error": "No usable sales-channel rows for this forecasted product.",
        })
        continue

    product_meta = eligible_lookup.get(product_code, {})
    product_name = product_target_info.get(
        "ProductName",
        product_meta.get("OrderProductName"),
    )
    product_status = product_target_info.get(
        "ActivityStatus",
        product_meta.get("ActivityStatus"),
    )
    product_pattern = product_target_info.get(
        "DemandPattern",
        product_meta.get("DemandPattern"),
    )
    product_sales_forecast = float(product_target_info.get("SalesForecast", 0.0) or 0.0)
    product_smart_stretch_pct = float(product_target_info.get("SmartStretchPercent", 0.0) or 0.0)
    product_sales_target = float(product_target_info.get("SalesTarget", 0.0) or 0.0)
    target_month = product_target_info.get("TargetMonthJalali")
    month_start = pd.Timestamp(product_target_info.get("TargetMonthStartG"))
    month_end = pd.Timestamp(product_target_info.get("TargetMonthEndG"))

    # Product periods are the authoritative horizon so all child channels align.
    product_ts = demand_ts[demand_ts["ProductCode"] == product_code].sort_values("Period")
    if product_ts.empty:
        channel_error_rows.append({
            "ProductCode": product_code,
            "Error": "Parent product has no Demand_Time_Series rows for channel alignment.",
        })
        continue

    common_last_period = pd.Timestamp(product_ts["Period"].max())
    future_dates = future_periods(common_last_period, FORECAST_HORIZON)

    # Most recent descriptive metadata for each channel.
    product_rows = product_rows.sort_values("InvoiceGDate_Calc")

    channel_codes = (
        product_rows[CHANNEL_CODE_COL]
        .dropna()
        .astype(str)
        .unique()
        .tolist()
    )

    temporary_rows = []

    for channel_code in channel_codes:
        ch_rows = product_rows[
            product_rows[CHANNEL_CODE_COL].astype(str) == str(channel_code)
        ].copy()

        if ch_rows.empty:
            continue

        channel_name = (
            last_nonblank(ch_rows[CHANNEL_NAME_COL])
            if CHANNEL_NAME_COL is not None
            else None
        )
        brand = (
            last_nonblank(ch_rows[BRAND_COL])
            if BRAND_COL is not None
            else None
        )

        series, channel_periods = build_channel_weekly_series(
            ch_rows,
            common_last_period,
        )

        if len(series) == 0:
            channel_error_rows.append({
                "ProductCode": product_code,
                "SalesChannelCode": channel_code,
                "Error": "Unable to build weekly channel series.",
            })
            continue

        channel_pattern, channel_adi, channel_cv2 = classify_series_pattern(series)
        specs = candidate_specs(channel_pattern)
        channel_results = []

        for model_name, function, kwargs in specs:
            try:
                result, fold_rows = evaluate_model(
                    series,
                    channel_pattern,
                    model_name,
                    function,
                    kwargs,
                )

                if result is None:
                    continue

                lr = {
                    "ProductCode": product_code,
                    "ProductName": product_name,
                    "Brand": brand,
                    "SalesChannelCode": channel_code,
                    "SalesChannelName": channel_name,
                    "ProductDemandPattern": product_pattern,
                    "ChannelDemandPattern": channel_pattern,
                    **result,
                }
                channel_leaderboard_rows.append(lr)
                channel_results.append((lr, function, kwargs))

                for fr in fold_rows:
                    channel_backtest_rows.append({
                        "ProductCode": product_code,
                        "SalesChannelCode": channel_code,
                        "SalesChannelName": channel_name,
                        "ChannelDemandPattern": channel_pattern,
                        "Model": model_name,
                        **fr,
                    })

            except Exception as exc:
                channel_error_rows.append({
                    "ProductCode": product_code,
                    "SalesChannelCode": channel_code,
                    "Model": model_name,
                    "Error": str(exc),
                })

        if channel_results:
            channel_results.sort(
                key=lambda item: (
                    np.inf
                    if pd.isna(item[0]["SelectionScore"])
                    else item[0]["SelectionScore"]
                )
            )
            champion_row, champion_function, champion_kwargs = channel_results[0]
            champion_name = champion_row["Model"]
            champion_result = champion_row
        else:
            champion_name = "Mean"
            champion_function = model_mean
            champion_kwargs = {}
            champion_result = {
                "EvaluationBasis": "Insufficient Backtest",
                "SelectionScore": np.nan,
                "MainError": np.nan,
                "SelectionBiasPercent": np.nan,
                "BacktestFolds": 0,
                "WeeklyWAPE": np.nan,
                "BucketWAPE": np.nan,
                "BucketBiasPercent": np.nan,
                "RecentBucketWAPE": np.nan,
            }

        try:
            channel_future_fc = champion_function(
                series,
                FORECAST_HORIZON,
                season_length=SEASON_LENGTH,
                **champion_kwargs,
            )
            channel_future_fc = clip_forecast(channel_future_fc)
        except Exception as exc:
            channel_error_rows.append({
                "ProductCode": product_code,
                "SalesChannelCode": channel_code,
                "Model": champion_name,
                "Error": "Channel final fit failed: " + str(exc),
            })
            champion_name = "Mean"
            channel_future_fc = model_mean(series, FORECAST_HORIZON)

        channel_bias = champion_result.get("SelectionBiasPercent", np.nan)
        channel_correction_factor = bias_correction_factor(
            channel_bias,
            int(champion_result.get("BacktestFolds", 0) or 0),
        )

        channel_planning_fc = clip_forecast(
            channel_future_fc * channel_correction_factor
        )
        channel_momentum = recent_momentum_factor(series)
        channel_target_fc = clip_forecast(
            channel_planning_fc * channel_momentum
        )

        channel_daily_target = weekly_forecast_to_daily(
            future_dates,
            channel_target_fc,
        )
        channel_month_mask = (
            (channel_daily_target.index >= month_start)
            & (channel_daily_target.index <= month_end)
        )
        raw_channel_month_forecast = (
            float(channel_daily_target.loc[channel_month_mask].sum())
            if len(channel_daily_target)
            else 0.0
        )

        channel_main_error = champion_result.get("MainError", np.nan)
        channel_reliability = reliability_from_error(channel_main_error)
        model_weight = channel_model_weight(channel_reliability)

        recent_share_actual = float(
            series.iloc[-min(CHANNEL_SHARE_LOOKBACK_WEEKS, len(series)):].sum()
        )
        last4_actual = float(series.iloc[-4:].sum()) if len(series) else 0.0
        previous4_actual = (
            float(series.iloc[-8:-4].sum())
            if len(series) >= 8
            else np.nan
        )

        temporary_rows.append({
            "ProductCode": product_code,
            "ProductName": product_name,
            "Brand": brand,
            "ActivityStatus": product_status,
            "ProductDemandPattern": product_pattern,
            "SalesChannelCode": str(channel_code),
            "SalesChannelName": channel_name,
            "ChannelDemandPattern": channel_pattern,
            "ChannelADI": channel_adi,
            "ChannelCV2": channel_cv2,
            "ChannelHistoryPeriods": len(series),
            "ChannelNonZeroPeriods": int((series > 0).sum()),
            "ChannelZeroDemandPercent": round(float((series == 0).mean() * 100), 2),
            "ChannelChampionModel": champion_name,
            "ChannelMainBacktestError": channel_main_error,
            "ChannelFourWeekWAPE": champion_result.get("BucketWAPE"),
            "ChannelRecentFourWeekWAPE": champion_result.get("RecentBucketWAPE"),
            "ChannelFourWeekBiasPercent": champion_result.get("BucketBiasPercent"),
            "ChannelReliability": channel_reliability,
            "ChannelModelWeight": model_weight,
            "ChannelBiasCorrectionFactor": channel_correction_factor,
            "ChannelMomentumFactor": channel_momentum,
            "RawChannelMonthlyForecast": max(raw_channel_month_forecast, 0.0),
            "RecentShareActual": max(recent_share_actual, 0.0),
            "Last4WeeksActual": last4_actual,
            "Previous4WeeksActual": previous4_actual,
            "TargetMonthJalali": target_month,
            "TargetMonthStartG": month_start,
            "TargetMonthEndG": month_end,
            "ProductSalesForecast": product_sales_forecast,
            "ProductSmartStretchPercent": product_smart_stretch_pct,
            "ProductSalesTarget": product_sales_target,
        })

        channel_champion_rows.append({
            "ProductCode": product_code,
            "ProductName": product_name,
            "Brand": brand,
            "SalesChannelCode": str(channel_code),
            "SalesChannelName": channel_name,
            "ProductDemandPattern": product_pattern,
            "ChannelDemandPattern": channel_pattern,
            "ChannelADI": channel_adi,
            "ChannelCV2": channel_cv2,
            "ChampionModel": champion_name,
            "MainBacktestError": channel_main_error,
            "FourWeekWAPE": champion_result.get("BucketWAPE"),
            "RecentFourWeekWAPE": champion_result.get("RecentBucketWAPE"),
            "FourWeekBiasPercent": champion_result.get("BucketBiasPercent"),
            "ForecastReliability": channel_reliability,
            "BacktestFolds": champion_result.get("BacktestFolds", 0),
        })

    if not temporary_rows:
        continue

    raw_model_total = sum(
        float(r["RawChannelMonthlyForecast"]) for r in temporary_rows
    )
    recent_actual_total = sum(
        float(r["RecentShareActual"]) for r in temporary_rows
    )

    n_channels = len(temporary_rows)

    # First form two independent distributions: model-based mix and historical mix.
    for r in temporary_rows:
        if raw_model_total > 1e-9:
            model_share = float(r["RawChannelMonthlyForecast"]) / raw_model_total
        elif recent_actual_total > 1e-9:
            model_share = float(r["RecentShareActual"]) / recent_actual_total
        else:
            model_share = 1.0 / n_channels

        if recent_actual_total > 1e-9:
            historical_share = float(r["RecentShareActual"]) / recent_actual_total
        elif raw_model_total > 1e-9:
            historical_share = float(r["RawChannelMonthlyForecast"]) / raw_model_total
        else:
            historical_share = 1.0 / n_channels

        w = float(r["ChannelModelWeight"])
        blended_score = w * model_share + (1.0 - w) * historical_share

        r["RawModelShare"] = model_share
        r["HistoricalShare"] = historical_share
        r["BlendedShareScore"] = max(blended_score, 0.0)

    score_total = sum(float(r["BlendedShareScore"]) for r in temporary_rows)

    for r in temporary_rows:
        if score_total > 1e-12:
            r["AllocationShare"] = float(r["BlendedShareScore"]) / score_total
        else:
            r["AllocationShare"] = 1.0 / n_channels

    temporary_rows = reconcile_rounded_targets(
        temporary_rows,
        product_sales_target,
    )

    channel_sum = round(
        sum(float(r["ChannelSalesTarget"]) for r in temporary_rows),
        2,
    )
    reconciliation_difference = round(product_sales_target - channel_sum, 2)

    for r in temporary_rows:
        r["RawModelSharePercent"] = round(float(r["RawModelShare"]) * 100, 4)
        r["HistoricalSharePercent"] = round(float(r["HistoricalShare"]) * 100, 4)
        r["FinalAllocationSharePercent"] = round(float(r["AllocationShare"]) * 100, 4)
        r["RawChannelMonthlyForecast"] = round(float(r["RawChannelMonthlyForecast"]), 2)
        r["RecentShareActual"] = round(float(r["RecentShareActual"]), 2)
        r["Last4WeeksActual"] = round(float(r["Last4WeeksActual"]), 2)
        if pd.notna(r["Previous4WeeksActual"]):
            r["Previous4WeeksActual"] = round(float(r["Previous4WeeksActual"]), 2)
        r["ProductSalesForecast"] = round(float(product_sales_forecast), 2)
        r["ProductSmartStretchPercent"] = round(float(product_smart_stretch_pct), 2)
        r["ProductSalesTarget"] = round(float(product_sales_target), 2)
        r["ChannelTargetVsLast4Percent"] = (
            round((float(r["ChannelSalesTarget"]) / float(r["Last4WeeksActual"]) - 1.0) * 100.0, 2)
            if float(r["Last4WeeksActual"]) > 1e-9 else np.nan
        )
        r["ReconciledChannelSum"] = channel_sum
        r["ReconciliationDifference"] = reconciliation_difference
        channel_target_rows.append(r)

    channel_reconciliation_rows.append({
        "ProductCode": product_code,
        "ProductName": product_name,
        "TargetMonthJalali": target_month,
        "ProductSalesTarget": round(product_sales_target, 2),
        "ChannelCount": n_channels,
        "RawChannelForecastSum": round(raw_model_total, 2),
        "RecentChannelActualSum": round(recent_actual_total, 2),
        "ReconciledChannelTargetSum": channel_sum,
        "Difference": reconciliation_difference,
        "IsCoherent": abs(reconciliation_difference) < 0.01,
    })

    if (
        product_no == 1
        or product_no % 20 == 0
        or product_no == len(product_target_lookup)
    ):
        print(
            f"Processed hierarchical channels for "
            f"{product_no}/{len(product_target_lookup)} products"
        )


channel_sales_target = pd.DataFrame(channel_target_rows)
channel_champions = pd.DataFrame(channel_champion_rows)
channel_model_leaderboard = pd.DataFrame(channel_leaderboard_rows)
channel_backtest_details = pd.DataFrame(channel_backtest_rows)
channel_reconciliation = pd.DataFrame(channel_reconciliation_rows)
channel_errors = pd.DataFrame(channel_error_rows)

if not channel_sales_target.empty:
    channel_sales_target = (
        channel_sales_target
        .sort_values(
            ["TargetMonthJalali", "ProductCode", "SalesChannelCode"]
        )
        .reset_index(drop=True)
    )

if not channel_champions.empty:
    channel_champions = (
        channel_champions
        .sort_values(["ProductCode", "SalesChannelCode"])
        .reset_index(drop=True)
    )

if not channel_model_leaderboard.empty:
    channel_model_leaderboard = (
        channel_model_leaderboard
        .sort_values(
            ["ProductCode", "SalesChannelCode", "SelectionScore"],
            na_position="last",
        )
        .reset_index(drop=True)
    )

if not channel_reconciliation.empty:
    channel_reconciliation = (
        channel_reconciliation
        .sort_values("ProductCode")
        .reset_index(drop=True)
    )



# ============================================================
# 11. MANAGEMENT SUMMARIES
# ============================================================

if not champions.empty:

    reliability_summary = (
        champions["ForecastReliability"]
        .value_counts(dropna=False)
        .rename_axis("ForecastReliability")
        .reset_index(name="ProductCount")
    )

    model_summary = (
        champions["ChampionModel"]
        .value_counts(dropna=False)
        .rename_axis("ChampionModel")
        .reset_index(name="ProductCount")
    )

    pattern_summary = (
        champions
        .groupby(
            ["ActivityStatus", "DemandPattern"],
            dropna=False,
            as_index=False,
        )
        .agg(
            ProductCount=("ProductCode", "nunique"),
            MedianMainError=("MainBacktestError", "median"),
            MedianWeeklyWAPE=("WeeklyWAPE", "median"),
            MedianFourWeekWAPE=("FourWeekWAPE", "median"),
            Forecast12WeekTotal=("ForecastTotal12Weeks", "sum"),
        )
    )

    review_summary = (
        champions["ManagementFlag"]
        .value_counts(dropna=False)
        .rename_axis("ManagementFlag")
        .reset_index(name="ProductCount")
    )

else:
    reliability_summary = pd.DataFrame()
    model_summary = pd.DataFrame()
    pattern_summary = pd.DataFrame()
    review_summary = pd.DataFrame()


# ============================================================
# 12. CONFIGURATION / METHOD NOTES
# ============================================================

configuration = pd.DataFrame({
    "Parameter": [
        "Data Source",
        "Output File",
        "Forecast Statuses",
        "Forecast Horizon",
        "Target Evaluation Bucket",
        "Backtest Folds",
        "Minimum Train Periods",
        "Season Length",
        "Bias Weight",
        "Recency Decay",
        "Recent Error Weight",
        "Overall Error Weight",
        "Weekly Anchor Weight",
        "Bias Correction Enabled",
        "High Reliability Error Threshold",
        "Medium Reliability Error Threshold",
        "Target Stretch Percent",
        "Momentum Weight",
        "Low Reliability Model Weight",
        "Target Floor Factor",
        "Target Cap Factor",
        "Dormant Cap Factor",
        "Zero Model Allowed",
    ],
    "Value": [
        "SQL Server -> in-memory demand classification",
        str(OUTPUT_FILE),
        ", ".join(sorted(FORECAST_STATUSES)),
        f"{FORECAST_HORIZON} weeks",
        f"{TARGET_BUCKET} weeks",
        BACKTEST_FOLDS,
        MIN_TRAIN_PERIODS,
        SEASON_LENGTH,
        BIAS_WEIGHT,
        RECENCY_DECAY,
        RECENT_ERROR_WEIGHT,
        OVERALL_ERROR_WEIGHT,
        WEEKLY_ANCHOR_WEIGHT,
        APPLY_BIAS_CORRECTION,
        HIGH_ERROR_THRESHOLD,
        MEDIUM_ERROR_THRESHOLD,
        TARGET_STRETCH_PCT * 100.0,
        MOMENTUM_WEIGHT,
        LOW_RELIABILITY_MODEL_WEIGHT,
        TARGET_FLOOR_FACTOR,
        TARGET_CAP_FACTOR,
        DORMANT_CAP_FACTOR,
        "NO",
    ],
})

method_notes = pd.DataFrame({
    "Topic": [
        "Monthly target evaluation",
        "Weekly diagnostic anchor",
        "Recency weighting",
        "Recency-aware models",
        "Why Zero cannot be champion",
        "Intermittent models",
        "Champion selection",
        "Bias correction",
        "Base vs target forecast",
        "Reliability calibration",
        "Target guardrails",
        "Dormant products",
        "Lumpy products",
    ],
    "Explanation": [
        "All demand patterns are selected primarily on cumulative 4-week WAPE because the business objective is a monthly sales target.",
        f"Weekly WAPE remains diagnostic and contributes only {WEEKLY_ANCHOR_WEIGHT:.0%} as a tie-breaker in champion selection.",
        f"Champion error blends {RECENT_ERROR_WEIGHT:.0%} recent-weighted error with {OVERALL_ERROR_WEIGHT:.0%} overall backtest error; fold decay={RECENCY_DECAY}.",
        "Weighted means, EWMA and recent-window Holt are added for changing demand regimes; weighted two-stage models are added for intermittent demand.",
        "Zero forecast is not a candidate, preventing many zero-demand weeks from creating an artificial winner.",
        "Croston, SBA, TSB, ADIDA, two-stage and recency-weighted two-stage models are tested with multiple parameter settings.",
        "Selection score is the monthly-target-aligned blended 4-week error plus bias penalty and a small weekly accuracy anchor.",
        f"When at least {MIN_FOLDS_FOR_BIAS_CORRECTION} folds exist and bias exceeds {BIAS_CORRECTION_TRIGGER:.0f}%, a capped factor ({BIAS_CORRECTION_MIN_FACTOR:.2f}-{BIAS_CORRECTION_MAX_FACTOR:.2f}) corrects systematic bias.",
        "BaseForecastDemand is the raw champion output; ForecastDemand is the bias-adjusted statistical forecast used to build monthly sales targets.",
        "High reliability keeps the model target; Medium/Low/Insufficient targets are progressively blended toward a robust recent 4-week baseline.",
        f"Final targets are bounded to {TARGET_FLOOR_FACTOR:.0%}-{TARGET_CAP_FACTOR:.0%} of the robust baseline when that baseline is positive.",
        "Dormant forecasts are produced, conservatively capped, and always flagged for management review.",
        "Lumpy forecasts are produced but flagged because both timing and quantity remain intrinsically uncertain.",
    ],
})


# Add V4 target-setting configuration and methodology notes.
configuration = pd.concat(
    [
        configuration,
        pd.DataFrame({
            "Parameter": [
                "Management Output File",
                "Smart Stretch Enabled",
                "Smart Stretch High Reliability",
                "Smart Stretch Medium Reliability",
                "Smart Stretch Low Reliability",
                "Smart Stretch Insufficient Backtest",
                "Smart Stretch Maximum",
                "Historical Target Usage",
            ],
            "Value": [
                str(MANAGEMENT_OUTPUT_FILE),
                SMART_STRETCH_ENABLED,
                SMART_STRETCH_BY_RELIABILITY["High"],
                SMART_STRETCH_BY_RELIABILITY["Medium"],
                SMART_STRETCH_BY_RELIABILITY["Low"],
                SMART_STRETCH_BY_RELIABILITY["Insufficient Backtest"],
                SMART_STRETCH_MAX,
                "Diagnostic only - does not alter forecast or recommended target",
            ],
        }),
    ],
    ignore_index=True,
)

method_notes = pd.concat(
    [
        method_notes,
        pd.DataFrame({
            "Topic": [
                "V4 forecast-target separation",
                "Smart stretch",
                "Historical management targets",
            ],
            "Explanation": [
                "SalesForecast is the expected-sales estimate after model selection, bias correction, momentum and reliability calibration; it is kept separate from the management target.",
                "RecommendedSalesTarget adds a bounded stretch based on forecast reliability and recent sales momentum, with conservative caps for Intermittent, Lumpy and Dormant products.",
                "Historical sales-target data is exported for trend and achievement diagnostics only. Old management target values never pull V4 forecasts or recommended targets upward or downward.",
            ],
        }),
    ],
    ignore_index=True,
)

# Add hierarchical configuration and methodology notes.
configuration = pd.concat(
    [
        configuration,
        pd.DataFrame({
            "Parameter": [
                "Hierarchical Forecasting",
                "Sales Channel Code Column",
                "Sales Channel Name Column",
                "Brand Column",
                "Channel Share Lookback",
                "Channel High Model Weight",
                "Channel Medium Model Weight",
                "Channel Low Model Weight",
                "Channel Insufficient Model Weight",
                "Forecast Reconciliation",
            ],
            "Value": [
                "Product -> Product x SalesChannel",
                CHANNEL_CODE_COL,
                CHANNEL_NAME_COL or "Not found",
                BRAND_COL or "Not found",
                f"{CHANNEL_SHARE_LOOKBACK_WEEKS} weeks",
                CHANNEL_HIGH_MODEL_WEIGHT,
                CHANNEL_MEDIUM_MODEL_WEIGHT,
                CHANNEL_LOW_MODEL_WEIGHT,
                CHANNEL_INSUFFICIENT_MODEL_WEIGHT,
                "Exact: Sum(ChannelSalesTarget) = ProductSalesTarget",
            ],
        }),
    ],
    ignore_index=True,
)

method_notes = pd.concat(
    [
        method_notes,
        pd.DataFrame({
            "Topic": [
                "Channel demand pattern",
                "Channel forecast",
                "Historical channel share",
                "Reliability-aware channel blend",
                "Hierarchical reconciliation",
            ],
            "Explanation": [
                "ADI and CV^2 are recalculated independently for each Product x Channel series; the channel does not inherit the product demand pattern.",
                "Each Product x Channel series is independently backtested using the same candidate-model framework and 4-week target-aligned selection objective.",
                f"Recent {CHANNEL_SHARE_LOOKBACK_WEEKS}-week actual sales define a robust historical channel-mix baseline.",
                "Channel model share is blended with historical share; High-reliability channels receive more model weight, while weak channels rely more on historical mix.",
                "Final channel targets are normalized and rounded so their sum exactly equals the already calibrated parent Product SalesTarget.",
            ],
        }),
    ],
    ignore_index=True,
)



# ============================================================
# 13. SAVE
# ============================================================

print("\nSaving V4 detailed product-channel results...")

OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
MANAGEMENT_OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)

with pd.ExcelWriter(
    OUTPUT_FILE,
    engine="openpyxl",
) as writer:

    # Diagnostic outputs from the first stage (kept in memory; no intermediate workbook).
    basic_info.to_excel(writer, sheet_name="Diagnostic_Summary", index=False)
    product_diagnostic.to_excel(writer, sheet_name="Product_Diagnostic", index=False)
    product_activity.to_excel(writer, sheet_name="Product_Activity", index=False)
    demand_metrics.to_excel(writer, sheet_name="Demand_Metrics", index=False)
    classification_rules.to_excel(writer, sheet_name="Classification_Rules", index=False)

    # Keep Excel row limits in mind for the detailed time series.
    demand_time_series.head(1_000_000).to_excel(
        writer, sheet_name="Demand_Time_Series", index=False
    )

    champions.to_excel(
        writer,
        sheet_name="Champion_Summary",
        index=False,
    )

    monthly_sales_target.to_excel(
        writer,
        sheet_name="Sales_Target",
        index=False,
    )

    target_bucket_plan.to_excel(
        writer,
        sheet_name="Target_Buckets",
        index=False,
    )

    weekly_forecast.to_excel(
        writer,
        sheet_name="Weekly_Forecast",
        index=False,
    )

    leaderboard.to_excel(
        writer,
        sheet_name="Model_Leaderboard",
        index=False,
    )

    backtest_details.to_excel(
        writer,
        sheet_name="Backtest_Details",
        index=False,
    )

    reliability_summary.to_excel(
        writer,
        sheet_name="Reliability_Summary",
        index=False,
    )

    model_summary.to_excel(
        writer,
        sheet_name="Model_Summary",
        index=False,
    )

    pattern_summary.to_excel(
        writer,
        sheet_name="Pattern_Summary",
        index=False,
    )

    review_summary.to_excel(
        writer,
        sheet_name="Review_Summary",
        index=False,
    )

    excluded.to_excel(
        writer,
        sheet_name="Excluded_Products",
        index=False,
    )

    channel_sales_target.to_excel(
        writer,
        sheet_name="Channel_Target",
        index=False,
    )

    channel_reconciliation.to_excel(
        writer,
        sheet_name="Channel_Reconciliation",
        index=False,
    )

    channel_champions.to_excel(
        writer,
        sheet_name="Channel_Champion",
        index=False,
    )

    if EXPORT_CHANNEL_MODEL_LEADERBOARD:
        channel_model_leaderboard.head(1_000_000).to_excel(
            writer,
            sheet_name="Channel_Model_Leaderboard",
            index=False,
        )

    if EXPORT_CHANNEL_BACKTEST_DETAILS:
        channel_backtest_details.head(1_000_000).to_excel(
            writer,
            sheet_name="Channel_Backtest",
            index=False,
        )

    channel_errors.to_excel(
        writer,
        sheet_name="Channel_Errors",
        index=False,
    )

    errors.to_excel(
        writer,
        sheet_name="Errors",
        index=False,
    )

    configuration.to_excel(
        writer,
        sheet_name="Configuration",
        index=False,
    )

    method_notes.to_excel(
        writer,
        sheet_name="Method_Notes",
        index=False,
    )


# ============================================================
# 13B. MANAGEMENT WORKBOOK (SECOND EXCEL OUTPUT)
# ============================================================

def _safe_pct_change(new_value, old_value):
    old_value = float(old_value or 0.0)
    new_value = float(new_value or 0.0)
    if abs(old_value) <= 1e-9:
        return np.nan
    return (new_value / old_value - 1.0) * 100.0


def build_management_tables():
    # Channel-level executive summary. ChannelSalesTarget is the final V4 target.
    if channel_sales_target.empty:
        channel_summary = pd.DataFrame()
    else:
        ch = channel_sales_target.copy()
        ch["ChannelSalesTarget"] = pd.to_numeric(ch["ChannelSalesTarget"], errors="coerce").fillna(0.0)
        ch["Last4WeeksActual"] = pd.to_numeric(ch["Last4WeeksActual"], errors="coerce").fillna(0.0)
        ch["Previous4WeeksActual"] = pd.to_numeric(ch["Previous4WeeksActual"], errors="coerce").fillna(0.0)
        ch["RawChannelMonthlyForecast"] = pd.to_numeric(ch["RawChannelMonthlyForecast"], errors="coerce").fillna(0.0)

        channel_summary = (
            ch.groupby(["SalesChannelCode", "SalesChannelName"], dropna=False)
            .agg(
                RecommendedTarget=("ChannelSalesTarget", "sum"),
                ModelChannelForecast=("RawChannelMonthlyForecast", "sum"),
                Last4WeeksActual=("Last4WeeksActual", "sum"),
                Previous4WeeksActual=("Previous4WeeksActual", "sum"),
                ProductCount=("ProductCode", "nunique"),
            )
            .reset_index()
        )
        channel_summary["TargetVsLast4Percent"] = channel_summary.apply(
            lambda r: _safe_pct_change(r["RecommendedTarget"], r["Last4WeeksActual"]), axis=1
        )
        channel_summary["Last4VsPrevious4Percent"] = channel_summary.apply(
            lambda r: _safe_pct_change(r["Last4WeeksActual"], r["Previous4WeeksActual"]), axis=1
        )
        total_target = float(channel_summary["RecommendedTarget"].sum())
        channel_summary["ShareOfTotalTargetPercent"] = (
            channel_summary["RecommendedTarget"] / total_target * 100.0
            if total_target > 0 else 0.0
        )
        channel_summary = channel_summary.sort_values("RecommendedTarget", ascending=False).reset_index(drop=True)

    if monthly_sales_target.empty:
        top_products = pd.DataFrame()
        review_items = pd.DataFrame()
        target_history_review = pd.DataFrame()
    else:
        m = monthly_sales_target.copy()
        top_cols = [
            "ProductCode", "ProductName", "ActivityStatus", "DemandPattern",
            "SalesForecast", "SmartStretchPercent", "RecommendedSalesTarget",
            "Last4WeeksActual", "Previous4WeeksActual", "ForecastReliability",
            "ManagementFlag", "HistoricalTargetAchievementRate",
            "HistoricalTargetRecentAvgTarget", "HistoricalTargetRecentAvgSale",
        ]
        top_cols = [c for c in top_cols if c in m.columns]
        top_products = (
            m.sort_values("RecommendedSalesTarget", ascending=False)
            .head(25)[top_cols]
            .reset_index(drop=True)
        )

        reliability_review = m["ForecastReliability"].astype(str).isin(["Low", "Insufficient Backtest"])
        flag_review = ~m["ManagementFlag"].astype(str).isin(["OK", "None", "nan", ""])
        review_items = m.loc[reliability_review | flag_review, top_cols].copy()
        review_items = review_items.sort_values("RecommendedSalesTarget", ascending=False).reset_index(drop=True)

        hist_cols = [
            "ProductCode", "ProductName", "TargetHistoryRows",
            "HistoricalTargetRecentAvgTarget", "HistoricalTargetRecentAvgSale",
            "HistoricalTargetAchievementRate", "HistoricalTargetTrendFactor",
            "SalesForecast", "RecommendedSalesTarget",
        ]
        hist_cols = [c for c in hist_cols if c in m.columns]
        target_history_review = m[hist_cols].copy()
        target_history_review = target_history_review.sort_values(
            "HistoricalTargetAchievementRate", na_position="last"
        ).reset_index(drop=True)

    return channel_summary, top_products, review_items, target_history_review


channel_management, top_products_management, review_management, history_management = build_management_tables()

# KPI table for the first management sheet.
_total_recommended = float(monthly_sales_target["RecommendedSalesTarget"].sum()) if not monthly_sales_target.empty else 0.0
_total_forecast = float(monthly_sales_target["SalesForecast"].sum()) if not monthly_sales_target.empty else 0.0
_total_last4 = float(monthly_sales_target["Last4WeeksActual"].sum()) if not monthly_sales_target.empty else 0.0
_avg_stretch = float(monthly_sales_target["SmartStretchPercent"].mean()) if not monthly_sales_target.empty else 0.0
_coherence_pct = (
    float(channel_reconciliation["IsCoherent"].mean() * 100.0)
    if not channel_reconciliation.empty else np.nan
)

management_kpis = pd.DataFrame({
    "Metric": [
        "Forecasted Products",
        "Expected Sales Forecast",
        "Recommended Sales Target",
        "Last 4 Weeks Actual Sales",
        "Recommended Target vs Forecast %",
        "Recommended Target vs Last4 %",
        "Average Smart Stretch %",
        "Channel Reconciliation Coherence %",
        "Historical Target Policy",
    ],
    "Value": [
        len(monthly_sales_target),
        round(_total_forecast, 2),
        round(_total_recommended, 2),
        round(_total_last4, 2),
        round(_safe_pct_change(_total_recommended, _total_forecast), 2),
        round(_safe_pct_change(_total_recommended, _total_last4), 2),
        round(_avg_stretch, 2),
        round(_coherence_pct, 2) if pd.notna(_coherence_pct) else np.nan,
        "Diagnostic only; old targets do not drive V4 forecast/target",
    ],
})

with pd.ExcelWriter(MANAGEMENT_OUTPUT_FILE, engine="openpyxl") as writer:
    management_kpis.to_excel(writer, sheet_name="Executive_Summary", index=False, startrow=2)
    channel_management.to_excel(writer, sheet_name="Channel_Summary", index=False)
    top_products_management.to_excel(writer, sheet_name="Top_Products", index=False)
    review_management.to_excel(writer, sheet_name="Review_Items", index=False)
    history_management.to_excel(writer, sheet_name="Target_History_Review", index=False)

    # Lightweight professional formatting for management use.
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.chart import BarChart, Reference
    from openpyxl.utils import get_column_letter

    wb_mgmt = writer.book
    header_fill = PatternFill("solid", fgColor="17365D")
    section_fill = PatternFill("solid", fgColor="D9EAF7")
    white_font = Font(color="FFFFFF", bold=True)
    thin_side = Side(style="thin", color="D9E1F2")

    for ws in wb_mgmt.worksheets:
        ws.freeze_panes = "A2" if ws.title != "Executive_Summary" else "A4"
        ws.sheet_view.rightToLeft = True
        header_row = 3 if ws.title == "Executive_Summary" else 1
        for cell in ws[header_row]:
            if cell.value is not None:
                cell.fill = header_fill
                cell.font = white_font
                cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        for row in ws.iter_rows(min_row=header_row + 1):
            for cell in row:
                cell.border = Border(bottom=thin_side)
                cell.alignment = Alignment(vertical="center", wrap_text=True)
        for col_idx in range(1, ws.max_column + 1):
            letter = get_column_letter(col_idx)
            max_len = 0
            for cell in list(ws[letter])[:150]:
                if cell.value is not None:
                    max_len = max(max_len, len(str(cell.value)))
            ws.column_dimensions[letter].width = min(max(max_len + 2, 12), 34)

    ws_exec = wb_mgmt["Executive_Summary"]
    ws_exec["A1"] = "V4 Management Sales Target Summary"
    ws_exec["A1"].font = Font(size=18, bold=True, color="17365D")
    ws_exec["A2"] = "Expected sales are model forecasts; recommended targets add a bounded smart stretch. Historical targets are diagnostic only."
    ws_exec["A2"].font = Font(italic=True, color="666666")

    # Percent-like KPI values.
    for r in [8, 9, 10, 11]:
        if ws_exec.cell(r, 2).value is not None and isinstance(ws_exec.cell(r, 2).value, (int, float)):
            ws_exec.cell(r, 2).number_format = '0.00'

    # Channel chart on executive page if data exists.
    if not channel_management.empty:
        ws_ch = wb_mgmt["Channel_Summary"]
        chart = BarChart()
        chart.type = "bar"
        chart.title = "Recommended Target by Sales Channel"
        chart.y_axis.title = "Sales Channel"
        chart.x_axis.title = "Cartons"
        max_chart_row = min(ws_ch.max_row, 13)
        data = Reference(ws_ch, min_col=3, min_row=1, max_row=max_chart_row)
        cats = Reference(ws_ch, min_col=2, min_row=2, max_row=max_chart_row)
        chart.add_data(data, titles_from_data=True)
        chart.set_categories(cats)
        chart.height = 7
        chart.width = 13
        ws_exec.add_chart(chart, "D4")

print("✅ Detailed Excel:", OUTPUT_FILE)
print("✅ Management Excel:", MANAGEMENT_OUTPUT_FILE)


# ============================================================
# 14. FINAL REPORT
# ============================================================

print("\n========================================")
print("V4 SALES FORECAST + SMART TARGET SETTING COMPLETED")
print("========================================")
print("Detailed output:", OUTPUT_FILE)
print("Management output:", MANAGEMENT_OUTPUT_FILE)
print("Forecasted products:", len(champions))
print("Weekly forecast rows:", len(weekly_forecast))
print("Sales-target rows:", len(monthly_sales_target))
print("Target-bucket rows:", len(target_bucket_plan))
print("Channel-target rows:", len(channel_sales_target))
print("Channel reconciliation rows:", len(channel_reconciliation))

if not channel_reconciliation.empty:
    incoherent = int((~channel_reconciliation["IsCoherent"]).sum())
    max_diff = float(channel_reconciliation["Difference"].abs().max())
    print("Channel reconciliation failures:", incoherent)
    print("Maximum reconciliation difference:", round(max_diff, 4))

if not champions.empty:
    print("\nReliability:")
    print(champions["ForecastReliability"].value_counts(dropna=False))

    print("\nChampion models:")
    print(champions["ChampionModel"].value_counts(dropna=False).head(20))

    print("\nManagement flags:")
    print(champions["ManagementFlag"].value_counts(dropna=False))
