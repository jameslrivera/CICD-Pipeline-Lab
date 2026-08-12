"""phishing-detector — score a URL for phishing using a pre-trained classifier.

Two pieces of state, loaded very differently on purpose.

The MODEL is an immutable artifact baked into the image and loaded once at
startup. It is large-ish, expensive to deserialize, and changing it is a real
release: new image, new scan, new signature.

The THRESHOLD is mutable config read from disk on every request. It decides where
the cut between "phishing" and "legitimate" falls, and it is exactly the kind of
knob an analyst needs to turn at 2am when false positives are drowning the queue.
Reading it per request means a Kubernetes ConfigMap can retune detection
sensitivity without redeploying the model.

That split — immutable artifact, mutable policy — is the whole design.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import joblib
import yaml
from fastapi import FastAPI, HTTPException, Query, Request

logger = logging.getLogger("phishing_detector")

_PACKAGE_ROOT = Path(__file__).resolve().parent.parent
MODEL_PATH = Path(os.environ.get("MODEL_PATH", _PACKAGE_ROOT / "model" / "phishing_nb.joblib"))
CONFIG_PATH = Path(os.environ.get("DETECTOR_CONFIG", _PACKAGE_ROOT / "config" / "detector.yaml"))

# A URL long enough to exceed this is not a URL anyone is checking in good
# faith; refusing it early keeps a huge string out of the vectorizer.
MAX_URL_LENGTH = 2048
PROBABILITY_DIGITS = 4

# Scored by /readyz to prove the artifact is usable, not merely loaded.
_CANARY_URL = "readyz-probe.invalid/canary"

app = FastAPI(title="phishing-detector", version="0.1.0")


class ConfigError(RuntimeError):
    """The threshold config is missing, unparseable, or out of range."""


def _load_model() -> tuple[object | None, str | None]:
    """Load the artifact at import. Failure is recorded, not raised — a missing
    model must surface as 'not ready', never as a process that refuses to start.

    The underlying error goes to the log. Callers get a generic message, because
    these endpoints are unauthenticated and the raw exception would disclose
    filesystem layout and library internals.
    """
    try:
        return joblib.load(MODEL_PATH), None
    except Exception as exc:  # noqa: BLE001 - any failure here means "not ready"
        logger.error("model load failed from %s: %s", MODEL_PATH, exc)
        return None, "model artifact unavailable"


MODEL, MODEL_ERROR = _load_model()


def load_threshold() -> float:
    """Read the decision threshold. Called per request so a ConfigMap edit takes
    effect without a restart."""
    try:
        raw = yaml.safe_load(CONFIG_PATH.read_text())
    except OSError as exc:
        logger.error("cannot read detector config at %s: %s", CONFIG_PATH, exc)
        raise ConfigError("detector configuration unavailable") from exc
    except yaml.YAMLError as exc:
        logger.error("detector config at %s is not valid YAML: %s", CONFIG_PATH, exc)
        raise ConfigError("detector configuration is not valid YAML") from exc

    if not isinstance(raw, dict) or "threshold" not in raw:
        raise ConfigError("detector configuration must contain a 'threshold' key")

    value = raw["threshold"]

    # YAML resolves `yes`, `on`, and `true` to booleans, and float(True) is 1.0.
    # A threshold of 1.0 flags almost nothing, so a one-word typo in a ConfigMap
    # would silently disable detection while /readyz still reported healthy.
    # Failing open is the worst possible failure for a detector, so booleans are
    # rejected rather than coerced.
    if isinstance(value, bool):
        raise ConfigError("threshold must be a number, not a boolean")

    try:
        threshold = float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError("threshold must be a number") from exc

    # Also rejects NaN, since no comparison against NaN is ever true.
    if not 0.0 <= threshold <= 1.0:
        raise ConfigError("threshold must be between 0 and 1")
    return threshold


def _score(model: object, url: str) -> float:
    """Return the positive-class probability for one URL."""
    probabilities = model.predict_proba([url])[0]
    # A model fitted on a single class returns one column, and indexing [1]
    # would raise. Catching it here turns a 500 into a readiness failure.
    if len(probabilities) < 2:
        raise ValueError("model does not expose a positive-class probability")
    return float(probabilities[1])


def _require_model() -> object:
    if MODEL is None:
        raise HTTPException(status_code=503, detail=MODEL_ERROR or "model unavailable")
    return MODEL


def _require_threshold() -> float:
    try:
        return load_threshold()
    except ConfigError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/healthz")
def healthz() -> dict:
    """Liveness: the process is serving. Says nothing about the model or config,
    so a bad artifact cannot trigger a cluster-wide restart loop."""
    return {"status": "ok"}


@app.get("/readyz")
def readyz() -> dict:
    """Readiness: the model is usable and the threshold is valid. A 503 here
    pulls the pod out of the Service endpoints without killing it."""
    model = _require_model()
    threshold = _require_threshold()

    # Scoring one canary URL, rather than just checking the object is not None.
    # A wrong artifact — a bare estimator instead of a Pipeline, renamed steps,
    # a single-class model — deserializes perfectly and then fails on every real
    # request. Without this, Kubernetes would route traffic to a pod that 500s.
    try:
        _score(model, _CANARY_URL)
    except Exception as exc:  # noqa: BLE001 - any failure means "not ready"
        logger.error("model canary scoring failed: %s", exc)
        raise HTTPException(status_code=503, detail="model is not usable") from exc

    return {"status": "ready", "threshold": threshold}


@app.get("/info")
def info() -> dict:
    """What this instance is running — useful for confirming a ConfigMap change
    landed and which model served a verdict.

    Reports the artifact's filename rather than its absolute path: the path
    discloses container layout to an unauthenticated caller and identifies
    nothing the filename does not.
    """
    model = _require_model()
    try:
        classifier = type(model.named_steps["nb"]).__name__
        vocabulary_size = len(model.named_steps["tfidf"].vocabulary_)
    except (AttributeError, KeyError, TypeError) as exc:
        logger.error("model does not have the expected pipeline shape: %s", exc)
        raise HTTPException(status_code=503, detail="model is not usable") from exc

    return {
        "model": MODEL_PATH.name,
        "classifier": classifier,
        "vocabulary_size": vocabulary_size,
        "threshold": _require_threshold(),
    }


@app.get("/predict")
def predict(request: Request, url: str = Query(..., description="URL to score")) -> dict:
    """Score one URL against the current threshold."""
    # Starlette keeps the LAST value when a query parameter repeats. A proxy,
    # WAF, or access log that reads the first one would then record a different
    # URL than the service actually scored — for a tool whose whole job is
    # inspecting suspicious URLs, that is a forensic problem. Reject rather
    # than silently pick one.
    if len(request.query_params.getlist("url")) > 1:
        raise HTTPException(status_code=400, detail="url must be supplied exactly once")

    candidate = url.strip()
    if not candidate:
        raise HTTPException(status_code=400, detail="url must not be empty")
    if len(candidate) > MAX_URL_LENGTH:
        raise HTTPException(status_code=400, detail=f"url exceeds {MAX_URL_LENGTH} characters")

    model = _require_model()
    threshold = _require_threshold()

    try:
        raw_probability = _score(model, candidate)
    except Exception as exc:  # noqa: BLE001 - surfaces as unavailable, not 500
        logger.error("scoring failed: %s", exc)
        raise HTTPException(status_code=503, detail="model is not usable") from exc

    # Round first, then compare. Comparing the full-precision value while
    # reporting the rounded one produces responses that contradict themselves —
    # probability 0.5 alongside threshold 0.5 and a verdict of false — which is
    # indefensible when the output lands in a SOC ticket.
    probability = round(raw_probability, PROBABILITY_DIGITS)
    return {
        "url": candidate,
        "phishing": probability >= threshold,
        "probability": probability,
        "threshold": threshold,
    }
