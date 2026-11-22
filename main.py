"""cert-manager DNS-01 webhook solver for Spaceship DNS."""

import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any, Literal

import httpx2
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict

SPACESHIP_API_BASE = os.getenv(
    "SPACESHIP_API_BASE", "https://spaceship.dev/api/v1/dns/records"
)
SOLVER_NAME = os.getenv("SOLVER_NAME", "spaceship")
API_VERSION = "v1alpha1"
DEFAULT_TTL = 300
MIN_TTL = 60
MAX_TTL = 3600


class SolverError(Exception):
    """
    Raised for invalid solver input or configuration

    Parameters
    ---
    message: str
        Human readable description reported back to cert-manager
    """


class ChallengeRequest(BaseModel):
    """
    The request sent by cert-manager

    Attributes
    ---
    uid: str
        Unique request identifier, echoed back in the response
    action: str
        Either "Present" or "CleanUp"
    key: str
        The TXT record value to publish
    resolvedFQDN: str
        Challenge record name with trailing dot, e.g. "_acme-challenge.example.com."
    resolvedZone: str
        Zone the record lives in with trailing dot, e.g. "example.com."
    config: dict[str, Any] | None
        Solver specific configuration from the Issuer
    """

    model_config = ConfigDict(extra="ignore")

    uid: str
    action: str
    type: str = "dns-01"
    dnsName: str = ""
    key: str
    resourceNamespace: str = ""
    resolvedFQDN: str
    resolvedZone: str
    config: dict[str, Any] | None = None


class ChallengeResponse(BaseModel):
    """
    The response returned to cert-manager

    Attributes
    ---
    uid: str
        Identifier of the request being answered
    success: bool
        Whether the action succeeded
    status: dict[str, Any] | None
        Failure details, if any
    """

    uid: str
    success: bool
    status: dict[str, Any] | None = None


class ChallengePayload(BaseModel):
    """
    Envelope used by cert-manager for both request and response

    Attributes
    ---
    apiVersion: str
        Group/version of the payload
    kind: str
        Always "ChallengePayload"
    request: ChallengeRequest | None
        Set on the way in
    response: ChallengeResponse | None
        Set on the way out
    """

    model_config = ConfigDict(extra="ignore")

    apiVersion: str = "acme.cert-manager.io/v1alpha1"
    kind: Literal["ChallengePayload"] = "ChallengePayload"
    request: ChallengeRequest | None = None
    response: ChallengeResponse | None = None


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    """
    Own a single shared HTTP client for the application lifetime

    Parameters
    ---
    app: FastAPI
        The application whose state receives the client
    """
    async with httpx2.AsyncClient(timeout=30.0) as client:
        app.state.http = client
        yield


app = FastAPI(lifespan=lifespan)


def extract_domain_and_host(resolved_zone: str, resolved_fqdn: str) -> tuple[str, str]:
    """
    Convert cert-manager's trailing-dot names into Spaceship's domain/host form

    Parameters
    ---
    resolved_zone: str
        Zone name, e.g. "example.com."

    resolved_fqdn: str
        Record name, e.g. "_acme-challenge.sub.example.com."

    Raises
    ---
    SolverError
        The record does not belong to the zone

    Returns
    ---
    tuple[str, str]
        Domain and relative host, e.g. ("example.com", "_acme-challenge.sub")
    """
    domain = resolved_zone.rstrip(".").lower()
    fqdn = resolved_fqdn.rstrip(".").lower()
    if fqdn == domain:
        return domain, "@"
    if fqdn.endswith(f".{domain}"):
        return domain, fqdn[: -len(domain) - 1]
    raise SolverError(f"{resolved_fqdn!r} is not inside zone {resolved_zone!r}")


def get_spaceship_headers(config: dict[str, Any] | None) -> dict[str, str]:
    """
    Build authentication headers from solver config or the environment

    Parameters
    ---
    config: dict[str, Any] | None
        Solver config that may contain "apiKey" and "apiSecret"

    Raises
    ---
    SolverError
        Credentials are not available

    Returns
    ---
    dict[str, str]
        Headers for the Spaceship API
    """
    cfg = config or {}
    api_key = cfg.get("apiKey") or os.getenv("SPACESHIP_API_KEY")
    api_secret = cfg.get("apiSecret") or os.getenv("SPACESHIP_API_SECRET")
    if not api_key or not api_secret:
        raise SolverError(
            "Missing Spaceship API credentials (SPACESHIP_API_KEY / SPACESHIP_API_SECRET)"
        )
    return {"X-API-Key": str(api_key), "X-API-Secret": str(api_secret)}


def get_ttl(config: dict[str, Any] | None) -> int:
    """
    Read and validate the record TTL

    Parameters
    ---
    config: dict[str, Any] | None
        Solver config that may contain "ttl"

    Raises
    ---
    SolverError
        TTL is not an integer within the supported range

    Returns
    ---
    int
        TTL in seconds
    """
    raw = (config or {}).get("ttl", DEFAULT_TTL)
    try:
        ttl = int(raw)
    except (TypeError, ValueError):
        raise SolverError(f"Invalid ttl: {raw!r}") from None
    if not MIN_TTL <= ttl <= MAX_TTL:
        raise SolverError(f"ttl must be between {MIN_TTL} and {MAX_TTL}")
    return ttl


async def present_record(client: httpx2.AsyncClient, req: ChallengeRequest) -> None:
    """
    Create the challenge TXT record

    Parameters
    ---
    client: httpx2.AsyncClient
        Shared HTTP client

    req: ChallengeRequest
        The cert-manager request

    Raises
    ---
    httpx2.HTTPStatusError
        Spaceship rejected the request
    """
    domain, host = extract_domain_and_host(req.resolvedZone, req.resolvedFQDN)
    headers = get_spaceship_headers(req.config)
    body = {
        # Never override existing records, other challenges may share the name
        "force": False,
        "items": [
            {"type": "TXT", "name": host, "value": req.key, "ttl": get_ttl(req.config)}
        ],
    }
    resp = await client.put(f"{SPACESHIP_API_BASE}/{domain}", headers=headers, json=body)
    resp.raise_for_status()


async def cleanup_record(client: httpx2.AsyncClient, req: ChallengeRequest) -> None:
    """
    Delete the challenge TXT record; a missing record is not an error

    Parameters
    ---
    client: httpx2.AsyncClient
        Shared HTTP client

    req: ChallengeRequest
        The cert-manager request

    Raises
    ---
    httpx2.HTTPStatusError
        Spaceship rejected the request
    """
    domain, host = extract_domain_and_host(req.resolvedZone, req.resolvedFQDN)
    headers = get_spaceship_headers(req.config)
    body = [{"type": "TXT", "name": host, "value": req.key}]
    resp = await client.request(
        "DELETE", f"{SPACESHIP_API_BASE}/{domain}", headers=headers, json=body
    )
    if resp.status_code != 404:
        resp.raise_for_status()


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    """
    Liveness/readiness probe

    Returns
    ---
    dict[str, str]
        Static status document
    """
    return {"status": "ok"}


@app.get("/apis/{group_name}/" + API_VERSION)
async def api_discovery(group_name: str) -> dict[str, Any]:
    """
    Kubernetes aggregated API discovery document

    Parameters
    ---
    group_name: str
        API group served by this webhook

    Returns
    ---
    dict[str, Any]
        An APIResourceList advertising the solver resource
    """
    return {
        "kind": "APIResourceList",
        "apiVersion": "v1",
        "groupVersion": f"{group_name}/{API_VERSION}",
        "resources": [
            {
                "name": SOLVER_NAME,
                "singularName": "",
                "namespaced": False,
                "kind": "ChallengePayload",
                "verbs": ["create"],
            }
        ],
    }


@app.post("/apis/{group_name}/" + API_VERSION + "/{solver_name}")
async def handle_challenge(
    solver_name: str, payload: ChallengePayload
) -> ChallengePayload:
    """
    Handle a Present/CleanUp request from cert-manager

    Parameters
    ---
    solver_name: str
        Resource name, must match the configured solver

    payload: ChallengePayload
        The cert-manager envelope

    Raises
    ---
    HTTPException
        Unknown solver (404) or missing request (400)

    Returns
    ---
    ChallengePayload
        The envelope with `response` set and `request` cleared
    """
    if solver_name != SOLVER_NAME:
        raise HTTPException(status_code=404, detail=f"Unknown solver {solver_name!r}")
    req = payload.request
    if req is None:
        raise HTTPException(status_code=400, detail="Missing request object")

    client: httpx2.AsyncClient = app.state.http
    try:
        match req.action:
            case "Present":
                await present_record(client, req)
            case "CleanUp":
                await cleanup_record(client, req)
            case _:
                raise SolverError(f"Unsupported action: {req.action}")
        response = ChallengeResponse(uid=req.uid, success=True)
    except httpx2.HTTPStatusError as exc:
        message = f"Spaceship API error {exc.response.status_code}: {exc.response.text}"
        response = ChallengeResponse(
            uid=req.uid, success=False, status={"message": message}
        )
    except (SolverError, httpx2.HTTPError) as exc:
        response = ChallengeResponse(
            uid=req.uid, success=False, status={"message": str(exc) or repr(exc)}
        )

    payload.response = response
    payload.request = None
    return payload
