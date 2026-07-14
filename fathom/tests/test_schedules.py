from datetime import date, timedelta


def test_schedule_page_empty(client, tank_id):
    r = client.get(f"/tanks/{tank_id}/schedule")
    assert r.status_code == 200
    assert "Schedule" in r.text


def test_add_feeding_reference(client, tank_id):
    r = client.post(
        f"/tanks/{tank_id}/schedule",
        data={"category": "feeding", "day_of_week": "mon", "description": "Flakes"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    r = client.get(f"/tanks/{tank_id}/schedule")
    assert "Flakes" in r.text
    assert "Monday" in r.text


def test_add_dosing_no_dow(client, tank_id):
    r = client.post(
        f"/tanks/{tank_id}/schedule",
        data={"category": "dosing", "day_of_week": "", "description": "5ml Flourish weekly"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    r = client.get(f"/tanks/{tank_id}/schedule")
    assert "5ml Flourish weekly" in r.text


def test_add_maintenance_with_interval(client, tank_id):
    r = client.post(
        f"/tanks/{tank_id}/schedule",
        data={"category": "maintenance", "description": "Clean pre-filter", "interval_days": "30"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    r = client.get(f"/tanks/{tank_id}/schedule")
    assert "Clean pre-filter" in r.text
    assert "30" in r.text


def test_delete_schedule(client, tank_id):
    client.post(
        f"/tanks/{tank_id}/schedule",
        data={"category": "feeding", "day_of_week": "tue", "description": "To delete"},
        follow_redirects=False,
    )
    r = client.get(f"/tanks/{tank_id}/schedule")
    assert "To delete" in r.text

    import re
    match = re.search(r"/tanks/\d+/schedule/(\d+)/delete", r.text)
    assert match, "delete link not found"
    sch_id = match.group(1)

    r = client.post(f"/tanks/{tank_id}/schedule/{sch_id}/delete", follow_redirects=False)
    assert r.status_code == 303
    r = client.get(f"/tanks/{tank_id}/schedule")
    assert "To delete" not in r.text


def test_add_schedule_logs_timeline_event(client, tank_id):
    r = client.post(
        f"/tanks/{tank_id}/schedule",
        data={"category": "feeding", "day_of_week": "tue", "description": "Shrimp Cuisine"},
        follow_redirects=False,
    )
    assert r.status_code == 303

    r = client.get(f"/tanks/{tank_id}/events", headers={"Accept": "application/json"})
    events = r.json()["events"]
    other = [e for e in events if e["event_type"] == "other"]
    assert len(other) == 1
    assert "Schedule added" in other[0]["notes"]
    assert "Shrimp Cuisine" in other[0]["notes"]
    assert "Tuesday" in other[0]["notes"]

    r = client.get(f"/tanks/{tank_id}/timeline")
    assert "Shrimp Cuisine" in r.text


def test_delete_schedule_logs_timeline_event(client, tank_id):
    client.post(
        f"/tanks/{tank_id}/schedule",
        data={"category": "feeding", "day_of_week": "tue", "description": "PM feeding"},
        follow_redirects=False,
    )
    import database as _db
    with _db.get_db() as conn:
        row = conn.execute(
            "SELECT id FROM recurring_schedule WHERE tank_id=? AND description='PM feeding'",
            (tank_id,),
        ).fetchone()
    sch_id = row[0]

    r = client.post(f"/tanks/{tank_id}/schedule/{sch_id}/delete", follow_redirects=False)
    assert r.status_code == 303

    r = client.get(f"/tanks/{tank_id}/events", headers={"Accept": "application/json"})
    events = r.json()["events"]
    other = [e for e in events if e["event_type"] == "other"]
    assert len(other) == 2  # the initial "Schedule added" plus this "Schedule removed"
    removed = [e for e in other if "removed" in e["notes"]]
    assert len(removed) == 1
    assert "PM feeding" in removed[0]["notes"]
    assert "Tuesday" in removed[0]["notes"]
    assert removed[0]["schedule_id"] is None  # ON DELETE SET NULL once the schedule row is gone

    r = client.get(f"/tanks/{tank_id}/timeline")
    assert "PM feeding" in r.text


def test_update_schedule(client, tank_id):
    client.post(
        f"/tanks/{tank_id}/schedule",
        data={"category": "feeding", "day_of_week": "wed", "description": "Original"},
        follow_redirects=False,
    )
    r = client.get(f"/tanks/{tank_id}/schedule")
    import re
    match = re.search(r"openEditSched\((\{.*?\})\)", r.text)
    assert match
    import json
    s = json.loads(match.group(1))
    sch_id = s["id"]

    r = client.post(
        f"/tanks/{tank_id}/schedule/{sch_id}/update",
        data={"category": "feeding", "day_of_week": "wed", "description": "Updated", "is_active": "1"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    r = client.get(f"/tanks/{tank_id}/schedule")
    assert "Updated" in r.text
    assert "Original" not in r.text


def test_update_schedule_logs_timeline_event(client, tank_id):
    client.post(
        f"/tanks/{tank_id}/schedule",
        data={"category": "feeding", "day_of_week": "mon", "description": "Flakes"},
        follow_redirects=False,
    )
    import database as _db
    with _db.get_db() as conn:
        row = conn.execute(
            "SELECT id FROM recurring_schedule WHERE tank_id=? AND description='Flakes'",
            (tank_id,),
        ).fetchone()
    sch_id = row[0]

    r = client.post(
        f"/tanks/{tank_id}/schedule/{sch_id}/update",
        data={"category": "feeding", "day_of_week": "tue", "description": "Pellets", "is_active": "1"},
        follow_redirects=False,
    )
    assert r.status_code == 303

    r = client.get(f"/tanks/{tank_id}/events", headers={"Accept": "application/json"})
    events = r.json()["events"]
    other = [e for e in events if e["event_type"] == "other"]
    assert len(other) == 2  # the initial "Schedule added" plus this update
    updated = [e for e in other if "updated" in e["notes"]]
    assert len(updated) == 1
    assert "Flakes" in updated[0]["notes"] or "Pellets" in updated[0]["notes"]
    assert "Monday" in updated[0]["notes"] and "Tuesday" in updated[0]["notes"]
    assert updated[0]["schedule_id"] == sch_id

    r = client.get(f"/tanks/{tank_id}/timeline")
    assert "Pellets" in r.text
    assert "Monday" in r.text and "Tuesday" in r.text


def test_update_schedule_no_change_skips_timeline_event(client, tank_id):
    client.post(
        f"/tanks/{tank_id}/schedule",
        data={"category": "feeding", "day_of_week": "mon", "description": "Flakes"},
        follow_redirects=False,
    )
    import database as _db
    with _db.get_db() as conn:
        row = conn.execute(
            "SELECT id FROM recurring_schedule WHERE tank_id=? AND description='Flakes'",
            (tank_id,),
        ).fetchone()
    sch_id = row[0]

    r = client.post(
        f"/tanks/{tank_id}/schedule/{sch_id}/update",
        data={"category": "feeding", "day_of_week": "mon", "description": "Flakes", "is_active": "1"},
        follow_redirects=False,
    )
    assert r.status_code == 303

    r = client.get(f"/tanks/{tank_id}/events", headers={"Accept": "application/json"})
    events = r.json()["events"]
    other = [e for e in events if e["event_type"] == "other"]
    assert len(other) == 1  # only the initial "Schedule added" — the no-op edit logs nothing
    assert "added" in other[0]["notes"]


def test_mark_done_updates_schedule(client, tank_id):
    today = date.today().isoformat()
    expected_next = (date.today() + timedelta(days=30)).isoformat()

    client.post(
        f"/tanks/{tank_id}/schedule",
        data={"category": "maintenance", "description": "Clean filter", "interval_days": "30"},
        follow_redirects=False,
    )
    r = client.get(f"/tanks/{tank_id}/schedule")
    import re
    match = re.search(r"/tanks/\d+/schedule/(\d+)/mark-done", r.text)
    assert match, "mark-done button not found on schedule page"
    sch_id = match.group(1)

    r = client.post(f"/tanks/{tank_id}/schedule/{sch_id}/mark-done", follow_redirects=False)
    assert r.status_code == 303

    r = client.get(f"/tanks/{tank_id}/schedule")
    assert today in r.text
    assert expected_next in r.text


def test_mark_done_creates_event(client, tank_id):
    client.post(
        f"/tanks/{tank_id}/schedule",
        data={"category": "maintenance", "description": "Sponge squeeze", "interval_days": "30"},
        follow_redirects=False,
    )
    r = client.get(f"/tanks/{tank_id}/schedule")
    import re
    match = re.search(r"/tanks/\d+/schedule/(\d+)/mark-done", r.text)
    sch_id = match.group(1)
    client.post(f"/tanks/{tank_id}/schedule/{sch_id}/mark-done", follow_redirects=False)

    r = client.get(f"/tanks/{tank_id}/events", headers={"Accept": "application/json"})
    events = r.json()["events"]
    maint = [e for e in events if e["event_type"] == "maintenance"]
    assert any("Sponge squeeze" in (e.get("notes") or "") for e in maint)


def test_mark_done_no_interval_leaves_next_due_null(client, tank_id):
    client.post(
        f"/tanks/{tank_id}/schedule",
        data={"category": "maintenance", "description": "Manual task"},
        follow_redirects=False,
    )
    r = client.get(f"/tanks/{tank_id}/schedule")
    import re
    match = re.search(r"/tanks/\d+/schedule/(\d+)/mark-done", r.text)
    sch_id = match.group(1)
    client.post(f"/tanks/{tank_id}/schedule/{sch_id}/mark-done", follow_redirects=False)

    r = client.get(f"/tanks/{tank_id}/schedule")
    assert "manual reminder" in r.text


def test_mark_done_redirects_to_schedule_page_when_marked_from_there(client, tank_id):
    client.post(
        f"/tanks/{tank_id}/schedule",
        data={"category": "maintenance", "description": "Clean filter", "interval_days": "30"},
        follow_redirects=False,
    )
    r = client.get(f"/tanks/{tank_id}/schedule")
    import re
    match = re.search(r"/tanks/\d+/schedule/(\d+)/mark-done", r.text)
    sch_id = match.group(1)

    r = client.post(
        f"/tanks/{tank_id}/schedule/{sch_id}/mark-done",
        data={"return_to": "schedule"},
        follow_redirects=False,
    )
    assert r.headers["location"] == f"/tanks/{tank_id}/schedule"


def test_mark_done_redirects_to_dashboard_by_default(client, tank_id):
    client.post(
        f"/tanks/{tank_id}/schedule",
        data={"category": "maintenance", "description": "Clean filter", "interval_days": "30"},
        follow_redirects=False,
    )
    r = client.get(f"/tanks/{tank_id}/schedule")
    import re
    match = re.search(r"/tanks/\d+/schedule/(\d+)/mark-done", r.text)
    sch_id = match.group(1)

    r = client.post(f"/tanks/{tank_id}/schedule/{sch_id}/mark-done", follow_redirects=False)
    assert r.headers["location"] == f"/tanks/{tank_id}"


def test_dashboard_today_schedule_widget(client, tank_id):
    today_dow = date.today().strftime('%a').lower()
    client.post(
        f"/tanks/{tank_id}/schedule",
        data={"category": "feeding", "day_of_week": today_dow, "description": "Today Food"},
        follow_redirects=False,
    )
    r = client.get(f"/tanks/{tank_id}")
    assert r.status_code == 200
    assert "Today Food" in r.text
    assert "Today's Schedule" in r.text


def test_dashboard_today_schedule_excludes_floating_day(client, tank_id):
    client.post(
        f"/tanks/{tank_id}/schedule",
        data={"category": "feeding", "day_of_week": "", "description": "Floating Food"},
        follow_redirects=False,
    )
    r = client.get(f"/tanks/{tank_id}")
    assert r.status_code == 200
    # "Floating Food" now legitimately appears elsewhere on the dashboard (the
    # recent-events panel picks up the "Schedule added" event) — check specifically
    # that it's absent from the Today's Schedule widget's own item markup.
    assert '<div class="sched-today-item">Floating Food</div>' not in r.text


def test_dashboard_maintenance_widget(client, tank_id):
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    client.post(
        f"/tanks/{tank_id}/schedule",
        data={"category": "maintenance", "description": "Overdue task", "interval_days": "30"},
        follow_redirects=False,
    )
    r = client.get(f"/tanks/{tank_id}/schedule")
    import re
    match = re.search(r"openEditSched\((\{.*?\})\)", r.text)
    import json
    s = json.loads(match.group(1))
    sch_id = s["id"]
    # Force next_due to yesterday so widget shows it as overdue
    import database as _db
    with _db.get_db() as conn:
        conn.execute(
            "UPDATE recurring_schedule SET next_due=?, last_done=? WHERE id=?",
            (yesterday, (date.today() - timedelta(days=31)).isoformat(), sch_id),
        )
    r = client.get(f"/tanks/{tank_id}")
    assert "Overdue task" in r.text
    assert "Maintenance" in r.text


def test_404_unknown_schedule(client, tank_id):
    r = client.post(f"/tanks/{tank_id}/schedule/99999/delete", follow_redirects=False)
    assert r.status_code in (303, 404)


def test_schedule_tracking_mode_auto_set(client, tank_id):
    client.post(
        f"/tanks/{tank_id}/schedule",
        data={"category": "maintenance", "description": "Monthly clean", "interval_days": "30"},
        follow_redirects=False,
    )
    import database as _db
    with _db.get_db() as conn:
        row = conn.execute(
            "SELECT tracking_mode FROM recurring_schedule WHERE tank_id=? AND description='Monthly clean'",
            (tank_id,),
        ).fetchone()
    assert row is not None
    assert row[0] == "logged"


def test_feeding_tracking_mode_reference_only(client, tank_id):
    client.post(
        f"/tanks/{tank_id}/schedule",
        data={"category": "feeding", "day_of_week": "fri", "description": "Veggie wafer"},
        follow_redirects=False,
    )
    import database as _db
    with _db.get_db() as conn:
        row = conn.execute(
            "SELECT tracking_mode FROM recurring_schedule WHERE tank_id=? AND description='Veggie wafer'",
            (tank_id,),
        ).fetchone()
    assert row is not None
    assert row[0] == "reference_only"


def test_event_with_schedule_id_updates_next_due(client, tank_id):
    client.post(
        f"/tanks/{tank_id}/schedule",
        data={"category": "maintenance", "description": "Linked maint", "interval_days": "14"},
        follow_redirects=False,
    )
    import database as _db
    with _db.get_db() as conn:
        row = conn.execute(
            "SELECT id FROM recurring_schedule WHERE tank_id=? AND description='Linked maint'",
            (tank_id,),
        ).fetchone()
    sch_id = row[0]

    today = date.today().isoformat()
    expected_next = (date.today() + timedelta(days=14)).isoformat()

    client.post(
        f"/tanks/{tank_id}/events",
        data={"event_type": "maintenance", "notes": "did it", "schedule_id": str(sch_id)},
        headers={"Accept": "application/json"},
    )
    with _db.get_db() as conn:
        sched = dict(conn.execute(
            "SELECT last_done, next_due FROM recurring_schedule WHERE id=?", (sch_id,)
        ).fetchone())
    assert sched["last_done"] == today
    assert sched["next_due"] == expected_next


def test_event_with_schedule_id_snaps_to_day_of_week(client, tank_id):
    dow_names = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    target_dow = dow_names[(date.today().weekday() - 1) % 7]

    client.post(
        f"/tanks/{tank_id}/schedule",
        data={"category": "maintenance", "day_of_week": target_dow, "description": "Linked weekly", "interval_days": "7"},
        follow_redirects=False,
    )
    import database as _db
    with _db.get_db() as conn:
        row = conn.execute(
            "SELECT id FROM recurring_schedule WHERE tank_id=? AND description='Linked weekly'",
            (tank_id,),
        ).fetchone()
    sch_id = row[0]

    client.post(
        f"/tanks/{tank_id}/events",
        data={"event_type": "maintenance", "notes": "did it", "schedule_id": str(sch_id)},
        headers={"Accept": "application/json"},
    )
    expected_next = (date.today() + timedelta(days=6)).isoformat()
    with _db.get_db() as conn:
        sched = dict(conn.execute("SELECT next_due FROM recurring_schedule WHERE id=?", (sch_id,)).fetchone())
    assert sched["next_due"] == expected_next


def test_mark_done_snaps_to_day_of_week_not_flat_interval(client, tank_id):
    # Regression: a task pinned to a weekday (e.g. Thursday) but marked done a day
    # late/early (e.g. Friday) should come due on the *next* occurrence of that
    # weekday, not exactly interval_days after whatever day it was actually done.
    dow_names = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    target_dow = dow_names[(date.today().weekday() - 1) % 7]

    client.post(
        f"/tanks/{tank_id}/schedule",
        data={"category": "maintenance", "day_of_week": target_dow, "description": "Weekly wipe", "interval_days": "7"},
        follow_redirects=False,
    )
    import database as _db
    with _db.get_db() as conn:
        row = conn.execute(
            "SELECT id FROM recurring_schedule WHERE tank_id=? AND description='Weekly wipe'",
            (tank_id,),
        ).fetchone()
    sch_id = row[0]

    client.post(f"/tanks/{tank_id}/schedule/{sch_id}/mark-done", follow_redirects=False)

    expected_next = (date.today() + timedelta(days=6)).isoformat()
    with _db.get_db() as conn:
        sched = dict(conn.execute("SELECT next_due FROM recurring_schedule WHERE id=?", (sch_id,)).fetchone())
    assert sched["next_due"] == expected_next


def test_mark_done_long_interval_with_day_of_week_aligns_after_interval(client, tank_id):
    # Regression: a task pinned to a weekday AND a long interval (e.g. "clean
    # pre-filter every 30 days, on a Thursday") must come due at the *next*
    # occurrence of that weekday on/after last_done + interval_days, not just
    # the next occurrence of the weekday from last_done (which collapses a
    # 30-day interval down to a 7-day one whenever last_done itself already
    # fell on the target weekday).
    client.post(
        f"/tanks/{tank_id}/schedule",
        data={"category": "maintenance", "day_of_week": "thu", "description": "Clean pre-filter", "interval_days": "30"},
        follow_redirects=False,
    )
    import database as _db
    with _db.get_db() as conn:
        row = conn.execute(
            "SELECT id FROM recurring_schedule WHERE tank_id=? AND description='Clean pre-filter'",
            (tank_id,),
        ).fetchone()
    sch_id = row[0]

    r = client.post(
        f"/tanks/{tank_id}/schedule/{sch_id}/update",
        data={
            "category": "maintenance",
            "day_of_week": "thu",
            "description": "Clean pre-filter",
            "interval_days": "30",
            "is_active": "1",
            "last_done": "2026-06-11",  # a Thursday
        },
        follow_redirects=False,
    )
    assert r.status_code == 303

    with _db.get_db() as conn:
        sched = dict(conn.execute("SELECT last_done, next_due FROM recurring_schedule WHERE id=?", (sch_id,)).fetchone())
    assert sched["last_done"] == "2026-06-11"
    assert sched["next_due"] == "2026-07-16"


def test_edit_last_done_recomputes_next_due(client, tank_id):
    client.post(
        f"/tanks/{tank_id}/schedule",
        data={"category": "maintenance", "description": "Filter change", "interval_days": "30"},
        follow_redirects=False,
    )
    import database as _db
    with _db.get_db() as conn:
        row = conn.execute(
            "SELECT id FROM recurring_schedule WHERE tank_id=? AND description='Filter change'",
            (tank_id,),
        ).fetchone()
    sch_id = row[0]

    corrected_date = date.today() - timedelta(days=5)
    expected_next = (corrected_date + timedelta(days=30)).isoformat()

    r = client.post(
        f"/tanks/{tank_id}/schedule/{sch_id}/update",
        data={
            "category": "maintenance",
            "description": "Filter change",
            "interval_days": "30",
            "is_active": "1",
            "last_done": corrected_date.isoformat(),
        },
        follow_redirects=False,
    )
    assert r.status_code == 303

    with _db.get_db() as conn:
        sched = dict(conn.execute("SELECT last_done, next_due FROM recurring_schedule WHERE id=?", (sch_id,)).fetchone())
    assert sched["last_done"] == corrected_date.isoformat()
    assert sched["next_due"] == expected_next


def test_edit_without_last_done_preserves_existing(client, tank_id):
    client.post(
        f"/tanks/{tank_id}/schedule",
        data={"category": "maintenance", "description": "Preserve me", "interval_days": "30"},
        follow_redirects=False,
    )
    import database as _db
    with _db.get_db() as conn:
        row = conn.execute(
            "SELECT id FROM recurring_schedule WHERE tank_id=? AND description='Preserve me'",
            (tank_id,),
        ).fetchone()
    sch_id = row[0]

    client.post(f"/tanks/{tank_id}/schedule/{sch_id}/mark-done", follow_redirects=False)
    with _db.get_db() as conn:
        before = dict(conn.execute("SELECT last_done, next_due FROM recurring_schedule WHERE id=?", (sch_id,)).fetchone())

    r = client.post(
        f"/tanks/{tank_id}/schedule/{sch_id}/update",
        data={"category": "maintenance", "description": "Preserve me", "interval_days": "30", "is_active": "1"},
        follow_redirects=False,
    )
    assert r.status_code == 303

    with _db.get_db() as conn:
        after = dict(conn.execute("SELECT last_done, next_due FROM recurring_schedule WHERE id=?", (sch_id,)).fetchone())
    assert after == before


def test_dashboard_not_yet_done_is_red(client, tank_id):
    client.post(
        f"/tanks/{tank_id}/schedule",
        data={"category": "maintenance", "description": "Never done task", "interval_days": "14"},
        follow_redirects=False,
    )
    r = client.get(f"/tanks/{tank_id}")
    assert "Never done task" in r.text
    import re
    assert re.search(r'maint-due maint-overdue">\s*not yet done', r.text)
