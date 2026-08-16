"""LifetimeClient tests with a FakeSession (no network).

Payloads here are synthetic but shaped to match the real API (per the parsing logic in
client.py). Capturing real, scrubbed fixtures is a documented prerequisite chore in the
plan; these cover the branch logic in the meantime.
"""

from datetime import date

import pytest
import requests

from lifetime_reserve.api.client import LifetimeClient
from lifetime_reserve.api.errors import BookingIncompleteError
from tests.conftest import FakeResponse, FakeSession


def make_client():
    c = LifetimeClient(session=FakeSession())
    c.token, c.sso_id = "TOK", "SSO"
    return c


# ── login ────────────────────────────────────────────────────────────────────

def test_login_success_sets_tokens():
    s = FakeSession().queue("POST", FakeResponse(
        {"status": "0", "username": "me", "token": "T", "ssoId": "S"}))
    c = LifetimeClient(session=s)
    assert c.login("u", "p") == ("T", "S")
    assert c.token == "T" and c.sso_id == "S"


def test_login_non_zero_status_raises():
    s = FakeSession().queue("POST", FakeResponse({"status": "1", "message": "bad creds"}))
    c = LifetimeClient(session=s)
    with pytest.raises(RuntimeError):
        c.login("u", "p")


def test_login_http_error_raises():
    s = FakeSession().queue("POST", FakeResponse(status_code=500, text="oops"))
    c = LifetimeClient(session=s)
    with pytest.raises(requests.HTTPError):
        c.login("u", "p")


# ── search_courts ────────────────────────────────────────────────────────────

def test_search_courts_params_and_return():
    c = make_client()
    c.session.queue("GET", FakeResponse({"results": {"dayParts": []}}))
    out = c.search_courts("36", "Pickleball: Indoor", date(2026, 8, 10), 60)
    assert out == {"results": {"dayParts": []}}
    _, url, kwargs = c.session.calls[-1]
    assert kwargs["params"]["date"] == "2026-08-10"
    assert kwargs["params"]["clubId"] == "36"
    assert kwargs["headers"]["x-ltf-jwe"] == "TOK"


# ── get_reservations / reserved_dates_and_labels ─────────────────────────────

RESERVATIONS_PAYLOAD = {"results": [
    {"start": "2026-08-10T07:00:00", "regId": "R1",
     "location": "Fairfax | Indoor | Court 03, VA",
     "registration": {"registeredMembers": [
         {"attendeeId": "A1", "cancelCtas": [{"method": "online", "registrationId": "CANCEL1"}]}]}},
    {"start": "2026-08-04T06:30:00", "regId": "R2",
     "location": "Fairfax | Indoor | Court 02, VA",
     "registration": {"registeredMembers": [{"attendeeId": "A2", "cancelCtas": []}]}},
    {"start": "", "regId": "IGNORED"},  # empty start → skipped
]}


def test_get_reservations_parses_and_sorts():
    c = make_client()
    c.session.queue("GET", FakeResponse(RESERVATIONS_PAYLOAD))
    res = c.get_reservations([111], date(2026, 8, 1), date(2026, 8, 31))
    # sorted by start ascending → 08-04 first
    assert [r["date_str"] for r in res] == ["2026-08-04", "2026-08-10"]
    by_date = {r["date_str"]: r for r in res}
    # online cancelCta registrationId overrides the top-level attendeeId
    assert by_date["2026-08-10"]["attendee_id"] == "CANCEL1"
    # no online cta → falls back to attendeeId
    assert by_date["2026-08-04"]["attendee_id"] == "A2"
    # location parsed to court name
    assert "Court 03" in by_date["2026-08-10"]["label"]


def test_reserved_dates_and_labels():
    c = make_client()
    c.session.queue("GET", FakeResponse(RESERVATIONS_PAYLOAD))
    reserved, labels = c.reserved_dates_and_labels([111], date(2026, 8, 1), date(2026, 8, 31))
    assert reserved == {"2026-08-04", "2026-08-10"}
    assert len(labels) == 2


# ── book_court ───────────────────────────────────────────────────────────────

def _booking(skip_confirmation=False):
    return {
        "regId": "REG1", "regStatus": "pending",
        "agreement": {"agreementId": "42"},
        "registrationType": {"skipConfirmation": skip_confirmation},
        "location": "Court 03",
    }


def test_book_court_complete_success():
    c = make_client()
    c.session.queue("POST", FakeResponse(_booking()))          # create
    c.session.queue("PUT", FakeResponse({"ok": True}))         # complete
    booking = c.book_court("RES1", "2026-08-10T07:00:00", 60)
    assert booking["regStatus"] == "completed"
    assert [m for m, _, _ in c.session.calls] == ["POST", "PUT"]


def test_book_court_complete_failure_raises_booking_incomplete():
    """A rejected /complete leaves no reservation — it must never look like a success."""
    c = make_client()
    c.session.queue("POST", FakeResponse(_booking()))
    c.session.queue("PUT", FakeResponse(status_code=400, text="waiver error"))
    with pytest.raises(BookingIncompleteError) as excinfo:
        c.book_court("RES1", "2026-08-10T07:00:00", 60)
    assert excinfo.value.reg_id == "REG1"
    assert excinfo.value.response.status_code == 400   # 4xx → callers move to another slot


def test_book_court_skip_confirmation_no_complete_call():
    c = make_client()
    c.session.queue("POST", FakeResponse(_booking(skip_confirmation=True)))
    booking = c.book_court("RES1", "2026-08-10T07:00:00", 60)
    assert [m for m, _, _ in c.session.calls] == ["POST"]  # no PUT
    assert booking["regStatus"] == "pending"


def test_book_court_create_http_error_raises_with_body():
    c = make_client()
    c.session.queue("POST", FakeResponse(status_code=409, text="slot taken"))
    with pytest.raises(requests.HTTPError) as ei:
        c.book_court("RES1", "2026-08-10T07:00:00", 60)
    assert "slot taken" in str(ei.value)


# ── cancel_attendee ──────────────────────────────────────────────────────────

def test_cancel_attendee_success():
    c = make_client()
    c.session.queue("DELETE", FakeResponse({}, status_code=200))
    c.cancel_attendee("A1")
    assert c.session.calls[-1][0] == "DELETE"


def test_cancel_attendee_http_error_includes_body():
    c = make_client()
    c.session.queue("DELETE", FakeResponse(status_code=400, text="cannot cancel"))
    with pytest.raises(requests.HTTPError) as ei:
        c.cancel_attendee("A1")
    assert "cannot cancel" in str(ei.value)
