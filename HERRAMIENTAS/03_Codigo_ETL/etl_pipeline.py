"""ETL and statistical analysis for Project 7 using the Yelp Open Dataset.

Expected input files in --raw-dir:
    yelp_academic_dataset_business.json
    yelp_academic_dataset_review.json
    yelp_academic_dataset_user.json

The Yelp files are JSON Lines files. The pipeline keeps only businesses whose
categories include Restaurants and creates a Power BI-ready star schema.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
from pathlib import Path
from typing import Any, Iterator

import numpy as np
import pandas as pd
from scipy import stats
from statsmodels.stats.multicomp import pairwise_tukeyhsd
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

LOG = logging.getLogger("yelp_etl")
RESTAURANT_TOKEN = re.compile(r"(?:^|,\s*)restaurants(?:,|$)", re.IGNORECASE)


def jsonl(path: Path) -> Iterator[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as file:
        for line_number, line in enumerate(file, 1):
            try:
                yield json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(f"JSON inválido en {path}:{line_number}") from error


def require_file(directory: Path, name: str) -> Path:
    path = directory / name
    if not path.is_file():
        raise FileNotFoundError(f"Falta el archivo requerido: {path}")
    return path


def sentiment_label(compound: float) -> str:
    if compound >= 0.05:
        return "Positive"
    if compound <= -0.05:
        return "Negative"
    return "Neutral"


def welch_anova(samples: list[list[float]]) -> tuple[float, float, float, float]:
    """Return Welch's F statistic, p-value, and numerator/denominator degrees of freedom."""
    arrays = [np.asarray(sample, dtype=float) for sample in samples]
    group_count = len(arrays)
    if group_count < 2 or any(len(sample) < 2 for sample in arrays):
        raise ValueError("Welch ANOVA requiere al menos dos grupos con dos observaciones.")

    counts = np.asarray([len(sample) for sample in arrays], dtype=float)
    means = np.asarray([np.mean(sample) for sample in arrays])
    variances = np.asarray([np.var(sample, ddof=1) for sample in arrays])
    if np.any(variances <= 0):
        raise ValueError("Welch ANOVA requiere varianza positiva en cada grupo.")

    weights = counts / variances
    weight_total = np.sum(weights)
    weighted_mean = np.sum(weights * means) / weight_total
    numerator = np.sum(weights * (means - weighted_mean) ** 2) / (group_count - 1)
    correction_term = np.sum(
        (1 - weights / weight_total) ** 2 / (counts - 1)
    )
    correction = 1 + (2 * (group_count - 2) / (group_count**2 - 1)) * correction_term
    f_statistic = numerator / correction
    df1 = float(group_count - 1)
    df2_denominator = 3 * correction_term
    df2 = float("inf") if df2_denominator == 0 else (group_count**2 - 1) / df2_denominator
    p_value = float(stats.f.sf(f_statistic, df1, df2))
    return float(f_statistic), p_value, df1, float(df2)


def run_anova(reviews: pd.DataFrame, output: Path) -> None:
    """Compare ratings across the five most frequent Yelp tags for restaurants."""
    category_groups = (
        reviews.dropna(subset=["category", "stars"])
        .groupby("category")["stars"]
        .apply(list)
        .sort_values(key=lambda values: values.map(len), ascending=False)
        .head(5)
    )
    category_groups = category_groups[category_groups.map(len) >= 30]
    rows: list[dict[str, Any]] = []
    if len(category_groups) < 2:
        raise ValueError("No hay al menos dos categorías con 30 observaciones para ANOVA.")

    samples = list(category_groups)
    levene_stat, levene_p = stats.levene(*samples, center="median")
    f_stat, p_value = stats.f_oneway(*samples)
    welch_stat, welch_p, welch_df1, welch_df2 = welch_anova(samples)
    anova_df1 = len(samples) - 1
    anova_df2 = sum(map(len, samples)) - len(samples)
    for category, values in category_groups.items():
        shapiro_sample = values[:5000]
        shapiro_stat, shapiro_p = stats.shapiro(shapiro_sample)
        rows.append(
            {
                "test": "Shapiro-Wilk",
                "group": category,
                "statistic": shapiro_stat,
                "p_value": shapiro_p,
                "n": len(values),
            }
        )
    rows.extend(
        [
            {"test": "Levene", "group": "All selected categories", "statistic": levene_stat, "p_value": levene_p, "n": sum(map(len, samples))},
            {"test": "One-way ANOVA", "group": "All selected categories", "statistic": f_stat, "p_value": p_value, "n": sum(map(len, samples)), "df1": anova_df1, "df2": anova_df2},
            {"test": "Welch ANOVA", "group": "All selected categories", "statistic": welch_stat, "p_value": welch_p, "n": sum(map(len, samples)), "df1": welch_df1, "df2": welch_df2},
        ]
    )
    pd.DataFrame(rows).to_csv(output / "anova_assumptions_and_result.csv", index=False)
    tukey = pairwise_tukeyhsd(
        endog=reviews.loc[reviews["category"].isin(category_groups.index), "stars"],
        groups=reviews.loc[reviews["category"].isin(category_groups.index), "category"],
    )
    pd.DataFrame(
        data=tukey._results_table.data[1:], columns=tukey._results_table.data[0]
    ).to_csv(output / "anova_tukey_posthoc.csv", index=False)


def prepare_review_batch(rows: list[dict[str, Any]]) -> pd.DataFrame:
    reviews = pd.DataFrame(rows)
    reviews["review_date"] = pd.to_datetime(reviews["review_date"], errors="coerce")
    reviews["review_day"] = reviews["review_date"].dt.normalize()
    reviews["review_year"] = reviews["review_date"].dt.year
    reviews["review_month"] = reviews["review_date"].dt.month
    reviews["review_month_name"] = reviews["review_date"].dt.strftime("%B")
    reviews["review_quarter"] = reviews["review_date"].dt.quarter
    return reviews


def build_date_dimension(review_dates: set[pd.Timestamp]) -> pd.DataFrame:
    """Create a gap-free calendar for Power BI time-intelligence functions."""
    if not review_dates:
        raise ValueError("No se encontraron fechas válidas para crear dim_date.")

    dates = pd.DataFrame(
        {
            "date": pd.date_range(
                start=min(review_dates), end=max(review_dates), freq="D"
            )
        }
    )
    dates["year"] = dates["date"].dt.year
    dates["month_number"] = dates["date"].dt.month
    dates["month_name"] = dates["date"].dt.strftime("%B")
    dates["quarter"] = dates["date"].dt.quarter
    dates["day_of_week"] = dates["date"].dt.day_name()
    return dates


def transform(raw_dir: Path, output: Path, max_reviews: int | None) -> None:
    output.mkdir(parents=True, exist_ok=True)
    business_path = require_file(raw_dir, "yelp_academic_dataset_business.json")
    review_path = require_file(raw_dir, "yelp_academic_dataset_review.json")
    user_path = require_file(raw_dir, "yelp_academic_dataset_user.json")
    fact_path = output / "fact_review.csv"

    businesses: list[dict[str, Any]] = []
    business_categories: list[dict[str, str]] = []
    business_ids: set[str] = set()
    for row in jsonl(business_path):
        categories = row.get("categories") or ""
        if not RESTAURANT_TOKEN.search(categories):
            continue
        business_id = row["business_id"]
        business_ids.add(business_id)
        businesses.append(
            {
                "business_id": business_id,
                "business_name": row.get("name"),
                "city": row.get("city"),
                "state": row.get("state"),
                "postal_code": row.get("postal_code"),
                "latitude": row.get("latitude"),
                "longitude": row.get("longitude"),
                "price_range": pd.to_numeric(
                    (row.get("attributes") or {}).get("RestaurantsPriceRange2"),
                    errors="coerce",
                ),
                "is_open": row.get("is_open"),
            }
        )
        business_categories.extend(
            {"business_id": business_id, "category": category}
            for category in categories.split(", ")
            if category
        )

    category_frame = pd.DataFrame(business_categories)
    category_frame = category_frame[
        ~category_frame["category"].str.casefold().isin(
            {"restaurants", "food", "nightlife"}
        )
    ]
    # This first remaining Yelp tag is not guaranteed to be a cuisine type.
    category_by_business = category_frame.groupby("business_id")["category"].first()

    analyzer = SentimentIntensityAnalyzer()
    review_batch: list[dict[str, Any]] = []
    anova_rows: list[tuple[str | None, Any]] = []
    review_dates: set[pd.Timestamp] = set()
    user_ids: set[str] = set()
    reviews_written = 0
    first_batch = True
    if fact_path.exists():
        fact_path.unlink()

    for row in jsonl(review_path):
        business_id = row.get("business_id")
        if business_id not in business_ids:
            continue
        text = row.get("text") or ""
        compound = analyzer.polarity_scores(text)["compound"]
        category = category_by_business.get(business_id)
        review_batch.append(
            {
                "review_id": row["review_id"],
                "business_id": business_id,
                "user_id": row.get("user_id"),
                "review_date": row.get("date"),
                "stars": row.get("stars"),
                "useful": row.get("useful", 0),
                "funny": row.get("funny", 0),
                "cool": row.get("cool", 0),
                "review_text": text,
                "sentiment_score": compound,
                "sentiment_label": sentiment_label(compound),
                "category": category,
            }
        )
        anova_rows.append((category, row.get("stars")))
        parsed_date = pd.to_datetime(row.get("date"), errors="coerce")
        if not pd.isna(parsed_date):
            review_dates.add(parsed_date.normalize())
        if row.get("user_id"):
            user_ids.add(row["user_id"])
        reviews_written += 1

        if len(review_batch) >= 50000:
            batch_frame = prepare_review_batch(review_batch)
            batch_frame.to_csv(fact_path, mode="w" if first_batch else "a", header=first_batch, index=False)
            first_batch = False
            review_batch.clear()
            LOG.info("Reseñas procesadas: %d", reviews_written)
        if max_reviews and reviews_written >= max_reviews:
            break

    if review_batch:
        batch_frame = prepare_review_batch(review_batch)
        batch_frame.to_csv(fact_path, mode="w" if first_batch else "a", header=first_batch, index=False)
    if reviews_written == 0:
        raise ValueError("No se encontraron reseñas de restaurantes.")

    users: list[dict[str, Any]] = []
    for row in jsonl(user_path):
        if row.get("user_id") in user_ids:
            users.append(
                {
                    "user_id": row["user_id"],
                    "review_count": row.get("review_count"),
                    "average_stars": row.get("average_stars"),
                    "fans": row.get("fans"),
                    "yelping_since": row.get("yelping_since"),
                }
            )
    if not users:
        raise ValueError("No se encontraron perfiles para las reseñas de restaurantes.")

    pd.DataFrame(businesses).drop_duplicates("business_id").to_csv(
        output / "dim_business.csv", index=False
    )
    pd.DataFrame(business_categories).drop_duplicates().to_csv(
        output / "bridge_business_category.csv", index=False
    )
    pd.DataFrame(users).drop_duplicates("user_id").to_csv(
        output / "dim_user.csv", index=False
    )
    build_date_dimension(review_dates).to_csv(
        output / "dim_date.csv", index=False
    )
    run_anova(
        pd.DataFrame(anova_rows, columns=["category", "stars"]),
        output,
    )
    LOG.info("ETL terminado: %d negocios y %d reseñas.", len(businesses), reviews_written)


def main() -> None:
    parser = argparse.ArgumentParser(description="ETL Yelp para el Proyecto Final 7")
    parser.add_argument("--raw-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, default=Path("data/processed"))
    parser.add_argument("--max-reviews", type=int, default=None)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    transform(args.raw_dir, args.output_dir, args.max_reviews)


if __name__ == "__main__":
    main()
