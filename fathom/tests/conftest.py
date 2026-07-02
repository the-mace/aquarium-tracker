import pytest
from fastapi.testclient import TestClient
import database as _db
import routers.ai_analysis as _ai
import routers.reference_info as _ref


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(_db, "DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(_db, "REFERENCE_CACHE_DB_PATH", str(tmp_path / "test_ref_cache.db"))
    monkeypatch.setattr(_ai, "run_ai_analysis", lambda *a, **kw: None)
    monkeypatch.setattr(_ai, "run_test_recommendation", lambda *a, **kw: None)
    monkeypatch.setattr(_ref, "fetch_reference_info_bg", lambda *a, **kw: None)
    from main import app
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


@pytest.fixture()
def make_tank(client):
    def _factory(name="Test Tank", water_type="fresh"):
        r = client.post(
            "/tanks",
            data={"name": name, "water_type": water_type},
            follow_redirects=False,
        )
        assert r.status_code == 303, r.text
        return int(r.headers["location"].rsplit("/", 1)[-1])
    return _factory


@pytest.fixture()
def tank_id(make_tank):
    return make_tank()
