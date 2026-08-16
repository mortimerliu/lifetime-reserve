"""Shared pytest fixtures and fakes.

`FakeResponse` / `FakeSession` stand in for `requests` so the API client can be tested
with no network. `FakeClient` (Phase 3) drives the mode handlers. `load_fixture` reads
the captured+scrubbed API payloads in tests/fixtures/.
"""

import json
import pathlib

import requests

FIXTURES_DIR = pathlib.Path(__file__).parent / "fixtures"


def load_fixture(name):
    """Load a scrubbed real API payload from tests/fixtures/."""
    return json.loads((FIXTURES_DIR / name).read_text())


class FakeResponse:
    def __init__(self, json_data=None, status_code=200, text=""):
        self._json = json_data if json_data is not None else {}
        self.status_code = status_code
        self.text = text

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} Error", response=self)


class FakeSession:
    """Records requests and returns queued responses per HTTP method.

    Queue a response with `queue(method, resp)`; calls pop FIFO. A queued value may be
    a FakeResponse or a zero-arg callable (e.g. to raise). Exhausted queues return a
    default 200/empty response.
    """

    def __init__(self):
        self.headers = {}
        self.calls = []          # list of (method, url, kwargs)
        self._responses = {}     # method -> list

    def queue(self, method, *responses):
        self._responses.setdefault(method, []).extend(responses)
        return self

    def _handle(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        q = self._responses.get(method, [])
        r = q.pop(0) if q else FakeResponse()
        return r() if callable(r) else r

    def post(self, url, **kwargs):
        return self._handle("POST", url, **kwargs)

    def get(self, url, **kwargs):
        return self._handle("GET", url, **kwargs)

    def put(self, url, **kwargs):
        return self._handle("PUT", url, **kwargs)

    def delete(self, url, **kwargs):
        return self._handle("DELETE", url, **kwargs)


def make_slot(time, court, resource_id="RES", start="2026-08-10T07:00:00"):
    return {"time": time, "resourceName": court, "resourceId": resource_id, "start": start}


def search_envelope(slots):
    """Wrap a flat list of slot dicts in the search API's dayParts envelope."""
    return {"results": {"dayParts": [{"name": "Morning", "availableTimes": list(slots)}]}}


class FakeClient:
    """Scriptable stand-in for LifetimeClient — drives the mode handlers with no network.

    - search_results: queue of values returned by successive search_courts() calls. Each
      may be a search envelope, an Exception (raised), or a callable.
    - book_results: queue for book_court() calls (booking dict, Exception, or callable).
    - reserved: (set, labels) returned by reserved_dates_and_labels().
    - reservations: list returned by get_reservations().
    """

    def __init__(self, search_results=None, book_results=None,
                 reserved=None, reservations=None):
        self.token, self.sso_id = "T", "S"
        self._search = list(search_results or [])
        self._book = list(book_results or [])
        self._reserved = reserved or (set(), [])
        self._reservations = reservations or []
        self.search_calls = []        # target_date per call
        self.book_calls = []          # (resource_id, start) per call
        self.reservation_calls = []   # (start_date, end_date) per get_reservations call
        self.cancelled = []           # attendee_ids

    @staticmethod
    def _next(queue, default):
        if not queue:
            return default() if callable(default) else default
        v = queue.pop(0)
        if isinstance(v, Exception):
            raise v
        return v() if callable(v) else v

    def search_courts(self, club_id, sport, target_date, duration):
        self.search_calls.append(target_date)
        return self._next(self._search, lambda: search_envelope([]))

    def book_court(self, resource_id, start, duration):
        self.book_calls.append((resource_id, start))
        return self._next(self._book, lambda: {"regId": "R", "regStatus": "completed"})

    def reserved_dates_and_labels(self, member_ids, start_date, end_date):
        return self._reserved

    def get_reservations(self, member_ids, start_date, end_date):
        self.reservation_calls.append((start_date, end_date))
        return self._reservations

    def cancel_attendee(self, attendee_id):
        self.cancelled.append(attendee_id)
