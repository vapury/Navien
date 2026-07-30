"""Navien Smart integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store

from .api import NavienSmartApiClient
from .const import DOMAIN, STORAGE_KEY, STORAGE_VERSION
from .coordinator import NavienSmartDataUpdateCoordinator

PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.NUMBER,
    Platform.FAN,
]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Navien Smart from a config entry."""
    session = async_get_clientsession(hass)
    store: Store[dict[str, object]] = Store(hass, STORAGE_VERSION, STORAGE_KEY)
    stored_state = await store.async_load()
    if not isinstance(stored_state, dict):
        stored_state = {}
    stored_target_humidities = stored_state.get("target_humidities")
    if not isinstance(stored_target_humidities, dict):
        stored_target_humidities = {}

    async def async_save_target_humidities(values: dict[str, int]) -> None:
        await store.async_save({"target_humidities": values})

    client = NavienSmartApiClient(
        session=session,
        username=entry.data[CONF_USERNAME],
        password=entry.data[CONF_PASSWORD],
        stored_target_humidities=stored_target_humidities,
        async_save_target_humidities=async_save_target_humidities,
    )
    pending_auth = hass.data.setdefault(DOMAIN, {}).setdefault("_pending_auth", {})
    client.import_auth_state(pending_auth.pop(entry.data[CONF_USERNAME], None))

    coordinator = NavienSmartDataUpdateCoordinator(hass, client)
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a Navien Smart config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        coordinator = hass.data[DOMAIN].pop(entry.entry_id)
        coordinator.client.set_status_update_callback(None)
        await coordinator.client.async_close()
    return unload_ok
