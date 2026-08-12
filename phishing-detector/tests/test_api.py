"""API behavior tests."""

import pytest
from fastapi.testclient import TestClient

from app import main


class _Vectorizer:
    vocabulary_ = {"example": 0, "token": 1, "third": 2}


class StubModel:
    """A pipeline-shaped object that always returns a fixed phishing probability."""

    def __init__(self, probability: float):
        self.probability = probability
        self.named_steps = {"tfidf": _Vectorizer(), "nb": self}

    def predict_proba(self, urls):
        return [[1.0 - self.probability, self.probability] for _ in urls]


class SingleClassModel:
    """Deserializes fine, then fails on use — a model fitted on one class only."""

    def __init__(self):
        self.named_steps = {"tfidf": _Vectorizer(), "nb": self}

    def predict_proba(self, urls):
        return [[1.0] for _ in urls]


class NotAPipeline:
    """Deserializes fine, but has no named_steps — e.g. a bare estimator."""

    def predict_proba(self, urls):
        return [[0.5, 0.5] for _ in urls]


@pytest.fixture
def config(tmp_path, monkeypatch):
    """A writable threshold config; returns a setter so tests can retune it."""
    path = tmp_path / "detector.yaml"
    path.write_text("threshold: 0.5\n")
    monkeypatch.setattr(main, "CONFIG_PATH", path)

    def write(text):
        path.write_text(text)

    return write


@pytest.fixture
def client(config, monkeypatch):
    monkeypatch.setattr(main, "MODEL", StubModel(0.80))
    monkeypatch.setattr(main, "MODEL_ERROR", None)
    return TestClient(main.app)


def _client_with(model, monkeypatch):
    monkeypatch.setattr(main, "MODEL", model)
    monkeypatch.setattr(main, "MODEL_ERROR", None)
    return TestClient(main.app)


# --------------------------------------------------------------------------
# Liveness / readiness
# --------------------------------------------------------------------------


def test_healthz_returns_200_even_with_no_model_and_no_config(monkeypatch, tmp_path):
    # The status code is the assertion that matters. If liveness depended on the
    # model, one bad artifact would restart-loop every pod in the cluster instead
    # of just marking them unready, and the containers would die before anyone
    # could debug them.
    monkeypatch.setattr(main, "MODEL", None)
    monkeypatch.setattr(main, "MODEL_ERROR", "boom")
    monkeypatch.setattr(main, "CONFIG_PATH", tmp_path / "missing.yaml")
    response = TestClient(main.app).get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readyz_is_ready_when_model_and_config_load(client):
    response = client.get("/readyz")
    assert response.status_code == 200
    assert response.json() == {"status": "ready", "threshold": 0.5}


def test_readyz_returns_503_when_model_failed_to_load(config, monkeypatch):
    monkeypatch.setattr(main, "MODEL", None)
    monkeypatch.setattr(main, "MODEL_ERROR", "model artifact unavailable")
    assert TestClient(main.app).get("/readyz").status_code == 503


def test_readyz_returns_503_when_the_model_loads_but_cannot_score(config, monkeypatch):
    assert _client_with(SingleClassModel(), monkeypatch).get("/readyz").status_code == 503


def test_readyz_returns_503_when_config_missing(client, monkeypatch, tmp_path):
    monkeypatch.setattr(main, "CONFIG_PATH", tmp_path / "gone.yaml")
    assert client.get("/readyz").status_code == 503


@pytest.mark.parametrize(
    "text",
    [
        "threshold: 1.7",  # out of range
        "threshold: -0.1",  # out of range
        "threshold: high",  # not a number
        "threshold: [0.5]",  # not a scalar
        "not_a_threshold: 0.5",  # key missing
        "",  # empty file
        "threshold: 0.5\n  bad indent: [",  # unparseable YAML
    ],
)
def test_readyz_returns_503_for_invalid_threshold_config(client, config, text):
    config(text)
    assert client.get("/readyz").status_code == 503


@pytest.mark.parametrize("word", ["yes", "on", "true"])
def test_boolean_threshold_is_rejected_rather_than_coerced(client, config, word):
    config(f"threshold: {word}\n")
    assert client.get("/readyz").status_code == 503


@pytest.mark.parametrize("value", ["0.0", "1.0"])
def test_threshold_accepts_the_range_endpoints(client, config, value):
    config(f"threshold: {value}\n")
    response = client.get("/readyz")
    assert response.status_code == 200
    assert response.json()["threshold"] == float(value)


# --------------------------------------------------------------------------
# /predict
# --------------------------------------------------------------------------


def test_predict_flags_a_url_above_the_threshold(client):
    body = client.get("/predict", params={"url": "http://evil.tk/login"}).json()
    assert body["phishing"] is True
    assert body["probability"] == 0.8
    assert body["threshold"] == 0.5


def test_predict_clears_a_url_below_the_threshold(config, monkeypatch):
    body = (
        _client_with(StubModel(0.10), monkeypatch)
        .get("/predict", params={"url": "wikipedia.org"})
        .json()
    )
    assert body["phishing"] is False


def test_probability_exactly_equal_to_the_threshold_is_flagged(config, monkeypatch):
    config("threshold: 0.5\n")
    body = (
        _client_with(StubModel(0.5), monkeypatch)
        .get("/predict", params={"url": "borderline.example"})
        .json()
    )
    assert body["probability"] == 0.5
    assert body["threshold"] == 0.5
    assert body["phishing"] is True


def test_verdict_agrees_with_the_probability_that_is_reported(config, monkeypatch):
    config("threshold: 0.5\n")
    body = (
        _client_with(StubModel(0.49999), monkeypatch)
        .get("/predict", params={"url": "borderline.example"})
        .json()
    )
    assert (body["probability"] >= body["threshold"]) is body["phishing"]


def test_probability_is_reported_to_four_decimals(config, monkeypatch):
    body = (
        _client_with(StubModel(0.123456789), monkeypatch)
        .get("/predict", params={"url": "x.example"})
        .json()
    )
    assert body["probability"] == 0.1235


def test_threshold_change_flips_the_verdict_without_touching_the_model(client, config):
    before = client.get("/predict", params={"url": "http://evil.tk/login"}).json()
    assert before["phishing"] is True

    config("threshold: 0.95\n")
    after = client.get("/predict", params={"url": "http://evil.tk/login"}).json()

    assert after["probability"] == before["probability"]
    assert after["phishing"] is False
    assert after["threshold"] == 0.95


def test_predict_returns_503_when_the_model_is_unavailable(config, monkeypatch):
    monkeypatch.setattr(main, "MODEL", None)
    monkeypatch.setattr(main, "MODEL_ERROR", "model artifact unavailable")
    assert TestClient(main.app).get("/predict", params={"url": "a.com"}).status_code == 503


def test_predict_does_not_disclose_internal_paths(config, monkeypatch, tmp_path):
    monkeypatch.setattr(main, "MODEL", None)
    monkeypatch.setattr(main, "MODEL_ERROR", "model artifact unavailable")
    monkeypatch.setattr(main, "CONFIG_PATH", tmp_path / "nope.yaml")
    body = TestClient(main.app).get("/predict", params={"url": "a.com"}).text
    assert "/app/" not in body
    assert str(tmp_path) not in body


@pytest.mark.parametrize("value", ["   ", "", "　"])
def test_predict_rejects_empty_or_whitespace_urls(client, value):
    assert client.get("/predict", params={"url": value}).status_code == 400


def test_predict_accepts_a_url_at_exactly_the_length_limit(client):
    url = "a" * main.MAX_URL_LENGTH
    assert client.get("/predict", params={"url": url}).status_code == 200


def test_predict_rejects_a_url_one_character_over_the_limit(client):
    url = "a" * (main.MAX_URL_LENGTH + 1)
    assert client.get("/predict", params={"url": url}).status_code == 400


def test_predict_rejects_a_repeated_url_parameter(client):
    response = client.get("/predict?url=safe.example&url=evil.example")
    assert response.status_code == 400


def test_predict_requires_the_url_parameter(client):
    assert client.get("/predict").status_code == 422


def test_predict_handles_non_ascii_without_crashing(client):
    response = client.get("/predict", params={"url": "пример.рф/вход"})
    assert response.status_code == 200


# --------------------------------------------------------------------------
# /info
# --------------------------------------------------------------------------


def test_info_reports_the_running_configuration(client):
    body = client.get("/info").json()
    assert body["classifier"] == "StubModel"
    assert body["vocabulary_size"] == 3
    assert body["threshold"] == 0.5
    assert body["model"] == main.MODEL_PATH.name


def test_info_does_not_disclose_the_absolute_model_path(client):
    assert "/" not in client.get("/info").json()["model"]


def test_info_returns_503_when_the_model_is_not_a_pipeline(config, monkeypatch):
    assert _client_with(NotAPipeline(), monkeypatch).get("/info").status_code == 503


# --------------------------------------------------------------------------
# Integration — exercises the real committed artifact
# --------------------------------------------------------------------------


@pytest.mark.integration
def test_real_artifact_loads_and_scores(config):
    if main.MODEL is None:
        pytest.fail(f"model artifact failed to load: {main.MODEL_ERROR}")

    client = TestClient(main.app)
    assert client.get("/readyz").status_code == 200

    phish = client.get(
        "/predict",
        params={"url": "paypal.co.uk.secure-login.verify-account.tk/cgi-bin/webscr?cmd=_login"},
    ).json()
    legit = client.get("/predict", params={"url": "www.wikipedia.org/wiki/Cat"}).json()

    assert phish["phishing"] is True
    assert legit["phishing"] is False
    assert phish["probability"] > legit["probability"]


@pytest.mark.integration
@pytest.mark.parametrize(
    "url",
    ["www.wikipedia.org/wiki/Cat", "nytimes.com", "amazon.com", "usa.gov", "cisa.gov"],
)
def test_known_good_domains_stay_below_the_shipped_threshold(config, url):
    """Known-good domains must not flag."""
    if main.MODEL is None:
        pytest.fail(f"model artifact failed to load: {main.MODEL_ERROR}")

    config("threshold: 0.30\n")
    body = TestClient(main.app).get("/predict", params={"url": url}).json()
    assert body["phishing"] is False, f"{url} scored {body['probability']}"
