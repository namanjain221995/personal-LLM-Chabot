"""Offline HTTP smoke tests: /health and the /reports endpoints (spec §8).

POST /chat is not exercised here — it requires the model runtimes, which the
offline suite must not touch. /health now probes the four vLLM services and
DuckDB (§8), so those probes are mocked at the app.health module level; the
DuckDB check itself runs for real against temp files (no services needed).
"""
import pytest
from fastapi.testclient import TestClient

from app import health as health_mod
from app.config import settings
from app.health import service_root
from app.main import app

_VLLM_CHECKS = {"vllm", "vllm-router", "vllm-vision", "vllm-embed"}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "reports_dir", str(tmp_path))
    with TestClient(app) as c:
        yield c


def _mock_probes(monkeypatch, *, down=frozenset(), duckdb_ok=True):
    """Replace the network/disk probes with offline fakes."""

    async def fake_probe(client_, base_url):
        name = next(
            n for n in _VLLM_CHECKS if service_root(base_url).startswith(f"http://{n}:")
        )
        if name in down:
            return {"status": "error", "detail": "ConnectError: connection refused"}
        return {"status": "ok"}

    def fake_duckdb(path):
        if duckdb_ok:
            return {"status": "ok"}
        return {"status": "error", "detail": "IOException: no such file"}

    monkeypatch.setattr(health_mod, "_probe_vllm", fake_probe)
    monkeypatch.setattr(health_mod, "_check_duckdb", fake_duckdb)


def test_health_ok_when_all_dependencies_up(client, monkeypatch):
    _mock_probes(monkeypatch)
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    # §8: one check per vLLM service + DuckDB — never a static ok.
    # app_db joined the probe set so a bad migration surfaces at /health
    # rather than on the first request that touches app.sqlite3.
    # vllm-vision shares the main endpoint (one multimodal model), so /health
    # lists DISTINCT services rather than probing one process twice.
    assert set(body["checks"]) == {
        "vllm", "vllm-router", "vllm-embed", "duckdb", "app_db",
    }
    assert body["checks"]["app_db"]["status"] == "ok"
    assert all(c["status"] == "ok" for c in body["checks"].values())


def test_health_degraded_when_a_vllm_service_is_down(client, monkeypatch):
    _mock_probes(monkeypatch, down={"vllm-embed"})
    body = client.get("/health").json()
    assert body["status"] == "degraded"
    assert body["checks"]["vllm-embed"]["status"] == "error"
    assert body["checks"]["vllm"]["status"] == "ok"


def test_health_degraded_when_duckdb_check_fails(client, monkeypatch):
    _mock_probes(monkeypatch, duckdb_ok=False)
    body = client.get("/health").json()
    assert body["status"] == "degraded"
    assert body["checks"]["duckdb"]["status"] == "error"


def test_duckdb_check_real_file_roundtrip(tmp_path):
    import duckdb

    db = tmp_path / "warehouse.duckdb"
    duckdb.connect(str(db)).close()  # create a valid warehouse file
    assert health_mod._check_duckdb(str(db)) == {"status": "ok"}

    missing = health_mod._check_duckdb(str(tmp_path / "missing.duckdb"))
    assert missing["status"] == "error"
    assert missing["detail"]


def test_service_root_strips_v1_suffix():
    assert service_root("http://vllm:30000/v1") == "http://vllm:30000"
    assert service_root("http://vllm-embed:30003/v1/") == "http://vllm-embed:30003"
    assert service_root("http://host:9000") == "http://host:9000"


def test_reports_empty(client):
    resp = client.get("/reports")
    assert resp.status_code == 200
    assert resp.json() == {"reports": []}


def test_reports_lists_and_serves_files(client, tmp_path):
    (tmp_path / "q3.pdf").write_bytes(b"%PDF-1.4 fake")
    listing = client.get("/reports").json()["reports"]
    assert [i["filename"] for i in listing] == ["q3.pdf"]

    resp = client.get("/reports/q3.pdf")
    assert resp.status_code == 200
    assert resp.content == b"%PDF-1.4 fake"
    assert resp.headers["content-type"].startswith("application/pdf")


def test_reports_missing_file_404(client):
    assert client.get("/reports/nope.docx").status_code == 404


def test_reports_traversal_400(client):
    # No separators, but contains '..' → rejected by the sanitizer.
    assert client.get("/reports/evil..name.pdf").status_code == 400
