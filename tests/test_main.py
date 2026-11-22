"""Unit tests for the Spaceship cert-manager webhook."""

import json
from collections.abc import Callable, Iterator

import httpx2
import pytest
from fastapi.testclient import TestClient

import main

ZONE = "example.com."
FQDN = "_acme-challenge.www.example.com."


def make_request(action: str = "Present", **overrides) -> dict:
    """
    Build a cert-manager payload

    Parameters
    ---
    action: str
        "Present" or "CleanUp"

    **overrides
        Fields of the request to replace

    Returns
    ---
    dict
        JSON-serialisable ChallengePayload
    """
    request = {
        "uid": "uid-1",
        "action": action,
        "type": "dns-01",
        "dnsName": "www.example.com",
        "key": "token-value",
        "resolvedFQDN": FQDN,
        "resolvedZone": ZONE,
        "config": None,
    } | overrides
    return {
        "apiVersion": "acme.cert-manager.io/v1alpha1",
        "kind": "ChallengePayload",
        "request": request,
    }


class Spaceship:
    """
    Fake Spaceship API recording the requests it receives

    Attributes
    ---
    calls: list[httpx2.Request]
        Requests seen so far
    status: int
        Status code answered to every request
    """

    def __init__(self) -> None:
        self.calls: list[httpx2.Request] = []
        self.status = 200

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.calls.append(request)
        return httpx2.Response(self.status, text="upstream says no")


@pytest.fixture(autouse=True)
def credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPACESHIP_API_KEY", "env-key")
    monkeypatch.setenv("SPACESHIP_API_SECRET", "env-secret")


@pytest.fixture
def spaceship() -> Spaceship:
    return Spaceship()


@pytest.fixture
def client(spaceship: Spaceship) -> Iterator[TestClient]:
    with TestClient(main.app) as test_client:
        main.app.state.http = httpx2.AsyncClient(
            transport=httpx2.MockTransport(spaceship)
        )
        yield test_client


def post(client: TestClient, payload: dict, solver: str = "spaceship"):
    return client.post(f"/apis/dns01.spaceship.webhook/v1alpha1/{solver}", json=payload)


class TestExtractDomainAndHost:
    def test_subdomain_record(self):
        assert main.extract_domain_and_host(ZONE, FQDN) == (
            "example.com",
            "_acme-challenge.www",
        )

    def test_apex_record(self):
        assert main.extract_domain_and_host(ZONE, "example.com.") == ("example.com", "@")

    def test_is_case_insensitive(self):
        assert main.extract_domain_and_host("Example.COM.", "_A.Example.com.") == (
            "example.com",
            "_a",
        )

    def test_rejects_foreign_name(self):
        with pytest.raises(main.SolverError):
            main.extract_domain_and_host(ZONE, "_acme-challenge.other.org.")

    def test_rejects_lookalike_suffix(self):
        with pytest.raises(main.SolverError):
            main.extract_domain_and_host(ZONE, "_acme-challengeexample.com.")


class TestCredentials:
    def test_from_environment(self):
        assert main.get_spaceship_headers(None) == {
            "X-API-Key": "env-key",
            "X-API-Secret": "env-secret",
        }

    def test_config_wins_over_environment(self):
        headers = main.get_spaceship_headers({"apiKey": "k", "apiSecret": "s"})
        assert headers == {"X-API-Key": "k", "X-API-Secret": "s"}

    def test_missing(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("SPACESHIP_API_KEY")
        with pytest.raises(main.SolverError):
            main.get_spaceship_headers({})


class TestTtl:
    @pytest.mark.parametrize(
        ("config", "expected"),
        [(None, 300), ({}, 300), ({"ttl": 60}, 60), ({"ttl": "600"}, 600)],
    )
    def test_valid(self, config, expected):
        assert main.get_ttl(config) == expected

    @pytest.mark.parametrize("ttl", ["abc", None, [], 59, 3601])
    def test_invalid(self, ttl):
        with pytest.raises(main.SolverError):
            main.get_ttl({"ttl": ttl})


class TestPresent:
    def test_puts_txt_record(self, client: TestClient, spaceship: Spaceship):
        body = post(client, make_request(config={"ttl": 120})).json()

        assert body["response"] == {"uid": "uid-1", "success": True, "status": None}
        assert body["request"] is None
        (call,) = spaceship.calls
        assert call.method == "PUT"
        assert str(call.url) == f"{main.SPACESHIP_API_BASE}/example.com"
        assert call.headers["X-API-Key"] == "env-key"
        assert call.headers["X-API-Secret"] == "env-secret"
        assert json.loads(call.content) == {
            "force": False,
            "items": [
                {
                    "type": "TXT",
                    "name": "_acme-challenge.www",
                    "value": "token-value",
                    "ttl": 120,
                }
            ],
        }

    def test_upstream_error_is_reported(self, client: TestClient, spaceship: Spaceship):
        spaceship.status = 401

        response = post(client, make_request()).json()["response"]

        assert response["success"] is False
        assert "401" in response["status"]["message"]
        assert "upstream says no" in response["status"]["message"]

    def test_network_error_is_reported(self, client: TestClient):
        def boom(request: httpx2.Request) -> httpx2.Response:
            raise httpx2.ConnectError("unreachable", request=request)

        main.app.state.http = httpx2.AsyncClient(transport=httpx2.MockTransport(boom))

        response = post(client, make_request()).json()["response"]

        assert response["success"] is False
        assert "unreachable" in response["status"]["message"]

    def test_missing_credentials_never_calls_api(
        self, client: TestClient, spaceship: Spaceship, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.delenv("SPACESHIP_API_SECRET")

        response = post(client, make_request()).json()["response"]

        assert response["success"] is False
        assert "credentials" in response["status"]["message"]
        assert not spaceship.calls

    def test_bad_ttl_never_calls_api(self, client: TestClient, spaceship: Spaceship):
        response = post(client, make_request(config={"ttl": 1})).json()["response"]

        assert response["success"] is False
        assert not spaceship.calls


class TestCleanUp:
    def test_deletes_txt_record(self, client: TestClient, spaceship: Spaceship):
        response = post(client, make_request("CleanUp")).json()["response"]

        assert response["success"] is True
        (call,) = spaceship.calls
        assert call.method == "DELETE"
        assert str(call.url) == f"{main.SPACESHIP_API_BASE}/example.com"
        assert json.loads(call.content) == [
            {"type": "TXT", "name": "_acme-challenge.www", "value": "token-value"}
        ]

    def test_missing_record_is_not_an_error(
        self, client: TestClient, spaceship: Spaceship
    ):
        spaceship.status = 404

        assert post(client, make_request("CleanUp")).json()["response"]["success"]

    def test_upstream_error_is_reported(self, client: TestClient, spaceship: Spaceship):
        spaceship.status = 500

        assert not post(client, make_request("CleanUp")).json()["response"]["success"]


class TestEndpoints:
    def test_unknown_action(self, client: TestClient, spaceship: Spaceship):
        response = post(client, make_request("Explode")).json()["response"]

        assert response["success"] is False
        assert "Unsupported action" in response["status"]["message"]
        assert not spaceship.calls

    def test_unknown_solver(self, client: TestClient):
        assert post(client, make_request(), solver="other").status_code == 404

    def test_missing_request(self, client: TestClient):
        assert post(client, {"kind": "ChallengePayload"}).status_code == 400

    def test_malformed_payload(self, client: TestClient):
        assert post(client, {"request": {"uid": "x"}}).status_code == 422

    def test_discovery(self, client: TestClient):
        body = client.get("/apis/dns01.spaceship.webhook/v1alpha1").json()

        assert body["groupVersion"] == "dns01.spaceship.webhook/v1alpha1"
        (resource,) = body["resources"]
        assert resource["name"] == main.SOLVER_NAME
        assert resource["kind"] == "ChallengePayload"
        assert resource["verbs"] == ["create"]

    def test_healthz(self, client: TestClient):
        assert client.get("/healthz").json() == {"status": "ok"}
