"""Kasta Bluetooth integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .const import (
    CONF_DATABASE_PATH,
    CONF_KEEPALIVE_INTERVAL,
    CONF_POLL_INTERVAL,
    DEFAULT_DATABASE_PATH,
    DEFAULT_KEEPALIVE_INTERVAL,
    DEFAULT_POLL_INTERVAL,
)
from .database import KastaDatabaseError, load_mesh, resolve_database_path
from .hub import KastaConnectionError, KastaHub

PLATFORMS = [Platform.LIGHT]

KastaConfigEntry = ConfigEntry[KastaHub]


async def _async_reload_entry(hass: HomeAssistant, entry: KastaConfigEntry) -> None:
    """Reload after options change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_setup_entry(hass: HomeAssistant, entry: KastaConfigEntry) -> bool:
    """Set up Kasta Bluetooth from a config entry."""
    configured_path = entry.options.get(
        CONF_DATABASE_PATH,
        entry.data.get(CONF_DATABASE_PATH, DEFAULT_DATABASE_PATH),
    )
    database_path = resolve_database_path(hass.config.config_dir, configured_path)
    try:
        mesh = await hass.async_add_executor_job(load_mesh, database_path)
    except KastaDatabaseError as exc:
        raise ConfigEntryNotReady(str(exc)) from exc

    address = entry.options.get(CONF_ADDRESS, entry.data[CONF_ADDRESS])
    poll_interval = int(entry.options.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL))
    keepalive_interval = int(
        entry.options.get(CONF_KEEPALIVE_INTERVAL, DEFAULT_KEEPALIVE_INTERVAL)
    )
    hub = KastaHub(
        hass,
        address,
        mesh,
        poll_interval=poll_interval,
        keepalive_interval=keepalive_interval,
    )
    try:
        await hub.async_connect()
    except KastaConnectionError as exc:
        raise ConfigEntryNotReady(str(exc)) from exc

    entry.runtime_data = hub
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    hub.start_state_sync()
    return True


async def async_unload_entry(hass: HomeAssistant, entry: KastaConfigEntry) -> bool:
    """Unload Kasta Bluetooth and close its persistent connection."""
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    await entry.runtime_data.async_close()
    return True
