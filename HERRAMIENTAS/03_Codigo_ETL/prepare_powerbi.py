import argparse
import csv
import os
import re
from collections import Counter
from pathlib import Path

import pandas as pd


FIELDS = [
    "review_id",
    "business_id",
    "user_id",
    "review_day",
    "stars",
    "useful",
    "sentiment_score",
    "sentiment_label",
    "category",
    "complaint_terms",
]
BUSINESS_FIELDS = [
    "business_id",
    "business_name",
    "city",
    "state",
    "price_range",
    "latitude",
    "longitude",
    "is_open",
]
USER_FIELDS = ["user_id", "review_count", "average_stars", "fans", "yelping_since"]
DATE_FIELDS = ["date", "year", "month_number", "month_name", "quarter", "day_of_week"]
COMPLAINT_WORDS = ("bad", "slow", "cold", "rude", "dirty")
TOKEN_PATTERN = re.compile(r"[a-z']{3,}")
STOP_WORDS = {
    "about", "after", "again", "all", "also", "and", "are", "because", "been",
    "before", "but", "can", "could", "did", "does", "dont", "even", "for",
    "from", "get", "got", "had", "has", "have", "her", "here", "him", "his",
    "how", "i've", "into", "it's", "its", "just", "like", "lol", "more", "only",
    "other", "our", "out", "over", "place", "really", "she", "should", "some",
    "that", "the", "their", "them", "then", "there", "they", "this", "time",
    "too", "very", "was", "well", "were", "what", "when", "which", "who",
    "will", "with", "would", "you", "your", "one",
}


def export_dimension(source: Path, destination: Path, fields: list[str]) -> None:
    with source.open("r", encoding="utf-8-sig", newline="") as input_file:
        reader = csv.DictReader(input_file)
        missing = set(fields) - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"{source.name} no contiene las columnas: {sorted(missing)}")
        with destination.open("w", encoding="utf-8-sig", newline="") as output_file:
            writer = csv.DictWriter(output_file, fieldnames=fields)
            writer.writeheader()
            writer.writerows({field: row[field] for field in fields} for row in reader)


def export_user_dimension(source: Path, destination: Path, fields: list[str]) -> None:
    users = pd.read_csv(source, usecols=fields)
    review_counts = pd.to_numeric(users["review_count"], errors="raise")
    if review_counts.empty or review_counts.isna().any():
        raise ValueError("dim_user.csv contiene review_count vacío o inválido.")

    q1, q2, q3 = (
        int(value) for value in review_counts.quantile([0.25, 0.5, 0.75])
    )
    if not q1 < q2 < q3:
        raise ValueError(
            "No se pueden formar cuatro segmentos distintos con los cuartiles de review_count."
        )

    labels = [
        f"Q1 (0-{q1} reseñas)",
        f"Q2 ({q1 + 1}-{q2} reseñas)",
        f"Q3 ({q2 + 1}-{q3} reseñas)",
        f"Q4 (más de {q3} reseñas)",
    ]
    users["activity_segment"] = pd.cut(
        review_counts,
        bins=[-float("inf"), q1, q2, q3, float("inf")],
        labels=labels,
        include_lowest=True,
    )
    if users["activity_segment"].isna().any():
        raise ValueError("No se pudo asignar un segmento a todos los usuarios.")

    users.to_csv(destination, index=False, encoding="utf-8-sig")
    print(
        "Segmentos de actividad por reseñas: "
        f"Q1 <= {q1}; Q2 <= {q2}; Q3 <= {q3}; Q4 > {q3}.",
        flush=True,
    )


def prepare(source_dir: Path, output_dir: Path, top_words: int) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    review_source = source_dir / "fact_review.csv"
    if not review_source.is_file():
        raise FileNotFoundError(f"No se encontró {review_source}")

    review_tmp = output_dir / "fact_review.csv.tmp"
    counts_tmp = output_dir / "review_keyword_counts.csv.tmp"
    complaint_counts_tmp = output_dir / "complaint_term_counts.csv.tmp"
    frequencies: Counter[str] = Counter()
    complaint_frequencies: Counter[str] = Counter()
    processed = 0

    with review_source.open("r", encoding="utf-8-sig", newline="") as input_file:
        reader = csv.DictReader(input_file)
        required = set(FIELDS) - {"complaint_terms"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"fact_review.csv no contiene las columnas: {sorted(missing)}")

        with review_tmp.open("w", encoding="utf-8-sig", newline="") as output_file:
            writer = csv.DictWriter(output_file, fieldnames=FIELDS)
            writer.writeheader()
            for row in reader:
                text = row.get("review_text") or ""
                tokens = TOKEN_PATTERN.findall(text.casefold())
                token_set = set(tokens)
                complaint_terms = [
                    word for word in COMPLAINT_WORDS if word in token_set
                ]
                complaint_frequencies.update(complaint_terms)
                writer.writerow(
                    {
                        field: row.get(field, "")
                        for field in FIELDS
                        if field != "complaint_terms"
                    }
                    | {"complaint_terms": ", ".join(complaint_terms)}
                )
                frequencies.update(
                    token for token in tokens if token not in STOP_WORDS
                )
                processed += 1
                if processed % 250000 == 0:
                    print(f"Reseñas preparadas para Power BI: {processed:,}", flush=True)

    with counts_tmp.open("w", encoding="utf-8-sig", newline="") as output_file:
        writer = csv.writer(output_file)
        writer.writerow(["keyword", "frequency"])
        writer.writerows(frequencies.most_common(top_words))

    with complaint_counts_tmp.open("w", encoding="utf-8-sig", newline="") as output_file:
        writer = csv.writer(output_file)
        writer.writerow(["complaint_term", "frequency"])
        writer.writerows(
            (term, complaint_frequencies[term]) for term in COMPLAINT_WORDS
        )

    dimensions = [
        ("dim_business.csv", BUSINESS_FIELDS),
        ("dim_user.csv", USER_FIELDS),
        ("dim_date.csv", DATE_FIELDS),
    ]
    for filename, fields in dimensions:
        source = source_dir / filename
        if not source.is_file():
            raise FileNotFoundError(f"No se encontró {source}")
        destination = output_dir / f"{filename}.tmp"
        if filename == "dim_user.csv":
            export_user_dimension(source, destination, fields)
        else:
            export_dimension(source, destination, fields)

    os.replace(review_tmp, output_dir / "fact_review.csv")
    os.replace(counts_tmp, output_dir / "review_keyword_counts.csv")
    os.replace(complaint_counts_tmp, output_dir / "complaint_term_counts.csv")
    for filename, _ in dimensions:
        os.replace(output_dir / f"{filename}.tmp", output_dir / filename)
    print(f"Preparación terminada: {processed:,} reseñas.", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Prepara tablas compactas para el modelo de Power BI."
    )
    parser.add_argument("--source-dir", type=Path, default=Path("data/processed"))
    parser.add_argument("--output-dir", type=Path, default=Path("data/processed/powerbi"))
    parser.add_argument("--top-words", type=int, default=100)
    args = parser.parse_args()
    prepare(args.source_dir, args.output_dir, args.top_words)


if __name__ == "__main__":
    main()
