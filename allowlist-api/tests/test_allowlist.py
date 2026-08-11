"""Tests for allowlist-api.

Each test points the app at a temporary config file instead of the real one, so
the tests describe behavior rather than the contents of config/allowlist.yaml.
"""

import textwrap

import pytest
from fastapi.testclient import TestClient

from app import main

FIXTURE = textwrap.dedent(
    """
    blocks:
      - cidr: 10.20.0.0/16
        label: scada-field-devices
        owner: ot-engineering
      - cidr: 192.168.50.0/24
        label: jump-hosts
        owner: security
    """
)


@pytest.fixture
def client(tmp_path, monkeypatch):
    """A client whose app reads a known two-block allowlist."""
    config = tmp_path / "allowlist.yaml"
    config.write_text(FIXTURE)
    monkeypatch.setattr(main, "CONFIG_PATH", config)
    return TestClient(main.app)


@pytest.fixture
def client_without_config(tmp_path, monkeypatch):
    """A client pointed at a path that does not exist."""
    monkeypatch.setattr(main, "CONFIG_PATH", tmp_path / "missing.yaml")
    return TestClient(main.app)


def test_healthz_is_ok_even_without_config(client_without_config):
    # Liveness must not depend on config, or a bad ConfigMap would make
    # Kubernetes restart-loop pods that are actually running fine.
    response = client_without_config.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readyz_reports_ready_when_config_loads(client):
    response = client.get("/readyz")
    assert response.status_code == 200
    assert response.json() == {"status": "ready", "blocks_loaded": 2}


def test_readyz_returns_503_when_config_missing(client_without_config):
    response = client_without_config.get("/readyz")
    assert response.status_code == 503


def test_blocks_lists_the_loaded_allowlist(client):
    body = client.get("/blocks").json()
    assert body["count"] == 2
    assert [b["cidr"] for b in body["blocks"]] == ["10.20.0.0/16", "192.168.50.0/24"]
    assert body["blocks"][0]["owner"] == "ot-engineering"


def test_check_allows_address_inside_a_block(client):
    body = client.get("/check", params={"ip": "10.20.7.42"}).json()
    assert body["allowed"] is True
    assert body["matched"]["label"] == "scada-field-devices"


def test_check_denies_address_outside_every_block(client):
    body = client.get("/check", params={"ip": "8.8.8.8"}).json()
    assert body["allowed"] is False
    assert body["matched"] is None


def test_check_rejects_a_malformed_address(client):
    response = client.get("/check", params={"ip": "10.20.7.999"})
    assert response.status_code == 400


def test_check_requires_the_ip_parameter(client):
    # FastAPI's own validation layer returns 422 for a missing required query param.
    assert client.get("/check").status_code == 422
