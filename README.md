# 📈 Sales Demand Classification & Forecasting Pipeline

An end-to-end **Python-based demand analytics, forecasting, and sales target optimization pipeline** designed for real-world sales data.

The system connects directly to **SQL Server**, analyzes historical product activity and sales behavior, classifies demand patterns, evaluates multiple forecasting strategies through backtesting, and generates data-driven sales targets at both **product and sales-channel levels**.

> **From raw transactional data to demand intelligence and actionable sales targets.**

---

## 🚀 Project Overview

Sales forecasting becomes particularly challenging when products exhibit very different demand behaviors.

Some products sell consistently, while others experience irregular, intermittent, or highly volatile demand. Applying the same forecasting model to every product can therefore produce unreliable forecasts and unrealistic sales targets.

This project addresses that problem through a multi-stage forecasting architecture:

```text
SQL Server
    ↓
Data Extraction & Cleaning
    ↓
Product Activity Analysis
    ↓
Demand Time-Series Construction
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
Reliability & Bias Analysis
    ↓
Smart Sales Target Generation
    ↓
Product × Sales Channel Allocation
    ↓
Excel Management Reports
```

The objective is not simply to generate forecasts, but to build a **decision-support pipeline for sales planning**.

---

# 🧠 Demand Classification

Products are first analyzed according to their historical activity and demand characteristics.

Demand behavior is classified using two widely used intermittent-demand measures:

### Average Demand Interval — ADI

```text
ADI = Number of Time Periods / Number of Non-Zero Demand Periods
```

ADI measures how frequently demand occurs.

### Squared Coefficient of Variation — CV²

```text
CV² = (Standard Deviation of Non-Zero Demand / Mean Non-Zero Demand)²
```

CV² captures variability in demand size.

Using these metrics, products are classified into four demand patterns:

| Demand Pattern  |    ADI |    CV² | Interpretation                                |
| --------------- | -----: | -----: | --------------------------------------------- |
| 🟢 Smooth       | ≤ 1.32 | ≤ 0.49 | Frequent and relatively stable demand         |
| 🟡 Erratic      | ≤ 1.32 | > 0.49 | Frequent but highly variable demand           |
| 🔵 Intermittent | > 1.32 | ≤ 0.49 | Infrequent but relatively stable demand sizes |
| 🔴 Lumpy        | > 1.32 | > 0.49 | Infrequent and highly variable demand         |

This classification allows the forecasting strategy to adapt to the statistical characteristics of each product instead of applying one model universally.

---

# 🏭 Product Activity Classification

Before forecasting, products are also evaluated based on their production/order history.

Historical order gaps are analyzed using statistics including:

* Average order gap
* Median order gap
* 90th percentile order gap
* 95th percentile order gap
* Days since the most recent order

Products are then assigned an operational status:

```text
Active
Dormant
Inactive
Insufficient History
```

Only appropriate products proceed to the forecasting pipeline.

This prevents discontinued or historically insufficient products from receiving misleading statistical forecasts.

---

# 🔮 Forecasting Engine

The forecasting engine evaluates multiple model families instead of relying on a single forecasting algorithm.

## Regular Demand Models

For smoother or more regular demand patterns, candidate models include:

* Naive Forecast
* Moving Average
* Historical Mean
* Exponentially Weighted Mean
* Simple Exponential Smoothing
* Holt Trend
* ETS / Exponential Smoothing
* Seasonal Naive
* ARIMA / SARIMAX
* Recent Median Baselines

## Intermittent Demand Models

For intermittent and lumpy demand, specialized approaches are evaluated, including:

* Croston
* SBA — Syntetos-Boylan Approximation
* TSB — Teunter-Syntetos-Babai
* ADIDA
* Two-stage occurrence × demand-size models

This distinction is important because conventional time-series models may perform poorly when a large proportion of periods contain zero demand.

---

# 🏆 Champion Model Selection

The system performs rolling backtesting and evaluates candidate models before selecting the final forecasting strategy for each product.

Evaluation considers metrics such as:

* **WAPE — Weighted Absolute Percentage Error**
* **MAE — Mean Absolute Error**
* **RMSE — Root Mean Squared Error**
* **Forecast Bias**
* Recent-period forecasting performance
* Demand occurrence accuracy
* Demand-event recall

Rather than weighting all historical backtests equally, the pipeline can assign greater importance to recent demand behavior.

This helps the selected model adapt when the underlying sales regime changes over time.

---

# 📅 Sales-Target-Oriented Evaluation

An important design choice in this project is that forecasting performance is not evaluated only at the weekly level.

Because sales targets are often defined over approximately monthly horizons, weekly forecasts are aggregated into:

```text
4-week target buckets
```

Model selection therefore emphasizes cumulative four-week forecasting accuracy while retaining weekly accuracy as a diagnostic signal.

This aligns the statistical objective more closely with the actual business decision.

---

# ⚖️ Forecast Bias Correction

Forecasting models may systematically overestimate or underestimate demand.

The pipeline therefore measures historical forecast bias during backtesting and can apply a bounded correction when:

* sufficient backtesting evidence exists, and
* systematic bias exceeds a predefined threshold.

Corrections are intentionally constrained to prevent aggressive adjustments caused by temporary anomalies.

---

# 🎯 Smart Sales Target Generation

A statistical forecast and a managerial sales target are not necessarily the same thing.

For this reason, the project explicitly separates:

```text
Expected Demand Forecast
        ↓
Management Adjustment
        ↓
Recommended Sales Target
```

Recommended targets can incorporate:

* Forecast reliability
* Recent sales momentum
* Product activity status
* Demand pattern
* Historical sales behavior
* Conservative upper/lower guardrails

This produces targets that are intended to remain **challenging but data-grounded**.

---

# 🛡️ Reliability-Aware Forecasting

Not every forecast should receive the same level of trust.

Products are assigned reliability levels based on backtesting performance.

For weaker forecasts, the system can blend the selected model with a robust recent-sales baseline.

Conceptually:

```text
High Reliability
      ↓
Trust statistical model more

Medium Reliability
      ↓
Blend model + recent baseline

Low Reliability
      ↓
Trust robust recent behavior more
```

This reduces the risk of extreme targets caused by unstable models.

---

# 📊 Hierarchical Product × Channel Forecasting

The pipeline extends forecasting beyond product-level demand.

Sales targets can also be distributed across sales channels.

The architecture follows a hierarchical structure:

```text
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

A reconciliation step ensures that:

```text
Σ Channel Targets = Product Sales Target
```

for each product.

This maintains consistency between operational sales-channel targets and the overall product forecast.

---

# 🗄️ Data Pipeline

The pipeline retrieves data directly from SQL Server rather than requiring intermediate Excel inputs.

Main data sources include:

```text
Production Orders
Sales Invoices
Date Dimension
Historical Sales Targets
```

The workflow integrates production activity information with historical sales transactions before constructing the forecasting dataset.

---

# 🗓️ Persian / Jalali Calendar Support

The pipeline includes support for **Jalali dates** and converts them to Gregorian timestamps for time-series modeling.

It also incorporates Iranian calendar characteristics such as:

* Official holidays
* Fridays
* Jalali-to-Gregorian conversion

This allows the forecasting workflow to operate correctly on datasets originating from Iranian enterprise systems.

---

# 📂 Output

The pipeline generates Excel-based diagnostic and management reports containing information such as:

* Product activity status
* Demand pattern
* ADI
* CV²
* Historical demand characteristics
* Selected forecasting model
* Forecast accuracy
* Forecast bias
* Reliability level
* Weekly forecasts
* Four-week forecast buckets
* Recommended sales targets
* Channel-level target allocation
* Model comparison diagnostics
* Backtesting results

The resulting reports can be used for further analysis in tools such as **Excel and Power BI**.

---

# 🛠️ Technology Stack

```text
Python
├── pandas
├── NumPy
├── SQLAlchemy
├── pyodbc
├── statsmodels
└── jdatetime

Database
└── Microsoft SQL Server

Analytics
├── Time-Series Forecasting
├── Intermittent Demand Forecasting
├── Rolling Backtesting
├── Forecast Bias Analysis
├── Demand Classification
└── Hierarchical Forecast Reconciliation

Reporting
├── Excel
└── Power BI-ready outputs
```

---

# 📁 Suggested Repository Structure

```text
Sales-Demand-Classification-and-Forecasting/
│
├── sales_target_forecasting.py
│
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

---

# 🔐 Data Privacy

The original implementation was developed for enterprise sales data.

For confidentiality and security reasons:

* Database credentials are not included.
* Internal server information is removed from the public version.
* Raw transactional data is not published.
* Customer or organization-specific information is excluded.
* Example outputs should contain anonymized or synthetic data only.

Users who wish to reproduce the pipeline should configure their own SQL Server connection and adapt the database schema to their environment.

---

# 💡 Key Features

* End-to-end SQL-to-forecast pipeline
* Automatic product activity classification
* ADI/CV² demand segmentation
* Smooth, Erratic, Intermittent and Lumpy demand detection
* Multiple statistical forecasting models
* Specialized intermittent-demand forecasting
* Rolling backtesting
* Recency-aware model evaluation
* Forecast bias detection and correction
* Reliability-aware forecast calibration
* Smart sales target generation
* Product × sales-channel hierarchical forecasting
* Forecast reconciliation
* Jalali calendar support
* Automated Excel reporting
* Power BI-ready analytical outputs

---

# 🔭 Future Development

Potential extensions include:

* Machine-learning forecasting models
* Gradient boosting with lag and calendar features
* Probabilistic forecasting
* Prediction intervals
* Automated hyperparameter optimization
* Forecast monitoring and model-drift detection
* Interactive Power BI dashboards
* REST API deployment
* Dockerized forecasting service
* Automated scheduled model retraining

---

# 👩‍💻 Author

**Mona Faghfouri Azar**

Data Analyst | AI & Computational Social Science Researcher

Interested in:

`Artificial Intelligence` · `Data Science` · `Time-Series Forecasting` · `NLP` · `Computational Social Science` · `Business Analytics`

GitHub: **MonaFaghfouri**

---

## ⭐ About This Project

This project demonstrates how statistical forecasting can be transformed from a standalone modeling exercise into an **end-to-end business decision-support system**.

Instead of asking only:

> *“Which model predicts next week's sales?”*

the pipeline addresses a broader question:

> **“Given the behavior, uncertainty, recent trajectory, and sales-channel structure of each product, what forecast and sales target can support a defensible business decision?”**

---

⭐ If you find this project useful, consider starring the repository.

