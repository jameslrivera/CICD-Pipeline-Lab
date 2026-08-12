"""Train the classifier and write the artifact."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
import scipy
import sklearn
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics import (
    accuracy_score,
    confusion_matrix,
    f1_score,
    precision_score,
    recall_score,
    roc_auc_score,
)
from sklearn.model_selection import train_test_split
from sklearn.naive_bayes import MultinomialNB
from sklearn.pipeline import Pipeline

SEED = 42

# url tokens
TOKEN_PATTERN = r"[A-Za-z0-9]+"

# minimum usable rows
MIN_SURVIVAL_FRACTION = 0.90


def load_dataset(csv_path: Path) -> pd.DataFrame:
    # baseline row count
    with csv_path.open(encoding="utf-8", errors="replace") as handle:
        physical_rows = max(sum(1 for _ in handle) - 1, 0)
    if physical_rows == 0:
        raise SystemExit(f"{csv_path} contains no data rows")

    # index_col=False prevents shifting
    frame = pd.read_csv(
        csv_path,
        index_col=False,
        dtype=str,
        keep_default_na=False,
        on_bad_lines="skip",
        engine="python",
    )

    missing = {"URL", "Label"} - set(frame.columns)
    if missing:
        raise SystemExit(f"{csv_path} is missing required column(s): {sorted(missing)}")

    frame = frame[frame["URL"].str.strip() != ""]
    frame["Label"] = frame["Label"].str.strip().str.lower()

    unexpected = set(frame["Label"].unique()) - {"good", "bad"}
    frame = frame[frame["Label"].isin(["good", "bad"])]
    usable = len(frame)

    # drop duplicates
    frame = frame.drop_duplicates(subset=["URL", "Label"])
    conflicting = int(frame.duplicated(subset=["URL"], keep=False).sum())
    frame = frame.drop_duplicates(subset=["URL"]).reset_index(drop=True)

    survival = usable / physical_rows
    print(f"physical rows in file : {physical_rows}")
    print(f"parsed and labelled   : {usable} ({survival:.1%})")
    if unexpected:
        print(f"unexpected labels     : {sorted(unexpected)[:5]}")
    print(f"duplicate URLs dropped: {usable - len(frame)}")
    if conflicting:
        print(f"URLs with conflicting labels: {conflicting} (resolved arbitrarily)")
    print(f"training rows         : {len(frame)}")

    if survival < MIN_SURVIVAL_FRACTION:
        raise SystemExit(
            f"only {usable} of {physical_rows} rows survived parsing ({survival:.1%}); "
            f"refusing to train on a dataset this damaged"
        )
    return frame


def build_pipeline(max_features: int, min_df: int, alpha: float) -> Pipeline:
    return Pipeline(
        [
            (
                "tfidf",
                TfidfVectorizer(
                    lowercase=True,
                    token_pattern=TOKEN_PATTERN,
                    min_df=min_df,
                    max_features=max_features,
                    sublinear_tf=True,
                ),
            ),
            ("nb", MultinomialNB(alpha=alpha)),
        ]
    )


def verify_round_trip(pipeline: Pipeline, path: Path, sample: list[str]) -> None:
    """Verify the artifact reloads identically."""
    reloaded = joblib.load(path)
    before = pipeline.predict_proba(sample)[:, 1]
    after = reloaded.predict_proba(sample)[:, 1]
    if not np.allclose(before, after):
        raise SystemExit("round-trip verification failed: reloaded artifact disagrees")
    print(f"round-trip verified   : {len(sample)} samples identical after reload")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path, required=True, help="path to phishing_site_urls.csv")
    parser.add_argument(
        "--out", type=Path, required=True, help="path to write the .joblib artifact"
    )
    parser.add_argument("--metrics", type=Path, help="optional path to write metrics JSON")
    parser.add_argument("--max-features", type=int, default=100_000)
    parser.add_argument("--min-df", type=int, default=3)
    parser.add_argument("--alpha", type=float, default=0.1)
    args = parser.parse_args()

    frame = load_dataset(args.csv)
    features = frame["URL"]
    labels = (frame["Label"] == "bad").astype(int)

    x_train, x_test, y_train, y_test = train_test_split(
        features, labels, test_size=0.2, random_state=SEED, stratify=labels
    )
    print(f"train / test          : {len(x_train)} / {len(x_test)}")

    pipeline = build_pipeline(args.max_features, args.min_df, args.alpha)
    pipeline.fit(x_train, y_train)

    probabilities = pipeline.predict_proba(x_test)[:, 1]
    predictions = (probabilities >= 0.5).astype(int)

    # majority-class baseline
    baseline = 1.0 - y_test.mean()

    metrics = {
        "seed": SEED,
        "n_train": int(len(x_train)),
        "n_test": int(len(x_test)),
        "vocabulary_size": int(len(pipeline.named_steps["tfidf"].vocabulary_)),
        "majority_class_baseline_accuracy": round(float(baseline), 4),
        "accuracy": round(float(accuracy_score(y_test, predictions)), 4),
        "precision": round(float(precision_score(y_test, predictions)), 4),
        "recall": round(float(recall_score(y_test, predictions)), 4),
        "f1": round(float(f1_score(y_test, predictions)), 4),
        "roc_auc": round(float(roc_auc_score(y_test, probabilities)), 4),
        # provenance
        "versions": {
            "python": sys.version.split()[0],
            "scikit_learn": sklearn.__version__,
            "numpy": np.__version__,
            "scipy": scipy.__version__,
            "joblib": joblib.__version__,
        },
    }

    tn, fp, fn, tp = confusion_matrix(y_test, predictions).ravel()
    metrics["confusion_matrix"] = {
        "true_negatives": int(tn),
        "false_positives": int(fp),
        "false_negatives": int(fn),
        "true_positives": int(tp),
    }

    print("\n=== held-out test metrics (threshold 0.5) ===")
    for key in (
        "majority_class_baseline_accuracy",
        "accuracy",
        "precision",
        "recall",
        "f1",
        "roc_auc",
    ):
        print(f"{key:38s}: {metrics[key]}")
    print(f"{'vocabulary_size':38s}: {metrics['vocabulary_size']}")
    print(f"\nconfusion matrix: TN={tn}  FP={fp}  FN={fn}  TP={tp}\n")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(pipeline, args.out, compress=3)
    verify_round_trip(pipeline, args.out, list(x_test.iloc[:200]))

    size_mb = args.out.stat().st_size / 1_048_576
    print(f"artifact written      : {args.out} ({size_mb:.1f} MB)")

    if args.metrics:
        args.metrics.parent.mkdir(parents=True, exist_ok=True)
        args.metrics.write_text(json.dumps(metrics, indent=2) + "\n")
        print(f"metrics written       : {args.metrics}")


if __name__ == "__main__":
    main()
