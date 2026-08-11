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

import os
from pathlib import Path

import joblib
import yaml
from fastapi import FastAPI, HTTPException, Query

_PACKAGE_ROOT = Path(__file__).resolve().parent.parent
MODEL_PATH = Path(os.environ.get("MODEL_PATH", _PACKAGE_ROOT / "model" / "phishing_nb.joblib"))
CONFIG_PATH = Path(os.environ.get("DETECTOR_CONFIG", _PACKAGE_ROOT / "config" / "detector.yaml"))

# A URL long enough to exceed this is not a URL anyone is checking in good
# faith; refusing it early keeps a huge string out of the vectorizer.
MAX_URL_LENGTH = 2048

app = FastAPI(title="phishing-detector", version="0.1.0")


class ConfigError(RuntimeError):
    """The threshold config is missing, unparseable, or out of range."""


def _load_model() -> tuple[object | None, str | None]:
    """Load the artifact at import. Failure is recorded, not raised — a missing
    model must surface as 'not ready', never as a process that refuses to start."""
    try:
        return joblib.load(MODEL_PATH), None
    except Exception as exc:  # noqa: BLE001 - any failure here means "not ready"
        return None, f"cannot load model from {MODEL_PATH}: {exc}"


MODEL, MODEL_ERROR = _load_model()


def load_threshold() -> float:
    """Read the decision threshold. Called per request so a ConfigMap edit takes
    effect without a restart."""
    try:
        raw = yaml.safe_load(CONFIG_PATH.read_text())
    except OSError as exc:
        raise ConfigError(f"cannot read detector config at {CONFIG_PATH}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"detector config at {CONFIG_PATH} is not valid YAML") from exc

    if not isinstance(raw, dict) or "threshold" not in raw:
        raise ConfigError("detector config must contain a 'threshold' key")

    try:
        threshold = float(raw["threshold"])
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"threshold must be a number, got {raw['threshold']!r}") from exc

    if not 0.0 <= threshold <= 1.0:
        raise ConfigError(f"threshold must be between 0 and 1, got {threshold}")
    return threshold


def _require_model() -> object:
    if MODEL is None:
        raise HTTPException(status_code=503, detail=MODEL_ERROR or "model not loaded")
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
    """Readiness: the model deserialized and the threshold is valid. A 503 here
    pulls the pod out of the Service endpoints without killing it."""
    _require_model()
    threshold = _require_threshold()
    return {"status": "ready", "threshold": threshold}


@app.get("/info")
def info() -> dict:
    """What this instance is actually running — useful for confirming a
    ConfigMap change landed, and which model version served a verdict."""
    model = _require_model()
    vectorizer = model.named_steps["tfidf"]
    return {
        "model_path": str(MODEL_PATH),
        "classifier": type(model.named_steps["nb"]).__name__,
        "vocabulary_size": len(vectorizer.vocabulary_),
        "threshold": _require_threshold(),
    }


@app.get("/predict")
def predict(url: str = Query(..., description="URL to score")) -> dict:
    """Score one URL. `probability` is the model's raw output and does not move;
    `phishing` is that probability compared against the current threshold."""
    candidate = url.strip()
    if not candidate:
        raise HTTPException(status_code=400, detail="url must not be empty")
    if len(candidate) > MAX_URL_LENGTH:
        raise HTTPException(status_code=400, detail=f"url exceeds {MAX_URL_LENGTH} characters")

    model = _require_model()
    threshold = _require_threshold()

    probability = float(model.predict_proba([candidate])[0][1])
    return {
        "url": candidate,
        "phishing": probability >= threshold,
        "probability": round(probability, 4),
        "threshold": threshold,
    }
