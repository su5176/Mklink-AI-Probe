from __future__ import annotations

import threading

import pytest

from mklink.serial._ymodem import (
    ACK,
    CAN,
    CPMEOF,
    CRC_REQUEST,
    EOT,
    NAK,
    SOH,
    STX,
    YModemCancelled,
    YModemSender,
    YModemTimeout,
    crc16_xmodem,
)


class Receiver:
    def __init__(self, *, reject_first_data: bool = False):
        self.responses = bytearray((CRC_REQUEST,))
        self.writes: list[bytes] = []
        self.eot_count = 0
        self.reject_first_data = reject_first_data
        self.rejected_data = False

    def read(self, _timeout: float) -> bytes:
        if not self.responses:
            return b""
        data = bytes(self.responses)
        self.responses.clear()
        return data

    def write(self, data: bytes) -> None:
        self.writes.append(data)
        if data == bytes((CAN, CAN)):
            return
        if data == bytes((EOT,)):
            self.eot_count += 1
            self.responses.extend((NAK,) if self.eot_count == 1 else (ACK, CRC_REQUEST))
            return
        marker, block = data[0], data[1]
        assert marker in (SOH, STX)
        if marker == SOH and block == 0 and any(data[3:-2]):
            self.responses.extend((ACK, CRC_REQUEST))
        elif marker == SOH and block == 0:
            self.responses.append(ACK)
        elif self.reject_first_data and not self.rejected_data:
            self.rejected_data = True
            self.responses.append(NAK)
        else:
            self.responses.append(ACK)


def payload(packet: bytes) -> bytes:
    return packet[3:-2]


def assert_packet_crc(packet: bytes) -> None:
    assert int.from_bytes(packet[-2:], "big") == crc16_xmodem(payload(packet))


def test_crc16_xmodem_known_vector():
    assert crc16_xmodem(b"123456789") == 0x31C3


def test_sender_emits_header_data_eot_and_empty_batch_header():
    receiver = Receiver()
    progress = []
    raw = bytes(index & 0xFF for index in range(1500))
    sender = YModemSender(receiver.read, receiver.write, progress_callback=progress.append)

    import io

    sender.send(io.BytesIO(raw), r"folder\rtthread.bin", len(raw))

    packets = [item for item in receiver.writes if len(item) > 2]
    assert [packet[0] for packet in packets] == [SOH, STX, STX, SOH]
    assert [packet[1] for packet in packets] == [0, 1, 2, 0]
    assert payload(packets[0]).startswith(b"rtthread.bin\x001500\x00")
    assert payload(packets[1]) == raw[:1024]
    assert payload(packets[2])[:476] == raw[1024:]
    assert payload(packets[2])[476:] == bytes((CPMEOF,)) * (1024 - 476)
    assert payload(packets[3]) == bytes(128)
    for packet in packets:
        assert_packet_crc(packet)
    assert receiver.eot_count == 2
    assert progress[-1].phase == "completed"
    assert progress[-1].sent_bytes == len(raw)
    assert progress[-1].percent == 100


def test_sender_retries_rejected_data_packet_without_advancing_source():
    receiver = Receiver(reject_first_data=True)
    progress = []
    raw = b"firmware" * 200
    sender = YModemSender(receiver.read, receiver.write, progress_callback=progress.append)

    import io

    sender.send(io.BytesIO(raw), "app.bin", len(raw))

    data_packets = [item for item in receiver.writes if len(item) > 2 and item[1] == 1]
    assert len(data_packets) == 2
    assert data_packets[0] == data_packets[1]
    assert any(item.phase == "retrying" and item.retries == 1 for item in progress)


def test_sender_wraps_data_block_sequence_from_255_to_zero():
    receiver = Receiver()
    block_count = 257
    raw = bytes(range(256)) * (block_count * 4)
    sender = YModemSender(receiver.read, receiver.write)

    import io

    sender.send(io.BytesIO(raw), "large.bin", len(raw))

    data_packets = [packet for packet in receiver.writes if packet[:1] == bytes((STX,))]
    assert len(data_packets) == block_count
    assert [data_packets[index][1] for index in (253, 254, 255, 256)] == [
        254, 255, 0, 1,
    ]
    for packet in data_packets[253:257]:
        assert packet[2] == 0xFF - packet[1]
        assert_packet_crc(packet)


def test_sender_times_out_and_sends_cancel_sequence():
    writes = []
    sender = YModemSender(
        lambda _timeout: b"",
        writes.append,
        handshake_timeout=0.001,
        retries=1,
    )

    import io

    with pytest.raises(YModemTimeout, match="receiver handshake"):
        sender.send(io.BytesIO(b"x"), "app.bin", 1)
    assert writes == [bytes((CAN, CAN))]


def test_sender_honours_local_cancellation_before_transmitting():
    cancel = threading.Event()
    cancel.set()
    writes = []
    sender = YModemSender(lambda _timeout: b"", writes.append, cancel_event=cancel)

    import io

    with pytest.raises(YModemCancelled, match="cancelled"):
        sender.send(io.BytesIO(b"x"), "app.bin", 1)
    assert writes == [bytes((CAN, CAN))]


def test_sender_honours_microboot_single_can_response():
    responses = bytearray((CRC_REQUEST,))
    writes = []

    def read(_timeout: float) -> bytes:
        data = bytes(responses)
        responses.clear()
        return data

    def write(data: bytes) -> None:
        writes.append(data)
        if len(data) > 2:
            responses.append(CAN)

    sender = YModemSender(read, write)

    import io

    with pytest.raises(YModemCancelled, match="receiver cancelled"):
        sender.send(io.BytesIO(b"x"), "app.bin", 1)
    assert writes[-1] == bytes((CAN, CAN))


def test_sender_rejects_header_that_cannot_fit():
    sender = YModemSender(lambda _timeout: b"", lambda _data: None)

    import io

    with pytest.raises(ValueError, match="128-byte header"):
        sender.send(io.BytesIO(b"x"), "x" * 128, 1)
