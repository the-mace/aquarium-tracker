"""Cultures (live-food stations) — not tanks."""
from datetime import date, timedelta

import routers.ai_analysis as _ai
from database import get_db


JSON = {"Accept": "application/json"}


def _create_culture(client, name="Live Food", **extra):
    data = {"name": name, **extra}
    r = client.post("/cultures", data=data, headers=JSON)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _add_vessel(client, culture_id, name, **extra):
    data = {"name": name, **extra}
    r = client.post(f"/cultures/{culture_id}/vessels", data=data, headers=JSON)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_cultures_list_empty(client):
    r = client.get("/cultures")
    assert r.status_code == 200
    assert "No culture stations yet" in r.text
    assert "Add a culture station" in r.text


def test_create_culture_and_vessels_render(client):
    cid = _create_culture(client, "Daphnia", kind="daphnia")
    _add_vessel(client, cid, "Left")
    _add_vessel(client, cid, "Right")

    r = client.get("/cultures")
    assert r.status_code == 200
    assert "Daphnia" in r.text
    assert "Left" in r.text
    assert "Right" in r.text

    r = client.get(f"/cultures/{cid}")
    assert r.status_code == 200
    assert "Left" in r.text
    assert "Right" in r.text
    assert 'name="role"' not in r.text


def test_bin_role_follows_culture_kind(client):
    cid = _create_culture(client, name="Green", kind="green_water")
    r = client.post(
        f"/cultures/{cid}/vessels",
        data={"name": "A", "role": "daphnia"},
        headers=JSON,
    )
    assert r.status_code == 201
    with get_db() as conn:
        row = dict(conn.execute(
            "SELECT role FROM culture_vessels WHERE id=?", (r.json()["id"],)
        ).fetchone())
    assert row["role"] == "green_water"


def test_log_feed_tags_two_vessels(client):
    cid = _create_culture(client)
    v1 = _add_vessel(client, cid, "Daphnia 1")
    v2 = _add_vessel(client, cid, "Daphnia 2")
    r = client.post(
        f"/cultures/{cid}/log",
        data={
            "kind": "feed",
            "food": "spirulina",
            "amount_text": "~1/32 tsp",
            "vessel_ids": [str(v1), str(v2)],
        },
        headers=JSON,
    )
    assert r.status_code == 201, r.text
    log_id = r.json()["id"]

    with get_db() as conn:
        row = conn.execute("SELECT kind, food, amount_text FROM culture_log WHERE id=?", (log_id,)).fetchone()
        assert dict(row)["kind"] == "feed"
        assert dict(row)["food"] == "spirulina"
        tagged = [r[0] for r in conn.execute(
            "SELECT vessel_id FROM culture_log_vessels WHERE log_id=? ORDER BY vessel_id", (log_id,)
        ).fetchall()]
        assert tagged == sorted([v1, v2])

    r = client.get(f"/cultures/{cid}")
    assert "spirulina" in r.text.lower() or "Spirulina" in r.text
    assert "~1/32 tsp" in r.text
    assert "Daphnia 1" in r.text and "Daphnia 2" in r.text


def test_log_look_with_tint_and_density(client):
    cid = _create_culture(client)
    v = _add_vessel(client, cid, "Green A")
    r = client.post(
        f"/cultures/{cid}/log",
        data={"kind": "look", "tint": "clear", "density": "thin", "vessel_ids": [str(v)]},
        headers=JSON,
    )
    assert r.status_code == 201
    with get_db() as conn:
        row = dict(conn.execute("SELECT kind, tint, density FROM culture_log WHERE id=?", (r.json()["id"],)).fetchone())
    assert row["kind"] == "look"
    assert row["tint"] == "clear"
    assert row["density"] == "thin"
    page = client.get(f"/cultures/{cid}")
    assert "tint Clear" in page.text or "Clear" in page.text
    assert "Thin" in page.text


def test_harvest_logs_tank_feeding_without_ai(client, make_tank, monkeypatch):
    called = []
    monkeypatch.setattr(_ai, "run_ai_analysis", lambda *a, **kw: called.append(a))

    tid = make_tank(name="Fish Tank")
    cid = _create_culture(client, kind="daphnia", destination=f"tank:{tid}")
    v = _add_vessel(client, cid, "Daphnia right")
    r = client.post(
        f"/cultures/{cid}/log",
        data={
            "kind": "harvest",
            "cups": "1",
            "notes": "first net",
            "vessel_ids": [str(v)],
            "log_on_tank": "1",
        },
        headers=JSON,
    )
    assert r.status_code == 201, r.text
    assert "tank_event_id" in r.json()
    with get_db() as conn:
        ev = dict(conn.execute(
            "SELECT event_type, notes, tank_id FROM events WHERE id=?",
            (r.json()["tank_event_id"],),
        ).fetchone())
    assert ev["event_type"] == "feeding"
    assert ev["tank_id"] == tid
    assert "Daphnia right" in ev["notes"]
    assert "1 cup" in ev["notes"]
    assert "first net" in ev["notes"]
    assert called == []
    with get_db() as conn:
        log = dict(conn.execute(
            "SELECT amount_text FROM culture_log WHERE id=?", (r.json()["id"],)
        ).fetchone())
    assert log["amount_text"] == "1 cup"


def test_harvest_without_checkbox_skips_tank_event(client, make_tank, monkeypatch):
    called = []
    monkeypatch.setattr(_ai, "run_ai_analysis", lambda *a, **kw: called.append(a))
    tid = make_tank(name="Fish Tank")
    cid = _create_culture(client, kind="daphnia", destination=f"tank:{tid}")
    _add_vessel(client, cid, "Daphnia 1")
    r = client.post(
        f"/cultures/{cid}/log",
        data={"kind": "harvest", "notes": "not yet"},
        headers=JSON,
    )
    assert r.status_code == 201
    assert "tank_event_id" not in r.json()
    with get_db() as conn:
        n = conn.execute("SELECT COUNT(*) AS n FROM events WHERE tank_id=?", (tid,)).fetchone()["n"]
    assert n == 0
    assert called == []


def test_schedule_mark_done_writes_log_and_next_due(client):
    cid = _create_culture(client)
    v = _add_vessel(client, cid, "Daphnia 1")
    r = client.post(
        f"/cultures/{cid}/schedule",
        data={
            "category": "feeding",
            "description": "Feed Daphnia",
            "tracking_mode": "logged",
            "interval_days": "1",
        },
        headers=JSON,
    )
    assert r.status_code == 201
    sch_id = r.json()["id"]
    r = client.post(
        f"/cultures/{cid}/schedule/{sch_id}/mark-done",
        headers=JSON,
    )
    assert r.status_code == 200
    today = date.today().isoformat()
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    with get_db() as conn:
        sched = dict(conn.execute("SELECT last_done, next_due FROM culture_schedule WHERE id=?", (sch_id,)).fetchone())
        logs = conn.execute(
            "SELECT kind, notes FROM culture_log WHERE culture_id=? ORDER BY id DESC LIMIT 1", (cid,)
        ).fetchone()
        tagged = [row[0] for row in conn.execute(
            """SELECT lv.vessel_id FROM culture_log_vessels lv
               JOIN culture_log l ON l.id = lv.log_id
               WHERE l.culture_id=?""",
            (cid,),
        ).fetchall()]
    assert sched["last_done"] == today
    assert sched["next_due"] == tomorrow
    assert dict(logs)["kind"] == "feed"
    assert tagged == [v]


def test_today_shows_due_culture_item_and_mark_done(client):
    cid = _create_culture(client, "Live Food")
    _add_vessel(client, cid, "Daphnia 1")
    r = client.post(
        f"/cultures/{cid}/schedule",
        data={"category": "feeding", "description": "Feed Daphnia", "tracking_mode": "logged", "interval_days": "1"},
        headers=JSON,
    )
    sch_id = r.json()["id"]
    page = client.get("/today")
    assert page.status_code == 200
    assert "Live Food" in page.text
    assert "Feed Daphnia" in page.text
    assert "Cultures" in page.text

    r = client.post(
        f"/cultures/{cid}/schedule/{sch_id}/mark-done",
        data={"return_to": "today"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/today"
    page = client.get("/today")
    assert "done today" in page.text
    assert "Feed Daphnia" in page.text


def test_delete_culture_cascades(client):
    cid = _create_culture(client)
    v = _add_vessel(client, cid, "Daphnia 1")
    client.post(
        f"/cultures/{cid}/log",
        data={"kind": "look", "vessel_ids": [str(v)]},
        headers=JSON,
    )
    client.post(
        f"/cultures/{cid}/schedule",
        data={"category": "look", "description": "Check tint", "tracking_mode": "reference_only"},
        headers=JSON,
    )
    r = client.post(f"/cultures/{cid}/delete", headers=JSON)
    assert r.status_code == 200
    with get_db() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM cultures").fetchone()["n"] == 0
        assert conn.execute("SELECT COUNT(*) AS n FROM culture_vessels").fetchone()["n"] == 0
        assert conn.execute("SELECT COUNT(*) AS n FROM culture_log").fetchone()["n"] == 0
        assert conn.execute("SELECT COUNT(*) AS n FROM culture_log_vessels").fetchone()["n"] == 0
        assert conn.execute("SELECT COUNT(*) AS n FROM culture_schedule").fetchone()["n"] == 0


def test_delete_consumer_tank_nulls_culture_link(client, make_tank):
    tid = make_tank(name="Fish Tank")
    cid = _create_culture(client, kind="daphnia", destination=f"tank:{tid}")
    r = client.post(f"/tanks/{tid}/delete", data={"confirmation": "Fish Tank"}, follow_redirects=False)
    assert r.status_code == 303
    with get_db() as conn:
        row = dict(conn.execute("SELECT id, consumer_tank_id FROM cultures WHERE id=?", (cid,)).fetchone())
    assert row["id"] == cid
    assert row["consumer_tank_id"] is None


def test_crashed_vessel_in_history_not_default_feed_checked(client):
    cid = _create_culture(client)
    live = _add_vessel(client, cid, "Daphnia live")
    crashed = _add_vessel(client, cid, "Daphnia crashed", status="crashed")
    client.post(
        f"/cultures/{cid}/log",
        data={"kind": "look", "notes": "this bin crashed", "vessel_ids": [str(crashed)]},
        headers=JSON,
    )
    r = client.get(f"/cultures/{cid}")
    assert r.status_code == 200
    assert "Daphnia crashed" in r.text
    assert "this bin crashed" in r.text
    # Feed modal: live daphnia checked, crashed not.
    feed_start = r.text.find('id="modal-feed"')
    feed_end = r.text.find('id="modal-look"', feed_start)
    feed_html = r.text[feed_start:feed_end if feed_end > 0 else feed_start + 8000]
    assert f'value="{live}"' in feed_html
    assert f'value="{crashed}"' in feed_html
    assert f'value="{live}" checked' in feed_html or f'value="{live}"  checked' in feed_html
    crashed_idx = feed_html.find(f'value="{crashed}"')
    snippet = feed_html[crashed_idx:crashed_idx + 40]
    assert "checked" not in snippet


def test_green_water_harvest_feeds_daphnia_culture_not_tank(client, make_tank, monkeypatch):
    called = []
    monkeypatch.setattr(_ai, "run_ai_analysis", lambda *a, **kw: called.append(a))
    tid = make_tank(name="Fish Tank")
    daph_cid = _create_culture(client, name="Daphnia", kind="daphnia", destination=f"tank:{tid}")
    daph = _add_vessel(client, daph_cid, "Daphnia 1")
    green_cid = _create_culture(
        client, name="Green water", kind="green_water", destination=f"culture:{daph_cid}"
    )
    green = _add_vessel(client, green_cid, "Green A")
    r = client.post(
        f"/cultures/{green_cid}/log",
        data={"kind": "harvest", "cups": "0.5", "vessel_ids": [str(green)], "log_on_tank": "1"},
        headers=JSON,
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert "tank_event_id" not in body
    assert "feed_log_id" in body
    with get_db() as conn:
        harvest = dict(conn.execute(
            "SELECT kind, amount_text, culture_id FROM culture_log WHERE id=?", (body["id"],)
        ).fetchone())
        feed = dict(conn.execute(
            "SELECT kind, food, amount_text, culture_id FROM culture_log WHERE id=?",
            (body["feed_log_id"],),
        ).fetchone())
        tagged = [row[0] for row in conn.execute(
            "SELECT vessel_id FROM culture_log_vessels WHERE log_id=?", (body["feed_log_id"],)
        ).fetchall()]
        n_events = conn.execute("SELECT COUNT(*) AS n FROM events WHERE tank_id=?", (tid,)).fetchone()["n"]
    assert harvest["kind"] == "harvest"
    assert harvest["culture_id"] == green_cid
    assert harvest["amount_text"] == "0.5 cups"
    assert feed["kind"] == "feed"
    assert feed["food"] == "green_water"
    assert feed["culture_id"] == daph_cid
    assert feed["amount_text"] == "0.5 cups"
    assert tagged == [daph]
    assert n_events == 0
    assert called == []

    page = client.get(f"/cultures/{green_cid}")
    assert "Log feeding" not in page.text
    assert "→ Daphnia" in page.text


def test_schedule_edit_next_due(client):
    cid = _create_culture(client)
    r = client.post(
        f"/cultures/{cid}/schedule",
        data={
            "category": "feeding",
            "description": "Feed Daphnia",
            "tracking_mode": "logged",
            "interval_days": "1",
        },
        headers=JSON,
    )
    sch_id = r.json()["id"]
    r = client.post(
        f"/cultures/{cid}/schedule/{sch_id}/update",
        data={
            "category": "feeding",
            "description": "Feed Daphnia",
            "tracking_mode": "logged",
            "interval_days": "1",
            "last_done": "2026-08-20",
            "next_due": "2026-08-24",
        },
        headers=JSON,
    )
    assert r.status_code == 200
    with get_db() as conn:
        row = dict(conn.execute(
            "SELECT last_done, next_due FROM culture_schedule WHERE id=?", (sch_id,)
        ).fetchone())
    assert row["last_done"] == "2026-08-20"
    assert row["next_due"] == "2026-08-24"

    r = client.post(
        f"/cultures/{cid}/schedule/{sch_id}/update",
        data={
            "category": "feeding",
            "description": "Feed Daphnia",
            "tracking_mode": "logged",
            "interval_days": "2",
            "last_done": "2026-08-21",
            "next_due": "",
        },
        headers=JSON,
    )
    assert r.status_code == 200
    with get_db() as conn:
        row = dict(conn.execute(
            "SELECT last_done, next_due, interval_days FROM culture_schedule WHERE id=?", (sch_id,)
        ).fetchone())
    assert row["last_done"] == "2026-08-21"
    assert row["next_due"] == "2026-08-23"
    assert row["interval_days"] == 2

    page = client.get(f"/cultures/{cid}")
    assert "Last done" in page.text
    assert "Next due" in page.text
    assert 'name="cups"' in page.text


def test_new_culture_form_destination_not_daphnia_prompt(client):
    r = client.get("/cultures/new")
    assert r.status_code == 200
    assert "Destination" in r.text
    assert "Daphnia go to" not in r.text
    assert "Kind" in r.text


def test_delete_destination_culture_nulls_link(client):
    dest = _create_culture(client, name="Daphnia", kind="daphnia")
    src = _create_culture(client, name="Green water", kind="green_water", destination=f"culture:{dest}")
    client.post(f"/cultures/{dest}/delete", headers=JSON)
    with get_db() as conn:
        row = dict(conn.execute(
            "SELECT destination_culture_id FROM cultures WHERE id=?", (src,)
        ).fetchone())
    assert row["destination_culture_id"] is None


def test_green_water_look_has_tint_not_guts(client):
    cid = _create_culture(client, name="Green", kind="green_water")
    _add_vessel(client, cid, "Left")
    page = client.get(f"/cultures/{cid}")
    assert "Guts —" not in page.text
    assert 'name="guts"' not in page.text
    assert "Tint —" in page.text


def test_look_hitchhikers_update_bin_overview(client):
    cid = _create_culture(client, kind="daphnia")
    left = _add_vessel(client, cid, "Left")
    right = _add_vessel(
        client, cid, "Right",
        hitchhikers="1 transparent baby shrimp — leave in bin",
    )
    r = client.post(
        f"/cultures/{cid}/log",
        data={
            "kind": "look",
            "vessel_ids": [str(left), str(right)],
            f"density_{right}": "thin",
            f"hitchhikers_{right}": "1 ramshorn (baby) snail — leave in bin",
            f"notes_{right}": "one ramshorn in the right tub",
        },
        headers=JSON,
    )
    assert r.status_code == 201
    with get_db() as conn:
        by_id = {
            row["id"]: dict(row)
            for row in conn.execute(
                "SELECT id, hitchhikers FROM culture_vessels WHERE culture_id=?", (cid,)
            ).fetchall()
        }
        note = dict(conn.execute(
            "SELECT notes FROM culture_log_vessels WHERE log_id=? AND vessel_id=?",
            (r.json()["id"], right),
        ).fetchone())
    assert by_id[right]["hitchhikers"] == "1 ramshorn (baby) snail — leave in bin"
    assert by_id[left]["hitchhikers"] is None
    assert note["notes"] == "one ramshorn in the right tub"

    page = client.get(f"/cultures/{cid}")
    assert "1 ramshorn (baby) snail — leave in bin" in page.text
    assert "one ramshorn in the right tub" in page.text
    assert "transparent baby shrimp" not in page.text


def test_look_without_hitchhikers_field_leaves_existing(client):
    cid = _create_culture(client, kind="daphnia")
    v = _add_vessel(client, cid, "Right", hitchhikers="1 baby shrimp — leave in bin")
    r = client.post(
        f"/cultures/{cid}/log",
        data={"kind": "look", "vessel_ids": [str(v)], f"density_{v}": "ok"},
        headers=JSON,
    )
    assert r.status_code == 201
    with get_db() as conn:
        row = dict(conn.execute(
            "SELECT hitchhikers FROM culture_vessels WHERE id=?", (v,)
        ).fetchone())
    assert row["hitchhikers"] == "1 baby shrimp — leave in bin"


def test_look_empty_hitchhikers_clears_bin(client):
    cid = _create_culture(client, kind="daphnia")
    v = _add_vessel(client, cid, "Right", hitchhikers="1 baby shrimp")
    r = client.post(
        f"/cultures/{cid}/log",
        data={
            "kind": "look",
            "vessel_ids": [str(v)],
            f"hitchhikers_{v}": "",
        },
        headers=JSON,
    )
    assert r.status_code == 201
    with get_db() as conn:
        row = dict(conn.execute(
            "SELECT hitchhikers FROM culture_vessels WHERE id=?", (v,)
        ).fetchone())
    assert row["hitchhikers"] is None


def test_feed_hitchhikers_update_bin_overview(client):
    cid = _create_culture(client, kind="daphnia")
    v = _add_vessel(client, cid, "Right", hitchhikers="old shrimp")
    r = client.post(
        f"/cultures/{cid}/log",
        data={
            "kind": "feed",
            "food": "spirulina",
            "vessel_ids": [str(v)],
            f"hitchhikers_{v}": "1 ramshorn snail — leave in bin",
            f"notes_{v}": "saw a snail while feeding",
        },
        headers=JSON,
    )
    assert r.status_code == 201
    with get_db() as conn:
        row = dict(conn.execute(
            "SELECT hitchhikers FROM culture_vessels WHERE id=?", (v,)
        ).fetchone())
    assert row["hitchhikers"] == "1 ramshorn snail — leave in bin"
    page = client.get(f"/cultures/{cid}")
    assert "1 ramshorn snail — leave in bin" in page.text
    assert "saw a snail while feeding" in page.text
    assert "old shrimp" not in page.text


def test_crash_log_marks_bin_crashed(client):
    cid = _create_culture(client, kind="daphnia")
    v = _add_vessel(client, cid, "Right")
    r = client.post(
        f"/cultures/{cid}/log",
        data={"kind": "crash", "vessel_ids": [str(v)], "notes": "died off"},
        headers=JSON,
    )
    assert r.status_code == 201
    with get_db() as conn:
        row = dict(conn.execute(
            "SELECT status FROM culture_vessels WHERE id=?", (v,)
        ).fetchone())
    assert row["status"] == "crashed"
    page = client.get(f"/cultures/{cid}")
    assert "crashed" in page.text


def test_temp_log_on_bin_shows_on_card_and_spells_out_humidity(client):
    cid = _create_culture(client, kind="daphnia")
    v = _add_vessel(client, cid, "Left")
    r = client.post(
        f"/cultures/{cid}/log",
        data={
            "kind": "temp",
            "temp_kind": "air",
            "temp_f": "70.5",
            "temp_low": "68.7",
            "temp_high": "73.2",
            "rh": "65",
            "rh_low": "56",
            "rh_high": "66",
            "vessel_ids": [str(v)],
        },
        headers=JSON,
    )
    assert r.status_code == 201
    page = client.get(f"/cultures/{cid}")
    assert "relative humidity" in page.text
    assert "65% relative humidity" in page.text
    assert "70.5°F" in page.text
    assert "% RH" not in page.text


def test_look_per_bin_density_and_guts(client):
    cid = _create_culture(client, kind="daphnia")
    left = _add_vessel(client, cid, "Left")
    right = _add_vessel(client, cid, "Right")
    r = client.post(
        f"/cultures/{cid}/log",
        data={
            "kind": "look",
            "vessel_ids": [str(left), str(right)],
            f"density_{left}": "thin",
            f"guts_{left}": "mixed",
            f"density_{right}": "dense",
            f"guts_{right}": "darker",
        },
        headers=JSON,
    )
    assert r.status_code == 201
    with get_db() as conn:
        rows = conn.execute(
            "SELECT vessel_id, density, guts FROM culture_log_vessels WHERE log_id=? ORDER BY vessel_id",
            (r.json()["id"],),
        ).fetchall()
        by_id = {row["vessel_id"]: dict(row) for row in rows}
    assert by_id[left]["density"] == "thin"
    assert by_id[left]["guts"] == "mixed"
    assert by_id[right]["density"] == "dense"
    assert by_id[right]["guts"] == "darker"
    page = client.get(f"/cultures/{cid}")
    assert "Thin" in page.text and "Dense" in page.text


def test_hold_mark_done_writes_held_look(client):
    cid = _create_culture(client, kind="daphnia")
    _add_vessel(client, cid, "Left")
    r = client.post(
        f"/cultures/{cid}/schedule",
        data={"category": "feeding", "description": "Feed spirulina",
              "tracking_mode": "logged", "interval_days": "1"},
        headers=JSON,
    )
    sch_id = r.json()["id"]
    r = client.post(
        f"/cultures/{cid}/schedule/{sch_id}/mark-done",
        data={"outcome": "held"},
        headers=JSON,
    )
    assert r.status_code == 200
    with get_db() as conn:
        log = dict(conn.execute(
            "SELECT kind, held, notes FROM culture_log WHERE culture_id=? ORDER BY id DESC LIMIT 1",
            (cid,),
        ).fetchone())
        feeds = conn.execute(
            "SELECT COUNT(*) AS n FROM culture_log WHERE culture_id=? AND kind='feed'", (cid,)
        ).fetchone()["n"]
    assert log["kind"] == "look"
    assert log["held"] == 1
    assert feeds == 0


def test_seed_uses_cups(client):
    cid = _create_culture(client, kind="green_water")
    v = _add_vessel(client, cid, "Left")
    r = client.post(
        f"/cultures/{cid}/log",
        data={"kind": "seed", "cups": "1", "vessel_ids": [str(v)]},
        headers=JSON,
    )
    assert r.status_code == 201
    with get_db() as conn:
        row = dict(conn.execute(
            "SELECT amount_text FROM culture_log WHERE id=?", (r.json()["id"],)
        ).fetchone())
    assert row["amount_text"] == "1 cup"


def test_update_log_look_per_bin_notes_and_timestamp(client):
    cid = _create_culture(client, kind="daphnia")
    left = _add_vessel(client, cid, "Left")
    right = _add_vessel(client, cid, "Right")
    r = client.post(
        f"/cultures/{cid}/log",
        data={
            "kind": "look",
            "vessel_ids": [str(left), str(right)],
            f"density_{left}": "thin",
            f"guts_{left}": "mixed",
            "notes": "first look",
            "timestamp": "2026-08-01 12:00:00",
        },
        headers=JSON,
    )
    assert r.status_code == 201
    log_id = r.json()["id"]

    r = client.post(
        f"/cultures/{cid}/log/{log_id}/update",
        data={
            "kind": "look",
            "vessel_ids": [str(left)],
            f"density_{left}": "dense",
            f"guts_{left}": "darker",
            "notes": "corrected look",
            "timestamp": "2026-08-02 15:30:00",
        },
        headers=JSON,
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "updated"

    with get_db() as conn:
        row = dict(conn.execute(
            "SELECT kind, notes, timestamp, density FROM culture_log WHERE id=?",
            (log_id,),
        ).fetchone())
        tagged = [d["vessel_id"] for d in conn.execute(
            """SELECT vessel_id, density, guts FROM culture_log_vessels
               WHERE log_id=? ORDER BY vessel_id""",
            (log_id,),
        ).fetchall()]
        detail = dict(conn.execute(
            "SELECT density, guts FROM culture_log_vessels WHERE log_id=? AND vessel_id=?",
            (log_id, left),
        ).fetchone())
    assert row["kind"] == "look"
    assert row["notes"] == "corrected look"
    assert row["timestamp"] == "2026-08-02 15:30:00"
    assert tagged == [left]
    assert detail["density"] == "dense"
    assert detail["guts"] == "darker"

    page = client.get(f"/cultures/{cid}")
    assert page.status_code == 200
    assert "corrected look" in page.text
    assert "openEditLog(" in page.text
    assert 'id="modal-edit-log"' in page.text
    assert f"/cultures/{cid}/log/{log_id}/update" not in page.text or "Edit" in page.text


def test_update_log_feed_food_and_blank_timestamp_keeps_existing(client):
    cid = _create_culture(client, kind="daphnia")
    v1 = _add_vessel(client, cid, "Left")
    v2 = _add_vessel(client, cid, "Right")
    r = client.post(
        f"/cultures/{cid}/log",
        data={
            "kind": "feed",
            "food": "spirulina",
            "amount_text": "~1/32 tsp",
            "vessel_ids": [str(v1), str(v2)],
            "timestamp": "2026-08-10 09:00:00",
        },
        headers=JSON,
    )
    log_id = r.json()["id"]
    r = client.post(
        f"/cultures/{cid}/log/{log_id}/update",
        data={
            "kind": "feed",
            "food": "yeast",
            "amount_text": "pinch",
            "vessel_ids": [str(v1)],
            f"amount_{v1}": "a pinch each",
        },
        headers=JSON,
    )
    assert r.status_code == 200
    with get_db() as conn:
        row = dict(conn.execute(
            "SELECT food, amount_text, timestamp FROM culture_log WHERE id=?",
            (log_id,),
        ).fetchone())
        tagged = [row[0] for row in conn.execute(
            "SELECT vessel_id FROM culture_log_vessels WHERE log_id=?", (log_id,)
        ).fetchall()]
        amt = dict(conn.execute(
            "SELECT amount_text FROM culture_log_vessels WHERE log_id=? AND vessel_id=?",
            (log_id, v1),
        ).fetchone())
    assert row["food"] == "yeast"
    assert row["amount_text"] == "pinch"
    assert row["timestamp"] == "2026-08-10 09:00:00"
    assert tagged == [v1]
    assert amt["amount_text"] == "a pinch each"


def test_update_feed_held_becomes_look(client):
    cid = _create_culture(client, kind="daphnia")
    v = _add_vessel(client, cid, "Left")
    r = client.post(
        f"/cultures/{cid}/log",
        data={"kind": "feed", "food": "spirulina", "vessel_ids": [str(v)]},
        headers=JSON,
    )
    log_id = r.json()["id"]
    r = client.post(
        f"/cultures/{cid}/log/{log_id}/update",
        data={"kind": "feed", "food": "spirulina", "held": "1", "vessel_ids": [str(v)]},
        headers=JSON,
    )
    assert r.status_code == 200
    with get_db() as conn:
        row = dict(conn.execute(
            "SELECT kind, held, food FROM culture_log WHERE id=?", (log_id,)
        ).fetchone())
    assert row["kind"] == "look"
    assert row["held"] == 1
    assert row["food"] == "spirulina"


def test_update_log_harvest_does_not_create_another_tank_event(client, make_tank, monkeypatch):
    called = []
    monkeypatch.setattr(_ai, "run_ai_analysis", lambda *a, **kw: called.append(a))
    tid = make_tank(name="Fish Tank")
    cid = _create_culture(client, kind="daphnia", destination=f"tank:{tid}")
    v = _add_vessel(client, cid, "Left")
    r = client.post(
        f"/cultures/{cid}/log",
        data={
            "kind": "harvest",
            "cups": "1",
            "notes": "first net",
            "vessel_ids": [str(v)],
            "log_on_tank": "1",
        },
        headers=JSON,
    )
    log_id = r.json()["id"]
    tank_event_id = r.json()["tank_event_id"]
    r = client.post(
        f"/cultures/{cid}/log/{log_id}/update",
        data={"kind": "harvest", "cups": "2", "notes": "first net", "vessel_ids": [str(v)]},
        headers=JSON,
    )
    assert r.status_code == 200
    with get_db() as conn:
        log = dict(conn.execute(
            "SELECT amount_text, notes FROM culture_log WHERE id=?", (log_id,)
        ).fetchone())
        n_events = conn.execute(
            "SELECT COUNT(*) AS n FROM events WHERE tank_id=?", (tid,)
        ).fetchone()["n"]
        ev = dict(conn.execute(
            "SELECT notes FROM events WHERE id=?", (tank_event_id,)
        ).fetchone())
    assert log["amount_text"] == "2 cups"
    assert n_events == 1
    assert "1 cup" in ev["notes"]
    assert called == []


def test_update_log_404_wrong_culture(client):
    a = _create_culture(client, name="A")
    b = _create_culture(client, name="B")
    r = client.post(
        f"/cultures/{a}/log",
        data={"kind": "other", "notes": "misc"},
        headers=JSON,
    )
    log_id = r.json()["id"]
    r = client.post(
        f"/cultures/{b}/log/{log_id}/update",
        data={"kind": "other", "notes": "nope"},
        headers=JSON,
    )
    assert r.status_code == 404
    with get_db() as conn:
        row = dict(conn.execute(
            "SELECT notes FROM culture_log WHERE id=?", (log_id,)
        ).fetchone())
    assert row["notes"] == "misc"


def test_culture_nav_on_pages(client):
    r = client.get("/today")
    assert 'href="/cultures"' in r.text
    r = client.get("/cultures/new")
    assert r.status_code == 200
    assert "New culture" in r.text


def _add_logged_feeding(client, culture_id, description="Feed Daphnia", last_done=None, next_due=None):
    r = client.post(
        f"/cultures/{culture_id}/schedule",
        data={
            "category": "feeding",
            "description": description,
            "tracking_mode": "logged",
            "interval_days": "1",
        },
        headers=JSON,
    )
    assert r.status_code == 201, r.text
    sch_id = r.json()["id"]
    if last_done is not None or next_due is not None:
        data = {
            "category": "feeding",
            "description": description,
            "tracking_mode": "logged",
            "interval_days": "1",
        }
        if last_done is not None:
            data["last_done"] = last_done
        if next_due is not None:
            data["next_due"] = next_due
        r = client.post(
            f"/cultures/{culture_id}/schedule/{sch_id}/update",
            data=data,
            headers=JSON,
        )
        assert r.status_code == 200, r.text
    return sch_id


def test_next_uses_upcoming_schedule_not_harvest_status(client):
    """Harvest readiness is a badge; Next is the coming scheduled feeding."""
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    cid = _create_culture(
        client, name="Live Food", kind="daphnia",
        harvest_status="not_ready",
        next_action="Don't harvest yet",
    )
    _add_logged_feeding(client, cid, last_done=yesterday, next_due=tomorrow)

    page = client.get(f"/cultures/{cid}")
    assert page.status_code == 200
    assert "badge-harvest-not_ready" in page.text
    assert "Next: Feed Daphnia" in page.text
    assert tomorrow in page.text
    assert "Next: Don" not in page.text

    listing = client.get("/cultures")
    assert "Next: Feed Daphnia" in listing.text
    assert "Next: Don" not in listing.text

    today = client.get("/today")
    assert today.status_code == 200
    assert "Live Food" in today.text
    assert "Feed Daphnia" in today.text
    assert "harvest" not in today.text.lower()
    assert "badge-warning" in today.text


def test_harvest_status_alone_is_not_next(client):
    cid = _create_culture(
        client, name="Live Food", kind="daphnia",
        harvest_status="not_ready",
        next_action="Don't harvest yet",
    )
    page = client.get(f"/cultures/{cid}")
    assert "badge-harvest-not_ready" in page.text
    assert "Next: Don" not in page.text
    assert "culture-next-action" not in page.text
    listing = client.get("/cultures")
    assert "culture-next-action" not in listing.text
    today = client.get("/today")
    assert "Live Food" not in today.text


def test_next_prefers_sooner_one_off_over_later_schedule(client):
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    later = (date.today() + timedelta(days=3)).isoformat()
    cid = _create_culture(
        client, name="Live Food",
        next_action="mag-scraper swish if still clear",
        next_action_date=tomorrow,
    )
    _add_logged_feeding(client, cid, next_due=later)
    page = client.get(f"/cultures/{cid}")
    assert "Next: mag-scraper swish if still clear" in page.text
    assert "Next: Feed Daphnia" not in page.text


def test_due_today_feeding_is_due_not_next(client):
    cid = _create_culture(client, name="Live Food")
    _add_logged_feeding(client, cid)  # no next_due → "not yet done" belongs in Due
    page = client.get(f"/cultures/{cid}")
    assert "Feed Daphnia" in page.text
    assert "not yet done" in page.text
    assert "Next: Feed Daphnia" not in page.text
