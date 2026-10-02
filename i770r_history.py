# SPDX-License-Identifier: GPL-3.0-or-later
"""HISTORY-only i770R research CLI. Hardware validation applies to the prototype."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any

SERVICE = "cb3c4555-d670-4670-bc20-b61dbc851e9a"
WRITE = "6606ab42-89d5-4a00-a8ce-4eb5e1414ee0"
NOTIFY = "a60b8e5c-b267-44d7-9764-837caf96489e"
SUPPORTED_VERSION = b"AQUA770R 2A 0006"
HISTORY = slice(0x60, 0x70)
PROTECTED = (0x3FE00, 0x3FF00)
ZERO_HISTORY = bytes(16)


class ProtocolError(RuntimeError):
    pass


def validate_name(name: str) -> str:
    if re.fullmatch(r"FQ[0-9]{6}", name) is None:
        raise ValueError("Use the exact Bluetooth name: FQ followed by six digits")
    return name


def handshake_command(name: str) -> bytes:
    body = bytes(int(digit) for digit in validate_name(name)[2:]) + bytes(2)
    return b"\xe5" + body + bytes([sum(body) & 0xFF])


def history_command(history: bytes) -> bytes:
    if len(history) != 16:
        raise ValueError("HISTORY must contain exactly 16 bytes")
    # The address cannot be supplied by the CLI: B2 is restricted to page six.
    return b"\xb2\x00\x06" + history + bytes([sum(history) & 0xFF])


def command_frames(command: bytes, *, sequence: int) -> list[bytes]:
    if not command or len(command) > 16 * 32:
        raise ValueError("Invalid command length")
    frames = []
    for offset in range(0, len(command), 16):
        chunk = command[offset:offset + 16]
        more = offset + len(chunk) < len(command)
        status = 0x40 | (0x20 if more else 0) | (offset // 16)
        frames.append(bytes([0xCD, status, sequence & 0xFF, len(chunk)]) + chunk)
    return frames


def checked_body(reply: bytes, *, size: int, checksum_size: int) -> bytes:
    if len(reply) != 1 + size + checksum_size or reply[0] != 0x5A:
        raise ProtocolError("Unexpected response length or ACK")
    body = reply[1:1 + size]
    checksum = int.from_bytes(reply[-checksum_size:], "little")
    mask = 0xFF if checksum_size == 1 else 0xFFFF
    if sum(body) & mask != checksum:
        raise ProtocolError("Response checksum mismatch")
    return body


def history_values(history: bytes) -> dict[str, int]:
    if len(history) != 16:
        raise ValueError("Invalid HISTORY length")
    return {
        "total_dive": int.from_bytes(history[4:6], "little"),
        "total_hours": int.from_bytes(history[0:4], "little"),
        "remaining_minutes": int.from_bytes(history[6:8], "little"),
        "max_depth_sixteenth_feet": int.from_bytes(history[8:10], "little"),
        "max_dive_minutes": int.from_bytes(history[10:12], "little"),
        "depth_sum_raw": int.from_bytes(history[12:16], "little"),
    }


@dataclass(frozen=True)
class Snapshot:
    low: bytes
    protected: dict[int, bytes]

    @property
    def history(self) -> bytes:
        return self.low[HISTORY]


def verify_identity(low: bytes, *, name: str) -> None:
    if len(low) != 256 or low[8:10] != b"FQ" or low[10:13].hex() != name[2:]:
        raise ProtocolError("Memory identity does not match the selected device")


def verify_change(before: Snapshot, after: Snapshot, *, history: bytes) -> None:
    expected = bytearray(before.low[:0xF0])
    expected[HISTORY] = history
    if after.low[:0xF0] != bytes(expected):
        raise ProtocolError("Readback mismatch or unexpected change outside HISTORY")
    if after.protected != before.protected:
        raise ProtocolError("Protected-state snapshots changed; no further writes")


class Connection:
    def __init__(self, *, client: Any, name: str, timeout: float = 10.0):
        self.client = client
        self.name = validate_name(name)
        self.timeout = timeout
        self.sequence = 0
        self.inbox: asyncio.Queue[bytes] = asyncio.Queue()

    def on_notify(self, _sender: Any, data: bytearray) -> None:
        self.inbox.put_nowait(bytes(data))

    async def start(self) -> None:
        service = self.client.services.get_service(SERVICE)
        if service is None:
            raise ProtocolError("Pelagic service not found")
        write = service.get_characteristic(WRITE)
        notify = service.get_characteristic(NOTIFY)
        if write is None or notify is None or "write-without-response" not in write.properties:
            raise ProtocolError("Required Pelagic characteristics not found")
        await self.client.start_notify(NOTIFY, self.on_notify)
        reply = await self.exchange(b"\x84")
        version = checked_body(reply, size=16, checksum_size=1)
        if version != SUPPORTED_VERSION:
            raise ProtocolError("Unsupported firmware; expected AQUA770R 2A 0006")
        if await self.exchange(handshake_command(self.name)) != b"\x5a":
            raise ProtocolError("E5 handshake rejected")

    async def exchange(self, command: bytes) -> bytes:
        if not self.inbox.empty():
            raise ProtocolError("Unexpected queued notification; disconnect and inspect")
        # A missing ACK leaves the outcome unknown. Never resend automatically.
        async with asyncio.timeout(self.timeout):
            for frame in command_frames(command, sequence=self.sequence):
                await self.client.write_gatt_char(WRITE, frame, response=False)
                await asyncio.sleep(0.02)
            body = bytearray()
            for index in range(32):
                packet = await self.inbox.get()
                if len(packet) < 4 or packet[0] != 0xCD or packet[3] > 16:
                    raise ProtocolError("Malformed BLE notification")
                status = packet[1]
                if status != (0xC0 | (status & 0x20) | index):
                    raise ProtocolError("Out-of-order BLE notification")
                if packet[2] != self.sequence & 0xFF or len(packet) < 4 + packet[3]:
                    raise ProtocolError("Wrong sequence or truncated BLE notification")
                body.extend(packet[4:4 + packet[3]])
                if not status & 0x20:
                    self.sequence = (self.sequence + 1) & 0xFF
                    return bytes(body)
            raise ProtocolError("Too many BLE fragments")

    async def snapshot(self) -> Snapshot:
        async def read(address: int) -> bytes:
            page = address // 16
            reply = await self.exchange(b"\xb8" + page.to_bytes(2, "big"))
            return checked_body(reply, size=256, checksum_size=2)
        low = await read(0)
        verify_identity(low, name=self.name)
        protected = {address: await read(address) for address in PROTECTED}
        return Snapshot(low=low, protected=protected)

    async def write_history(self, history: bytes) -> None:
        reply = await self.exchange(history_command(history))
        if reply != b"\x5a":
            raise ProtocolError("B2 did not return ACK 5A; no further writes")


def private_file(path: Path, content: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    if os.name == "posix":
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


def private_json(path: Path, value: dict[str, Any]) -> None:
    private_file(path, (json.dumps(value, indent=2) + "\n").encode())


def save_backup(directory: Path, *, name: str, snapshot: Snapshot) -> None:
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    blobs = {"low-before.bin": snapshot.low}
    blobs.update({f"protected-{address:05x}.bin": data for address, data in snapshot.protected.items()})
    for filename, data in blobs.items():
        private_file(directory / filename, data)
    private_json(directory / "backup.json", {
        "schema": 1, "name": name, "version": SUPPORTED_VERSION.decode(),
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "files": {filename: {"bytes": len(data), "sha256": sha256(data).hexdigest()}
                  for filename, data in blobs.items()},
    })


def load_backup(directory: Path, *, name: str) -> Snapshot:
    manifest = json.loads((directory / "backup.json").read_text())
    if (manifest.get("schema") != 1 or manifest.get("name") != name
            or manifest.get("version") != SUPPORTED_VERSION.decode()):
        raise ProtocolError("Backup belongs to another device or format")
    blobs = {}
    for filename in ("low-before.bin", "protected-3fe00.bin", "protected-3ff00.bin"):
        data = (directory / filename).read_bytes()
        recorded = manifest["files"][filename]
        if len(data) != 256 or recorded["bytes"] != 256 or recorded["sha256"] != sha256(data).hexdigest():
            raise ProtocolError("Backup is incomplete or has been modified")
        blobs[filename] = data
    verify_identity(blobs["low-before.bin"], name=name)
    return Snapshot(low=blobs["low-before.bin"], protected={
        address: blobs[f"protected-{address:05x}.bin"] for address in PROTECTED})


async def reset_history(connection: Connection, *, backup: Path) -> dict[str, Any]:
    before = await connection.snapshot()
    save_backup(backup, name=connection.name, snapshot=before)
    result = {"operation": "reset-history", "before": history_values(before.history),
              "memory_write_attempts": 0, "backup": str(backup)}
    if before.history == ZERO_HISTORY:
        result.update(status="already_zero", after=history_values(before.history))
        private_json(backup / "result.json", result)
        return result
    try:
        private_json(backup / "same-value-attempt.json", {"page": 6, "data": "original_history"})
        result["memory_write_attempts"] = 1
        await connection.write_history(before.history)
        same = await connection.snapshot()
        verify_change(before, same, history=before.history)
        private_json(backup / "same-value-verified.json", {"ack": "5a", "readback_match": True})
        private_json(backup / "reset-attempt.json", {"page": 6, "data": "sixteen_zero_bytes"})
        result["memory_write_attempts"] = 2
        await connection.write_history(ZERO_HISTORY)
        after = await connection.snapshot()
        verify_change(before, after, history=ZERO_HISTORY)
        private_file(backup / "low-after.bin", after.low)
        result.update(status="reset_verified", after=history_values(after.history),
                      below_f0_outside_history_unchanged=True, protected_snapshots_unchanged=True)
        private_json(backup / "result.json", result)
        return result
    except Exception as error:
        private_json(backup / "failure.json", {**result, "status": "stopped",
                     "error": str(error) or type(error).__name__,
                     "automatic_retry": False, "automatic_rollback": False})
        raise


async def restore_history(connection: Connection, *, backup: Path) -> dict[str, Any]:
    original = load_backup(backup, name=connection.name)
    current = await connection.snapshot()
    if current.history not in (ZERO_HISTORY, original.history):
        raise ProtocolError("Current HISTORY is neither zero nor the backup; refusing restoration")
    verify_change(original, current, history=current.history)
    if current.history == original.history:
        return {"status": "already_restored", "memory_write_attempts": 0}
    # An exclusive marker also prevents repeating a restore with unknown outcome.
    private_json(backup / "restore-attempt.json", {"page": 6, "data": "backup_history"})
    await connection.write_history(original.history)
    after = await connection.snapshot()
    verify_change(current, after, history=original.history)
    result = {"status": "restore_verified", "memory_write_attempts": 1,
              "after": history_values(after.history)}
    private_json(backup / "restore-result.json", result)
    return result


@asynccontextmanager
async def connect(*, name: str):
    from bleak import BleakClient, BleakScanner
    device = await BleakScanner.find_device_by_filter(
        lambda device, advert: (advert.local_name or device.name or "") == name, timeout=20.0)
    if device is None:
        raise ProtocolError("Device not found; wake its screen, enable Bluetooth and close DiverLog+")
    async with BleakClient(device, timeout=20.0) as client:
        connection = Connection(client=client, name=name)
        await connection.start()
        yield connection
    # QUIT is omitted because it made subsequent connections unreliable in the investigation.


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)
    for command in ("inspect", "reset-history", "restore-history"):
        child = commands.add_parser(command)
        child.add_argument("--name", required=True, type=validate_name)
        if command != "inspect":
            child.add_argument("--backup", required=True, type=Path)
            flag = "--confirm-reset-history" if command == "reset-history" else "--confirm-restore-history"
            child.add_argument(flag, required=True, action="store_true")
    return result


async def run(args: argparse.Namespace) -> dict[str, Any]:
    if args.command == "reset-history" and args.backup.exists():
        raise FileExistsError("Use a new backup directory; existing backups are never overwritten")
    if args.command == "restore-history":
        load_backup(args.backup, name=args.name)
    async with connect(name=args.name) as connection:
        if args.command == "inspect":
            snapshot = await connection.snapshot()
            return {"version": SUPPORTED_VERSION.decode(), "history": history_values(snapshot.history),
                    "memory_write_attempts": 0}
        if args.command == "reset-history":
            return await reset_history(connection, backup=args.backup)
        return await restore_history(connection, backup=args.backup)


def main() -> None:
    args = parser().parse_args()
    try:
        print(json.dumps(asyncio.run(run(args)), indent=2))
    except (ProtocolError, TimeoutError, OSError, ValueError, KeyError) as error:
        raise SystemExit(f"Stopped: {str(error) or type(error).__name__}. No automatic retry.") from None


if __name__ == "__main__":
    main()
