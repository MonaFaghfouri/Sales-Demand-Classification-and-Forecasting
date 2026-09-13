# 📈 Sales Demand Classification & Forecasting Pipeline

> **From raw transactional data to demand intelligence and actionable
> sales targets.**

An end-to-end **Python-based demand analytics, forecasting, and
sales-target optimization pipeline** for real-world sales planning.

The system connects directly to **Microsoft SQL Server**, analyzes
product activity and historical sales behavior, classifies demand
patterns, evaluates multiple forecasting strategies through rolling
backtesting, and generates data-driven targets at both **product and
sales-channel levels**.

------------------------------------------------------------------------

## 🚀 Project Overview

Sales forecasting becomes difficult when products exhibit fundamentally
different demand behaviors. Some products sell consistently, while
others are irregular, intermittent, or highly volatile. Applying the
same forecasting model to every SKU can therefore produce unreliable
forecasts and unrealistic targets.

This project addresses that problem through a pattern-aware,
reliability-aware forecasting architecture:

``` text
SQL Server
    ↓
Data Extraction & Cleaning
    ↓
Product Activity Analysis
    ↓
Weekly Demand Time-Series
    ↓
ADI / CV² Analysis
    ↓
Demand Pattern Classification
    ↓
Pattern-Aware Forecasting
    ↓
Rolling Backtesting
    ↓
Champion Model Selection
    ↓
Reliability & Bias Calibration
    ↓
Smart Sales Target Generation
    ↓
Product × Sales Channel Allocation
    ↓
Hierarchical Reconciliation
    ↓
Excel Management Reports
```

The goal is not simply to predict future demand. The pipeline is
designed as a **decision-support system for sales planning**.

------------------------------------------------------------------------

## 🧠 Demand Pattern Classification

Products are classified using two established intermittent-demand
measures:

### Average Demand Interval --- ADI

``` text
ADI = Number of Time Periods / Number of Non-Zero Demand Periods
```

ADI measures how frequently demand occurs.

### Squared Coefficient of Variation --- CV²

``` text
CV² = (Standard Deviation of Non-Zero Demand / Mean Non-Zero Demand)²
```

CV² measures variability in non-zero demand size.

### Classification Matrix

  -----------------------------------------------------------------------------
  Demand Pattern                      ADI                  CV² Interpretation
  ------------------ -------------------- -------------------- ----------------
  🟢 **Smooth**                    ≤ 1.32               ≤ 0.49 Frequent and
                                                               relatively
                                                               stable demand

  🟡 **Erratic**                   ≤ 1.32              \> 0.49 Frequent but
                                                               highly variable
                                                               demand

  🔵                              \> 1.32               ≤ 0.49 Infrequent
  **Intermittent**                                             demand with
                                                               relatively
                                                               stable sizes

  🔴 **Lumpy**                    \> 1.32              \> 0.49 Infrequent and
                                                               highly variable
                                                               demand
  -----------------------------------------------------------------------------

### Visual Guide

![Demand Pattern Classification using ADI and
CV²](demand_classification.PNG)

This segmentation allows the forecasting strategy to adapt to each
product's statistical behavior instead of applying one model
universally.

------------------------------------------------------------------------

## 🏭 Product Activity Classification

Before demand forecasting, products are evaluated using historical
production/order activity.

The pipeline calculates indicators such as:

-   Average order gap
-   Median order gap
-   90th-percentile order gap
-   95th-percentile order gap
-   Days since the most recent order
-   Number of historical orders
-   Number of active order days

Products are then assigned an operational state:

``` text
Active
Dormant
Inactive
Insufficient History
```

This layer helps prevent discontinued products or products with
inadequate history from receiving misleading statistical forecasts.

------------------------------------------------------------------------

## 🔮 Pattern-Aware Forecasting Engine

Instead of relying on one algorithm, the engine evaluates multiple
forecasting families.

### Regular-Demand Models

For smoother or more regular demand, candidate models include:

-   Naive Forecast
-   Historical Mean
-   Moving Average
-   Exponentially Weighted Mean
-   Recent Median
-   Simple Exponential Smoothing
-   Holt Trend
-   ETS / Exponential Smoothing
-   Seasonal Naive
-   ARIMA / SARIMAX

### Intermittent-Demand Models

For intermittent and lumpy demand, specialized candidates include:

-   **Croston**
-   **SBA --- Syntetos-Boylan Approximation**
-   **TSB --- Teunter-Syntetos-Babai**
-   **ADIDA**
-   **Two-stage occurrence × demand-size models**
-   Recency-weighted two-stage variants

This distinction is important because conventional time-series models
may perform poorly when a large proportion of historical periods contain
zero demand.

------------------------------------------------------------------------

## 🏆 Rolling Backtesting & Champion Model Selection

Candidate models are evaluated through rolling historical backtests
before a champion model is selected for each forecastable product.

The evaluation framework considers:

-   **WAPE --- Weighted Absolute Percentage Error**
-   **MAE --- Mean Absolute Error**
-   **RMSE --- Root Mean Squared Error**
-   **Forecast Bias**
-   Recent-period forecasting performance
-   Demand occurrence accuracy
-   Demand-event recall

Recent backtest folds receive greater weight than older folds, allowing
model selection to respond to changing demand regimes.

A zero forecast is not permitted to become the champion merely because a
product contains many zero-demand weeks.

------------------------------------------------------------------------

## 📅 Sales-Target-Oriented Evaluation

A key design choice is that forecasting performance is not evaluated
only at the weekly level.

Because the business objective is approximately monthly sales planning,
forecasts are also evaluated using:

``` text
4-week cumulative demand buckets
```

Champion selection therefore emphasizes **four-week cumulative forecast
accuracy**, while weekly accuracy remains a smaller diagnostic signal.

This aligns model selection with the actual planning objective.

------------------------------------------------------------------------

## ⚖️ Forecast Bias Correction

Forecasting models can systematically overestimate or underestimate
demand.

The pipeline measures historical bias during backtesting and can apply a
bounded correction when:

-   sufficient backtesting evidence exists, and
-   systematic bias exceeds a predefined threshold.

The correction factor is constrained to prevent temporary anomalies from
causing aggressive forecast adjustments.

------------------------------------------------------------------------

## 🛡️ Reliability-Aware Forecast Calibration

Not every statistical forecast should receive the same level of trust.

Products are assigned forecast-reliability levels based on historical
backtesting performance.

``` text
High Reliability
      ↓
Trust the statistical model more

Medium Reliability
      ↓
Blend model + robust recent baseline

Low Reliability
      ↓
Trust robust recent behavior more
```

This reduces the risk of unstable models producing extreme or
operationally unrealistic targets.

------------------------------------------------------------------------

## 📈 Recent Demand Momentum

Recent demand behavior is incorporated through a bounded momentum
adjustment.

The pipeline compares recent short-term sales with a longer recent
baseline while constraining the adjustment factor. This allows the
forecast to react to meaningful changes without simply chasing temporary
spikes.

------------------------------------------------------------------------

## 🎯 Smart Sales Target Generation

A statistical forecast and a managerial sales target are not necessarily
the same thing.

The project explicitly separates:

``` text
Expected Sales Forecast
        ↓
Reliability & Momentum Calibration
        ↓
Management Adjustment
        ↓
Recommended Sales Target
```

Recommended targets can incorporate:

-   Forecast reliability
-   Recent sales momentum
-   Product activity status
-   Demand pattern
-   Historical sales behavior
-   Conservative upper/lower guardrails

The result is intended to be **challenging but data-grounded**.

Historical management targets can remain available for diagnostic and
achievement analysis without forcing the statistical forecast toward old
target values.

------------------------------------------------------------------------

## 📊 Hierarchical Product × Sales-Channel Forecasting

The pipeline extends forecasting beyond product-level demand.

``` text
Product Sales Target
        ↓
Channel Demand Models
        ↓
Historical Channel Mix
        ↓
Reliability-Aware Allocation
        ↓
Reconciliation
```

A final reconciliation step guarantees:

``` text
Σ Channel Targets = Product Sales Target
```

for every product, including after target rounding.

This maintains consistency between product-level planning and
operational sales-channel targets.

------------------------------------------------------------------------

## 🗄️ Data Pipeline

The workflow retrieves data directly from SQL Server and performs the
analytical handoff in memory rather than depending on intermediate Excel
input files.

Conceptually, the source data includes:

``` text
Production Orders
Sales Invoices
Date Dimension
Historical Sales Targets
```

Database names, server credentials, organization-specific paths, and
confidential business data are intentionally excluded from the public
repository.

------------------------------------------------------------------------

## 🗓️ Persian / Jalali Calendar Support

The pipeline supports **Jalali dates** and converts them to Gregorian
timestamps for time-series modeling.

It also supports calendar characteristics relevant to Iranian enterprise
data, including:

-   Fridays
-   Configured official holidays
-   Jalali-to-Gregorian conversion

------------------------------------------------------------------------

## 📂 Outputs

The detailed analytical workbook can contain:

-   Product activity diagnostics
-   Demand patterns
-   ADI and CV²
-   Historical demand characteristics
-   Model leaderboards
-   Rolling backtest results
-   Champion models
-   Forecast accuracy metrics
-   Forecast bias
-   Reliability levels
-   Weekly forecasts
-   Four-week target buckets
-   Recommended sales targets
-   Channel-level target allocation
-   Channel reconciliation
-   Configuration and methodology notes

A second workbook provides a more management-oriented summary suitable
for operational review.

The resulting outputs can also be used for downstream analysis in
**Excel** and **Power BI**.

------------------------------------------------------------------------

## 🛠️ Technology Stack

``` text
Python
├── pandas
├── NumPy
├── SQLAlchemy
├── pyodbc
├── statsmodels
├── openpyxl
└── jdatetime

Database
└── Microsoft SQL Server

Analytics
├── Time-Series Forecasting
├── Intermittent Demand Forecasting
├── Rolling Backtesting
├── Forecast Bias Analysis
├── Demand Classification
├── Reliability Calibration
└── Hierarchical Forecast Reconciliation

Reporting
├── Excel
└── Power BI-ready outputs
```

------------------------------------------------------------------------

## 📁 Repository Structure

``` text
Sales-Demand-Classification-and-Forecasting/
│
├── sales_target_forecasting_v4.py
├── README.md
├── requirements.txt
├── .gitignore
│
├── images/
│   └── demand_classification.png
│
├── outputs/
│   └── .gitkeep
│
└── docs/
    └── methodology.md
```

------------------------------------------------------------------------

## ⚙️ Installation

Clone the repository and install the required Python packages:

``` bash
pip install -r requirements.txt
```

The public version reads database configuration from environment
variables rather than storing production credentials directly in source
code.

Example configuration variables:

``` text
SALES_DB_SERVER
SALES_DB_NAME
SALES_DB_USERNAME
SALES_DB_PASSWORD
```

Users should adapt database table and column mappings to their own
environment.

------------------------------------------------------------------------

## 🔐 Data Privacy

The original implementation was designed for enterprise sales data.

For confidentiality and security:

-   Database credentials are not included.
-   Internal server and database information is removed.
-   Personal computer paths are removed.
-   Raw transactional data is not published.
-   Customer and organization-specific information is excluded.
-   Generated enterprise workbooks are excluded from Git tracking.
-   Public examples should use anonymized or synthetic data.

------------------------------------------------------------------------

## 💡 Key Features

-   End-to-end SQL-to-forecast pipeline
-   Automatic product activity classification
-   ADI/CV² demand segmentation
-   Smooth, Erratic, Intermittent, and Lumpy demand detection
-   Pattern-aware forecasting
-   Specialized intermittent-demand models
-   Rolling backtesting
-   Four-week sales-target-oriented evaluation
-   Recency-aware champion selection
-   Forecast bias detection and correction
-   Reliability-aware forecast calibration
-   Bounded recent-momentum adjustment
-   Smart sales-target generation
-   Product × sales-channel hierarchical forecasting
-   Forecast reconciliation
-   Jalali calendar support
-   Automated Excel reporting
-   Power BI-ready analytical outputs

------------------------------------------------------------------------

## 🔭 Future Development

Potential extensions include:

-   Machine-learning forecasting with lag and calendar features
-   Gradient-boosting models
-   Probabilistic forecasting
-   Prediction intervals
-   Automated hyperparameter optimization
-   Forecast monitoring and model-drift detection
-   Interactive Power BI dashboards
-   REST API deployment
-   Dockerized forecasting service
-   Automated scheduled retraining

------------------------------------------------------------------------

## 👩‍💻 Author

**Mona Faghfouri Azar**

Data Analyst \| AI & Computational Social Science Researcher

`Artificial Intelligence` · `Data Science` · `Time-Series Forecasting` ·
`NLP` · `Computational Social Science` · `Business Analytics`

GitHub: **MonaFaghfouri**

------------------------------------------------------------------------

## ⭐ About This Project

This project demonstrates how statistical forecasting can be transformed
from a standalone modeling exercise into an **end-to-end business
decision-support system**.

Instead of asking only:

> *"Which model predicts next week's sales?"*

the pipeline addresses a broader question:

> **"Given the behavior, uncertainty, recent trajectory, and
> sales-channel structure of each product, what forecast and sales
> target can support a defensible business decision?"**

------------------------------------------------------------------------

⭐ If you find this project useful, consider starring the repository.
