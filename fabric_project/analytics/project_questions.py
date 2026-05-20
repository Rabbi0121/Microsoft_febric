from __future__ import annotations

import argparse
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from fabric_project.common.logging import configure_logging
from fabric_project.common.settings import load_settings

logger = logging.getLogger(__name__)


def _load_parquet(path: Path) -> pd.DataFrame | None:
    if not path.exists():
        return None
    return pd.read_parquet(path)


def _fmt_date(value: Any) -> str | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    try:
        parsed = pd.to_datetime(value, errors="coerce")
    except Exception:
        return str(value)
    if pd.isna(parsed):
        return str(value)
    return parsed.date().isoformat()


def _coverage(df: pd.DataFrame | None, date_col: str, *, name: str) -> dict[str, Any]:
    if df is None or df.empty:
        return {"dataset": name, "rows": 0, "min_date": None, "max_date": None}

    dt = pd.to_datetime(df[date_col], errors="coerce")
    dt = dt.dropna()
    if dt.empty:
        return {"dataset": name, "rows": len(df), "min_date": None, "max_date": None}

    return {
        "dataset": name,
        "rows": int(len(df)),
        "min_date": dt.min().date().isoformat(),
        "max_date": dt.max().date().isoformat(),
    }


def _question_1(taxi_gold: pd.DataFrame | None, aq_gold: pd.DataFrame | None) -> dict[str, Any]:
    if taxi_gold is None or taxi_gold.empty:
        return {
            "status": "insufficient_data",
            "message": "Taxi gold table is missing or empty.",
        }
    if aq_gold is None or aq_gold.empty:
        return {
            "status": "insufficient_data",
            "message": "Air quality gold table is missing or empty.",
        }

    taxi = taxi_gold.copy()
    taxi["date"] = pd.to_datetime(taxi["pickup_date"], errors="coerce").dt.date
    taxi_daily = (
        taxi.groupby("date", as_index=False)
        .agg(trips_per_day=("trip_count", "sum"))
        .dropna(subset=["date"])
    )

    aq = aq_gold.copy()
    aq["date"] = pd.to_datetime(aq["date"], errors="coerce").dt.date
    aq["parameter"] = aq["parameter"].astype(str).str.lower()
    aq = aq[aq["parameter"].isin(["pm25", "no2"])]
    if aq.empty:
        return {
            "status": "insufficient_data",
            "message": "OpenAQ snapshot does not contain PM2.5/NO2 records.",
        }

    aq_daily = (
        aq.groupby(["date", "parameter"], as_index=False)
        .agg(avg_pollutant=("avg_pollutant_value", "mean"))
        .dropna(subset=["date"])
    )
    aq_pivot = aq_daily.pivot(index="date", columns="parameter", values="avg_pollutant").reset_index()
    joined = taxi_daily.merge(aq_pivot, on="date", how="inner")

    correlations: dict[str, float] = {}
    for col in ["pm25", "no2"]:
        if col in joined.columns and joined[col].notna().sum() >= 3:
            corr = joined["trips_per_day"].corr(joined[col])
            if pd.notna(corr):
                correlations[col] = float(corr)

    if len(joined) < 3 or not correlations:
        return {
            "status": "insufficient_data",
            "message": (
                "Traffic vs PM2.5/NO2 correlation could not be estimated "
                "because date overlap is too small or missing."
            ),
            "overlap_rows": int(len(joined)),
            "available_aq_parameters": sorted(aq["parameter"].dropna().unique().tolist()),
        }

    return {
        "status": "answered",
        "message": "Daily traffic-to-pollution correlation estimated on overlapping dates.",
        "overlap_rows": int(len(joined)),
        "correlation_trips_vs_pollutant": correlations,
    }


def _find_first_column(df: pd.DataFrame, candidates: list[str]) -> str | None:
    for candidate in candidates:
        if candidate in df.columns:
            return candidate
    return None


def _question_2(data_lake_root: Path) -> dict[str, Any]:
    taxi_silver = _load_parquet(
        data_lake_root / "silver" / "nyc_taxi" / "taxi_trips_clean.parquet"
    )
    aq_silver = _load_parquet(
        data_lake_root / "silver" / "openaq" / "air_quality_measurements.parquet"
    )

    if taxi_silver is None or taxi_silver.empty:
        return {
            "status": "insufficient_data",
            "message": "Taxi silver table is missing or empty.",
        }
    if aq_silver is None or aq_silver.empty:
        return {
            "status": "insufficient_data",
            "message": "OpenAQ silver table is missing or empty.",
        }

    pickup_col = _find_first_column(
        taxi_silver,
        ["tpep_pickup_datetime", "pickup_datetime", "pickup_ts"],
    )
    if pickup_col is None:
        fallback = [
            col
            for col in taxi_silver.columns
            if "pickup" in col.lower() and "time" in col.lower()
        ]
        pickup_col = fallback[0] if fallback else None
    if pickup_col is None:
        return {
            "status": "insufficient_data",
            "message": "Could not detect taxi pickup timestamp column.",
        }

    taxi = taxi_silver.copy()
    taxi[pickup_col] = pd.to_datetime(taxi[pickup_col], errors="coerce")
    taxi = taxi.dropna(subset=[pickup_col])
    taxi["date"] = taxi[pickup_col].dt.date
    taxi["hour"] = taxi[pickup_col].dt.hour

    top_taxi_hours = (
        taxi.groupby("hour", as_index=False)
        .size()
        .rename(columns={"size": "trip_count"})
        .sort_values("trip_count", ascending=False)
        .head(5)
    )

    zone_col = _find_first_column(taxi, ["PULocationID", "pu_location_id"])
    top_zones: list[dict[str, Any]] = []
    if zone_col is not None:
        top_zones_df = (
            taxi.groupby(zone_col, as_index=False)
            .size()
            .rename(columns={"size": "trip_count"})
            .sort_values("trip_count", ascending=False)
            .head(10)
        )
        top_zones = [
            {"zone_id": int(row[zone_col]), "trip_count": int(row["trip_count"])}
            for _, row in top_zones_df.iterrows()
        ]

    aq = aq_silver.copy()
    aq["parameter"] = aq["parameter"].astype(str).str.lower()
    aq = aq[aq["parameter"].isin(["pm25", "no2"])]
    if aq.empty:
        return {
            "status": "insufficient_data",
            "message": "OpenAQ silver snapshot contains neither PM2.5 nor NO2.",
            "top_taxi_hours": top_taxi_hours.to_dict(orient="records"),
            "top_taxi_zones": top_zones,
        }

    aq["timestamp_utc"] = pd.to_datetime(aq["timestamp_utc"], utc=True, errors="coerce")
    aq = aq.dropna(subset=["timestamp_utc", "value"])
    aq["date"] = aq["timestamp_utc"].dt.date
    aq["hour"] = aq["timestamp_utc"].dt.hour

    aq_top_hours: dict[str, list[dict[str, Any]]] = {}
    for parameter in sorted(aq["parameter"].dropna().unique()):
        top = (
            aq[aq["parameter"] == parameter]
            .groupby("hour", as_index=False)
            .agg(avg_pollutant=("value", "mean"), observations=("value", "count"))
            .sort_values("avg_pollutant", ascending=False)
            .head(5)
        )
        aq_top_hours[parameter] = top.to_dict(orient="records")

    taxi_hourly_daily = (
        taxi.groupby(["date", "hour"], as_index=False)
        .size()
        .rename(columns={"size": "trip_count"})
    )

    overlap_summary: dict[str, dict[str, Any]] = {}
    for parameter in ["pm25", "no2"]:
        param = (
            aq[aq["parameter"] == parameter]
            .groupby(["date", "hour"], as_index=False)
            .agg(avg_pollutant=("value", "mean"))
        )
        joined = taxi_hourly_daily.merge(param, on=["date", "hour"], how="inner")
        if len(joined) < 10:
            overlap_summary[parameter] = {
                "status": "insufficient_overlap",
                "overlap_rows": int(len(joined)),
            }
            continue

        corr = joined["trip_count"].corr(joined["avg_pollutant"])
        by_hour = (
            joined.groupby("hour", as_index=False)
            .agg(trips=("trip_count", "mean"), pollutant=("avg_pollutant", "mean"))
        )
        by_hour["trip_norm"] = (
            (by_hour["trips"] - by_hour["trips"].min())
            / max((by_hour["trips"].max() - by_hour["trips"].min()), 1e-9)
        )
        by_hour["pollutant_norm"] = (
            (by_hour["pollutant"] - by_hour["pollutant"].min())
            / max((by_hour["pollutant"].max() - by_hour["pollutant"].min()), 1e-9)
        )
        by_hour["co_peak_score"] = by_hour["trip_norm"] * by_hour["pollutant_norm"]
        strongest = by_hour.sort_values("co_peak_score", ascending=False).head(1).iloc[0]

        overlap_summary[parameter] = {
            "status": "estimated",
            "overlap_rows": int(len(joined)),
            "hourly_correlation_trips_vs_pollutant": float(corr) if pd.notna(corr) else None,
            "strongest_co_peak_hour": int(strongest["hour"]),
            "strongest_co_peak_score": float(strongest["co_peak_score"]),
        }

    return {
        "status": "answered_with_coverage_limits",
        "message": (
            "Taxi demand peaks were estimated from silver taxi data; pollutant peak hours were "
            "estimated from silver OpenAQ data. Correlation by hour is only shown where overlap exists."
        ),
        "top_taxi_hours": top_taxi_hours.to_dict(orient="records"),
        "top_taxi_zones": top_zones,
        "top_pollutant_hours": aq_top_hours,
        "hourly_overlap_analysis": overlap_summary,
    }


def _question_3(taxi_gold: pd.DataFrame | None, economy_gold: pd.DataFrame | None) -> dict[str, Any]:
    if taxi_gold is None or taxi_gold.empty:
        return {"status": "insufficient_data", "message": "Taxi gold table is missing or empty."}
    if economy_gold is None or economy_gold.empty:
        return {"status": "insufficient_data", "message": "Economy gold table is missing or empty."}

    taxi = taxi_gold.copy()
    taxi["date"] = pd.to_datetime(taxi["pickup_date"], errors="coerce").dt.date
    daily = (
        taxi.groupby("date", as_index=False)
        .agg(trips=("trip_count", "sum"), revenue_usd=("revenue_usd", "sum"))
        .dropna(subset=["date"])
    )
    daily = daily[daily["trips"] > 0]
    daily["avg_revenue_per_trip_usd"] = daily["revenue_usd"] / daily["trips"]

    eco = economy_gold.copy()
    eco["date"] = pd.to_datetime(eco["fx_date"], errors="coerce").dt.date
    fx = eco[["date", "usd_to_eur_rate"]].drop_duplicates(subset=["date"])

    joined = daily.merge(fx, on="date", how="inner").dropna(subset=["usd_to_eur_rate"])
    if joined.empty:
        return {
            "status": "insufficient_data",
            "message": "No overlapping taxi and FX dates were found.",
        }

    joined["avg_revenue_per_trip_eur"] = (
        joined["avg_revenue_per_trip_usd"] * joined["usd_to_eur_rate"]
    )
    joined["revenue_eur"] = joined["revenue_usd"] * joined["usd_to_eur_rate"]

    avg_usd = float(joined["revenue_usd"].sum() / joined["trips"].sum())
    avg_eur = float(joined["revenue_eur"].sum() / joined["trips"].sum())
    mean_fx = float(joined["usd_to_eur_rate"].mean())
    fx_min = float(joined["usd_to_eur_rate"].min())
    fx_max = float(joined["usd_to_eur_rate"].max())

    return {
        "status": "answered",
        "message": (
            "Average revenue/trip was estimated on overlapping taxi + FX dates. "
            "EUR conversion follows the project's current `usd_to_eur_rate` convention."
        ),
        "overlap_days": int(len(joined)),
        "avg_revenue_per_trip_usd_weighted": avg_usd,
        "avg_revenue_per_trip_eur_weighted": avg_eur,
        "fx_rate_mean": mean_fx,
        "fx_rate_min": fx_min,
        "fx_rate_max": fx_max,
        "fx_spread_pct": float(((fx_max - fx_min) / mean_fx) * 100) if mean_fx else None,
    }


def _question_4(
    taxi_gold: pd.DataFrame | None,
    aq_gold: pd.DataFrame | None,
    economy_gold: pd.DataFrame | None,
) -> dict[str, Any]:
    if taxi_gold is None or taxi_gold.empty:
        return {"status": "insufficient_data", "message": "Taxi gold table is missing or empty."}
    if aq_gold is None or aq_gold.empty:
        return {"status": "insufficient_data", "message": "Air quality gold table is missing or empty."}
    if economy_gold is None or economy_gold.empty:
        return {"status": "insufficient_data", "message": "Economy gold table is missing or empty."}

    taxi_years = set(pd.to_datetime(taxi_gold["pickup_date"], errors="coerce").dropna().dt.year.tolist())
    aq_years = set(pd.to_datetime(aq_gold["date"], errors="coerce").dropna().dt.year.tolist())
    eco_years = set(pd.to_datetime(economy_gold["fx_date"], errors="coerce").dropna().dt.year.tolist())
    overlap_years = sorted(taxi_years & aq_years & eco_years)

    pm25 = aq_gold.copy()
    pm25["parameter"] = pm25["parameter"].astype(str).str.lower()
    pm25 = pm25[pm25["parameter"] == "pm25"]
    pm25["year"] = pd.to_datetime(pm25["date"], errors="coerce").dt.year
    pm25_yearly = (
        pm25.groupby("year", as_index=False)
        .agg(avg_pm25=("avg_pollutant_value", "mean"))
        .dropna()
    )

    gdp_yearly = (
        economy_gold.copy()
        .groupby("year", as_index=False)
        .agg(gdp_usd=("gdp_usd", "mean"))
        .dropna(subset=["year", "gdp_usd"])
        .sort_values("year")
    )

    if not overlap_years:
        return {
            "status": "insufficient_data",
            "message": (
                "No common year exists across taxi, air-quality, and economy datasets, "
                "so a multi-year cross-domain growth-vs-environment conclusion is not yet reliable."
            ),
            "overlap_years": overlap_years,
            "pm25_yearly_sample": pm25_yearly.tail(5).to_dict(orient="records"),
            "gdp_yearly_sample": gdp_yearly.tail(5).to_dict(orient="records"),
        }

    return {
        "status": "answered",
        "message": "Common years exist across all 3 core domains; trend analysis can be estimated.",
        "overlap_years": overlap_years,
        "pm25_yearly_sample": pm25_yearly.tail(5).to_dict(orient="records"),
        "gdp_yearly_sample": gdp_yearly.tail(5).to_dict(orient="records"),
    }


def _to_jsonable(payload: Any) -> Any:
    if isinstance(payload, dict):
        return {str(key): _to_jsonable(value) for key, value in payload.items()}
    if isinstance(payload, list):
        return [_to_jsonable(item) for item in payload]
    if isinstance(payload, tuple):
        return [_to_jsonable(item) for item in payload]
    if isinstance(payload, pd.Timestamp):
        return payload.isoformat()
    if hasattr(payload, "item"):
        try:
            return payload.item()
        except Exception:
            return str(payload)
    return payload


def _to_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Project Questions Report",
        "",
        f"Generated at (UTC): {report['generated_at_utc']}",
        "",
        "## Data Coverage",
    ]
    for item in report["coverage"]:
        lines.append(
            f"- {item['dataset']}: rows={item['rows']}, "
            f"min_date={item['min_date']}, max_date={item['max_date']}"
        )

    lines.extend(
        [
            "",
            "## Q1: Trips per day vs PM2.5/NO2",
            f"- Status: {report['q1']['status']}",
            f"- Summary: {report['q1'].get('message')}",
            f"- Details: {json.dumps(report['q1'], default=str)}",
            "",
            "## Q2: Zones/Times strongest taxi-vs-pollution link",
            f"- Status: {report['q2']['status']}",
            f"- Summary: {report['q2'].get('message')}",
            f"- Details: {json.dumps(report['q2'], default=str)}",
            "",
            "## Q3: Revenue per trip USD vs EUR and FX effect",
            f"- Status: {report['q3']['status']}",
            f"- Summary: {report['q3'].get('message')}",
            f"- Details: {json.dumps(report['q3'], default=str)}",
            "",
            "## Q4: Multi-year mobility/economy vs environment",
            f"- Status: {report['q4']['status']}",
            f"- Summary: {report['q4'].get('message')}",
            f"- Details: {json.dumps(report['q4'], default=str)}",
            "",
        ]
    )
    return "\n".join(lines)


def run_report(output_dir: Path) -> dict[str, Any]:
    settings = load_settings()
    data_lake_root = settings.data_lake_root

    taxi_gold = _load_parquet(data_lake_root / "gold" / "nyc_taxi" / "fact_taxi_daily.parquet")
    aq_gold = _load_parquet(data_lake_root / "gold" / "openaq" / "fact_air_quality_daily.parquet")
    eco_gold = _load_parquet(data_lake_root / "gold" / "economy" / "fact_fx_gdp_daily.parquet")
    weather_gold = _load_parquet(
        data_lake_root / "gold" / "weather" / "fact_weather_daily.parquet"
    )

    report = {
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "coverage": [
            _coverage(taxi_gold, "pickup_date", name="taxi_gold"),
            _coverage(aq_gold, "date", name="air_quality_gold"),
            _coverage(eco_gold, "fx_date", name="economy_gold"),
            _coverage(weather_gold, "date", name="weather_gold"),
        ],
        "q1": _question_1(taxi_gold, aq_gold),
        "q2": _question_2(data_lake_root),
        "q3": _question_3(taxi_gold, eco_gold),
        "q4": _question_4(taxi_gold, aq_gold, eco_gold),
    }

    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    json_path = output_dir / f"project_questions_{stamp}.json"
    md_path = output_dir / f"project_questions_{stamp}.md"

    json_path.write_text(json.dumps(_to_jsonable(report), indent=2), encoding="utf-8")
    md_path.write_text(_to_markdown(report), encoding="utf-8")

    logger.info("Project questions report written: %s", md_path)
    logger.info("Project questions report written: %s", json_path)
    print(f"Project questions report (markdown): {md_path}")
    print(f"Project questions report (json): {json_path}")
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Generate an analysis-ready report that answers the 4 core project questions "
            "using current lakehouse artifacts."
        )
    )
    parser.add_argument(
        "--output-dir",
        default="reports/insights",
        help="Output directory for markdown/json report files",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    configure_logging()
    settings = load_settings()
    output_dir = settings.resolve_path(args.output_dir)
    run_report(output_dir)


if __name__ == "__main__":
    main()

