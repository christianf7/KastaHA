"""UI configuration flow for Kasta Bluetooth."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
)

from .const import (
    CONF_DATABASE_PATH,
    CONF_KEEPALIVE_INTERVAL,
    CONF_POLL_INTERVAL,
    DEFAULT_DATABASE_PATH,
    DEFAULT_KEEPALIVE_INTERVAL,
    DEFAULT_POLL_INTERVAL,
    DOMAIN,
    MESH_SERVICE,
)
from .database import KastaDatabaseError, load_mesh, resolve_database_path
from .hub import async_find_connectable_bearer
from .models import KastaMesh


class CannotConnect(Exception):
    """Raised when Home Assistant cannot currently reach the bearer."""


async def _async_validate_input(
    hass: HomeAssistant,
    address: str,
    configured_path: str,
) -> KastaMesh:
    path = resolve_database_path(hass.config.config_dir, configured_path)
    mesh = await hass.async_add_executor_job(load_mesh, path)
    if await async_find_connectable_bearer(hass, address) is None:
        raise CannotConnect
    return mesh


def _database_selector() -> TextSelector:
    return TextSelector(TextSelectorConfig(multiline=False))


class KastaConfigFlow(ConfigFlow, domain=DOMAIN):
    """Configure a Kasta mesh database and Bluetooth bearer."""

    VERSION = 1

    def __init__(self) -> None:
        self._address: str | None = None

    def _bearer_options(self) -> list[SelectOptionDict]:
        options: dict[str, str] = {}
        for info in async_discovered_service_info(self.hass, True):
            if MESH_SERVICE.lower() not in {uuid.lower() for uuid in info.service_uuids}:
                continue
            options[info.address] = f"{info.name or 'Kasta bearer'} ({info.address})"
        return [
            SelectOptionDict(value=address, label=label)
            for address, label in sorted(options.items(), key=lambda item: item[1])
        ]

    async def _async_create(
        self,
        address: str,
        configured_path: str,
        errors: dict[str, str],
    ) -> ConfigFlowResult | None:
        try:
            mesh = await _async_validate_input(self.hass, address, configured_path)
        except KastaDatabaseError as exc:
            errors[CONF_DATABASE_PATH] = (
                "database_not_found" if "not found" in str(exc) else "invalid_database"
            )
            return None
        except CannotConnect:
            errors[CONF_ADDRESS] = "cannot_connect"
            return None

        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title=mesh.name,
            data={
                CONF_ADDRESS: address,
                CONF_DATABASE_PATH: configured_path,
            },
        )

    async def async_step_user(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Let the user select a discovered bearer and local database path."""
        errors: dict[str, str] = {}
        options = self._bearer_options()
        if user_input is not None:
            result = await self._async_create(
                user_input[CONF_ADDRESS],
                user_input[CONF_DATABASE_PATH],
                errors,
            )
            if result is not None:
                return result

        address_default = options[0]["value"] if options else None
        address_field = vol.Required(CONF_ADDRESS)
        if address_default is not None:
            address_field = vol.Required(CONF_ADDRESS, default=address_default)
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    address_field: SelectSelector(
                        SelectSelectorConfig(
                            options=options,
                            custom_value=True,
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    ),
                    vol.Required(
                        CONF_DATABASE_PATH,
                        default=DEFAULT_DATABASE_PATH,
                    ): _database_selector(),
                }
            ),
            errors=errors,
        )

    async def async_step_bluetooth(
        self,
        discovery_info: BluetoothServiceInfoBleak,
    ) -> ConfigFlowResult:
        """Handle discovery of a connectable CSRmesh bearer."""
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        self._address = discovery_info.address
        self.context["title_placeholders"] = {
            "name": discovery_info.name or "Kasta Bluetooth"
        }
        return await self.async_step_database()

    async def async_step_database(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Ask for the Android database after Bluetooth discovery."""
        assert self._address is not None
        errors: dict[str, str] = {}
        if user_input is not None:
            result = await self._async_create(
                self._address,
                user_input[CONF_DATABASE_PATH],
                errors,
            )
            if result is not None:
                return result
            if CONF_ADDRESS in errors:
                errors["base"] = errors.pop(CONF_ADDRESS)
        return self.async_show_form(
            step_id="database",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_DATABASE_PATH,
                        default=DEFAULT_DATABASE_PATH,
                    ): _database_selector()
                }
            ),
            description_placeholders={"address": self._address},
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry) -> KastaOptionsFlow:
        """Return the options flow."""
        return KastaOptionsFlow()


class KastaOptionsFlow(OptionsFlow):
    """Change the database location or bearer without recreating entities."""

    async def async_step_init(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Validate and save updated connection options."""
        current_address = self.config_entry.options.get(
            CONF_ADDRESS,
            self.config_entry.data[CONF_ADDRESS],
        )
        current_path = self.config_entry.options.get(
            CONF_DATABASE_PATH,
            self.config_entry.data[CONF_DATABASE_PATH],
        )
        current_poll_interval = self.config_entry.options.get(
            CONF_POLL_INTERVAL,
            DEFAULT_POLL_INTERVAL,
        )
        current_keepalive_interval = self.config_entry.options.get(
            CONF_KEEPALIVE_INTERVAL,
            DEFAULT_KEEPALIVE_INTERVAL,
        )
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                await _async_validate_input(
                    self.hass,
                    user_input[CONF_ADDRESS],
                    user_input[CONF_DATABASE_PATH],
                )
            except KastaDatabaseError as exc:
                errors[CONF_DATABASE_PATH] = (
                    "database_not_found" if "not found" in str(exc) else "invalid_database"
                )
            except CannotConnect:
                errors[CONF_ADDRESS] = "cannot_connect"
            else:
                return self.async_create_entry(title="", data=user_input)

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ADDRESS, default=current_address): str,
                    vol.Required(CONF_DATABASE_PATH, default=current_path): _database_selector(),
                    vol.Required(
                        CONF_POLL_INTERVAL,
                        default=current_poll_interval,
                    ): vol.All(vol.Coerce(int), vol.Range(min=0, max=3600)),
                    vol.Required(
                        CONF_KEEPALIVE_INTERVAL,
                        default=current_keepalive_interval,
                    ): vol.All(vol.Coerce(int), vol.Range(min=0, max=3600)),
                }
            ),
            errors=errors,
        )
