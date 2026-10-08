"""Exercise real SDK signature, claim and scope validation with local JWKS."""

import time

import pytest
from auth0_api_python.api_client import ApiClient
from authlib.jose import JsonWebKey, jwt
from fastapi.testclient import TestClient
from test_api import make_app


@pytest.fixture
def signed_token(monkeypatch):
    key = JsonWebKey.generate_key("RSA", 2048, is_private=True, options={"kid": "test"})

    async def discover(self, issuer=None):
        return {
            "issuer": "https://test.auth0.com/",
            "jwks_uri": "https://test.auth0.com/.well-known/jwks.json",
        }

    async def jwks(self, uri):
        return {"keys": [key.as_dict(is_private=False)]}

    monkeypatch.setattr(ApiClient, "_discover", discover)
    monkeypatch.setattr(ApiClient, "_fetch_jwks", jwks)

    def token(**updates):
        now = int(time.time())
        claims = {
            "sub": "alice",
            "iss": "https://test.auth0.com/",
            "aud": "https://test/api",
            "exp": now + 300,
            "iat": now,
            "scope": "research:read",
        }
        claims.update(updates)
        return jwt.encode({"alg": "RS256", "kid": "test"}, claims, key).decode()

    return token


@pytest.mark.parametrize(
    "updates,status",
    [
        ({}, 200),
        ({"sub": None}, 401),
        ({"sub": ""}, 401),
        ({"scope": ""}, 403),
        ({"aud": "wrong-audience"}, 401),
        ({"iss": "https://other.auth0.com/"}, 401),
        ({"exp": 1}, 401),
    ],
)
def test_signed_token_validation(tmp_path, signed_token, updates, status):
    with TestClient(make_app(tmp_path)) as client:
        response = client.get(
            "/research", headers={"Authorization": "Bearer " + signed_token(**updates)}
        )
        assert response.status_code == status


def test_bad_signature(tmp_path, signed_token):
    parts = signed_token().split(".")
    parts[2] = ("A" if parts[2][0] != "A" else "B") + parts[2][1:]
    with TestClient(make_app(tmp_path)) as client:
        assert (
            client.get(
                "/research", headers={"Authorization": "Bearer " + ".".join(parts)}
            ).status_code
            == 401
        )
