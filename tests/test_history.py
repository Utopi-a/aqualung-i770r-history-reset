import asyncio
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

import i770r_history as tool

NAME = "FQ123456"  # Synthetic identifier; not the investigated device.


def initial_low() -> bytes:
    low = bytearray(256)
    low[8:10] = b"FQ"
    low[10:13] = bytes.fromhex("123456")
    low[16:64] = bytes(range(48))
    low[0x40:0x50] = bytes(range(16))
    low[0x60:0x70] = bytes.fromhex("010000002a0002000300040005000000")
    return bytes(low)


class Characteristic:
    properties = ["write", "write-without-response"]


class Services:
    def get_service(self, uuid):
        return self if uuid == tool.SERVICE else None

    def get_characteristic(self, uuid):
        return Characteristic() if uuid in (tool.WRITE, tool.NOTIFY) else None


class FakeGatt:
    """The external GATT boundary, including independently framed device replies."""
    def __init__(self):
        self.services = Services()
        self.low = bytearray(initial_low())
        self.protected = {0x3FE00: bytes([0x12]) * 256, 0x3FF00: bytes([0x34]) * 256}
        self.received = []
        self.att_writes = []
        self.pending = bytearray()
        self.version = b"AQUA770R 2A 0006"
        self.reject_b2 = False
        self.silence_b2 = False
        self.corrupt_read_checksum = False
        self.wrong_sequence = False
        self.change_calibration_on_write = False
        self.change_protected_on_write = False
        self.ignore_reset = False

    async def start_notify(self, uuid, callback):
        assert uuid == tool.NOTIFY
        self.callback = callback

    def respond(self, payload, sequence):
        for offset in range(0, len(payload), 16):
            fragment = payload[offset:offset + 16]
            more = offset + len(fragment) < len(payload)
            state = 0xC0 | (0x20 if more else 0) | offset // 16
            seq = (sequence + int(self.wrong_sequence)) & 255
            self.callback(None, bytearray([0xCD, state, seq, len(fragment)]) + fragment)

    async def write_gatt_char(self, uuid, frame, *, response):
        assert uuid == tool.WRITE and response is False
        assert len(frame) <= 20 and frame[0] == 0xCD and len(frame) == frame[3] + 4
        self.att_writes.append(bytes(frame))
        self.pending.extend(frame[4:])
        if frame[1] & 0x20:
            return
        command = bytes(self.pending)
        self.pending.clear()
        self.received.append(command)
        seq = frame[2]
        if command == b"\x84":
            self.respond(b"\x5a" + self.version + bytes([sum(self.version) & 255]), seq)
        elif command[0] == 0xE5:
            assert command == bytes.fromhex("e5010203040506000015")
            self.respond(b"\x5a", seq)
        elif command[0] == 0xB8:
            address = int.from_bytes(command[1:3], "big") * 16
            data = bytes(self.low) if address == 0 else self.protected[address]
            checksum = sum(data) ^ int(self.corrupt_read_checksum)
            self.respond(b"\x5a" + data + checksum.to_bytes(2, "little"), seq)
        elif command[0] == 0xB2:
            assert len(command) == 20 and command[1:3] == b"\x00\x06"
            assert command[19] == sum(command[3:19]) & 255
            if self.silence_b2:
                return
            if self.reject_b2:
                self.respond(b"\xa5", seq)
                return
            if not (self.ignore_reset and command[3:19] == bytes(16)):
                self.low[0x60:0x70] = command[3:19]
            if self.change_calibration_on_write:
                self.low[16] ^= 1
            if self.change_protected_on_write:
                self.protected[0x3FE00] = bytes(256)
            self.respond(b"\x5a", seq)
        else:
            raise AssertionError(f"Unexpected command: {command.hex()}")

    @property
    def memory_writes(self):
        return [command for command in self.received if command[0] == 0xB2]


class WireTests(unittest.TestCase):
    def test_zero_history_wire_matches_observed_complete_transaction_format(self):
        command = tool.history_command(bytes(16))
        self.assertEqual(command.hex(), "b20006" + "00" * 17)
        self.assertEqual(tool.command_frames(command, sequence=7), [
            bytes.fromhex("cd600710b20006" + "00" * 13),
            bytes.fromhex("cd41070400000000"),
        ])

    def test_nonzero_history_uses_sum8_not_crc_or_big_endian_page(self):
        history = bytes(range(16))
        self.assertEqual(tool.history_command(history), b"\xb2\x00\x06" + history + b"\x78")
        for length in (0, 15, 17):
            with self.assertRaises(ValueError):
                tool.history_command(bytes(length))

    def test_handshake_uses_decimal_digits_and_rejects_unknown_names(self):
        self.assertEqual(tool.handshake_command(NAME).hex(), "e5010203040506000015")
        for name in ("FQ12345", "XX123456", "FQ１２３４５６", "FQ1234567"):
            with self.assertRaises(ValueError):
                tool.handshake_command(name)

    def test_real_version_reply_checksum_and_rejection(self):
        reply = bytes.fromhex("5a4151554137373052203241203030303691")
        self.assertEqual(tool.checked_body(reply, size=16, checksum_size=1), tool.SUPPORTED_VERSION)
        with self.assertRaises(tool.ProtocolError):
            tool.checked_body(reply[:-1] + b"\x90", size=16, checksum_size=1)
        with self.assertRaises(tool.ProtocolError):
            tool.checked_body(reply + b"\x00", size=16, checksum_size=1)

    def test_mutations_need_an_explicit_cli_flag(self):
        for command in ("reset-history", "restore-history"):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                tool.parser().parse_args([command, "--name", NAME, "--backup", "backups/test"])
            self.assertEqual(error.exception.code, 2)


class DeviceContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.backup = Path(self.temp.name) / "backup"
        self.gatt = FakeGatt()
        self.connection = tool.Connection(client=self.gatt, name=NAME)
        await self.connection.start()

    async def test_inspection_only_reads_and_checks_device_identity(self):
        snapshot = await self.connection.snapshot()
        self.assertEqual(tool.history_values(snapshot.history)["total_dive"], 42)
        self.assertEqual(self.gatt.memory_writes, [])
        self.gatt.low[10] = 0x99
        with self.assertRaises(tool.ProtocolError):
            await self.connection.snapshot()
        self.assertEqual(self.gatt.memory_writes, [])

    async def test_reset_preserves_every_non_history_byte_and_protected_block(self):
        original_low = bytes(self.gatt.low)
        original_protected = dict(self.gatt.protected)
        result = await tool.reset_history(self.connection, backup=self.backup)
        expected = bytearray(original_low)
        expected[0x60:0x70] = bytes(16)
        self.assertEqual(bytes(self.gatt.low), bytes(expected))
        self.assertEqual(self.gatt.protected, original_protected)
        self.assertEqual(result["status"], "reset_verified")
        self.assertEqual(result["memory_write_attempts"], 2)
        self.assertEqual([value[3:19] for value in self.gatt.memory_writes], [original_low[0x60:0x70], bytes(16)])
        self.assertEqual((self.backup / "low-before.bin").read_bytes(), original_low)
        self.assertEqual(tool.load_backup(self.backup, name=NAME).low, original_low)
        if os.name == "posix":
            self.assertEqual(self.backup.stat().st_mode & 0o777, 0o700)
            self.assertEqual((self.backup / "low-before.bin").stat().st_mode & 0o777, 0o600)

    async def test_already_zero_is_a_noop(self):
        self.gatt.low[0x60:0x70] = bytes(16)
        result = await tool.reset_history(self.connection, backup=self.backup)
        self.assertEqual(result["status"], "already_zero")
        self.assertEqual(self.gatt.memory_writes, [])

    async def test_existing_backup_blocks_all_writes(self):
        self.backup.mkdir()
        with self.assertRaises(FileExistsError):
            await tool.reset_history(self.connection, backup=self.backup)
        self.assertEqual(self.gatt.memory_writes, [])

    async def test_bad_read_checksum_blocks_all_writes(self):
        self.gatt.corrupt_read_checksum = True
        with self.assertRaises(tool.ProtocolError):
            await tool.reset_history(self.connection, backup=self.backup)
        self.assertEqual(self.gatt.memory_writes, [])
        self.assertFalse(self.backup.exists())

    async def test_nak_stops_after_same_value_control(self):
        self.gatt.reject_b2 = True
        with self.assertRaises(tool.ProtocolError):
            await tool.reset_history(self.connection, backup=self.backup)
        self.assertEqual(len(self.gatt.memory_writes), 1)
        self.assertEqual(bytes(self.gatt.low), initial_low())
        self.assertFalse((self.backup / "reset-attempt.json").exists())
        self.assertEqual(json.loads((self.backup / "failure.json").read_text())["automatic_retry"], False)

    async def test_missing_ack_is_not_retried(self):
        self.gatt.silence_b2 = True
        self.connection.timeout = 0.2
        with self.assertRaises(TimeoutError):
            await tool.reset_history(self.connection, backup=self.backup)
        self.assertEqual(len(self.gatt.memory_writes), 1)
        self.assertFalse((self.backup / "reset-attempt.json").exists())

    async def test_unexpected_calibration_change_stops_before_reset(self):
        self.gatt.change_calibration_on_write = True
        with self.assertRaises(tool.ProtocolError):
            await tool.reset_history(self.connection, backup=self.backup)
        self.assertEqual(len(self.gatt.memory_writes), 1)
        self.assertEqual(bytes(self.gatt.low[0x60:0x70]), initial_low()[0x60:0x70])

    async def test_protected_change_stops_before_reset(self):
        self.gatt.change_protected_on_write = True
        with self.assertRaises(tool.ProtocolError):
            await tool.reset_history(self.connection, backup=self.backup)
        self.assertEqual(len(self.gatt.memory_writes), 1)

    async def test_ack_without_actual_reset_is_not_success(self):
        self.gatt.ignore_reset = True
        with self.assertRaises(tool.ProtocolError):
            await tool.reset_history(self.connection, backup=self.backup)
        self.assertEqual(len(self.gatt.memory_writes), 2)
        self.assertFalse((self.backup / "result.json").exists())

    async def test_history_only_restoration_and_repeat_is_noop(self):
        await tool.reset_history(self.connection, backup=self.backup)
        result = await tool.restore_history(self.connection, backup=self.backup)
        self.assertEqual(result["status"], "restore_verified")
        self.assertEqual(bytes(self.gatt.low), initial_low())
        self.assertEqual(len(self.gatt.memory_writes), 3)
        result = await tool.restore_history(self.connection, backup=self.backup)
        self.assertEqual(result["status"], "already_restored")
        self.assertEqual(len(self.gatt.memory_writes), 3)

    async def test_restore_with_missing_ack_cannot_be_repeated(self):
        await tool.reset_history(self.connection, backup=self.backup)
        self.gatt.silence_b2 = True
        self.connection.timeout = 0.2
        with self.assertRaises(TimeoutError):
            await tool.restore_history(self.connection, backup=self.backup)
        self.assertEqual(len(self.gatt.memory_writes), 3)
        with self.assertRaises(FileExistsError):
            await tool.restore_history(self.connection, backup=self.backup)
        self.assertEqual(len(self.gatt.memory_writes), 3)
        self.assertEqual(bytes(self.gatt.low[0x60:0x70]), bytes(16))

    async def test_tampered_or_other_device_backup_cannot_restore(self):
        await tool.reset_history(self.connection, backup=self.backup)
        with self.assertRaises(tool.ProtocolError):
            tool.load_backup(self.backup, name="FQ654321")
        original = (self.backup / "low-before.bin").read_bytes()
        (self.backup / "low-before.bin").write_bytes(original[:-1] + b"\xff")
        with self.assertRaises(tool.ProtocolError):
            await tool.restore_history(self.connection, backup=self.backup)
        self.assertEqual(len(self.gatt.memory_writes), 2)

    async def test_new_history_cannot_be_overwritten_by_old_backup(self):
        await tool.reset_history(self.connection, backup=self.backup)
        self.gatt.low[0x64:0x66] = b"\x01\x00"
        with self.assertRaises(tool.ProtocolError):
            await tool.restore_history(self.connection, backup=self.backup)
        self.assertEqual(len(self.gatt.memory_writes), 2)

    async def test_wrong_response_sequence_blocks_handshake_and_memory_writes(self):
        gatt = FakeGatt()
        gatt.wrong_sequence = True
        connection = tool.Connection(client=gatt, name=NAME)
        with self.assertRaises(tool.ProtocolError):
            await connection.start()
        self.assertEqual(gatt.received, [b"\x84"])

    async def test_unsupported_firmware_does_not_handshake_or_write(self):
        gatt = FakeGatt()
        gatt.version = b"AQUA770R 2A 9999"
        connection = tool.Connection(client=gatt, name=NAME)
        with self.assertRaises(tool.ProtocolError):
            await connection.start()
        self.assertEqual(gatt.received, [b"\x84"])


if __name__ == "__main__":
    unittest.main()
