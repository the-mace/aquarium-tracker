"""Tests for global home/source water tests (routers/home_water.py)."""
import io
import json
from unittest.mock import MagicMock

import database as _db
from database import get_db
from routers.home_water import (
    _coerce_reading,
    _parse_json_object,
    _parse_summary_sections,
    build_home_water_summary_prompt,
    home_water_summary_is_stale,
    should_refresh_home_water_summary_after_write,
    run_home_water_summary,
)


def test_list_home_water_empty(client):
    r = client.get("/home-water")
    assert r.status_code == 200
    assert "No home water readings" in r.text or "Home Water" in r.text
    assert "Log" in r.text
    assert "Lab report" in r.text or "Manual entry" in r.text


def test_add_home_water_gh_kh(client):
    r = client.post(
        "/home-water",
        data={"gh": "8.0", "kh": "10.0", "sample_point": "tap", "water_blend": "mixed"},
        headers={"Accept": "application/json"},
    )
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "created"
    test_id = body["id"]

    with get_db() as conn:
        row = conn.execute(
            "SELECT gh, kh, sample_point, water_blend, is_lab_test, ph FROM home_water_tests WHERE id=?",
            (test_id,),
        ).fetchone()
    assert row["gh"] == 8.0
    assert row["kh"] == 10.0
    assert row["sample_point"] == "tap"
    assert row["water_blend"] == "mixed"
    assert row["is_lab_test"] == 0
    assert row["ph"] is None


def test_add_home_water_lab_and_raw_sample(client):
    r = client.post(
        "/home-water",
        data={
            "gh": "12", "kh": "14", "tds": "280",
            "sample_point": "raw", "is_lab_test": "1",
            "notes": "municipal lab report",
            "timestamp": "2026-03-01 12:00:00",
        },
        headers={"Accept": "application/json"},
    )
    assert r.status_code == 201
    with get_db() as conn:
        row = conn.execute(
            "SELECT sample_point, is_lab_test, notes, tds FROM home_water_tests WHERE id=?",
            (r.json()["id"],),
        ).fetchone()
    assert row["sample_point"] == "raw"
    assert row["is_lab_test"] == 1
    assert "municipal" in row["notes"]
    assert row["tds"] == 280.0


def test_add_invalid_sample_point_defaults_to_tap(client):
    r = client.post(
        "/home-water",
        data={"gh": "7", "sample_point": "not_a_real_point"},
        headers={"Accept": "application/json"},
    )
    assert r.status_code == 201
    with get_db() as conn:
        row = conn.execute(
            "SELECT sample_point FROM home_water_tests WHERE id=?", (r.json()["id"],)
        ).fetchone()
    assert row["sample_point"] == "tap"


def test_update_home_water(client):
    created = client.post(
        "/home-water",
        data={"gh": "8", "kh": "10", "sample_point": "tap"},
        headers={"Accept": "application/json"},
    ).json()["id"]

    r = client.post(
        f"/home-water/{created}/update",
        data={"gh": "7.5", "kh": "9", "sample_point": "post_neutralizer", "is_lab_test": "1",
              "notes": "diagnostic", "timestamp": "2026-07-01 15:00:00"},
        headers={"Accept": "application/json"},
    )
    assert r.status_code == 200
    with get_db() as conn:
        row = conn.execute(
            "SELECT gh, kh, sample_point, is_lab_test, notes FROM home_water_tests WHERE id=?",
            (created,),
        ).fetchone()
    assert row["gh"] == 7.5
    assert row["kh"] == 9.0
    assert row["sample_point"] == "post_neutralizer"
    assert row["is_lab_test"] == 1
    assert row["notes"] == "diagnostic"


def test_delete_home_water(client):
    created = client.post(
        "/home-water",
        data={"gh": "8", "kh": "10"},
        headers={"Accept": "application/json"},
    ).json()["id"]

    r = client.post(f"/home-water/{created}/delete", headers={"Accept": "application/json"})
    assert r.status_code == 200
    with get_db() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM home_water_tests WHERE id=?", (created,)
        ).fetchone()[0]
    assert count == 0


def test_delete_missing_404(client):
    r = client.post("/home-water/99999/delete", headers={"Accept": "application/json"})
    assert r.status_code == 404


def test_list_shows_readings(client):
    client.post(
        "/home-water",
        data={"gh": "8", "kh": "10", "sample_point": "tap"},
        headers={"Accept": "application/json"},
    )
    r = client.get("/home-water")
    assert r.status_code == 200
    assert "8" in r.text
    assert "10" in r.text
    assert "Tap" in r.text or "tap" in r.text.lower()


def test_home_water_not_tank_scoped(client, tank_id):
    """Home water rows have no tank_id and survive independent of tanks."""
    client.post(
        "/home-water",
        data={"gh": "8", "kh": "10"},
        headers={"Accept": "application/json"},
    )
    with get_db() as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(home_water_tests)").fetchall()}
        assert "tank_id" not in cols
        count = conn.execute("SELECT COUNT(*) FROM home_water_tests").fetchone()[0]
    assert count == 1

    # Deleting the tank must not touch home water
    with get_db() as conn:
        name = conn.execute("SELECT name FROM tanks WHERE id=?", (tank_id,)).fetchone()[0]
    client.post(
        f"/tanks/{tank_id}/delete",
        data={"confirmation": name},
        follow_redirects=False,
    )
    with get_db() as conn:
        count = conn.execute("SELECT COUNT(*) FROM home_water_tests").fetchone()[0]
    assert count == 1


def test_dashboard_shows_latest_tap_home_water(client, tank_id):
    client.post(
        "/home-water",
        data={"gh": "8.5", "kh": "11", "sample_point": "tap", "timestamp": "2026-07-20 12:00:00"},
        headers={"Accept": "application/json"},
    )
    # Newer raw sample should NOT replace the dashboard WC-source card
    client.post(
        "/home-water",
        data={"gh": "14", "kh": "16", "sample_point": "raw", "timestamp": "2026-07-21 12:00:00"},
        headers={"Accept": "application/json"},
    )
    r = client.get(f"/tanks/{tank_id}")
    assert r.status_code == 200
    assert "Home Water" in r.text
    assert "8.5" in r.text
    assert "11" in r.text
    # Featured card is tap GH/KH, not the newer raw-only reading
    assert "Home Water (tap)" in r.text


def test_latest_wc_source_prefers_tap(client):
    from routers.home_water import latest_wc_source_test

    client.post(
        "/home-water",
        data={"gh": "8", "kh": "10", "sample_point": "tap", "timestamp": "2026-07-01 12:00:00"},
        headers={"Accept": "application/json"},
    )
    client.post(
        "/home-water",
        data={"gh": "3", "kh": "2", "sample_point": "post_neutralizer",
              "timestamp": "2026-07-15 12:00:00"},
        headers={"Accept": "application/json"},
    )
    with get_db() as conn:
        latest = latest_wc_source_test(conn)
    assert latest is not None
    assert latest["gh"] == 8.0
    assert (latest["sample_point"] or "tap") == "tap"


def test_redirect_after_add(client):
    r = client.post(
        "/home-water",
        data={"gh": "8", "kh": "10"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/home-water"


def test_coerce_reading_converts_date_only_and_defaults():
    r = _coerce_reading(
        {"timestamp": "2024-02-16", "gh": "8", "nitrate": 39.3, "flags": ["converted"]},
        default_sp="tap",
        default_blend="as_used",
    )
    assert r["timestamp"] == "2024-02-16 12:00:00"
    assert r["gh"] == 8.0
    assert r["nitrate"] == 39.3
    assert r["sample_point"] == "tap"
    assert r["water_blend"] == "as_used"
    assert r["is_lab_test"] == 1
    assert "converted" in r["flags"]


def test_parse_json_object_strips_fences():
    raw = '```json\n{"readings": [{"gh": 1}]}\n```'
    parsed = _parse_json_object(raw)
    assert parsed["readings"][0]["gh"] == 1


def test_bulk_save_lab_readings(client):
    r = client.post(
        "/home-water/bulk",
        json={
            "readings": [
                {
                    "timestamp": "2024-02-16 12:00:00",
                    "gh": 8.0,
                    "kh": 10.0,
                    "nitrate": 39.3,
                    "sample_point": "tap",
                    "water_blend": "as_used",
                    "is_lab_test": 1,
                    "notes": "SM 4500-NO3; 8.88 as N converted",
                },
                {
                    "timestamp": "2023-01-10 12:00:00",
                    "gh": 12.0,
                    "kh": 14.0,
                    "sample_point": "raw",
                    "water_blend": "hard",
                    "is_lab_test": 1,
                    "notes": "unfiltered well",
                },
            ]
        },
    )
    assert r.status_code == 201
    assert r.json()["count"] == 2
    with get_db() as conn:
        rows = conn.execute(
            "SELECT sample_point, water_blend, nitrate, is_lab_test FROM home_water_tests ORDER BY timestamp"
        ).fetchall()
    assert len(rows) == 2
    assert rows[0]["sample_point"] == "raw"
    assert rows[0]["water_blend"] == "hard"
    assert rows[1]["nitrate"] == 39.3
    assert rows[1]["is_lab_test"] == 1


def test_extract_lab_csv_mocked(client, monkeypatch):
    import anthropic
    import routers.home_water as hw

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")

    payload = {
        "readings": [{
            "timestamp": "2024-02-16",
            "ph": 7.4,
            "gh": 8.0,
            "kh": 10.0,
            "nitrate": 39.3,
            "notes": "Nitrate as N 8.88 converted to NO3",
            "flags": ["nitrate converted from as-N"],
            "sample_point_guess": None,
            "water_blend_guess": None,
        }],
        "report_meta": {"lab_name": "County Lab", "report_id": "R-99"},
    }

    class _Text:
        type = "text"
        text = json.dumps(payload)

    class _Msg:
        content = [_Text()]
        usage = MagicMock(input_tokens=10, output_tokens=20)
        stop_reason = "end_turn"

    class _Client:
        def __init__(self, *a, **k):
            pass

        class messages:
            @staticmethod
            def create(**kwargs):
                return _Msg()

    monkeypatch.setattr(anthropic, "Anthropic", _Client)

    csv_body = b"Analyte,Result,Units\nNitrate as N,8.88,mg/L\n"
    r = client.post(
        "/home-water/extract",
        data={
            "sample_point": "tap",
            "water_blend": "mixed",
            "user_notes": "normal WC mix",
        },
        files={"file": ("lab.csv", io.BytesIO(csv_body), "text/csv")},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["readings"]) == 1
    reading = body["readings"][0]
    assert reading["sample_point"] == "tap"
    assert reading["water_blend"] == "mixed"
    assert reading["is_lab_test"] == 1
    assert reading["nitrate"] == 39.3
    assert "2024-02-16" in reading["timestamp"]
    assert "County Lab" in (reading["notes"] or "") or "lab.csv" in (reading["notes"] or "")


def test_extract_requires_api_key(client, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    r = client.post(
        "/home-water/extract",
        data={"sample_point": "tap"},
        files={"file": ("lab.csv", io.BytesIO(b"a,b\n1,2\n"), "text/csv")},
    )
    assert r.status_code == 503


def test_update_water_blend(client):
    created = client.post(
        "/home-water",
        data={"gh": "8", "kh": "10", "sample_point": "tap"},
        headers={"Accept": "application/json"},
    ).json()["id"]
    r = client.post(
        f"/home-water/{created}/update",
        data={
            "gh": "8", "kh": "10", "sample_point": "raw",
            "water_blend": "hard", "is_lab_test": "1",
            "timestamp": "2024-01-01 12:00:00",
        },
        headers={"Accept": "application/json"},
    )
    assert r.status_code == 200
    with get_db() as conn:
        row = conn.execute(
            "SELECT sample_point, water_blend, is_lab_test FROM home_water_tests WHERE id=?",
            (created,),
        ).fetchone()
    assert row["sample_point"] == "raw"
    assert row["water_blend"] == "hard"
    assert row["is_lab_test"] == 1


def test_summary_stale_logic_older_backfill_does_not_refresh(client):
    client.post(
        "/home-water",
        data={"gh": "8", "kh": "10", "sample_point": "tap", "timestamp": "2025-01-01 12:00:00"},
        headers={"Accept": "application/json"},
    )
    with get_db() as conn:
        conn.execute(
            """INSERT INTO home_water_summary
               (id, summary_text, raw_outdoor_text, based_on_timestamp, generated_at)
               VALUES (1, 'Current suitability text.', 'Raw outdoor text.',
                       '2025-01-01 12:00:00', datetime('now'))"""
        )
        assert home_water_summary_is_stale(conn) is False
        pre = "2025-01-01 12:00:00"
        conn.execute(
            "INSERT INTO home_water_tests (timestamp, gh, sample_point) VALUES (?,?,?)",
            ("2024-01-01 12:00:00", 5.0, "tap"),
        )
        assert should_refresh_home_water_summary_after_write(
            conn, pre_max_ts=pre, written_ts="2024-01-01 12:00:00",
        ) is False
        assert home_water_summary_is_stale(conn) is False


def test_summary_stale_when_newer_test_added(client):
    client.post(
        "/home-water",
        data={"gh": "8", "sample_point": "tap", "timestamp": "2025-01-01 12:00:00"},
        headers={"Accept": "application/json"},
    )
    with get_db() as conn:
        conn.execute(
            """INSERT INTO home_water_summary
               (id, summary_text, based_on_timestamp)
               VALUES (1, 'old', '2025-01-01 12:00:00')"""
        )
        pre = "2025-01-01 12:00:00"
        conn.execute(
            "INSERT INTO home_water_tests (timestamp, gh, sample_point) VALUES (?,?,?)",
            ("2026-06-01 12:00:00", 7.0, "tap"),
        )
        assert home_water_summary_is_stale(conn) is True
        assert should_refresh_home_water_summary_after_write(
            conn, pre_max_ts=pre, written_ts="2026-06-01 12:00:00",
        ) is True


def test_page_shows_saved_summary(client):
    client.post(
        "/home-water",
        data={"gh": "8", "sample_point": "tap", "timestamp": "2025-06-01 12:00:00"},
        headers={"Accept": "application/json"},
    )
    with get_db() as conn:
        conn.execute(
            """INSERT INTO home_water_summary
               (id, summary_text, raw_outdoor_text, based_on_timestamp, based_on_raw_timestamp)
               VALUES (1, 'Shrimp tank WC source looks fine for targets.',
                       'Raw well is acceptable for horse trough water.',
                       '2025-06-01 12:00:00', '2025-04-01 12:00:00')"""
        )
    r = client.get("/home-water")
    assert r.status_code == 200
    assert "Suitability (WC source)" in r.text
    assert "Shrimp tank WC source looks fine" in r.text
    assert "Raw well — horses" in r.text
    assert "horse trough" in r.text
    assert "regenerates only when a newer-dated test" in r.text


def test_parse_summary_sections_markers():
    wc, raw = _parse_summary_sections(
        "=== WC_SOURCE ===\nGood for shrimp tanks.\n\n=== RAW_OUTDOOR ===\nOK for horses.\n"
    )
    assert "shrimp" in wc
    assert "horses" in raw


def test_parse_summary_sections_raw_first():
    """Model is asked to emit RAW before WC so horses aren't truncated."""
    wc, raw = _parse_summary_sections(
        "=== RAW_OUTDOOR ===\nFine for horse troughs.\n\n=== WC_SOURCE ===\nGood for shrimp.\n"
    )
    assert "horse troughs" in raw
    assert "shrimp" in wc
    assert "horse" not in wc
    assert "shrimp" not in raw


def test_parse_summary_sections_json_fallback():
    wc, raw = _parse_summary_sections(json.dumps({
        "summary_text": "WC fine",
        "raw_outdoor_text": "Raw fine",
    }))
    assert wc == "WC fine"
    assert raw == "Raw fine"


def test_build_home_water_summary_prompt_covers_tanks_drinking_raw():
    prompt = build_home_water_summary_prompt(
        [{
            "name": "Shrimp Tank", "water_type": "fresh", "volume_gallons": 5,
            "notes": "KH ~10 accepted", "inhabitants": "  12x Neocaridina",
        }],
        {
            "timestamp": "2025-02-04 12:00:00", "gh": 0.1, "kh": 6.4, "ph": 7.4,
            "nitrate": 39.2, "nitrite": 0.0, "sample_point": "tap", "is_lab_test": 1,
            "water_blend": "as_used", "notes": "lab",
            "ammonia": None, "tds": None, "temp": None,
        },
        [],
        {
            "timestamp": "2026-04-20 12:00:00", "gh": 5.0, "kh": 1.6, "ph": 6.8,
            "nitrate": 40.9, "nitrite": 0.0, "sample_point": "raw", "is_lab_test": 1,
            "water_blend": "hard", "notes": "raw",
            "ammonia": None, "tds": None, "temp": None,
        },
    )
    assert "Do NOT restate" in prompt
    assert "Shrimp Tank" in prompt
    assert "drinking" in prompt.lower()
    assert "horse" in prompt.lower()
    assert "raw" in prompt.lower()


def test_run_home_water_summary_persists(client, monkeypatch):
    import anthropic
    import routers.home_water as hw

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
    hw._summary_in_flight = False

    client.post(
        "/home-water",
        data={"gh": "8", "kh": "10", "sample_point": "tap", "timestamp": "2025-03-01 12:00:00"},
        headers={"Accept": "application/json"},
    )
    client.post(
        "/home-water",
        data={"gh": "5", "kh": "2", "sample_point": "raw", "timestamp": "2025-04-01 12:00:00"},
        headers={"Accept": "application/json"},
    )

    payload_text = (
        "=== WC_SOURCE ===\n"
        "WC source suits the tanks; drinking water is acceptable with elevated nitrate context.\n\n"
        "=== RAW_OUTDOOR ===\n"
        "Raw well is usable for horses with normal caveats.\n"
    )

    class _Text:
        type = "text"
        text = payload_text

    class _Msg:
        content = [_Text()]
        usage = MagicMock(input_tokens=11, output_tokens=22)

    class _Client:
        def __init__(self, *a, **k):
            pass

        class messages:
            @staticmethod
            def create(**kwargs):
                return _Msg()

    monkeypatch.setattr(anthropic, "Anthropic", _Client)

    import asyncio
    asyncio.run(run_home_water_summary(force=True))

    with get_db() as conn:
        row = conn.execute("SELECT * FROM home_water_summary WHERE id=1").fetchone()
    assert row is not None
    assert "suits the tanks" in row["summary_text"]
    assert "horses" in row["raw_outdoor_text"]
    assert row["based_on_timestamp"] is not None
