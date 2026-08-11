"""Train the phishing URL classifier and write a servable artifact.

This is an OFFLINE step. It is deliberately not part of the container image and
never runs at request time — the image ships the fitted artifact, not the data
or the training code.

Three corrections over the original notebook, all of which were inflating the
reported scores:

1. The vectorizer is fitted inside a Pipeline, so it only ever sees the training
   split. Fitting a scaler or vectorizer on the full dataset before splitting
   leaks test-set statistics into training.
2. Duplicate URLs are dropped before the split. The raw dataset has ~7.7%
   duplicates; if the same URL lands in both splits, the model is being scored
   on rows it memorized.
3. The split is stratified and seeded, so the run is reproducible and both
   splits carry the same class balance.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import joblib
import pandas as pd
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

# URLs are not prose. Splitting on runs of alphanumerics turns
# "paypal.co.uk/cgi-bin/webscr" into [paypal, co, uk, cgi, bin, webscr], which is
# where the signal lives — brand names in the path, suspicious TLDs, hex blobs.
TOKEN_PATTERN = r"[A-Za-z0-9]+"


def load_dataset(csv_path: Path) -> pd.DataFrame:
    df = pd.read_csv(csv_path, on_bad_lines="skip", engine="python")
    df = df.dropna(subset=["URL", "Label"])
    df["Label"] = df["Label"].astype(str).str.strip().str.lower()
    df = df[df["Label"].isin(["good", "bad"])]

    before = len(df)
    df = df.drop_duplicates(subset=["URL"]).reset_index(drop=True)
    dropped = before - len(df)
    print(f"rows after cleaning : {before}")
    print(f"duplicate URLs dropped: {dropped} ({dropped / before * 100:.1f}%)")
    return df


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

    df = load_dataset(args.csv)
    X = df["URL"]
    y = (df["Label"] == "bad").astype(int)

    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=0.2, random_state=SEED, stratify=y
    )
    print(f"train / test        : {len(X_train)} / {len(X_test)}")

    pipeline = build_pipeline(args.max_features, args.min_df, args.alpha)
    pipeline.fit(X_train, y_train)

    probabilities = pipeline.predict_proba(X_test)[:, 1]
    predictions = (probabilities >= 0.5).astype(int)

    # Always-predict-the-majority-class. Any model that cannot beat this by a
    # clear margin is not doing useful work, however good its accuracy looks.
    baseline = 1.0 - y_test.mean()

    metrics = {
        "seed": SEED,
        "n_train": int(len(X_train)),
        "n_test": int(len(X_test)),
        "vocabulary_size": int(len(pipeline.named_steps["tfidf"].vocabulary_)),
        "majority_class_baseline_accuracy": round(float(baseline), 4),
        "accuracy": round(float(accuracy_score(y_test, predictions)), 4),
        "precision": round(float(precision_score(y_test, predictions)), 4),
        "recall": round(float(recall_score(y_test, predictions)), 4),
        "f1": round(float(f1_score(y_test, predictions)), 4),
        "roc_auc": round(float(roc_auc_score(y_test, probabilities)), 4),
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
    print(f"\nconfusion matrix: TN={tn}  FP={fp}  FN={fn}  TP={tp}")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(pipeline, args.out, compress=3)
    size_mb = args.out.stat().st_size / 1_048_576
    print(f"\nartifact written    : {args.out} ({size_mb:.1f} MB)")

    if args.metrics:
        args.metrics.parent.mkdir(parents=True, exist_ok=True)
        args.metrics.write_text(json.dumps(metrics, indent=2) + "\n")
        print(f"metrics written     : {args.metrics}")


if __name__ == "__main__":
    main()
