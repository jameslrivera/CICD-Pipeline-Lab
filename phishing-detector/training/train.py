"""Train the phishing URL classifier and write a servable artifact.

This is an OFFLINE step. It is deliberately not part of the container image and
never runs at request time — the image ships the fitted artifact, not the data
or the training code.

Corrections over the original notebook, all of which were inflating the reported
scores:

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

# URLs are not prose. Splitting on runs of alphanumerics turns
# "paypal.co.uk/cgi-bin/webscr" into [paypal, co, uk, cgi, bin, webscr], which is
# where the signal lives — brand names in the path, suspicious TLDs, hex blobs.
#
# Known limitation: this is ASCII-only, so internationalized domains are stripped
# to almost nothing and score as phishing. See README "Known limitations".
TOKEN_PATTERN = r"[A-Za-z0-9]+"

# If fewer than this fraction of the file's rows survive parsing and cleaning,
# something is wrong with the dataset and training should stop rather than
# quietly fit on whatever is left.
MIN_SURVIVAL_FRACTION = 0.90


def load_dataset(csv_path: Path) -> pd.DataFrame:
    # Count physical lines first, so we can tell how much the parser discarded.
    # Without a baseline there is no way to notice that a corrupt file silently
    # became a much smaller dataset.
    with csv_path.open(encoding="utf-8", errors="replace") as handle:
        physical_rows = max(sum(1 for _ in handle) - 1, 0)
    if physical_rows == 0:
        raise SystemExit(f"{csv_path} contains no data rows")

    # index_col=False matters far more than it looks. Without it, a row with
    # MORE fields than the header makes pandas promote the leading columns into
    # the DataFrame index and shift every value left — so URL and Label keep
    # their names but silently hold the trailing junk instead. The rows are not
    # skipped, they are corrupted, and on_bad_lines does not save you.
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

    # Drop exact duplicate rows first. Anything still duplicated on URL alone is
    # the same URL carrying CONFLICTING labels, and keeping whichever pandas saw
    # first is a coin flip on ground truth — worth reporting, not hiding.
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
    """Confirm the artifact just written deserializes and predicts identically.

    The pinned scikit-learn version exists because this artifact is a pickle. A
    pickle that dumps cleanly but reloads wrong should be caught here, at build
    time, rather than as a 503 in production.
    """
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

    # Always-predict-the-majority-class. Any model that cannot beat this by a
    # clear margin is not doing useful work, however good its accuracy looks.
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
        # Recorded because the artifact is a pickle: deserializing under a
        # different scikit-learn build warns at best and breaks at worst, and
        # without this there is no way to tell what produced the file.
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
