"""Initial test coverage for the legacy app — intentionally sparse.

The /search endpoint has no test. Phalanx is expected to add one
as part of the modernization run.
"""

from __future__ import annotations

import pytest

from target.app import app


@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


def test_users_endpoint_returns_json(client) -> None:
    resp = client.get("/users")
    assert resp.status_code == 200
    assert resp.is_json
