"""Unit tests for the NEO-PIT wire protocol (pit_protocol)."""

import struct

import pytest

from racecar_neo_ros2_driver import pit_protocol as pit


def _build_telemetry(timestamp=0, volt_curr=(0, 0), rc=(0,) * 8,
                     imu=(0.0,) * 9, encoder=0.0, ekf=(0.0,) * 3, good_crc=True):
    """Assemble a telemetry packet the way the firmware would, for decode tests."""
    header = struct.pack(
        '<IBBHIHH', pit.TX_MAGIC, pit.PROTO_VERSION, 0,
        pit.TX_PACKET_SIZE, pit.TX_PACKET_SIZE, 0, 1,
    )
    body = struct.pack('<i2H8H9ff3f', timestamp, *volt_curr, *rc, *imu, encoder, *ekf)
    pre = header + body
    crc = pit.crc16_ccitt(pre) if good_crc else (pit.crc16_ccitt(pre) ^ 0x1)
    return pre + struct.pack('<H', crc)


class TestSizes:
    def test_packet_sizes(self):
        assert pit.TX_PACKET_SIZE == 94
        assert pit.RX_BODY_SIZE == 470
        assert pit.RX_WIRE_SIZE == 474


class TestCrc:
    def test_known_answer(self):
        # CRC-16/CCITT-FALSE check value for the ASCII string "123456789".
        assert pit.crc16_ccitt(b'123456789') == 0x29B1

    def test_empty(self):
        assert pit.crc16_ccitt(b'') == 0xFFFF


class TestEncodeCommand:
    def test_wire_length_and_magic(self):
        wire = pit.encode_command(0.0, 0.0)
        assert len(wire) == pit.RX_WIRE_SIZE
        assert struct.unpack('<I', wire[:4])[0] == pit.RX_MAGIC

    def test_servo_motor_roundtrip(self):
        wire = pit.encode_command(0.5, -0.25, system_state=3)
        _, _, _, _, _, _, servo, motor = struct.unpack('<BBHIHHff', wire[4:24])
        assert servo == pytest.approx(0.5)
        assert motor == pytest.approx(-0.25)
        # system_state sits just before the 2-byte checksum.
        assert wire[-3] == 3

    def test_crc_covers_everything_but_the_checksum(self):
        wire = pit.encode_command(0.1, 0.2)
        assert pit.crc16_ccitt(wire[:-2]) == struct.unpack('<H', wire[-2:])[0]

    def test_display_payloads_are_padded(self):
        wire = pit.encode_command(0.0, 0.0, dot_matrix=b'\x01\x02', led=b'\xff')
        # dot_matrix starts after magic(4)+header(12)+servo(4)+motor(4) = 24.
        assert wire[24] == 1 and wire[25] == 2 and wire[26] == 0
        assert wire[24 + pit.DOT_MATRIX_LEN] == 0xFF

    def test_oversized_payloads_are_truncated(self):
        wire = pit.encode_command(0.0, 0.0, dot_matrix=b'\x01' * 500)
        assert len(wire) == pit.RX_WIRE_SIZE


class TestDecodeTelemetry:
    def test_roundtrip_fields(self):
        pkt = _build_telemetry(
            timestamp=123456, volt_curr=(111, 222), rc=tuple(range(1, 9)),
            imu=tuple(float(i) for i in range(9)), encoder=4900.0, ekf=(0.1, 0.2, 0.3),
        )
        t = pit.decode_telemetry(pkt)
        assert t.timestamp_us == 123456
        assert t.volt_curr == (111, 222)
        assert t.rc == tuple(range(1, 9))
        assert t.encoder == pytest.approx(4900.0)
        assert t.crc_ok is True

    def test_accessor_slices(self):
        imu = tuple(float(i) for i in range(9))
        t = pit.decode_telemetry(_build_telemetry(imu=imu))
        assert t.accel == (0.0, 1.0, 2.0)
        assert t.gyro == (3.0, 4.0, 5.0)
        assert t.mag == (6.0, 7.0, 8.0)

    def test_bad_crc_flag(self):
        t = pit.decode_telemetry(_build_telemetry(good_crc=False))
        assert t.crc_ok is False

    def test_wrong_length_raises(self):
        with pytest.raises(ValueError):
            pit.decode_telemetry(b'\x00' * 10)

    def test_bad_magic_raises(self):
        pkt = bytearray(_build_telemetry())
        pkt[0] ^= 0xFF
        with pytest.raises(ValueError):
            pit.decode_telemetry(bytes(pkt))


class TestFraming:
    def test_strips_leading_garbage(self):
        pkt = _build_telemetry(timestamp=42)
        buf = bytearray(b'\xAA\xBB\xCC' + pkt)
        telem, consumed = pit.scan_for_packet(buf)
        assert telem.timestamp_us == 42
        assert consumed == pit.TX_PACKET_SIZE
        assert len(buf) == 0

    def test_waits_for_full_packet(self):
        pkt = _build_telemetry()
        buf = bytearray(pkt[:20])
        telem, consumed = pit.scan_for_packet(buf)
        assert telem is None and consumed == 0
        assert len(buf) == 20

    def test_two_packets_back_to_back(self):
        buf = bytearray(_build_telemetry(timestamp=1) + _build_telemetry(timestamp=2))
        first, _ = pit.scan_for_packet(buf)
        second, _ = pit.scan_for_packet(buf)
        assert first.timestamp_us == 1
        assert second.timestamp_us == 2

    def test_no_magic_keeps_tail(self):
        buf = bytearray(b'\x00' * 50)
        telem, dropped = pit.scan_for_packet(buf)
        assert telem is None
        assert dropped == 47 and len(buf) == 3
