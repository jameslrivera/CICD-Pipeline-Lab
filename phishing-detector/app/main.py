"""Phishing URL scoring service."""

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

MAX_URL_LENGTH = 2048
PROBABILITY_DIGITS = 4
_CANARY_URL = "readyz-probe.invalid/canary"

app = FastAPI(title="phishing-detector", version="0.1.0")


class ConfigError(RuntimeError):
    """Invalid threshold config."""


def _load_model() -> tuple[object | None, str | None]:
    """Load the model artifact at import."""
    try:
        return joblib.load(MODEL_PATH), None
    except Exception as exc:  # noqa: BLE001
        logger.error("model load failed from %s: %s", MODEL_PATH, exc)
        return None, "model artifact unavailable"


MODEL, MODEL_ERROR = _load_model()


def load_threshold() -> float:
    """Read the decision threshold from disk."""
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

    # reject yaml booleans
    if isinstance(value, bool):
        raise ConfigError("threshold must be a number, not a boolean")

    try:
        threshold = float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError("threshold must be a number") from exc

    # also rejects nan
    if not 0.0 <= threshold <= 1.0:
        raise ConfigError("threshold must be between 0 and 1")
    return threshold


def _score(model: object, url: str) -> float:
    """Positive-class probability for one URL."""
    probabilities = model.predict_proba([url])[0]
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
    """Liveness. Never checks the model."""
    return {"status": "ok"}


@app.get("/readyz")
def readyz() -> dict:
    """Readiness. Model must be usable."""
    model = _require_model()
    threshold = _require_threshold()

    # canary score
    try:
        _score(model, _CANARY_URL)
    except Exception as exc:  # noqa: BLE001
        logger.error("model canary scoring failed: %s", exc)
        raise HTTPException(status_code=503, detail="model is not usable") from exc

    return {"status": "ready", "threshold": threshold}


@app.get("/info")
def info() -> dict:
    """Running model and threshold."""
    model = _require_model()
    try:
        classifier = type(model.named_steps["nb"]).__name__
        vocabulary_size = len(model.named_steps["tfidf"].vocabulary_)
    except (AttributeError, KeyError, TypeError) as exc:
        logger.error("model does not have the expected pipeline shape: %s", exc)
        raise HTTPException(status_code=503, detail="model is not usable") from exc

    return {
        # filename only
        "model": MODEL_PATH.name,
        "classifier": classifier,
        "vocabulary_size": vocabulary_size,
        "threshold": _require_threshold(),
    }


@app.get("/predict")
def predict(request: Request, url: str = Query(..., description="URL to score")) -> dict:
    """Score one URL."""
    # reject repeated params
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
    except Exception as exc:  # noqa: BLE001
        logger.error("scoring failed: %s", exc)
        raise HTTPException(status_code=503, detail="model is not usable") from exc

    # round before comparing
    probability = round(raw_probability, PROBABILITY_DIGITS)
    return {
        "url": candidate,
        "phishing": probability >= threshold,
        "probability": probability,
        "threshold": threshold,
    }
