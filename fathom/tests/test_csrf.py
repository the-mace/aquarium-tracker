from security import origin_allowed, url_host


class _FakeRequest:
    def __init__(self, method, host="testserver", origin=None, referer=None):
        self.method = method
        self.headers = {}
        if host is not None:
            self.headers["host"] = host
        if origin is not None:
            self.headers["origin"] = origin
        if referer is not None:
            self.headers["referer"] = referer


def test_url_host_extracts_netloc():
    assert url_host("http://192.168.50.205:8000/tanks") == "192.168.50.205:8000"
    assert url_host("https://evil.example") == "evil.example"


def test_origin_allowed_missing_headers():
    assert origin_allowed(_FakeRequest("POST"))


def test_origin_allowed_matching_origin():
    req = _FakeRequest("POST", host="testserver", origin="http://testserver")
    assert origin_allowed(req)


def test_origin_allowed_rejects_foreign_origin():
    req = _FakeRequest("POST", host="testserver", origin="http://evil.example")
    assert not origin_allowed(req)


def test_origin_allowed_rejects_null_origin():
    req = _FakeRequest("POST", host="testserver", origin="null")
    assert not origin_allowed(req)


def test_origin_allowed_rejects_foreign_referer_when_no_origin():
    req = _FakeRequest("POST", host="testserver", referer="http://evil.example/page")
    assert not origin_allowed(req)


def test_origin_allowed_matching_referer():
    req = _FakeRequest("POST", host="testserver", referer="http://testserver/tanks/1")
    assert origin_allowed(req)


def test_get_always_allowed():
    req = _FakeRequest("GET", host="testserver", origin="http://evil.example")
    assert origin_allowed(req)


def test_post_from_evil_origin_is_403(client, tank_id):
    r = client.post(
        f"/tanks/{tank_id}/events",
        data={"event_type": "water_change", "notes": "csrf"},
        headers={"Origin": "http://evil.example", "Accept": "application/json"},
    )
    assert r.status_code == 403
    assert r.json()["detail"] == "Cross-origin request blocked"


def test_post_from_same_origin_still_works(client, tank_id):
    r = client.post(
        f"/tanks/{tank_id}/events",
        data={"event_type": "water_change", "notes": "ok"},
        headers={"Origin": "http://testserver", "Accept": "application/json"},
    )
    assert r.status_code == 201
