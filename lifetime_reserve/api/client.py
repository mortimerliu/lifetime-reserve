"""LifetimeClient — the Lifetime Fitness API surface behind one object.

Holds the session + auth tokens so callers stop threading (session, token, sso_id)
through every function. Method bodies are the original free-function bodies with the
session/token/sso_id references replaced by `self.*`; no behavior change.
"""

import logging
from datetime import datetime

import requests

from lifetime_reserve.api.errors import BookingIncompleteError, raise_for_status_with_body

log = logging.getLogger(__name__)

API_BASE = "https://api.lifetimefitness.com"
APIM_KEY = "924c03ce573d473793e184219a6a19bd"
ORIGIN = "https://my.lifetime.life"
HTTP_TIMEOUT = (5, 10)  # (connect, read) in seconds


class LifetimeClient:
    def __init__(self, session=None):
        self.session = session if session is not None else self._make_session()
        self.token = None
        self.sso_id = None

    @staticmethod
    def _make_session():
        s = requests.Session()
        s.headers.update({
            "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "origin": ORIGIN,
            "ocp-apim-subscription-key": APIM_KEY,
        })
        return s

    def _auth_headers(self):
        return {"x-ltf-jwe": self.token, "x-ltf-ssoid": self.sso_id}

    # ── auth ─────────────────────────────────────────────────────────────────

    def login(self, username, password):
        resp = self.session.post(
            f"{API_BASE}/auth/v2/login",
            json={"username": username, "password": password},
            headers={"content-type": "application/json; charset=UTF-8"},
            timeout=HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") != "0":
            raise RuntimeError(f"Login failed: {data}")
        log.info("Logged in as %s", data["username"])
        self.token = data["token"]
        self.sso_id = data["ssoId"]
        return self.token, self.sso_id

    # ── search ───────────────────────────────────────────────────────────────

    def search_courts(self, club_id, sport, target_date, duration):
        resp = self.session.get(
            f"{API_BASE}/ux/web-schedules/v2/resources/booking/search",
            params={
                "homeClub": club_id,
                "clubId": club_id,
                "sport": sport,
                "date": target_date.strftime("%Y-%m-%d"),
                "startTime": "-1",
                "duration": str(duration),
            },
            headers=self._auth_headers(),
            timeout=HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()

    # ── reservations ─────────────────────────────────────────────────────────

    def get_reservations(self, member_ids, start_date, end_date):
        """Return list of {date_str, reg_id, attendee_id, label} dicts sorted by date."""
        params = [
            ("start", start_date.strftime("%-m/%-d/%Y")),
            ("end", end_date.strftime("%-m/%-d/%Y")),
            ("groupCamps", "true"),
            ("pageSize", "0"),
        ]
        for mid in member_ids:
            params.append(("memberIds", str(mid)))
        resp = self.session.get(
            f"{API_BASE}/ux/web-schedules/v3/reservations",
            params=params,
            headers=self._auth_headers(),
            timeout=HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        out = []
        for item in sorted(resp.json().get("results", []), key=lambda x: x.get("start", "")):
            start = item.get("start", "")
            if not start:
                continue
            reg_id = item.get("regId") or item.get("registrationId") or item.get("id")
            # Extract the attendee id used by the cancel endpoint. The reservations
            # API exposes it under registration.registeredMembers[].cancelCtas (the
            # online CTA) — the top-level id is NOT accepted by the cancel endpoint.
            attendee_id = None
            for member in (item.get("registration", {}) or {}).get("registeredMembers", []):
                attendee_id = member.get("attendeeId") or attendee_id
                for cta in member.get("cancelCtas", []):
                    if cta.get("method") == "online" and cta.get("registrationId"):
                        attendee_id = cta["registrationId"]
                        break
                if attendee_id:
                    break
            try:
                dt = datetime.fromisoformat(start[:19])
                date_label = dt.strftime("%a %b %-d")
                time_label = dt.strftime("%-I:%M %p")
            except Exception:
                date_label = start[:10]
                time_label = ""
            raw_loc = item.get("location") or item.get("resourceName") or ""
            if " | " in raw_loc:
                raw_loc = raw_loc.split(" | ")[-1].split(",")[0].strip()
            court = raw_loc.strip()
            label = f"{date_label} — {time_label}" + (f", {court}" if court else "")
            out.append({"date_str": start[:10], "reg_id": reg_id,
                        "attendee_id": attendee_id, "label": label})
        return out

    def reserved_dates_and_labels(self, member_ids, start_date, end_date):
        """Return (date_set, label_list) for existing reservations in the date range."""
        reservations = self.get_reservations(member_ids, start_date, end_date)
        reserved = {r["date_str"] for r in reservations}
        labels = [r["label"] for r in reservations]
        return reserved, labels

    # ── booking / cancellation ───────────────────────────────────────────────

    def book_court(self, resource_id, start, duration):
        """Create a booking and immediately complete it (accept waiver).

        Returns only when the slot is actually held: a booking that needs confirmation
        but fails `/complete` raises `BookingIncompleteError`.
        """
        resp = self.session.post(
            f"{API_BASE}/sys/registrations/V3/ux/resource",
            json={
                "resourceId": resource_id,
                "start": start,
                "service": None,
                "duration": str(duration),
            },
            headers={**self._auth_headers(), "content-type": "application/json"},
            timeout=HTTP_TIMEOUT,
        )
        raise_for_status_with_body(resp)
        booking = resp.json()

        # Complete the booking (accept waiver) — moves from pending → completed
        reg_id = booking.get("regId")
        agreement_id = booking.get("agreement", {}).get("agreementId")
        if reg_id and agreement_id and not booking.get("registrationType", {}).get("skipConfirmation", True):
            complete_resp = self.session.put(
                f"{API_BASE}/sys/registrations/V3/ux/resource/{reg_id}/complete",
                json={"acceptedDocuments": [int(agreement_id)]},
                headers={**self._auth_headers(), "content-type": "application/json"},
                timeout=HTTP_TIMEOUT,
            )
            try:
                raise_for_status_with_body(complete_resp)
                booking["regStatus"] = "completed"
            except requests.HTTPError as e:
                # /complete is what actually claims the court — a rejection here means
                # the registration stays pending and never becomes a reservation. Raise
                # so callers report failure and move on to another slot; returning the
                # pending booking made every caller report a success that did not exist.
                log.warning("Booking created (regId=%s) but /complete failed: %s", reg_id, e)
                log.warning("Registration %s stays pending — it is NOT a reservation", reg_id)
                raise BookingIncompleteError(
                    f"booking {reg_id} could not be completed: {e}",
                    response=complete_resp, reg_id=reg_id) from None

        return booking

    def cancel_attendee(self, attendee_id):
        """Cancel a single reservation by its attendee/registration id."""
        resp = self.session.delete(
            f"{API_BASE}/sys/registrations/V3/ux/resource/0/attendees/{attendee_id}",
            headers=self._auth_headers(),
            timeout=HTTP_TIMEOUT,
        )
        raise_for_status_with_body(resp)
