"""Recovered Qualcomm CSRmesh protocol used by Kasta 2.9.10."""

from __future__ import annotations

import hashlib
import hmac
import secrets

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .const import CONTROL_POINT_1, CONTROL_POINT_2


def _bounded(name: str, value: int, low: int, high: int) -> int:
    if not low <= value <= high:
        raise ValueError(f"{name} must be between {low} and {high}")
    return value


def _le(value: int, length: int) -> bytes:
    return value.to_bytes(length, "little")


def derive_network_key(passphrase: str) -> bytes:
    """Derive the legacy MCP key from the database passphrase."""
    digest = hashlib.sha256(passphrase.encode() + b"\x00MCP").digest()
    return digest[::-1][:16]


def _aes_ecb_block(key: bytes, block: bytes) -> bytes:
    encryptor = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    return encryptor.update(block) + encryptor.finalize()


def protect_packet(plaintext: bytes, key: bytes, ttl: int = 0xFF) -> bytes:
    """Encrypt and authenticate one legacy CSRmesh packet."""
    if len(plaintext) < 8:
        raise ValueError("CSRmesh plaintext is too short")
    clear = plaintext[:5]
    protected = plaintext[5:]
    if len(protected) > 16:
        raise ValueError("protected payload exceeds 16 bytes")

    nonce = plaintext[:3] + b"\x00" + plaintext[3:5] + (b"\x00" * 10)
    stream = _aes_ecb_block(key, nonce)
    ciphertext = bytes(left ^ right for left, right in zip(protected, stream))
    tag = hmac.new(key, (b"\x00" * 8) + clear + ciphertext, hashlib.sha256).digest()[::-1][:8]
    return clear + ciphertext + tag + bytes([_bounded("ttl", ttl, 0, 255)])


def unprotect_packet(packet: bytes, key: bytes) -> bytes:
    """Authenticate and decrypt one legacy CSRmesh packet."""
    if len(packet) < 17:
        raise ValueError("CSRmesh packet is too short")
    clear = packet[:5]
    ciphertext = packet[5:-9]
    supplied_tag = packet[-9:-1]
    expected_tag = hmac.new(
        key,
        (b"\x00" * 8) + clear + ciphertext,
        hashlib.sha256,
    ).digest()[::-1][:8]
    if not hmac.compare_digest(supplied_tag, expected_tag):
        raise ValueError("CSRmesh authentication failed")

    nonce = packet[:3] + b"\x00" + packet[3:5] + (b"\x00" * 10)
    stream = _aes_ecb_block(key, nonce)
    protected = bytes(left ^ right for left, right in zip(ciphertext, stream))
    return clear + protected


def _plain_packet(source: int, destination: int, opcode: int, params: bytes) -> bytes:
    sequence = secrets.randbelow(1 << 24)
    return (
        _le(sequence, 3)
        + _le(_bounded("source", source, 1, 0xFFFF), 2)
        + _le(_bounded("destination", destination, 0, 0xFFFF), 2)
        + bytes([opcode])
        + params
    )


def power_packet(key: bytes, source: int, destination: int, on: bool) -> bytes:
    """Build an acknowledged Generic Power Set command."""
    transaction_id = secrets.randbelow(256)
    plain = _plain_packet(source, destination, 0x89, bytes([0x01, int(on), transaction_id]))
    return protect_packet(plain, key)


def brightness_packet(key: bytes, source: int, destination: int, level: int) -> bytes:
    """Build an acknowledged Light Level Set command."""
    transaction_id = secrets.randbelow(256)
    plain = _plain_packet(
        source,
        destination,
        0x8A,
        bytes([0x01, _bounded("brightness", level, 0, 255), transaction_id]),
    )
    return protect_packet(plain, key)


def status_request_packet(key: bytes, source: int, destination: int) -> bytes:
    """Build LightModelApi.getState (0x8A/0x07)."""
    transaction_id = secrets.randbelow(256)
    plain = _plain_packet(source, destination, 0x8A, bytes([0x07, transaction_id]))
    return protect_packet(plain, key)


def parse_mesh_packet(packet: bytes, key: bytes) -> dict[str, int | str]:
    """Decode an authenticated state, command, or request mesh packet."""
    plain = unprotect_packet(packet, key)
    if len(plain) < 9:
        raise ValueError("mesh packet is too short")
    result: dict[str, int | str] = {
        "sequence": int.from_bytes(plain[0:3], "little"),
        "source": int.from_bytes(plain[3:5], "little"),
        "destination": int.from_bytes(plain[5:7], "little"),
        "opcode": plain[7],
        "suboperation": plain[8],
        "parameters_hex": plain[9:].hex(),
    }

    opcode = plain[7]
    suboperation = plain[8]
    if opcode == 0x89 and suboperation in (0x00, 0x01, 0x05):
        if len(plain) < 10:
            raise ValueError("power packet is truncated")
        result["power"] = plain[9]
        result["event"] = "status" if suboperation == 0x05 else "command"
        return result
    if opcode == 0x89 and suboperation in (0x02, 0x03):
        result["toggle"] = 1
        result["event"] = "command"
        return result
    if opcode == 0x8A and suboperation == 0x08:
        if len(plain) < 17:
            raise ValueError("light-state response is truncated")
        result["event"] = "status"
        result.update(
            {
                "power": plain[9],
                "level": plain[10],
                "red": plain[11],
                "green": plain[12],
                "blue": plain[13],
                "color_temperature": int.from_bytes(plain[14:16], "little"),
                "supports": plain[16],
            }
        )
        return result
    if opcode == 0x8A and suboperation in (0x00, 0x01):
        if len(plain) < 10:
            raise ValueError("light-level command is truncated")
        result.update({"event": "command", "level": plain[9]})
        return result
    if opcode == 0x8A and suboperation in (0x02, 0x03):
        if len(plain) < 13:
            raise ValueError("RGB command is truncated")
        result.update(
            {
                "event": "command",
                "red": plain[9],
                "green": plain[10],
                "blue": plain[11],
                "level": plain[12],
            }
        )
        return result
    if opcode == 0x8A and suboperation == 0x06:
        if len(plain) < 11:
            raise ValueError("colour-temperature command is truncated")
        result.update(
            {
                "event": "command",
                "color_temperature": int.from_bytes(plain[9:11], "little"),
            }
        )
        return result
    if opcode == 0x8A and suboperation in (0x0A, 0x0B):
        if len(plain) < 10:
            raise ValueError("white-level command is truncated")
        result.update({"event": "command", "level": plain[9]})
        return result
    if opcode == 0x8A and suboperation == 0x07:
        result["event"] = "request"
        return result
    # Kasta firmware publishes physical control changes through CSRmesh's
    # generic Data Model rather than the standard Power/Light status opcodes.
    # Observed 0x73/0x52 report data:
    #   04 | channel | report_sequence | power | level
    if opcode == 0x73 and suboperation == 0x52:
        if len(plain) < 14:
            raise ValueError("Kasta physical-state report is truncated")
        if plain[9] == 0x04 and plain[12] in (0, 1):
            result.update(
                {
                    "event": "status",
                    "channel": plain[10],
                    "report_sequence": plain[11],
                    "power": plain[12],
                    "level": plain[13],
                }
            )
            return result
    # A second observed Kasta firmware family omits the channel byte and uses
    # report discriminator 0x87:
    #   03 | report_sequence | power | level
    if opcode == 0x73 and suboperation == 0x87:
        if len(plain) < 13:
            raise ValueError("Kasta compact physical-state report is truncated")
        if plain[9] == 0x03 and plain[11] in (0, 1):
            result.update(
                {
                    "event": "status",
                    "report_sequence": plain[10],
                    "power": plain[11],
                    "level": plain[12],
                }
            )
            return result

    result["event"] = "unknown"
    return result


def parse_status_packet(packet: bytes, key: bytes) -> dict[str, int | str]:
    """Decode the power or complete light-state response."""
    result = parse_mesh_packet(packet, key)
    if result["event"] != "status":
        raise ValueError("mesh packet is not a status response")
    return result


def transport_chunks(packet: bytes) -> tuple[tuple[str, bytes], ...]:
    """Split a packet across CSRmesh control points."""
    if len(packet) <= 20:
        return ((CONTROL_POINT_2, packet),)
    return ((CONTROL_POINT_1, packet[:20]), (CONTROL_POINT_2, packet[20:]))


class PacketAssembler:
    """Reassemble CP1 fragments followed by a CP2 final fragment."""

    def __init__(self) -> None:
        self._parts: list[bytes] = []

    def feed(self, data: bytes, final: bool) -> bytes | None:
        if not final:
            self._parts.append(bytes(data))
            return None
        packet = b"".join(self._parts) + bytes(data)
        self._parts.clear()
        return packet
