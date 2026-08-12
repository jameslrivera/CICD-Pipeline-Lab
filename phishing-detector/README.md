# phishing-detector

A FastAPI service that scores a URL for phishing using a tokenized Naive Bayes
classifier trained on ~507k labeled URLs.

## The design decision the rest of the lab depends on

The service holds two pieces of state and loads them in deliberately different
ways.

The **model** is an immutable artifact baked into the image and deserialized once
at startup. Changing it is a real release — new image, new scan, new signature.

The **threshold** is mutable policy read from disk on *every request*. It decides
where the cut between "phishing" and "legitimate" falls. Reading it per request
means a Kubernetes ConfigMap can retune detection sensitivity without redeploying
the model.

That split is the point: immutable artifact, mutable policy. It maps onto a real
operational need — an analyst drowning in false positives at 2am needs to move
the threshold, not ship a new model.

## API

| Method | Path | Purpose |
| ------ | ---- | ------- |
| GET | `/healthz` | Liveness — the process is up |
| GET | `/readyz` | Readiness — model deserialized and threshold valid; 503 if not |
| GET | `/info` | Model path, classifier type, vocabulary size, current threshold |
| GET | `/predict?url=<url>` | Score one URL; 400 on empty or oversized input |

`probability` is the model's raw output and does not move. `phishing` is that
probability compared against the current threshold.

```json
{
  "url": "paypal.co.uk.secure-login.verify-account.tk/cgi-bin/webscr?cmd=_login",
  "phishing": true,
  "probability": 1.0,
  "threshold": 0.5
}
```

### A note on putting URLs in query strings

`/predict` takes the URL as a query parameter, which is convenient for `curl` but
means the URL under investigation lands in every access log, proxy log, and
browser history along the path. For a tool that inspects potentially malicious
URLs, a POST body would be the better choice in production. It is a GET here for
demo ergonomics, and this is a deliberate, known tradeoff rather than an
oversight.

## Model

Trained with `training/train.py`. `TfidfVectorizer` splits each URL on runs of
alphanumerics — `paypal.co.uk/cgi-bin/webscr` becomes
`[paypal, co, uk, cgi, bin, webscr]` — feeding `MultinomialNB`. The signal lives
in those substrings: brand names appearing in paths, unusual TLDs, long hex
blobs.

Held-out test set, threshold 0.5, 101,439 URLs:

| Metric | Value |
| ------ | ----- |
| Majority-class baseline accuracy | 0.7746 |
| Accuracy | 0.9648 |
| Precision | 0.9617 |
| Recall | 0.8786 |
| F1 | 0.9183 |
| ROC AUC | 0.9898 |

Confusion matrix: TN=77,780 FP=799 FN=2,776 TP=20,084.

The baseline row matters. Always guessing "legitimate" scores 77.5% accuracy on
this class balance, so accuracy alone is close to meaningless — the model is
beating the baseline by 19 points, not by 96.

### Corrections made to the original notebook

The earlier version of this model used eight hand-crafted features and reported
F1 of 0.342 — barely above the do-nothing baseline. Three things were inflating
its numbers, and all three are fixed here:

**Preprocessing was fitted before the split.** `StandardScaler` was fitted on the
full dataset, so test-set statistics leaked into training. The vectorizer now
lives inside a `Pipeline`, which by construction only ever fits on the training
split.

**Duplicate URLs were never removed.** The raw dataset is 7.7% duplicates
(42,151 of 549,346). Without deduplication the same URL can land in both splits,
so the model gets scored on rows it memorized. They are dropped before the split.

**Two features could not be computed at inference time.** `feat1` was a group
mean of URL length by domain, computed across the entire dataset, and `feat2`
derived from it. At serving time there is no group to average over — that is
training/serving skew, and it made those features both leaky and unservable.
Tokenization replaced them entirely.

The split is also stratified and seeded now, so a rerun reproduces the artifact
exactly.

## Known limitations

Found by probing the deployed service rather than by reading the code. Both come
from the same root cause: the tokenizer is `[A-Za-z0-9]+`, so the model can only
see ASCII alphanumerics, and it scores on however many tokens it gets.

**Non-ASCII domains are systematically misclassified as phishing.** Every
character outside `[A-Za-z0-9]` is discarded before the model sees anything, so
an internationalized domain is stripped to almost nothing and scored on the
remains:

| URL | Probability | Verdict at 0.30 |
| --- | ----------- | --------------- |
| `пример.рф/login` | 0.9934 | phishing |
| `münchen.de/willkommen` | 0.4984 | phishing |
| `日本.jp/index.html` | 0.3833 | phishing |

`пример.рф/login` tokenizes to roughly `[login]` — the entire domain vanishes.
This matters more than a typical accuracy gap, because IDN homograph attacks are
themselves a phishing technique, so the model is blind in a phishing-relevant
area. The fix is to punycode-normalize (`idna.encode`) before tokenizing, so
`münchen.de` becomes `xn--mnchen-3ya.de` and survives as tokens.

**Short URLs score high because there is little evidence either way.** More
tokens means more signal, and the score drops sharply once a path is present:

| URL | Probability |
| --- | ----------- |
| `google.com` | 0.3861 |
| `google.com/search?q=weather` | 0.0036 |
| `http://` | 0.9812 |

A bare `http://` reduces to the single token `http` and scores 0.98. Anything
operational would want a minimum-token guard that returns "insufficient signal"
rather than a confident verdict.

Neither is fixed here. This phase is about the pipeline, and both are model
work — but shipping a detector without knowing where it fails is worse than the
failures themselves.

## Retraining

Training is an offline step. Neither the training code nor the dataset ships in
the image — a dependency that only training needs has no business in something
that only has to answer `/predict`, which is why `pandas` is in
`requirements-train.txt` and not `requirements.txt`.

The dataset is not committed; it is ~30MB of Kaggle-sourced data. Point `--csv`
at your local copy.

```bash
.venv/bin/python training/train.py --csv /path/to/phishing_site_urls.csv --out model/phishing_nb.joblib --metrics model/metrics.json
```

## Development

```bash
python3.12 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
```

```bash
.venv/bin/python -m pytest -q && .venv/bin/ruff check .
```

Most tests use a stub model with a fixed probability, so the API's behavior —
thresholding, validation, readiness — is testable independently of what the
classifier happens to think about any given URL. One integration test exercises
the real committed artifact.

## Container notes

Multi-stage build; the runtime stage receives only the finished virtualenv. Runs
as a fixed numeric UID 10001, because Kubernetes `runAsNonRoot` cannot resolve a
username against `/etc/passwd` before starting the container — give it a name
instead of a number and the pod fails with `image has non-numeric user`.

`pip` is deleted from both the venv and the base image's `/usr/local` copy.
Removing only one leaves `pip install` working inside a running container, and a
package manager sitting next to an attacker who already has a foothold is an
install tool.

Thread pools are pinned to 1 via `OMP_NUM_THREADS` and friends. scikit-learn and
numpy size their pools from the *host* CPU count, which ignores the container's
CPU limit — left alone they oversubscribe and get throttled by the cgroup once
Phase 3 sets limits.

The image is 594MB, up from 237MB for the previous three-package app.
scikit-learn, scipy and numpy are most of that. This is worth knowing rather than
hiding: it makes image scanning and SBOM generation in Phase 6 meaningful instead
of a formality, and it is a real optimization target.

### The pinned scikit-learn version is load-bearing

The model artifact is a pickle of fitted scikit-learn objects. Deserializing it
under a different scikit-learn version warns at best and breaks at worst, so the
pin in `requirements.txt` must match the version that produced the artifact.

Relatedly: `joblib.load()` executes arbitrary code during unpickling. Loading an
untrusted model file is equivalent to running an untrusted binary. Here the
artifact is built from this repo's own training script and travels inside the
signed image, but that property is worth protecting deliberately — signing the
model with Cosign is on the Phase 6 list for exactly this reason.
