"""allowlist-api — check whether an IP address sits inside an approved CIDR block.

The allowlist is read from disk on every request rather than cached at import
time. That is deliberate: it lets a Kubernetes ConfigMap supply the blocks and
change them without rebuilding the image.
"""

from __future__ import annotations

import ipaddress
import os
from pathlib import Path

import yaml
from fastapi import FastAPI, HTTPException, Query

# Anchored to the package location, not the working directory, so the path
# resolves the same whether run from a checkout or from WORKDIR /app in the
# container. In the container this is /app/config/allowlist.yaml — the same
# path a ConfigMap will later mount over.
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "allowlist.yaml"
CONFIG_PATH = Path(os.environ.get("ALLOWLIST_CONFIG", DEFAULT_CONFIG_PATH))

app = FastAPI(title="allowlist-api", version="0.1.0")


class ConfigError(RuntimeError):
    """The allowlist file is missing, unparseable, or structurally wrong."""


def load_blocks() -> list[dict]:
    """Read and validate the allowlist. Raises ConfigError on any problem."""
    try:
        raw = yaml.safe_load(CONFIG_PATH.read_text())
    except OSError as exc:
        raise ConfigError(f"cannot read allowlist config at {CONFIG_PATH}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"allowlist config at {CONFIG_PATH} is not valid YAML") from exc

    if not isinstance(raw, dict) or not isinstance(raw.get("blocks"), list):
        raise ConfigError("allowlist config must have a top-level 'blocks' list")

    blocks = []
    for entry in raw["blocks"]:
        if not isinstance(entry, dict) or "cidr" not in entry:
            raise ConfigError(f"block entry missing 'cidr': {entry!r}")
        try:
            network = ipaddress.ip_network(entry["cidr"], strict=False)
        except ValueError as exc:
            raise ConfigError(f"invalid CIDR {entry['cidr']!r}") from exc
        blocks.append(
            {
                "cidr": str(network),
                "label": entry.get("label", ""),
                "owner": entry.get("owner", ""),
                "network": network,
            }
        )
    return blocks


def _public(block: dict) -> dict:
    """Strip the ip_network object, which is not JSON-serializable."""
    return {k: v for k, v in block.items() if k != "network"}


def _load_or_503() -> list[dict]:
    try:
        return load_blocks()
    except ConfigError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/healthz")
def healthz() -> dict:
    """Liveness: the process is up and serving. Says nothing about config."""
    return {"status": "ok"}


@app.get("/readyz")
def readyz() -> dict:
    """Readiness: the config actually loads. 503 here pulls the pod out of
    the Service endpoints without restarting it."""
    blocks = _load_or_503()
    return {"status": "ready", "blocks_loaded": len(blocks)}


@app.get("/blocks")
def blocks() -> dict:
    """Show what allowlist this instance is currently serving."""
    loaded = _load_or_503()
    return {"count": len(loaded), "blocks": [_public(b) for b in loaded]}


@app.get("/check")
def check(ip: str = Query(..., description="IPv4 or IPv6 address to check")) -> dict:
    """Check one address against the allowlist."""
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"{ip!r} is not a valid IP address") from None

    for block in _load_or_503():
        if address in block["network"]:
            return {"ip": str(address), "allowed": True, "matched": _public(block)}
    return {"ip": str(address), "allowed": False, "matched": None}
