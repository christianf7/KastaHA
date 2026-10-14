"""Light entities for Kasta CSRmesh devices and groups."""

from __future__ import annotations

from typing import Any

from homeassistant.components.light import ATTR_BRIGHTNESS, ColorMode, LightEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .hub import KastaConnectionError, KastaHub
from .models import KastaTarget

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry[KastaHub],
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create one light entity for every database target."""
    hub = entry.runtime_data
    async_add_entities(KastaLight(hub, target) for target in hub.mesh.targets)


class KastaLight(LightEntity):
    """A light or group addressed through the shared Kasta mesh bearer."""

    _attr_should_poll = False
    _attr_supported_color_modes = {ColorMode.BRIGHTNESS}
    _attr_color_mode = ColorMode.BRIGHTNESS

    def __init__(self, hub: KastaHub, target: KastaTarget) -> None:
        self._hub = hub
        self._target = target
        self._attr_name = target.name
        self._attr_unique_id = (
            f"{hub.mesh.network_id}_{target.kind}_{target.address:04x}"
        )

    @property
    def device_info(self) -> DeviceInfo:
        """Represent each mesh target as a Home Assistant device."""
        return DeviceInfo(
            identifiers={(DOMAIN, self._attr_unique_id)},
            name=self._target.name,
            manufacturer="Kasta",
            model=("CSRmesh group" if self._target.kind == "group" else "CSRmesh light"),
        )

    @property
    def is_on(self) -> bool | None:
        """Return the latest known power state."""
        return self._hub.states[self._target.address].power

    @property
    def brightness(self) -> int | None:
        """Return the latest known raw brightness."""
        brightness = self._hub.states[self._target.address].brightness
        return brightness if brightness is not None and brightness > 0 else None

    @property
    def available(self) -> bool:
        """Expose a lost bearer while background reconnection is in progress."""
        return self._hub.connected

    @callback
    def _state_changed(self) -> None:
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        """Subscribe to shared hub updates."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self._hub.register_callback(self._target.address, self._state_changed)
        )

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on the target, optionally setting brightness first."""
        brightness = kwargs.get(ATTR_BRIGHTNESS)
        try:
            if brightness is not None:
                if brightness == 0:
                    await self._hub.async_set_power(self._target.address, False)
                    return
                await self._hub.async_set_brightness(self._target.address, brightness)
            await self._hub.async_set_power(self._target.address, True)
        except KastaConnectionError as exc:
            raise HomeAssistantError(str(exc)) from exc

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off the target."""
        try:
            await self._hub.async_set_power(self._target.address, False)
        except KastaConnectionError as exc:
            raise HomeAssistantError(str(exc)) from exc

    async def async_update(self) -> None:
        """Refresh one physical light when update_entity is explicitly called."""
        if self._target.kind == "group":
            return
        try:
            await self._hub.async_request_state(self._target.address)
        except KastaConnectionError as exc:
            raise HomeAssistantError(str(exc)) from exc
