"""Fixtures for the voice archive store's own suite.

No network, no Docker, no database: the app runs in-process through
Starlette's TestClient over a temporary directory.
"""
from __future__ import annotations

from typing import Any, Callable, Dict

import pytest

from store_helpers import TOKEN, server


@pytest.fixture()
def make_store(tmp_path) -> Callable[..., Any]:
    """make_store(**settings) -> (TestClient, Store, root)."""
    from starlette.testclient import TestClient

    clients = []

    def _make(**overrides: Any):
        root = tmp_path / "store"
        root.mkdir(exist_ok=True)
        values: Dict[str, Any] = {
            "root": str(root), "tokens": (TOKEN,), "min_free_bytes": 0, "max_object_bytes": 64 * 1024 * 1024,
        }
        values.update(overrides)
        app = server.create_app(server.Settings(**values), background=False)
        client = TestClient(app)
        clients.append(client)
        return client, app.state.store, root

    yield _make
    for client in clients:
        client.close()


@pytest.fixture()
def store(make_store):
    return make_store()
