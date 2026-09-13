# Methodology

## 1. Objective

This project implements an end-to-end sales demand classification, forecasting, and target-setting pipeline.

The workflow is designed to support operational sales planning rather than forecasting in isolation. It integrates product activity, demand-pattern classification, model evaluation, forecast reliability, management target calibration, and sales-channel reconciliation.

---

## 2. Data Sources

The implementation connects directly to Microsoft SQL Server and combines information from:

- production-order history,
- sales invoices,
- a date dimension,
- and historical sales-target records.

No intermediate Excel input is required for the core pipeline.

---

## 3. Product Activity Classification

Product activity is estimated from historical production-order gaps.

For each product, the pipeline calculates:

- first and most recent order dates,
- number of orders,
- number of active order days,
- average order gap,
- median order gap,
- 90th-percentile order gap,
- 95th-percentile order gap,
- days since the most recent order.

Products are then assigned to operational states:

- **Active**
- **Dormant**
- **Inactive**
- **Insufficient History**

The active and dormant thresholds are based on product-specific historical gap percentiles with minimum business-rule floors.

---

## 4. Demand-Series Construction

Sales transactions are aggregated into a regular weekly time series.

Missing periods are explicitly filled with zero demand so that intermittent behavior is represented correctly.

Negative demand caused by returns or adjustments can optionally be clipped to zero for demand-pattern analysis.

---

## 5. Demand Pattern Classification

Demand is classified using the Syntetos–Boylan framework based on:

### Average Demand Interval (ADI)

ADI = total number of periods / number of non-zero demand periods

### Squared Coefficient of Variation (CV²)

CV² = (standard deviation of non-zero demand / mean non-zero demand)²

Thresholds used in the implementation:

- ADI threshold: **1.32**
- CV² threshold: **0.49**

The resulting demand classes are:

| Pattern | Rule |
|---|---|
| Smooth | ADI ≤ 1.32 and CV² ≤ 0.49 |
| Erratic | ADI ≤ 1.32 and CV² > 0.49 |
| Intermittent | ADI > 1.32 and CV² ≤ 0.49 |
| Lumpy | ADI > 1.32 and CV² > 0.49 |

Demand-pattern classification is applied to products considered suitable for forecasting.

---

## 6. Candidate Forecasting Models

The forecasting engine evaluates multiple model families.

### Regular-demand candidates

Examples include:

- Naive
- Mean
- Moving Average
- Recency-weighted Mean
- EWMA
- Recent Median
- Simple Exponential Smoothing
- Holt
- ETS
- Seasonal Naive
- ARIMA / SARIMAX

### Intermittent-demand candidates

The implementation also evaluates models specifically suited to sparse demand, including:

- Croston
- SBA
- TSB
- ADIDA
- Two-stage occurrence × demand-size models
- Recency-weighted two-stage variants

---

## 7. Rolling Backtesting

Candidate models are compared using rolling historical backtests.

The main evaluation horizon is four weeks because the business objective is aligned with approximately monthly sales targets.

The implementation evaluates:

- WAPE
- MAE
- RMSE
- Bias Percent
- weekly diagnostic accuracy
- demand occurrence accuracy
- demand-event recall

Recent backtest folds receive more weight than older folds so that model selection can adapt to changing demand regimes.

---

## 8. Champion Model Selection

A champion model is selected for each forecastable product.

Selection is primarily driven by four-week cumulative forecasting error.

The scoring logic also considers:

- recent-weighted error,
- overall historical error,
- forecast bias,
- and a smaller weekly-error anchor.

A zero-demand forecast is not allowed to win simply because many historical periods contain zero sales.

---

## 9. Bias Correction

After model selection, systematic forecast bias can be corrected.

Bias correction is activated only when:

- enough backtest folds are available, and
- historical bias exceeds a predefined threshold.

The correction factor is bounded to avoid excessive forecast adjustments.

---

## 10. Reliability-Aware Forecast Calibration

Forecasts are assigned reliability levels based on historical backtest performance.

When reliability is weak, the champion-model forecast is blended with a robust recent-demand baseline.

This prevents unstable models from producing unrealistic targets.

---

## 11. Recent Momentum

Recent demand behavior is incorporated through a bounded momentum adjustment.

The implementation compares short-term and longer-term recent sales while constraining the adjustment factor to prevent temporary spikes from dominating future targets.

---

## 12. Sales Target Generation

The system distinguishes between:

1. the statistical sales forecast, and
2. the recommended management sales target.

The recommended target can apply a bounded smart stretch based on:

- forecast reliability,
- recent momentum,
- activity status,
- and demand pattern.

Guardrails prevent extreme targets while still allowing meaningful growth or decline.

Historical management targets are retained primarily as diagnostic information rather than directly forcing the statistical forecast toward previous target values.

---

## 13. Product × Sales Channel Allocation

The final product-level target acts as the coherent parent target.

Channel-level forecasts and recent historical shares are combined using reliability-aware weights.

A reconciliation procedure guarantees that:

**Sum of channel targets = product sales target**

for each product, including after rounding.

---

## 14. Calendar Handling

The pipeline supports Jalali dates and converts them to Gregorian timestamps for time-series modeling.

Iranian Fridays and configured official holidays are also represented in the analytical dataset.

---

## 15. Outputs

The detailed workbook contains diagnostic and modeling outputs such as:

- product diagnostics,
- demand patterns,
- model leaderboards,
- backtest details,
- champion models,
- weekly forecasts,
- four-week target buckets,
- reliability summaries,
- channel targets,
- channel reconciliation,
- configuration,
- and methodology notes.

A second management workbook provides executive-level summaries and sales-channel reporting.

---

## 16. Reproducibility and Data Privacy

The public GitHub version should not contain:

- production database credentials,
- internal server names,
- company-identifying paths,
- raw invoice or customer data,
- confidential historical targets,
- or generated workbooks containing proprietary business information.

Users should configure their own database connection and adapt table/column names to their environment.
