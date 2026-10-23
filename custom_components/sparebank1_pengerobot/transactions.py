"""Helpers for turning raw Sparebank1 transactions into stable, flat dicts."""
from __future__ import annotations

from datetime import date, datetime, timezone, tzinfo
from typing import Any


def _account_number(raw: dict[str, Any]) -> str | None:
    number = raw.get("accountNumber")
    if isinstance(number, dict):
        return number.get("unformatted") or number.get("value")
    return number


def _day(raw: dict[str, Any], tz: tzinfo) -> date | None:
    """The booking date. The API gives epoch milliseconds in UTC."""
    value = raw.get("date")
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc).astimezone(tz).date()
    except (TypeError, ValueError, OverflowError):
        return None


def normalize(raw: dict[str, Any], tz: tzinfo) -> dict[str, Any]:
    """Flatten one transaction to the fields worth exposing.

    The API's own `id` is encrypted and changes between sessions, so it is
    passed on for the details endpoint only, never used to recognise a
    transaction again (see fingerprint).
    """
    day = _day(raw, tz)
    merchant = raw.get("merchant") or {}
    try:
        amount = round(float(raw.get("amount")), 2)
    except (TypeError, ValueError):
        amount = None
    return {
        "id": raw.get("id"),
        "date": day.isoformat() if day else None,
        "amount": amount,
        "currency": raw.get("currencyCode") or raw.get("accountCurrency") or "NOK",
        "description": raw.get("cleanedDescription") or raw.get("description"),
        "raw_description": raw.get("description"),
        "remote_name": raw.get("remoteAccountName"),
        "remote_account": raw.get("remoteAccountNumber"),
        "kid": raw.get("kid"),
        "message": raw.get("kidOrMessage") or raw.get("melding"),
        "status": raw.get("bookingStatus"),
        "type_code": raw.get("typeCode"),
        "type_text": raw.get("typeText"),
        "merchant": merchant.get("name") if isinstance(merchant, dict) else None,
        "account_key": raw.get("accountKey"),
        "account_number": _account_number(raw),
        "account_name": raw.get("accountName"),
        "non_unique_id": raw.get("nonUniqueId"),
    }


def fingerprint(tx: dict[str, Any]) -> str:
    """Recognise the same transaction across refreshes.

    Deliberately leaves out the booking status, so a reserved card purchase
    that later gets booked isn't announced twice. The description is left out
    when nonUniqueId is there, as banks often rewrite it on booking.
    """
    amount = f"{tx.get('amount') or 0:.2f}"
    account = tx.get("account_number") or tx.get("account_key") or ""
    if tx.get("non_unique_id"):
        return f"{account}|{tx['non_unique_id']}|{amount}"
    return f"{account}|{tx.get('date')}|{amount}|{tx.get('remote_account') or ''}|{tx.get('description') or ''}"
