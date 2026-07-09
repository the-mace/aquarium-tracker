import re
from datetime import date, timedelta


def _dow(d=None):
    return (d or date.today()).strftime("%a").lower()


def test_today_page_empty(client):
    r = client.get("/today")
    assert r.status_code == 200
    assert "No active tanks" in r.text


def test_today_shows_tank_card_with_nothing_due(client, tank_id):
    r = client.get("/today")
    assert r.status_code == 200
    assert "Nothing due today" in r.text


def test_today_shows_feeding_scheduled_for_today(client, tank_id):
    client.post(
        f"/tanks/{tank_id}/schedule",
        data={"category": "feeding", "day_of_week": _dow(), "description": "Flakes AM"},
        follow_redirects=False,
    )
    r = client.get("/today")
    assert r.status_code == 200
    assert "Flakes AM" in r.text


def test_today_hides_feeding_scheduled_other_day(client, tank_id):
    other = _dow(date.today() + timedelta(days=1))
    client.post(
        f"/tanks/{tank_id}/schedule",
        data={"category": "feeding", "day_of_week": other, "description": "Not today food"},
        follow_redirects=False,
    )
    r = client.get("/today")
    assert "Not today food" not in r.text


def test_today_shows_overdue_maintenance(client, tank_id):
    client.post(
        f"/tanks/{tank_id}/schedule",
        data={"category": "maintenance", "description": "Clean filter", "interval_days": "30"},
        follow_redirects=False,
    )
    r = client.get("/today")
    assert "Clean filter" in r.text
    assert "not yet done" in r.text


def test_today_mark_done_keeps_card_and_shows_checkmark(client, tank_id):
    client.post(
        f"/tanks/{tank_id}/schedule",
        data={"category": "maintenance", "description": "Water change", "interval_days": "7"},
        follow_redirects=False,
    )
    r = client.get("/today")
    match = re.search(r"/tanks/\d+/schedule/(\d+)/mark-done", r.text)
    assert match, "mark-done form not found on today page"
    sch_id = match.group(1)

    r = client.post(
        f"/tanks/{tank_id}/schedule/{sch_id}/mark-done",
        data={"return_to": "today"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/today"

    r = client.get("/today")
    # Card stays, item stays, but now shown as completed rather than disappearing.
    assert "Water change" in r.text
    assert "done today" in r.text


def test_today_ignores_inactive_tanks(client, tank_id):
    client.post(
        f"/tanks/{tank_id}/schedule",
        data={"category": "feeding", "day_of_week": _dow(), "description": "Inactive tank food"},
        follow_redirects=False,
    )
    client.post(f"/tanks/{tank_id}/edit", data={"name": "Test Tank", "status": "inactive"}, follow_redirects=False)

    r = client.get("/today")
    assert "Inactive tank food" not in r.text
