"""Data models for Kasta Bluetooth."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class KastaTarget:
    """A mesh-addressed light or group."""

    name: str
    address: int
    kind: str
    cached_level: int | None = None
    cached_power: bool | None = None


@dataclass(frozen=True)
class KastaMesh:
    """Credentials and targets loaded from the Android database."""

    name: str
    network_id: str
    network_key: bytes
    source: int
    targets: tuple[KastaTarget, ...]
    group_memberships: tuple[tuple[int, tuple[int, ...]], ...] = ()


@dataclass
class KastaLightState:
    """Latest known state for one mesh target."""

    power: bool | None = None
    brightness: int | None = None
    red: int | None = None
    green: int | None = None
    blue: int | None = None
    color_temperature: int | None = None
