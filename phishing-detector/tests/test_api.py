"""Tests for phishing-detector.

Most tests use a stub model with a fixed probability. That is deliberate: it
makes the API's behavior — thresholding, validation, readiness — testable without
depending on what the real classifier happens to think about a given URL. A
single integration test at the bottom exercises the real committed artifact.
"""

import pytest
from fastapi.testclient import TestClient

from app import main


class _Step:
    """Stands in for a fitted TfidfVectorizer so /info has something to report."""

    vocabulary_ = {"example": 0, "token": 1}


class StubModel:
    """A pipeline-shaped object that always returns a fixed phishing probability."""

    def __init__(self, probability: float):
        self.probability = probability
        self.named_steps = {"tfidf": _Step(), "nb": self}

    def predict_proba(self, urls):
        return [[1.0 - self.probability, self.probability] for _ in urls]


@pytest.fixture
def config(tmp_path, monkeypatch):
    """A writable threshold config; returns a setter so tests can retune it."""
    path = tmp_path / "detector.yaml"
    path.write_text("threshold: 0.5\n")
    monkeypatch.setattr(main, "CONFIG_PATH", path)

    def set_threshold(value):
        path.write_text(f"threshold: {value}\n")

    return set_threshold


@pytest.fixture
def client(config, monkeypatch):
    monkeypatch.setattr(main, "MODEL", StubModel(0.80))
    monkeypatch.setattr(main, "MODEL_ERROR", None)
    return TestClient(main.app)


def test_healthz_is_ok_even_with_no_model_and_no_config(monkeypatch, tmp_path):
    # Liveness must not depend on the model. If it did, a bad artifact would
    # make Kubernetes restart-loop every pod instead of just marking them
    # unready, and the containers would die before anyone could debug them.
    monkeypatch.setattr(main, "MODEL", None)
    monkeypatch.setattr(main, "MODEL_ERROR", "boom")
    monkeypatch.setattr(main, "CONFIG_PATH", tmp_path / "missing.yaml")
    assert TestClient(main.app).get("/healthz").json() == {"status": "ok"}


def test_readyz_is_ready_when_model_and_config_load(client):
    response = client.get("/readyz")
    assert response.status_code == 200
    assert response.json() == {"status": "ready", "threshold": 0.5}


def test_readyz_returns_503_when_model_failed_to_load(config, monkeypatch):
    monkeypatch.setattr(main, "MODEL", None)
    monkeypatch.setattr(main, "MODEL_ERROR", "cannot load model")
    assert TestClient(main.app).get("/readyz").status_code == 503


def test_readyz_returns_503_when_config_missing(client, monkeypatch, tmp_path):
    monkeypatch.setattr(main, "CONFIG_PATH", tmp_path / "gone.yaml")
    assert client.get("/readyz").status_code == 503


def test_readyz_returns_503_when_threshold_out_of_range(client, config):
    config(1.7)
    assert client.get("/readyz").status_code == 503


def test_predict_flags_a_url_above_the_threshold(client):
    body = client.get("/predict", params={"url": "http://evil.tk/login"}).json()
    assert body["phishing"] is True
    assert body["probability"] == 0.8
    assert body["threshold"] == 0.5


def test_predict_clears_a_url_below_the_threshold(config, monkeypatch):
    monkeypatch.setattr(main, "MODEL", StubModel(0.10))
    monkeypatch.setattr(main, "MODEL_ERROR", None)
    body = TestClient(main.app).get("/predict", params={"url": "wikipedia.org"}).json()
    assert body["phishing"] is False


def test_threshold_change_flips_the_verdict_without_touching_the_model(client, config):
    # This is the ConfigMap behavior the whole design exists for: the model's
    # probability is unchanged, only the policy applied to it moves.
    before = client.get("/predict", params={"url": "http://evil.tk/login"}).json()
    assert before["phishing"] is True

    config(0.95)
    after = client.get("/predict", params={"url": "http://evil.tk/login"}).json()

    assert after["probability"] == before["probability"]
    assert after["phishing"] is False
    assert after["threshold"] == 0.95


def test_predict_rejects_an_empty_url(client):
    assert client.get("/predict", params={"url": "   "}).status_code == 400


def test_predict_rejects_an_oversized_url(client):
    assert client.get("/predict", params={"url": "a" * 3000}).status_code == 400


def test_predict_requires_the_url_parameter(client):
    # FastAPI's own validation returns 422 for a missing required query param.
    assert client.get("/predict").status_code == 422


def test_info_reports_the_running_configuration(client):
    body = client.get("/info").json()
    assert body["vocabulary_size"] == 2
    assert body["threshold"] == 0.5


@pytest.mark.integration
def test_real_artifact_scores_an_obvious_phish_above_an_obvious_legitimate_url(config):
    """Exercises the committed model, not a stub."""
    if main.MODEL is None:
        pytest.skip(f"model artifact not available: {main.MODEL_ERROR}")

    client = TestClient(main.app)
    phish = client.get(
        "/predict",
        params={"url": "paypal.co.uk.secure-login.verify-account.tk/cgi-bin/webscr?cmd=_login"},
    ).json()
    legit = client.get("/predict", params={"url": "www.wikipedia.org/wiki/Cat"}).json()

    assert phish["phishing"] is True
    assert legit["phishing"] is False
    assert phish["probability"] > legit["probability"]
