"""Data coordinator for Navien Smart."""

from __future__ import annotations

from datetime import timedelta
import asyncio
import logging

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import NavienDevice, NavienSmartApiClient, NavienSmartApiError
from .const import DOMAIN

SCAN_INTERVAL = timedelta(seconds=60)
LOGGER = logging.getLogger(__name__)


class NavienSmartDataUpdateCoordinator(DataUpdateCoordinator[list[NavienDevice]]):
    """Fetch Navien Smart data."""

    def __init__(self, hass: HomeAssistant, client: NavienSmartApiClient) -> None:
        super().__init__(
            hass,
            LOGGER,
            name=DOMAIN,
            update_interval=SCAN_INTERVAL,
        )
        self.client = client
        self._mqtt_update_pending = False
        self.client.set_status_update_callback(self.async_schedule_mqtt_update)

    @property
    def devices(self) -> list[NavienDevice]:
        """Return known devices."""
        return self.data or []

    @property
    def mat_devices(self) -> dict[str, Any]:
        """Return known mat devices."""
        return getattr(self.client, "mat_devices", {})

    def device_by_id(self, device_id: str) -> NavienDevice | None:
        """Return a device by ID."""
        return next((device for device in self.devices if device.id == device_id), None)

    async def _async_update_data(self) -> list[NavienDevice]:
        """Fetch data from API."""
        try:
            return await self.client.async_get_devices()
        except NavienSmartApiError as err:
            raise UpdateFailed(str(err)) from err

    def async_schedule_mqtt_update(self) -> None:
        """Schedule a Home Assistant state update from cached MQTT data."""
        if self._mqtt_update_pending:
            return
        self._mqtt_update_pending = True
        self.hass.async_create_task(self._async_apply_mqtt_update())

    async def _async_apply_mqtt_update(self) -> None:
        """Apply cached MQTT state to coordinator listeners."""
        await asyncio.sleep(0)
        try:
            devices = await self.client.async_get_cached_devices()
        except NavienSmartApiError as err:
            LOGGER.debug("Navien Smart MQTT cached update failed: %s", err)
        else:
            self.async_set_updated_data(devices)
        finally:
            self._mqtt_update_pending = False
