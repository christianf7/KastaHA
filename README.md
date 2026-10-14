# Kasta Bluetooth

A Home Assistant integration for local power and brightness control legacy
Kasta Smart Living/Qualcomm CSRmesh lights. Install through HACS, select a Bluetooth bearer,
and supply a copy of your own Android `ble.db` database. Each device and saved
Kasta group becomes a light entity, and physical switches update state through
Bluetooth notifications.

No separate server, CLI toolkit, APK, Python environment, or Kasta cloud
connection is required to run the integration.

## Requirements

- Home Assistant 2025.12 or newer, with HACS installed.
- A connection-capable Bluetooth adapter or ESPHome Bluetooth proxy in range
  of a powered Kasta CSRmesh node.
- A populated database from your existing Kasta Android installation.

Developed against Kasta Android 2.9.10 (`com.haneco.ble`). Compatibility with
every model or newer database schema has not been established. Wi-Fi/Tuya-only
devices are not supported.

## Install through HACS

1. Open **HACS → menu → Custom repositories**.
2. Paste `https://github.com/christianf7/KastaHA` and choose **Integration** as the type.
3. Add the repository, find **Kasta Bluetooth**, and download it.
4. Restart Home Assistant.
5. Obtain and copy your database files as explained below.
6. Open **Settings → Devices & services → Add integration → Kasta Bluetooth**.
7. Select a discovered CSRmesh bearer and enter `kasta/ble.db` as the database path.

Any powered, reachable node advertising Bluetooth service `FEF1` can carry
commands for the mesh. Only one connection is needed for all lights. Close the
official Kasta app before configuring Home Assistant, since another controller
may occupy the bearer connection.

Manual installation is also possible: copy `custom_components/kasta_ble` into
`/config/custom_components/kasta_ble`, restart Home Assistant, and follow
steps 5–7.

## Get your database files

The database provides the network credentials, controller ID, device addresses,
friendly names, groups, and memberships. An APK alone does not contain your
installation's credentials.

Kasta Android 2.9.10 normally stores these files at:

```text
/data/user/0/com.haneco.ble/databases/ble.db
/data/user/0/com.haneco.ble/databases/ble.db-wal
/data/user/0/com.haneco.ble/databases/ble.db-shm
```

`/data/data/com.haneco.ble/databases` is commonly an equivalent path for the
primary Android user. Work profiles and secondary users may use another path.

`ble.db` is required. Copy `ble.db-wal` and `ble.db-shm` too if they exist. The
WAL may contain recent changes not yet present in the main database. Keep all
files together under their original names.

These files contain mesh credentials. Keep them private, outside the integration
code directory, and never attach them to a public issue or GitHub repository.

### Prepare the app

1. Open the official Kasta app on the phone where your lights already work.
2. Select the correct home/network and wait for devices and groups to load.
3. Stop the app before copying and leave it stopped until the snapshot is complete.

Do not clear app storage or uninstall it. Ordinary USB debugging cannot read
a production app's private database. Use an already-rooted phone, a suitable
emulator with your installation restored, or an export that provides the actual
SQLite database.

### Rooted Android phone

Install Android SDK Platform Tools on your computer, enable USB debugging on
the phone, connect USB, and accept its debugging prompt. Android Studio is not
required. See the official [ADB guide](https://developer.android.com/tools/adb).

These commands use a macOS/Linux terminal. If multiple devices are attached,
add `-s DEVICE_SERIAL` immediately after `adb` in every command, replacing
`DEVICE_SERIAL` with the serial shown by `adb devices`.

```bash
adb devices
adb shell pm path com.haneco.ble
adb shell am force-stop com.haneco.ble
adb shell su -c 'id'
adb shell su -c 'ls -l /data/user/0/com.haneco.ble/databases/'
mkdir -p kasta-database
adb exec-out su -c 'cat /data/user/0/com.haneco.ble/databases/ble.db' > kasta-database/ble.db
```

Approve root access on the phone if prompted. `id` should report `uid=0`.
Some root managers use another `su` syntax; consult that manager if `su -c`
is unsupported.

If the directory listing shows the companions, copy them too:

```bash
adb exec-out su -c 'cat /data/user/0/com.haneco.ble/databases/ble.db-wal' > kasta-database/ble.db-wal
adb exec-out su -c 'cat /data/user/0/com.haneco.ble/databases/ble.db-shm' > kasta-database/ble.db-shm
```

Only run companion commands for files that exist. Check for errors: shell
redirection may leave an empty output file after a failed extraction. Keep the
app stopped throughout the copy. Do not temporarily copy credentials to public
phone storage just to bypass file permissions.

Rooting is not required by the integration itself and is not covered here.
Bootloader unlocking can erase the phone, including the database you need.

### Android emulator

Use an image that permits root. Google Play Store images prohibit `adb root`;
Android documents AOSP images as a root-capable alternative. App login may
depend on Google services, so verify compatibility before relying on this route.
See [Android virtual device documentation](https://developer.android.com/studio/run/managing-avds)
and [command-line emulator documentation](https://developer.android.com/studio/run/emulator-commandline).
Launch the emulator using your installed Android CLI; Android Studio is not
needed for these extraction commands.

After restoring your home inside Kasta:

```bash
adb devices
adb shell am force-stop com.haneco.ble
adb root
adb wait-for-device
adb shell id
adb shell ls -l /data/user/0/com.haneco.ble/databases/
mkdir -p kasta-database
adb pull /data/user/0/com.haneco.ble/databases/ble.db kasta-database/ble.db
```

If present:

```bash
adb pull /data/user/0/com.haneco.ble/databases/ble.db-wal kasta-database/ble.db-wal
adb pull /data/user/0/com.haneco.ble/databases/ble.db-shm kasta-database/ble.db-shm
```

### Check the snapshot

With SQLite installed on your computer, keep the snapshot files together and run:

```bash
sqlite3 -readonly kasta-database/ble.db 'PRAGMA integrity_check;'
sqlite3 -readonly kasta-database/ble.db "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('place','device','group');"
sqlite3 -readonly kasta-database/ble.db 'SELECT name, isCurrentNetwork FROM place;'
sqlite3 -readonly kasta-database/ble.db 'SELECT COUNT(*) FROM device;'
```

Expect `ok`, tables named `place` and `device` (plus `group` when available),
the correct current home, and a non-empty device list. These queries do not
print credentials. Home Assistant validates the snapshot during setup too.
The older `mesh.db` schema is not supported by this integration.

## Copy into Home Assistant

Create `kasta` inside Home Assistant's configuration directory. Transfer the
snapshot using Samba, SSH/SFTP, or your instance's file transfer facilities:

```text
/config/kasta/ble.db
/config/kasta/ble.db-wal   # if present
/config/kasta/ble.db-shm   # if present
```

For Home Assistant Container, use the host directory mounted at `/config`.
The files must be readable by Home Assistant Core; keep the directory writable
if SQLite needs to manage its shared-memory file. A laptop path is not accessible
inside Home Assistant unless explicitly mounted.

Enter **`kasta/ble.db`** in the setup form. Relative paths resolve below Home
Assistant's configuration directory. Absolute paths are accepted if accessible
inside the Home Assistant process/container.

Keep databases outside `custom_components/kasta_ble` so HACS upgrades do not
replace them. To update a snapshot, disable/unload the integration, replace the
files as a consistent set, then re-enable it. Do not combine a new database with
old WAL/SHM files. Re-export after changing device names or group memberships.

## How it works

SQLite supplies the current network and mesh target addresses. The integration
authenticates and encrypts CSRmesh packets and sends them over one persistent
GATT connection. Each packet's destination routes it to the requested device
or group. All runtime code lives inside `custom_components/kasta_ble`.

Commands are serialized with a minimum 250 ms gap to avoid overwhelming the
bearer. Entities update optimistically after a command; authenticated device
reports subsequently update the observed state. The two captured physical-state
formats (`73/52` and `73/87`) report power and raw brightness, including the
retained dim level while off. Saved groups update their mapped members. Group
state is on if any known member is on, with the brightest known on member's level.

A startup sweep requests live state once per device. Periodic full-mesh polling
is disabled by default. A small request to one light every 30 seconds keeps the
bearer active. Unexpected disconnects start background recovery, and entities
are unavailable while the bearer is disconnected. Adapter faults and power loss
can still delay recovery.

Entities expose power and brightness only. Receiving colour-related packets
does not add colour controls. The current-network importer does not combine
multiple homes.

## Options

Open the integration's **Configure** dialog:

| Option | Default | Purpose |
| --- | --- | --- |
| Database path | `kasta/ble.db` | Your local snapshot. |
| Bluetooth bearer address | Selected at setup | Node carrying mesh traffic. |
| Bearer keepalive interval | `30` seconds | One small request; `0` disables it. |
| Fallback polling interval | `0` | Pause between full sweeps; `0` disables them. |

Use push updates first. Try a fallback interval of 30 seconds if physical changes
are missed. Sweeps take additional time because requests are serialized. Call
`homeassistant.update_entity` on a physical light for a targeted refresh; saved
groups do not implement per-device refresh.

## Troubleshooting

**Bearer unreachable:** close the Kasta app and other BLE controllers, confirm
power and range, and enable Auto or Active scanning on a connectable adapter.
The integration requests a fresh active scan when necessary.

**No available connection slot:** free an unused active BLE connection or add
a connection-capable adapter/proxy nearby. Restarting Home Assistant may release
stale allocations. The whole mesh needs just one slot.

**Slow after idle:** leave keepalive at 30 seconds and inspect disconnect logs.
BlueZ transport failures, interference, adapter resets, or power loss can still
interrupt the connection. Background reconnection retries with backoff.

**Physical switch state stale:** verify bearer connectivity and try a targeted
refresh. A switch that cuts mains power cannot report its final state after
power is removed. An unreachable device cannot return brightness through polling.

**Database rejected:** verify it exists inside Home Assistant, passes integrity
checks, and contains the current home, credentials, and devices. Copy existing
WAL/SHM companions again while the Android app is stopped.

This is an independent community integration, not an official Kasta product.
