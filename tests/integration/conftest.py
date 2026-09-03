"""Shared fixtures for integration tests."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from yukti.api.main import app


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c
