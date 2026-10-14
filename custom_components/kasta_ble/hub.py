"""Persistent Bluetooth connection shared by all Kasta light entities."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import suppress
import logging

from bleak import BleakClient
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection

from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant

from .const import CONTROL_POINT_1, CONTROL_POINT_2, MESH_SERVICE
from .models import KastaLightState, KastaMesh
from .protocol import (
    PacketAssembler,
    brightness_packet,
    parse_mesh_packet,
    power_packet,
    status_request_packet,
    transport_chunks,
)

_LOGGER = logging.getLogger(__name__)
_BLE_ERRORS = (BleakError, TimeoutError, EOFError, BrokenPipeError, OSError)
# A successful GATT write means BlueZ handed the packet to the bearer; it does
# not mean the CSRmesh command and its acknowledgement have finished. The
# Android SDK serializes writes behind a state machine with a 200 ms watchdog.
# A small guard interval prevents a following command from being discarded by
# a bridge that is still processing the previous mesh packet.
_MIN_COMMAND_INTERVAL = 0.25


class KastaConnectionError(RuntimeError):
    """Raised when a Kasta bearer cannot be reached."""


async def async_find_connectable_bearer(
    hass: HomeAssistant,
    address: str,
    timeout: float = 12.0,
) -> BLEDevice | None:
    """Resolve a fresh connectable BLEDevice, actively scanning when needed."""
    if device := bluetooth.async_ble_device_from_address(
        hass,
        address,
        connectable=True,
    ):
        return device

    def found(service_info: bluetooth.BluetoothServiceInfoBleak) -> bool:
        return service_info.address.casefold() == address.casefold()

    try:
        service_info = await bluetooth.async_process_advertisements(
            hass,
            found,
            {"address": address, "connectable": True},
            bluetooth.BluetoothScanningMode.ACTIVE,
            timeout,
        )
    except asyncio.TimeoutError:
        return None
    return service_info.device


class KastaHub:
    """Maintain one warm GATT connection for the entire CSRmesh network."""

    def __init__(
        self,
        hass: HomeAssistant,
        address: str,
        mesh: KastaMesh,
        poll_interval: int = 0,
        keepalive_interval: int = 30,
    ) -> None:
        self.hass = hass
        self.address = address
        self.mesh = mesh
        self._client: BleakClient | None = None
        self._command_lock = asyncio.Lock()
        self._assembler = PacketAssembler()
        self._response_event = asyncio.Event()
        self._expected_source: int | None = None
        self._last_packet_completed_at = 0.0
        self._callbacks: dict[int, set[Callable[[], None]]] = {}
        self._poll_interval = poll_interval
        self._keepalive_interval = keepalive_interval
        self._refresh_task: asyncio.Task[None] | None = None
        self._keepalive_task: asyncio.Task[None] | None = None
        self._reconnect_task: asyncio.Task[None] | None = None
        self._closing = False
        self._target_addresses = {target.address for target in mesh.targets}
        self._device_addresses = {
            target.address for target in mesh.targets if target.kind == "device"
        }
        self._keepalive_target = min(self._device_addresses, default=None)
        self._group_members = dict(mesh.group_memberships)
        self.states = {
            target.address: KastaLightState(
                power=target.cached_power,
                brightness=target.cached_level,
            )
            for target in mesh.targets
        }

    @property
    def connected(self) -> bool:
        """Return whether the bearer connection is currently warm."""
        return self._client is not None and self._client.is_connected

    def register_callback(self, address: int, callback: Callable[[], None]) -> Callable[[], None]:
        """Register an entity state callback."""
        callbacks = self._callbacks.setdefault(address, set())
        callbacks.add(callback)

        def remove() -> None:
            callbacks.discard(callback)

        return remove

    def _notify(self, address: int) -> None:
        for callback in tuple(self._callbacks.get(address, ())):
            callback()

    def _notify_all(self) -> None:
        """Notify every entity when bearer availability changes."""
        for address in tuple(self._callbacks):
            self._notify(address)

    def _refresh_groups_for_device(self, address: int) -> set[int]:
        """Recompute mesh-group state from currently known member states."""
        changed: set[int] = set()
        for group, members in self._group_members.items():
            if address not in members:
                continue
            member_states = [self.states.setdefault(member, KastaLightState()) for member in members]
            known_power = [state.power for state in member_states if state.power is not None]
            power: bool | None
            if any(value is True for value in known_power):
                power = True
            elif len(known_power) == len(member_states) and member_states:
                power = False
            else:
                power = None
            levels = [
                state.brightness
                for state in member_states
                if state.power is True and state.brightness is not None
            ]
            group_state = self.states.setdefault(group, KastaLightState())
            group_state.power = power
            group_state.brightness = max(levels) if levels else None
            changed.add(group)
        return changed

    def _event_addresses(self, decoded: dict[str, int | str]) -> tuple[int, ...]:
        """Resolve a status source or command destination to mesh entities."""
        if decoded["event"] == "status":
            address = int(decoded["source"])
            return (address,) if address in self._target_addresses else ()
        destination = int(decoded["destination"])
        if destination in self._group_members:
            return tuple(dict.fromkeys((destination, *self._group_members[destination])))
        if destination in self._target_addresses:
            return (destination,)
        source = int(decoded["source"])
        return (source,) if source in self._target_addresses else ()

    def _apply_event(self, decoded: dict[str, int | str]) -> None:
        addresses = self._event_addresses(decoded)
        changed = set(addresses)
        for address in addresses:
            state = self.states.setdefault(address, KastaLightState())
            if "power" in decoded:
                state.power = bool(decoded["power"])
            if "toggle" in decoded and state.power is not None:
                state.power = not state.power
            if "level" in decoded:
                state.brightness = int(decoded["level"])
            if "red" in decoded:
                state.red = int(decoded["red"])
                state.green = int(decoded["green"])
                state.blue = int(decoded["blue"])
            if "color_temperature" in decoded:
                state.color_temperature = int(decoded["color_temperature"])
            if address in self._device_addresses:
                changed.update(self._refresh_groups_for_device(address))
        for address in changed:
            self._notify(address)

    def _disconnected(self, client: BleakClient) -> None:
        if client is self._client:
            self._client = None
            _LOGGER.warning("Kasta Bluetooth bearer %s disconnected", self.address)
            self._notify_all()
            if not self._closing and (
                self._reconnect_task is None or self._reconnect_task.done()
            ):
                self._reconnect_task = self.hass.async_create_task(
                    self._async_reconnect_loop()
                )

    async def _async_reconnect_loop(self) -> None:
        """Restore an unexpectedly lost bearer before the next user command."""
        delay = 0.0
        while not self._closing:
            if delay:
                await asyncio.sleep(delay)
            try:
                async with self._command_lock:
                    if self.connected:
                        return
                    await self._ensure_client()
            except KastaConnectionError:
                delay = min(30.0, max(1.0, delay * 2.0))
                _LOGGER.debug(
                    "Kasta bearer reconnect failed; retrying in %.0f seconds",
                    delay,
                    exc_info=True,
                )
                continue
            _LOGGER.info("Reconnected to Kasta Bluetooth bearer %s", self.address)
            return

    def _notification(self, final: bool, data: bytearray) -> None:
        packet = self._assembler.feed(bytes(data), final)
        if packet is None:
            return
        try:
            decoded = parse_mesh_packet(packet, self.mesh.network_key)
        except ValueError:
            return
        if decoded["event"] == "unknown":
            _LOGGER.debug(
                "Authenticated Kasta packet 0x%02X/0x%02X from 0x%04X to 0x%04X "
                "is not decoded (parameters %s)",
                decoded["opcode"],
                decoded["suboperation"],
                decoded["source"],
                decoded["destination"],
                decoded["parameters_hex"],
            )
            return
        if decoded["event"] != "request":
            self._apply_event(decoded)
        address = int(decoded["source"])
        if decoded["event"] == "status" and (
            self._expected_source is None or address == self._expected_source
        ):
            self._response_event.set()

    async def _ensure_client(self) -> BleakClient:
        if self._client is not None and self._client.is_connected:
            return self._client
        ble_device = await async_find_connectable_bearer(self.hass, self.address)
        if ble_device is None:
            reason = bluetooth.async_address_reachability_diagnostics(
                self.hass,
                self.address,
                bluetooth.BluetoothReachabilityIntent.CONNECTION,
            )
            raise KastaConnectionError(f"Bluetooth bearer is not reachable: {reason}")
        client: BleakClient | None = None
        try:
            client = await establish_connection(
                BleakClientWithServiceCache,
                ble_device,
                ble_device.name or self.address,
                disconnected_callback=self._disconnected,
                max_attempts=3,
            )
            if client.services.get_service(MESH_SERVICE) is None:
                await client.disconnect()
                raise KastaConnectionError("selected device does not expose the CSRmesh service")
            await client.start_notify(
                CONTROL_POINT_1,
                lambda _characteristic, data: self._notification(False, data),
            )
            await client.start_notify(
                CONTROL_POINT_2,
                lambda _characteristic, data: self._notification(True, data),
            )
        except _BLE_ERRORS as exc:
            if client is not None:
                with suppress(*_BLE_ERRORS):
                    await client.disconnect()
            raise KastaConnectionError(f"unable to connect to Bluetooth bearer: {exc}") from exc
        assert client is not None
        self._client = client
        _LOGGER.info("Connected to Kasta Bluetooth bearer %s", self.address)
        self._notify_all()
        return client

    async def async_connect(self) -> None:
        """Open and retain the bearer connection."""
        async with self._command_lock:
            await self._ensure_client()

    def start_state_sync(self) -> None:
        """Start synchronization, connection keepalive, and recovery tasks."""
        if self._refresh_task is None:
            self._refresh_task = self.hass.async_create_task(self._state_sync_loop())
        if self._keepalive_interval > 0 and self._keepalive_task is None:
            self._keepalive_task = self.hass.async_create_task(self._keepalive_loop())

    async def _keepalive_loop(self) -> None:
        """Prevent idle bearer expiry and detect silently dead connections."""
        if self._keepalive_target is None:
            return
        try:
            while True:
                await asyncio.sleep(self._keepalive_interval)
                packet = status_request_packet(
                    self.mesh.network_key,
                    self.mesh.source,
                    self._keepalive_target,
                )
                try:
                    async with self._command_lock:
                        await self._write_locked(packet)
                except KastaConnectionError:
                    _LOGGER.debug(
                        "Kasta bearer keepalive failed; reconnect will be retried",
                        exc_info=True,
                    )
        except asyncio.CancelledError:
            raise

    async def _state_sync_loop(self) -> None:
        """Synchronize once at startup, then poll only when configured."""
        try:
            await asyncio.sleep(1)
            while True:
                for address in self._device_addresses:
                    try:
                        await self.async_request_state(address, timeout=1.0)
                    except KastaConnectionError:
                        _LOGGER.debug(
                            "Unable to synchronize Kasta target 0x%04X",
                            address,
                            exc_info=True,
                        )
                if self._poll_interval <= 0:
                    return
                await asyncio.sleep(self._poll_interval)
        except asyncio.CancelledError:
            raise

    async def _disconnect_client(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            try:
                await client.disconnect()
            except _BLE_ERRORS:
                _LOGGER.debug("Error while disconnecting Kasta bearer", exc_info=True)

    async def async_close(self) -> None:
        """Release Bluetooth resources when the config entry unloads."""
        self._closing = True
        tasks = tuple(
            task
            for task in (
                self._refresh_task,
                self._keepalive_task,
                self._reconnect_task,
            )
            if task is not None
        )
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._refresh_task = None
        self._keepalive_task = None
        self._reconnect_task = None
        async with self._command_lock:
            await self._disconnect_client()

    async def _write_locked(self, packet: bytes) -> None:
        for attempt in range(2):
            client = await self._ensure_client()
            elapsed = asyncio.get_running_loop().time() - self._last_packet_completed_at
            if elapsed < _MIN_COMMAND_INTERVAL:
                await asyncio.sleep(_MIN_COMMAND_INTERVAL - elapsed)
            try:
                for characteristic, chunk in transport_chunks(packet):
                    await client.write_gatt_char(characteristic, chunk, response=True)
                return
            except _BLE_ERRORS as exc:
                await self._disconnect_client()
                if attempt:
                    raise KastaConnectionError(f"Bluetooth write failed: {exc}") from exc
                _LOGGER.debug("Retrying Kasta command after reconnect", exc_info=True)
            finally:
                self._last_packet_completed_at = asyncio.get_running_loop().time()

    async def async_set_power(self, address: int, on: bool) -> None:
        """Set power and optimistically update the entity state."""
        packet = power_packet(self.mesh.network_key, self.mesh.source, address, on)
        async with self._command_lock:
            await self._write_locked(packet)
        state = self.states.setdefault(address, KastaLightState())
        state.power = on
        changed = {address}
        if address in self._group_members:
            for member in self._group_members[address]:
                self.states.setdefault(member, KastaLightState()).power = on
                changed.add(member)
        elif address in self._device_addresses:
            changed.update(self._refresh_groups_for_device(address))
        for changed_address in changed:
            self._notify(changed_address)

    async def async_set_brightness(self, address: int, brightness: int) -> None:
        """Set raw brightness and optimistically update the entity state."""
        packet = brightness_packet(
            self.mesh.network_key,
            self.mesh.source,
            address,
            brightness,
        )
        async with self._command_lock:
            await self._write_locked(packet)
        state = self.states.setdefault(address, KastaLightState())
        state.brightness = brightness
        changed = {address}
        if address in self._group_members:
            for member in self._group_members[address]:
                self.states.setdefault(member, KastaLightState()).brightness = brightness
                changed.add(member)
        elif address in self._device_addresses:
            changed.update(self._refresh_groups_for_device(address))
        for changed_address in changed:
            self._notify(changed_address)

    async def async_request_state(self, address: int, timeout: float = 2.5) -> KastaLightState:
        """Request live state from one physical light."""
        packet = status_request_packet(self.mesh.network_key, self.mesh.source, address)
        async with self._command_lock:
            self._expected_source = address
            self._response_event.clear()
            self._assembler = PacketAssembler()
            try:
                await self._write_locked(packet)
                await asyncio.wait_for(self._response_event.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                _LOGGER.debug("No live state response from Kasta target 0x%04X", address)
            finally:
                self._expected_source = None
        return self.states.setdefault(address, KastaLightState())
