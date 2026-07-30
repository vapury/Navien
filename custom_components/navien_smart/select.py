"""Select platform for Navien Smart."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import NavienDevice, NavienMode
from .const import DOMAIN
from .coordinator import NavienSmartDataUpdateCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Navien Smart select entities."""
    coordinator: NavienSmartDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[SelectEntity] = []
    for device in coordinator.devices:
        if device.modes:
            entities.append(NavienSmartModeSelect(coordinator, device))
            entities.append(NavienSmartFanSelect(coordinator, device))
    async_add_entities(entities)


class NavienSmartSelectBase(
    CoordinatorEntity[NavienSmartDataUpdateCoordinator],
    SelectEntity,
):
    """Base select entity for Navien Smart."""

    def __init__(
        self,
        coordinator: NavienSmartDataUpdateCoordinator,
        device: NavienDevice,
        key: str,
        name: str,
    ) -> None:
        super().__init__(coordinator)
        self._device_id = device.id
        self._attr_unique_id = f"{device.id}_{key}"
        self._attr_name = name

    @property
    def device(self) -> NavienDevice | None:
        """Return the latest device snapshot."""
        return self.coordinator.device_by_id(self._device_id)

    @property
    def available(self) -> bool:
        """Return whether the entity is available."""
        raw = (self.device.raw if self.device else {}) or {}
        return self.device is not None and bool(raw.get("connected", True))

    @property
    def device_info(self) -> DeviceInfo | None:
        """Return device registry information."""
        if self.device is None:
            return None
        raw = self.device.raw or {}
        return DeviceInfo(
            identifiers={(DOMAIN, self.device.id)},
            manufacturer="KyungDong Navien",
            name=self.device.name,
            model=str(raw.get("modelDisplayName") or raw.get("modelCode"))
            if raw.get("modelDisplayName") or raw.get("modelCode")
            else None,
            serial_number=str(raw.get("deviceId")) if raw.get("deviceId") else None,
        )


class NavienSmartModeSelect(NavienSmartSelectBase):
    """Operation mode selector."""

    def __init__(
        self,
        coordinator: NavienSmartDataUpdateCoordinator,
        device: NavienDevice,
    ) -> None:
        super().__init__(coordinator, device, "operation_mode", "Operation Mode")

    @property
    def options(self) -> list[str]:
        """Return available mode names."""
        return [mode.name for mode in (self.device.modes if self.device else ())]

    @property
    def current_option(self) -> str | None:
        """Return selected mode."""
        device = self.device
        if device is None or device.current_mode_key is None:
            return None
        mode = _mode_by_key(device, device.current_mode_key)
        return mode.name if mode else None

    async def async_select_option(self, option: str) -> None:
        """Select a mode."""
        device = self.device
        if device is None:
            return
        mode = _mode_by_name(device, option)
        if mode is None:
            return
        await self.coordinator.client.async_set_mode(self._device_id, mode.key)
        await self.coordinator.async_request_refresh()


class NavienSmartFanSelect(NavienSmartSelectBase):
    """Fan option selector."""

    def __init__(
        self,
        coordinator: NavienSmartDataUpdateCoordinator,
        device: NavienDevice,
    ) -> None:
        super().__init__(coordinator, device, "fan", "Fan")

    @property
    def options(self) -> list[str]:
        """Return fan options for the current mode."""
        mode = self._current_mode()
        if mode is None:
            return []
        return [fan.name for fan in mode.fan_options]

    @property
    def current_option(self) -> str | None:
        """Return selected fan option."""
        mode = self._current_mode()
        device = self.device
        if mode is None or device is None:
            return None
        fan_key = device.current_fan_key
        if fan_key is None and mode.fan_options:
            return mode.fan_options[0].name
        for fan in mode.fan_options:
            if fan.key == fan_key:
                return fan.name
        return None

    async def async_select_option(self, option: str) -> None:
        """Select a fan option."""
        mode = self._current_mode()
        if mode is None:
            return
        for fan in mode.fan_options:
            if fan.name == option:
                await self.coordinator.client.async_set_fan(self._device_id, fan.key)
                await self.coordinator.async_request_refresh()
                return

    def _current_mode(self) -> NavienMode | None:
        """Return the current mode, or the first supported mode before a command is sent."""
        device = self.device
        if device is None or not device.modes:
            return None
        if device.current_mode_key is not None:
            return _mode_by_key(device, device.current_mode_key)
        return device.modes[0]


def _mode_by_key(device: NavienDevice, key: str) -> NavienMode | None:
    """Find a mode by key."""
    return next((mode for mode in device.modes if mode.key == key), None)


def _mode_by_name(device: NavienDevice, name: str) -> NavienMode | None:
    """Find a mode by display name."""
    return next((mode for mode in device.modes if mode.name == name), None)
