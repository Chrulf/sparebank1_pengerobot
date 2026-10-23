"""Button platform for Sparebank1 Pengerobot: refresh balances now."""
from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, INTEGRATION_NAME, MANUFACTURER
from .coordinator import Sparebank1Coordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the refresh button."""
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    async_add_entities([Sparebank1RefreshButton(coordinator, entry)])


class Sparebank1RefreshButton(ButtonEntity):
    """Fetch balances (and transactions, if enabled) from the bank now."""

    _attr_icon = "mdi:refresh"

    def __init__(self, coordinator: Sparebank1Coordinator, entry: ConfigEntry) -> None:
        self.coordinator = coordinator
        self.entry = entry
        self._attr_unique_id = f"{DOMAIN}_{entry.entry_id}_refresh"
        self._attr_name = f"{entry.data.get(CONF_NAME)} Refresh"

    @property
    def device_info(self):
        """Same device as the sensors."""
        return {
            "identifiers": {(DOMAIN, self.entry.entry_id)},
            "name": f"{INTEGRATION_NAME} {self.entry.data.get(CONF_NAME)}",
            "manufacturer": MANUFACTURER,
            "model": INTEGRATION_NAME,
        }

    async def async_press(self) -> None:
        """Refresh now; errors (e.g. rate limited) are shown to the user."""
        await self.coordinator.async_manual_refresh()
