import asyncio
import threading

from mklink.remote.serial_stream import SerialByteBatcher, SERIAL_BATCH_BYTES
from mklink.remote.stream_api import create_stream_registry


def test_serial_small_reads_survive_blocked_event_loop_without_byte_loss():
    async def scenario():
        hub = create_stream_registry()["serial"]
        queue = hub.subscribe()
        batcher = SerialByteBatcher(lambda data, direction: hub.publish(data, len(data)))
        original = b"".join(f"uart={i:08d},DATA_1234\r\n".encode() for i in range(2000))

        def producer():
            for index in range(0, len(original), 7):
                batcher.feed(original[index:index + 7], "RX")
            batcher.close()

        # Model SVD/symbol work preventing the loop from servicing subscriptions.
        worker = threading.Thread(target=producer)
        worker.start()
        worker.join(timeout=2)
        assert not worker.is_alive()
        await asyncio.sleep(0)
        payloads = []
        while not queue.empty():
            payloads.append(bytes(queue.get_nowait()))
            queue.task_done()
        assert b"".join(payloads) == original
        assert hub.stats().dropped_batches == 0
        assert all(0 < len(payload) <= SERIAL_BATCH_BYTES for payload in payloads)
        hub.unsubscribe(queue)

    asyncio.run(scenario())


def test_serial_batching_preserves_rx_tx_order_and_flushes_last_partial_on_close():
    batches = []
    batcher = SerialByteBatcher(lambda data, direction: batches.append((direction, data)))
    batcher.feed(b"\x00\xff", "RX")
    batcher.feed(b"\x80", "RX")
    batcher.feed(b"AT\r\n", "TX")
    batcher.feed(b"OK", "RX")
    batcher.close()
    batcher.close()
    assert batches == [("RX", b"\x00\xff\x80"), ("TX", b"AT\r\n"), ("RX", b"OK")]


def test_serial_single_byte_is_delivered_without_waiting_for_more_input():
    delivered = threading.Event()
    batches = []

    def publish(data, direction):
        batches.append((direction, data))
        delivered.set()

    batcher = SerialByteBatcher(publish)
    batcher.start()
    try:
        batcher.feed(b"C", "RX")
        assert delivered.wait(timeout=1)
        assert batches == [("RX", b"C")]
    finally:
        batcher.close()
