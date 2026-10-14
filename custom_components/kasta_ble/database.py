"""Read Kasta's Android Room database without modifying it."""

from __future__ import annotations

from pathlib import Path
import re
import sqlite3

from .const import DEFAULT_CONTROLLER_ID
from .models import KastaMesh, KastaTarget
from .protocol import derive_network_key


class KastaDatabaseError(ValueError):
    """Raised when a database cannot provide a usable mesh configuration."""


def resolve_database_path(config_directory: str, configured_path: str) -> Path:
    """Resolve relative paths inside Home Assistant's configuration directory."""
    path = Path(configured_path).expanduser()
    if not path.is_absolute():
        path = Path(config_directory) / path
    return path.resolve()


def load_mesh(path: Path) -> KastaMesh:
    """Load the current place and its targets from a copied ble.db."""
    if not path.is_file():
        raise KastaDatabaseError(f"database file not found: {path}")
    try:
        database = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        database.row_factory = sqlite3.Row
    except sqlite3.Error as exc:
        raise KastaDatabaseError(f"unable to open database: {exc}") from exc

    try:
        tables = {
            row[0]
            for row in database.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "place" not in tables or "device" not in tables:
            raise KastaDatabaseError("file is not a supported Kasta ble.db")
        place = database.execute(
            "SELECT pid, name, passphrase, networkKey, hostControlId, uid "
            "FROM place ORDER BY isCurrentNetwork DESC, pid LIMIT 1"
        ).fetchone()
        if place is None:
            raise KastaDatabaseError("database contains no Kasta place")

        passphrase = place["passphrase"]
        stored_key = place["networkKey"]
        if passphrase:
            network_key = derive_network_key(str(passphrase))
        elif stored_key is not None and len(stored_key) == 16:
            network_key = bytes(stored_key)
        else:
            raise KastaDatabaseError("current place has no usable mesh key")

        uid = place["uid"]
        targets: list[KastaTarget] = []
        memberships: dict[int, list[int]] = {}
        device_columns = {
            row[1] for row in database.execute("PRAGMA table_info(device)")
        }
        group_id_expression = "groupId" if "groupId" in device_columns else "NULL AS groupId"
        for row in database.execute(
            f"SELECT name, hardwareId, Level, Power, {group_id_expression} FROM device "
            "WHERE uid = ? ORDER BY dbId",
            (uid,),
        ):
            if row["hardwareId"] is None:
                continue
            address = int(row["hardwareId"])
            targets.append(
                KastaTarget(
                    name=(row["name"] or f"Device {address}").strip(),
                    address=address,
                    kind="device",
                    cached_level=(int(row["Level"]) if row["Level"] is not None else None),
                    cached_power=(bool(row["Power"]) if row["Power"] is not None else None),
                )
            )
            for group_id in re.findall(r"\d+", str(row["groupId"] or "")):
                memberships.setdefault(int(group_id), []).append(address)
        if "group" in tables:
            for row in database.execute(
                "SELECT name, gid FROM `group` WHERE uid = ? ORDER BY dbId",
                (uid,),
            ):
                if row["gid"] is None:
                    continue
                address = int(row["gid"])
                targets.append(
                    KastaTarget(
                        name=(row["name"] or f"Group {address}").strip(),
                        address=address,
                        kind="group",
                    )
                )
        if not targets:
            raise KastaDatabaseError("database contains no lights or groups")

        controller = place["hostControlId"]
        source = (
            int(controller)
            if controller and 0 < int(controller) <= 0xFFFF
            else DEFAULT_CONTROLLER_ID
        )
        network_id = str(uid if uid is not None else place["pid"])
        return KastaMesh(
            name=(place["name"] or "Kasta mesh").strip(),
            network_id=network_id,
            network_key=network_key,
            source=source,
            targets=tuple(targets),
            group_memberships=tuple(
                (group_id, tuple(dict.fromkeys(members)))
                for group_id, members in sorted(memberships.items())
            ),
        )
    except sqlite3.Error as exc:
        raise KastaDatabaseError(f"unable to read Kasta database: {exc}") from exc
    finally:
        database.close()
