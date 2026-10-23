"""Tests for the fork's additions: transactions, manual refresh, interval, available balance.

Run inside a Home Assistant core checkout with the integration copied to
tests/testing_config/custom_components/sparebank1_pengerobot.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.setup import async_setup_component

from tests.common import MockConfigEntry, async_capture_events

DOMAIN = "sparebank1_pengerobot"
EVENT = "sparebank1_pengerobot_transaction"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Enable custom integrations."""


def _ms(day: str) -> int:
    """Midnight Oslo time as the API's UTC epoch milliseconds."""
    local = datetime.fromisoformat(day).replace(tzinfo=timezone(timedelta(hours=1)))
    return int(local.timestamp() * 1000)


ACCOUNTS = [
    {"key": "KEY-R", "accountNumber": "36241234567", "name": "Regningskonto", "type": "USER",
     "balance": 1500.0, "availableBalance": 1200.0, "currencyCode": "NOK"},
    {"key": "KEY-C", "accountNumber": "K123", "name": "Mastercard", "type": "CREDITCARD",
     "balance": -400.0, "availableBalance": -400.0, "creditCardAccountID": "cc1"},
]


class FakeClient:
    """Stands in for Sparebank1Client."""

    def __init__(self) -> None:
        self.transactions: list[dict] = []
        self.tx_calls: list[tuple] = []
        self.tx_error: Exception | None = None

    async def get_accounts(self):
        return [dict(a) for a in ACCOUNTS]

    async def get_account_balances(self, numbers):
        raise AssertionError("balances come inline with the accounts")

    async def get_transactions(self, keys, from_date, to_date=None, row_limit=None):
        self.tx_calls.append((tuple(keys), from_date, to_date))
        if self.tx_error:
            raise self.tx_error
        return {"transactions": list(self.transactions), "errors": []}


async def _coordinator(hass: HomeAssistant, **options):
    from custom_components.sparebank1_pengerobot.coordinator import Sparebank1Coordinator

    await hass.config.async_set_time_zone("Europe/Oslo")
    entry = MockConfigEntry(domain=DOMAIN, data={"name": "Test"}, options=options)
    entry.add_to_hass(hass)
    coordinator = Sparebank1Coordinator(hass, entry)
    coordinator.client = FakeClient()
    coordinator.oauth_session = object()  # skip OAuth setup
    return entry, coordinator


def _raw(day, amount, text, **extra):
    return {
        "id": f"enc-{text}-{amount}", "date": _ms(day), "amount": amount, "description": text.upper(),
        "cleanedDescription": text, "accountKey": "KEY-R", "bookingStatus": "BOOKED", **extra,
    }


async def test_transactions_announced_once(hass: HomeAssistant, freezer) -> None:
    freezer.move_to("2026-11-12 08:00:00+01:00")
    entry, coordinator = await _coordinator(hass, fetch_transactions=True)
    events = async_capture_events(hass, EVENT)
    client = coordinator.client
    client.transactions = [
        _raw("2026-11-12", -1234.5, "Fjordkraft AS", remoteAccountName="Fjordkraft AS", kid="123456"),
        _raw("2026-11-10", 3000, "Overføring", remoteAccountName="Madde Nordmann", nonUniqueId="N1"),
    ]

    data = await coordinator._async_update_data()
    await hass.async_block_till_done()
    # One call for all accounts, credit cards left out, last 7 days
    assert client.tx_calls == [(("KEY-R",), "2026-11-05", None)]
    # Oldest first, flagged as the first fetch
    assert [(e.data["date"], e.data["amount"], e.data["initial"]) for e in events] == [
        ("2026-11-10", 3000.0, True),
        ("2026-11-12", -1234.5, True),
    ]
    first = events[1].data
    assert first["account_number"] == "36241234567"
    assert first["account_name"] == "Regningskonto"
    assert first["description"] == "Fjordkraft AS"
    assert first["remote_name"] == "Fjordkraft AS"
    assert first["kid"] == "123456"
    assert "account_key" not in first and "non_unique_id" not in first
    assert [t["date"] for t in data["transactions"]] == ["2026-11-12", "2026-11-10"]
    coordinator.data = data

    # Same transactions again (the deposit now booked with a new text): nothing new
    client.transactions[1] = _raw("2026-11-10", 3000, "Overføring fra Madde", nonUniqueId="N1")
    client.transactions.append(_raw("2026-11-12", -89, "Rema 1000", bookingStatus="PENDING"))
    await coordinator._async_update_data()
    await hass.async_block_till_done()
    assert len(events) == 3
    assert events[2].data["description"] == "Rema 1000"
    assert events[2].data["status"] == "PENDING"
    assert events[2].data["initial"] is False
    assert events[2].data["previously_pending"] is False
    assert events[0].data["previously_pending"] is False
    coordinator.data = await coordinator._async_update_data()

    # The reservation gets booked: announced once more, flagged
    client.transactions[2] = _raw("2026-11-12", -89, "Rema 1000")
    await coordinator._async_update_data()
    await coordinator._async_update_data()
    await hass.async_block_till_done()
    assert len(events) == 4
    assert (events[3].data["status"], events[3].data["previously_pending"]) == ("BOOKED", True)

    # Remembered across restarts
    _, again = await _coordinator(hass, fetch_transactions=True)
    again.entry = entry
    again._tx_store = coordinator._tx_store
    again.client.transactions = list(client.transactions)
    await again._async_update_data()
    await hass.async_block_till_done()
    assert len(events) == 4


async def test_transactions_off_and_rate_limited(hass: HomeAssistant, freezer) -> None:
    from custom_components.sparebank1_pengerobot.api import Sparebank1RateLimitError

    freezer.move_to("2026-11-12 08:00:00+01:00")
    _, coordinator = await _coordinator(hass)
    data = await coordinator._async_update_data()
    assert coordinator.client.tx_calls == []
    assert data["transactions"] == []

    entry, coordinator = await _coordinator(hass, fetch_transactions=True)
    coordinator.client.tx_error = Sparebank1RateLimitError("Rate limit exceeded. Retry after 120 seconds: {}")
    data = await coordinator._async_update_data()
    # Balances still fine, transactions paused for the Retry-After time
    assert data["accounts"][0]["balance"]["amount"] == "1500.0"
    assert data["transactions_error"] == "rate_limited"
    assert coordinator.transactions_rate_limited_until is not None
    coordinator.client.tx_error = None
    await coordinator._async_update_data()
    assert len(coordinator.client.tx_calls) == 1
    freezer.tick(timedelta(seconds=121))
    await coordinator._async_update_data()
    assert len(coordinator.client.tx_calls) == 2


async def test_manual_refresh_and_interval(hass: HomeAssistant, freezer) -> None:
    from custom_components.sparebank1_pengerobot.coordinator import update_interval_from

    freezer.move_to("2026-11-12 08:00:00+01:00")
    entry, coordinator = await _coordinator(hass, update_interval=10)
    assert coordinator.update_interval == timedelta(minutes=10)
    hass.config_entries.async_update_entry(entry, options={"update_interval": 1})
    assert update_interval_from(entry) == timedelta(minutes=5)
    hass.config_entries.async_update_entry(entry, options={"update_interval": 15})
    coordinator.apply_options()
    assert coordinator.update_interval == timedelta(minutes=15)

    calls = []
    original = coordinator.client.get_accounts

    async def counting():
        calls.append(1)
        return await original()

    coordinator.client.get_accounts = counting
    await coordinator.async_manual_refresh()
    await coordinator.async_manual_refresh()  # within the cooldown: skipped
    assert len(calls) == 1
    freezer.tick(timedelta(seconds=31))
    await coordinator.async_manual_refresh()
    assert len(calls) == 2

    # While the bank rate limits, the button says so instead of doing nothing
    coordinator._rate_limit_backoff_until = datetime.utcnow() + timedelta(minutes=20)
    freezer.tick(timedelta(seconds=31))
    with pytest.raises(HomeAssistantError, match="try again after"):
        await coordinator.async_manual_refresh()


async def test_available_balance_option(hass: HomeAssistant, freezer) -> None:
    from custom_components.sparebank1_pengerobot.sensor import Sparebank1AccountBalanceSensor

    entry, coordinator = await _coordinator(hass)
    coordinator.data = await coordinator._async_update_data()
    sensor = Sparebank1AccountBalanceSensor(coordinator, entry, coordinator.data["accounts"][0], 0)
    assert sensor.native_value == 1500.0
    attrs = sensor.extra_state_attributes
    assert (attrs["booked_balance"], attrs["available_balance"], attrs["balance_type"]) == (1500.0, 1200.0, "booked")
    hass.config_entries.async_update_entry(entry, options={"use_available_balance": True})
    assert sensor.native_value == 1200.0
    assert sensor.extra_state_attributes["balance_type"] == "available"
    assert "latest_transactions" not in sensor.extra_state_attributes


async def test_latest_transactions_per_account(hass: HomeAssistant, freezer) -> None:
    from custom_components.sparebank1_pengerobot.sensor import (
        Sparebank1AccountBalanceSensor,
        Sparebank1AccountSensor,
    )

    freezer.move_to("2026-11-12 08:00:00+01:00")
    entry, coordinator = await _coordinator(hass, fetch_transactions=True)
    coordinator.client.transactions = [
        _raw(f"2026-11-{day:02d}", -day, f"Kjøp {day}") for day in range(5, 12)
    ] + [_raw("2026-11-11", -1, "Annen konto", accountKey="KEY-X", accountNumber="11111111111")]
    coordinator.data = await coordinator._async_update_data()
    regning = Sparebank1AccountBalanceSensor(coordinator, entry, coordinator.data["accounts"][0], 0)
    latest = regning.extra_state_attributes["latest_transactions"]
    # This account only, newest first, five of them
    assert [t["description"] for t in latest] == ["Kjøp 11", "Kjøp 10", "Kjøp 9", "Kjøp 8", "Kjøp 7"]
    assert set(latest[0]) == {"date", "amount", "description", "remote_name", "status"}
    # Kept out of the recorder
    assert "latest_transactions" in regning._unrecorded_attributes
    # The overview sensor no longer carries a combined list
    status = Sparebank1AccountSensor(coordinator, entry)
    assert "latest_transactions" not in status.extra_state_attributes
    assert status.extra_state_attributes["transactions_last_update"]


async def test_services(hass: HomeAssistant, freezer) -> None:
    freezer.move_to("2026-11-12 08:00:00+01:00")
    assert await async_setup_component(hass, DOMAIN, {})
    entry, coordinator = await _coordinator(hass)
    coordinator.data = await coordinator._async_update_data()
    hass.data[DOMAIN][entry.entry_id] = {"coordinator": coordinator, "config": {"name": "Test"}}
    coordinator.client.transactions = [
        _raw("2026-11-01", -500, "Telenor"),
        _raw("2026-11-11", -89, "Rema 1000"),
    ]
    hass.states.async_set("sensor.test_regningskonto", "1500", {"account_number": "36241234567"})

    result = await hass.services.async_call(
        DOMAIN, "get_transactions",
        {"account": "sensor.test_regningskonto", "days": 30},
        blocking=True, return_response=True,
    )
    assert [t["description"] for t in result["transactions"]] == ["Rema 1000", "Telenor"]
    # non_unique_id is included for troubleshooting
    assert "non_unique_id" in result["transactions"][0]
    assert "account_key" not in result["transactions"][0]
    assert coordinator.client.tx_calls[-1] == (("KEY-R",), "2026-10-13", "2026-11-12")

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            DOMAIN, "get_transactions", {"account": "sensor.nope"}, blocking=True, return_response=True
        )

    calls = []

    async def fake_refresh():
        calls.append(1)

    coordinator.async_manual_refresh = fake_refresh
    await hass.services.async_call(DOMAIN, "refresh", {}, blocking=True)
    assert calls == [1]


def test_normalize_and_fingerprint() -> None:
    from zoneinfo import ZoneInfo

    from custom_components.sparebank1_pengerobot.transactions import fingerprint, normalize

    tz = ZoneInfo("Europe/Oslo")
    tx = normalize(
        {"date": _ms("2026-11-12"), "amount": -1234.5, "description": "FJORDKRAFT",
         "accountNumber": {"unformatted": "36241234567", "formatted": "3624.12.34567"},
         "merchant": {"name": "Fjordkraft"}},
        tz,
    )
    assert (tx["date"], tx["amount"], tx["account_number"], tx["merchant"]) == (
        "2026-11-12", -1234.5, "36241234567", "Fjordkraft",
    )
    # Booking status doesn't change the fingerprint
    assert fingerprint(tx) == fingerprint({**tx, "status": "BOOKED"})
    assert fingerprint(tx) != fingerprint({**tx, "amount": -1234.0})
