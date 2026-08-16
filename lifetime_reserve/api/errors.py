"""API error helpers."""

import requests


class ApiError(RuntimeError):
    """Raised for Lifetime API failures not already covered by requests exceptions."""


class BookingIncompleteError(requests.HTTPError):
    """The booking was created but `/complete` rejected it — no reservation exists.

    Subclasses HTTPError so callers that already branch on `e.response.status_code`
    treat it like any other booking failure. `reg_id` is the pending registration the
    create step returned, kept for logging and manual follow-up.
    """

    def __init__(self, *args, reg_id=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.reg_id = reg_id


def raise_for_status_with_body(resp):
    """Like raise_for_status() but includes the response body in the exception message."""
    try:
        resp.raise_for_status()
    except requests.HTTPError as e:
        body = resp.text[:500] if resp.text else "(empty)"
        raise requests.HTTPError(f"{e} — body: {body}", response=resp) from None
