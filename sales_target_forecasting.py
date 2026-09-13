"""
Sales Demand Forecasting & Target Planning - Public Demo
========================================================

This public version demonstrates the project architecture while keeping
the production target-calibration, model-ranking, reconciliation, and
management logic private.

Pipeline:
SQL Server -> Sales History -> Demand Classification -> Forecasting
-> Reliability Assessment -> Target Planning (proprietary layer)
-> Channel Allocation (proprietary layer) -> Excel Output

Author: Mona Faghfouri Azar
"""

import os
from getpass import getpass
from pathlib import Path

import numpy as np
import pandas as pd
from sqlalchemy import create_engine, text
from statsmodels.tsa.holtwinters import SimpleExpSmoothing, Holt


# ============================================================
# 1. CONFIGURATION
# ============================================================

SERVER = os.getenv("SALES_DB_SERVER", "localhost")
DATABASE = os.getenv("SALES_DB_NAME", "YOUR_DATABASE")
USERNAME = os.getenv("SALES_DB_USERNAME", "YOUR_USERNAME")
PASSWORD = os.getenv("SALES_DB_PASSWORD") or getpass("SQL Server password: ")
DRIVER = os.getenv("SALES_DB_DRIVER", "ODBC Driver 17 for SQL Server")

SALE_INVOICE_TABLE = os.getenv("SALE_INVOICE_TABLE", "dbo.SaleInvoice")

OUTPUT_DIR = Path(os.getenv("SALES_FORECAST_OUTPUT_DIR", "outputs"))
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

OUTPUT_FILE = OUTPUT_DIR / "Sales_Target_Demo.xlsx"

DEMAND_FREQUENCY = "W"
FORECAST_HORIZON = 12

# Standard Syntetos-Boylan demand classification thresholds
ADI_THRESHOLD = 1.32
CV2_THRESHOLD = 0.49

MIN_PERIODS = 8
MIN_NONZERO_PERIODS = 2


# ============================================================
# 2. DATABASE CONNECTION
# ============================================================

def create_db_engine():
    connection_string = (
        f"mssql+pyodbc://{USERNAME}:{PASSWORD}@{SERVER}/{DATABASE}"
        f"?driver={DRIVER.replace(' ', '+')}"
    )
    return create_engine(connection_string, fast_executemany=True)


# ============================================================
# 3. LOAD SALES DATA
# ============================================================

def load_sales_data(engine):
    """
    Expected minimum fields:
        ProductCode
        InvoiceDate
        SaleQuantity

    Adapt the SELECT statement to your own schema.
    """
    query = f"""
    SELECT
        ProductCode,
        InvoiceDate,
        SaleQuantity
    FROM {SALE_INVOICE_TABLE}
    WHERE ProductCode IS NOT NULL
    """

    with engine.connect() as connection:
        df = pd.read_sql_query(text(query), connection)

    df["ProductCode"] = df["ProductCode"].astype("string").str.strip()
    df["InvoiceDate"] = pd.to_datetime(df["InvoiceDate"], errors="coerce")
    df["SaleQuantity"] = pd.to_numeric(
        df["SaleQuantity"], errors="coerce"
    ).fillna(0.0)

    return df.dropna(subset=["ProductCode", "InvoiceDate"])


# ============================================================
# 4. BUILD WEEKLY DEMAND SERIES
# ============================================================

def build_weekly_series(product_df):
    series = (
        product_df
        .set_index("InvoiceDate")["SaleQuantity"]
        .sort_index()
        .resample(DEMAND_FREQUENCY)
        .sum()
    )

    if series.empty:
        return pd.Series(dtype=float)

    full_index = pd.date_range(
        start=series.index.min(),
        end=series.index.max(),
        freq=DEMAND_FREQUENCY,
    )

    return (
        series
        .reindex(full_index, fill_value=0.0)
        .clip(lower=0.0)
        .astype(float)
    )


# ============================================================
# 5. DEMAND CLASSIFICATION
# ============================================================

def classify_demand_pattern(series):
    """
    Classify demand as Smooth, Erratic, Intermittent, or Lumpy.
    """

    total_periods = len(series)
    nonzero = series[series > 0]
    nonzero_periods = len(nonzero)

    if total_periods < MIN_PERIODS or nonzero_periods < MIN_NONZERO_PERIODS:
        return "Insufficient Demand History", np.nan, np.nan

    adi = total_periods / nonzero_periods
    mean_nonzero = float(nonzero.mean())
    std_nonzero = float(nonzero.std(ddof=1))

    cv2 = (std_nonzero / mean_nonzero) ** 2 if mean_nonzero > 0 else np.nan

    if adi <= ADI_THRESHOLD and cv2 <= CV2_THRESHOLD:
        pattern = "Smooth"
    elif adi <= ADI_THRESHOLD and cv2 > CV2_THRESHOLD:
        pattern = "Erratic"
    elif adi > ADI_THRESHOLD and cv2 <= CV2_THRESHOLD:
        pattern = "Intermittent"
    else:
        pattern = "Lumpy"

    return pattern, adi, cv2


# ============================================================
# 6. SIMPLE PUBLIC FORECAST MODELS
# ============================================================

def model_mean(series, horizon):
    if len(series) == 0:
        return np.zeros(horizon)
    return np.repeat(float(series.mean()), horizon)


def model_moving_average(series, horizon, window=4):
    if len(series) == 0:
        return np.zeros(horizon)

    window = min(window, len(series))
    level = float(series.iloc[-window:].mean())
    return np.repeat(max(level, 0.0), horizon)


def model_ses(series, horizon):
    if len(series) < 3:
        return model_mean(series, horizon)

    try:
        fit = SimpleExpSmoothing(
            series.to_numpy(),
            initialization_method="estimated",
        ).fit(optimized=True)

        return np.maximum(np.asarray(fit.forecast(horizon), dtype=float), 0.0)

    except Exception:
        return model_mean(series, horizon)


def model_holt(series, horizon):
    if len(series) < 6:
        return model_ses(series, horizon)

    try:
        fit = Holt(
            series.to_numpy(),
            damped_trend=True,
            initialization_method="estimated",
        ).fit(optimized=True)

        return np.maximum(np.asarray(fit.forecast(horizon), dtype=float), 0.0)

    except Exception:
        return model_ses(series, horizon)


# ============================================================
# 7. DEMO MODEL SELECTION
# ============================================================

def select_demo_model(series):
    """
    Simplified public model-selection logic.

    The production version uses broader model families,
    rolling backtesting, reliability-aware scoring,
    bias control, recency weighting, and specialized
    intermittent-demand forecasting.
    """

    candidates = {
        "Mean": model_mean,
        "MovingAverage": model_moving_average,
        "SES": model_ses,
        "Holt": model_holt,
    }

    if len(series) < 8:
        return "Mean", model_mean

    train = series.iloc[:-4]
    test = series.iloc[-4:]

    best_name = "Mean"
    best_model = model_mean
    best_error = np.inf

    for name, function in candidates.items():
        forecast = function(train, len(test))
        error = np.mean(
            np.abs(
                test.to_numpy(dtype=float)
                - np.asarray(forecast, dtype=float)
            )
        )

        if error < best_error:
            best_error = error
            best_name = name
            best_model = function

    return best_name, best_model


# ============================================================
# 8. PROPRIETARY PRODUCTION LAYERS
# ============================================================

def calculate_recommended_target(forecast, product_context):
    """
    Proprietary target-calibration layer.

    Production logic considers forecast reliability, recent sales
    behavior, momentum, demand pattern, product status, and business
    constraints. The detailed implementation is intentionally excluded.
    """
    raise NotImplementedError(
        "Target calibration is available in the production version."
    )


def allocate_channel_targets(product_target, channel_context):
    """
    Proprietary Product x Channel allocation and reconciliation layer.
    The detailed implementation is intentionally excluded.
    """
    raise NotImplementedError(
        "Channel allocation is available in the production version."
    )


# ============================================================
# 9. PUBLIC DEMO PIPELINE
# ============================================================

def run_demo():
    engine = create_db_engine()
    sales = load_sales_data(engine)

    results = []

    for product_code, product_df in sales.groupby("ProductCode"):
        series = build_weekly_series(product_df)

        if series.empty:
            continue

        demand_pattern, adi, cv2 = classify_demand_pattern(series)
        model_name, model_function = select_demo_model(series)
        forecast = model_function(series, FORECAST_HORIZON)

        results.append({
            "ProductCode": product_code,
            "DemandPattern": demand_pattern,
            "ADI": round(adi, 4) if pd.notna(adi) else np.nan,
            "CV2": round(cv2, 4) if pd.notna(cv2) else np.nan,
            "SelectedDemoModel": model_name,
            "Forecast12Weeks": round(float(np.sum(forecast)), 2),
            "Last4WeeksActual": round(float(series.iloc[-4:].sum()), 2),
            "TargetLogic": "Proprietary / not included",
            "ChannelAllocation": "Proprietary / not included",
        })

    result_df = pd.DataFrame(results)

    with pd.ExcelWriter(OUTPUT_FILE, engine="openpyxl") as writer:
        result_df.to_excel(writer, sheet_name="Forecast_Demo", index=False)

    print(f"Demo output saved to: {OUTPUT_FILE}")


# ============================================================
# 10. ENTRY POINT
# ============================================================

if __name__ == "__main__":
    run_demo()
