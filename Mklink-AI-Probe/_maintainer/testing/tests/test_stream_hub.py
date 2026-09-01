import asyncio
import threading
from dataclasses import FrozenInstanceError

import pytest

from mklink.remote.stream_hub import StreamBatch, StreamHub


def test_stream_batch_freezes_a_publish_timestamp():
    first = StreamBatch(b"one", 1, 1)
    second = StreamBatch(b"two", 2, 1)

    assert first.timestamp_ns > 0
    assert second.timestamp_ns >= first.timestamp_ns


def test_stream_batch_repr_does_not_expose_payload_bytes():
    batch = StreamBatch(b"private-target-bytes", 1, 20)

    assert "private-target-bytes" not in repr(batch)


def test_each_subscriber_has_an_independent_bounded_queue():
    async def scenario():
        hub = StreamHub(max_batches_per_client=2)
        first = hub.subscribe()
        second = hub.subscribe()

        sequence = hub.publish(b"one", item_count=3)

        first_batch = await first.get()
        second_batch = await second.get()
        assert first is not second
        assert first.maxsize == second.maxsize == 2
        assert first_batch == second_batch == b"one"
        assert first_batch is second_batch
        assert first_batch.payload == b"one"
        assert first_batch.sequence == second_batch.sequence == sequence == 1
        assert first_batch.item_count == second_batch.item_count == 3

    asyncio.run(scenario())


def test_slow_client_drops_exactly_one_oldest_batch_when_full():
    async def scenario():
        hub = StreamHub(max_batches_per_client=2)
        client = hub.subscribe()
        hub.publish(b"one", item_count=1)
        hub.publish(b"two-two", item_count=2)
        hub.publish(b"three", item_count=3)

        assert await client.get() == b"two-two"
        client.task_done()
        assert await client.get() == b"three"
        client.task_done()
        await asyncio.wait_for(client.join(), timeout=0.1)
        stats = hub.stats()
        assert stats.produced_batches == 3
        assert stats.produced_items == 6
        assert stats.produced_bytes == 3 + 7 + 5
        assert stats.delivered_batches == 3
        assert stats.delivered_items == 6
        assert stats.delivered_bytes == 3 + 7 + 5
        assert stats.dropped_batches == 1
        assert stats.dropped_items == 1
        assert stats.dropped_bytes == 3
        assert stats.queue_high_water_mark == 2

    asyncio.run(scenario())


def test_fast_and_slow_clients_do_not_share_backpressure_or_drops():
    async def scenario():
        hub = StreamHub(max_batches_per_client=2)
        fast = hub.subscribe()
        slow = hub.subscribe()

        hub.publish(b"one", item_count=1)
        assert await fast.get() == b"one"
        hub.publish(b"two", item_count=1)
        assert await fast.get() == b"two"
        hub.publish(b"three", item_count=1)

        assert await fast.get() == b"three"
        assert await slow.get() == b"two"
        assert await slow.get() == b"three"
        assert hub.stats().dropped_batches == 1

    asyncio.run(scenario())


def test_sequence_increments_once_per_publish_and_is_shared_by_clients():
    async def scenario():
        hub = StreamHub(max_batches_per_client=3)
        first = hub.subscribe()
        second = hub.subscribe()

        assert hub.publish(b"a", item_count=1) == 1
        assert hub.publish(b"b", item_count=1) == 2

        assert [(await first.get()).sequence, (await first.get()).sequence] == [1, 2]
        assert [(await second.get()).sequence, (await second.get()).sequence] == [1, 2]
        assert hub.stats().last_sequence == 2

    asyncio.run(scenario())


def test_stats_and_status_frame_are_non_resetting_snapshots():
    async def scenario():
        hub = StreamHub(max_batches_per_client=1)
        client = hub.subscribe()
        hub.publish(b"first", item_count=2)
        hub.publish(b"second", item_count=4)
        await asyncio.sleep(0)

        first = hub.stats()
        status = hub.status_frame()
        second = hub.stats()
        assert status == first == second
        assert status.active_clients == 1
        assert status.produced_batches == 2
        assert status.delivered_batches == 2
        assert status.dropped_batches == 1
        assert status.last_sequence == 2
        assert await client.get() == b"second"

    asyncio.run(scenario())


def test_publish_threadsafe_never_waits_for_a_full_client_queue():
    async def scenario():
        hub = StreamHub(max_batches_per_client=1)
        client = hub.subscribe()
        hub.publish(b"old", item_count=1)
        loop = asyncio.get_running_loop()
        returned = []

        def producer():
            returned.append(hub.publish_threadsafe(loop, b"new", item_count=2))

        thread = threading.Thread(target=producer)
        thread.start()
        thread.join(timeout=0.5)
        assert not thread.is_alive()
        await asyncio.sleep(0)

        batch = await client.get()
        assert batch == b"new"
        assert returned == [2]
        assert batch.sequence == 2
        assert hub.stats().dropped_batches == 1

    asyncio.run(scenario())


def test_publish_threadsafe_accepts_a_batch_without_an_item_count():
    async def scenario():
        hub = StreamHub(max_batches_per_client=1)
        client = hub.subscribe()

        assert hub.publish_threadsafe(asyncio.get_running_loop(), b"raw") == 1
        await asyncio.sleep(0)

        assert await client.get() == b"raw"
        assert hub.stats().produced_items == 0

    asyncio.run(scenario())


def test_direct_publish_from_worker_runs_all_queue_operations_on_owner_loop():
    async def scenario():
        hub = StreamHub(max_batches_per_client=1)
        client = hub.subscribe()
        owner_thread = threading.get_ident()
        queue_threads = []
        original_put = client.put_nowait

        def recording_put(batch):
            queue_threads.append(threading.get_ident())
            original_put(batch)

        client.put_nowait = recording_put
        producer = threading.Thread(target=hub.publish, args=(b"worker", 1))
        producer.start()
        producer.join(timeout=0.5)
        assert not producer.is_alive()
        await asyncio.sleep(0)

        assert queue_threads == [owner_thread]
        assert await asyncio.wait_for(client.get(), timeout=0.1) == b"worker"

    asyncio.run(scenario())


def test_direct_publish_from_worker_wakes_a_waiting_owner_loop_immediately():
    async def scenario():
        hub = StreamHub(max_batches_per_client=1)
        client = hub.subscribe()
        waiting = asyncio.create_task(client.get())
        await asyncio.sleep(0)
        delayed_wakeup = []
        timer = asyncio.get_running_loop().call_later(
            0.2, delayed_wakeup.append, True
        )
        producer = threading.Thread(target=hub.publish, args=(b"wake", 1))

        try:
            producer.start()
            assert await asyncio.wait_for(waiting, timeout=0.5) == b"wake"
            assert delayed_wakeup == []
        finally:
            timer.cancel()
            producer.join(timeout=0.5)

    asyncio.run(scenario())


def test_mixed_direct_and_threadsafe_publish_preserve_sequence_order():
    async def scenario():
        hub = StreamHub(max_batches_per_client=2)
        client = hub.subscribe()
        loop = asyncio.get_running_loop()
        producer = threading.Thread(
            target=hub.publish_threadsafe, args=(loop, b"first", 1)
        )

        producer.start()
        producer.join(timeout=0.5)
        assert not producer.is_alive()
        assert hub.publish(b"second", item_count=1) == 2
        first = await asyncio.wait_for(client.get(), timeout=0.1)
        second = await asyncio.wait_for(client.get(), timeout=0.1)

        assert [first.sequence, second.sequence] == [1, 2]

    asyncio.run(scenario())


def test_publish_threadsafe_rejects_a_loop_other_than_the_owner():
    async def scenario():
        hub = StreamHub(max_batches_per_client=1)
        hub.subscribe()
        other_loop = asyncio.new_event_loop()
        try:
            with pytest.raises(RuntimeError, match="owner event loop"):
                hub.publish_threadsafe(other_loop, b"wrong", item_count=1)
        finally:
            other_loop.close()

    asyncio.run(scenario())


def test_subscribe_and_unsubscribe_reject_non_owner_loop():
    async def scenario():
        hub = StreamHub(max_batches_per_client=1)
        client = hub.subscribe()
        errors = []

        def use_another_loop():
            async def misuse():
                for operation in (hub.subscribe, lambda: hub.unsubscribe(client)):
                    try:
                        operation()
                    except RuntimeError as exc:
                        errors.append(str(exc))

            asyncio.run(misuse())

        thread = threading.Thread(target=use_another_loop)
        thread.start()
        thread.join(timeout=1)

        assert len(errors) == 2
        assert all("owner event loop" in message for message in errors)
        assert hub.stats().active_clients == 1

    asyncio.run(scenario())


def test_stream_batch_metadata_is_immutable_and_copies_mutable_input_once():
    async def scenario():
        hub = StreamHub(max_batches_per_client=2)
        first_client = hub.subscribe()
        second_client = hub.subscribe()
        source = bytearray(b"stable")
        hub.publish(source, item_count=6)
        source[:] = b"change"

        first = await first_client.get()
        second = await second_client.get()
        assert first is second
        assert first.payload == b"stable"
        assert first == b"stable"
        with pytest.raises(FrozenInstanceError):
            first.sequence = 99
        with pytest.raises(FrozenInstanceError):
            first.item_count = 0

    asyncio.run(scenario())


def test_unsubscribe_is_idempotent_and_releases_queued_batches():
    async def scenario():
        hub = StreamHub(max_batches_per_client=2)
        client = hub.subscribe()
        hub.publish(b"retained", item_count=1)

        assert hub.unsubscribe(client) is True
        assert hub.unsubscribe(client) is False
        assert client.empty()
        await asyncio.wait_for(client.join(), timeout=0.1)
        assert hub.stats().active_clients == 0
        hub.publish(b"after", item_count=1)
        assert client.empty()

    asyncio.run(scenario())


def test_unsubscribe_before_scheduled_delivery_prevents_retired_queue_enqueue():
    async def scenario():
        hub = StreamHub(max_batches_per_client=1)
        client = hub.subscribe()
        producer = threading.Thread(target=hub.publish, args=(b"late", 1))

        producer.start()
        producer.join(timeout=0.5)
        assert not producer.is_alive()
        assert hub.unsubscribe(client) is True
        await asyncio.sleep(0)

        assert client.empty()
        assert hub.stats().delivered_batches == 0

    asyncio.run(scenario())


def test_publish_without_subscribers_still_counts_production():
    hub = StreamHub(max_batches_per_client=1)

    assert hub.publish(memoryview(b"abc"), item_count=7) == 1

    stats = hub.stats()
    assert stats.produced_batches == 1
    assert stats.produced_items == 7
    assert stats.produced_bytes == 3
    assert stats.delivered_batches == 0
    assert stats.active_clients == 0


def test_subscriber_replay_precedes_live_data_without_lock_inversion():
    async def scenario():
        hub = StreamHub(max_batches_per_client=2)
        metadata_lock = threading.Lock()
        producer_has_metadata_lock = threading.Event()
        replay_started = threading.Event()

        def replay_metadata(enqueue_initial):
            replay_started.set()
            with metadata_lock:
                enqueue_initial(b"metadata", item_count=0, flags=1)

        def publish_while_metadata_locked():
            with metadata_lock:
                producer_has_metadata_lock.set()
                assert replay_started.wait(timeout=0.5)
                hub.publish(b"before-subscribe", item_count=1, flags=2)

        hub.set_subscribe_callback(replay_metadata)
        producer = threading.Thread(target=publish_while_metadata_locked)
        producer.start()
        assert producer_has_metadata_lock.wait(timeout=0.5)
        client = hub.subscribe()
        producer.join(timeout=0.5)
        assert not producer.is_alive()
        hub.publish(b"after-subscribe", item_count=1, flags=2)
        await asyncio.sleep(0)

        first = await asyncio.wait_for(client.get(), timeout=0.1)
        second = await asyncio.wait_for(client.get(), timeout=0.1)
        assert (first.flags, second.flags) == (1, 2)
        hub.unsubscribe(client)

    asyncio.run(scenario())


def test_subscriber_replay_survives_initial_queue_overflow():
    async def scenario():
        hub = StreamHub(max_batches_per_client=2)

        def replay_metadata(enqueue_initial):
            enqueue_initial(b"metadata", item_count=0, flags=1)

        hub.set_subscribe_callback(replay_metadata)
        client = hub.subscribe()
        hub.publish(b"first-live", item_count=1, flags=2)
        hub.publish(b"second-live", item_count=1, flags=2)
        await asyncio.sleep(0)

        first = await asyncio.wait_for(client.get(), timeout=0.1)
        second = await asyncio.wait_for(client.get(), timeout=0.1)
        assert first.payload == b"metadata"
        assert second.payload == b"first-live"
        client.task_done()
        client.task_done()
        await asyncio.wait_for(client.join(), timeout=0.1)

        hub.publish(b"third-live", item_count=1, flags=2)
        hub.publish(b"fourth-live", item_count=1, flags=2)
        hub.publish(b"fifth-live", item_count=1, flags=2)
        await asyncio.sleep(0)
        fourth = await asyncio.wait_for(client.get(), timeout=0.1)
        fifth = await asyncio.wait_for(client.get(), timeout=0.1)
        assert fourth.payload == b"fourth-live"
        assert fifth.payload == b"fifth-live"
        client.task_done()
        client.task_done()
        await asyncio.wait_for(client.join(), timeout=0.1)

        stats = hub.stats()
        assert stats.dropped_batches == 2
        assert stats.dropped_items == 2
        hub.unsubscribe(client)

    asyncio.run(scenario())


def test_owner_loop_is_released_after_last_subscriber_unsubscribes():
    hub = StreamHub(max_batches_per_client=1)

    async def first_service_loop():
        client = hub.subscribe()
        assert hub.unsubscribe(client) is True

    async def rebuilt_service_loop():
        client = hub.subscribe()
        hub.publish(b"rebuilt", item_count=1)
        assert await asyncio.wait_for(client.get(), timeout=0.1) == b"rebuilt"
        assert hub.unsubscribe(client) is True

    asyncio.run(first_service_loop())
    asyncio.run(rebuilt_service_loop())


def test_unsubscribe_remains_idempotent_after_owner_release():
    async def scenario():
        hub = StreamHub(max_batches_per_client=1)
        client = hub.subscribe()

        assert hub.unsubscribe(client) is True
        assert hub.unsubscribe(client) is False

    asyncio.run(scenario())


def test_pending_delivery_blocks_rebind_until_old_generation_drains():
    async def scenario():
        hub = StreamHub(max_batches_per_client=1)
        client = hub.subscribe()
        loop = asyncio.get_running_loop()
        scheduled = []
        original_schedule = loop.call_soon_threadsafe

        def capture(callback, *args):
            scheduled.append((callback, args))

        loop.call_soon_threadsafe = capture
        try:
            hub.publish(b"old", item_count=1)
        finally:
            loop.call_soon_threadsafe = original_schedule
        assert hub.unsubscribe(client) is True

        attempts = []

        def try_rebind():
            async def use_hub():
                try:
                    rebound = hub.subscribe()
                except RuntimeError as exc:
                    attempts.append(str(exc))
                else:
                    attempts.append("bound")
                    hub.unsubscribe(rebound)

            asyncio.run(use_hub())

        blocked = threading.Thread(target=try_rebind)
        blocked.start()
        blocked.join(timeout=1)
        assert attempts == ["owner event loop has pending deliveries"]

        callback, args = scheduled.pop()
        callback(*args)
        rebound = threading.Thread(target=try_rebind)
        rebound.start()
        rebound.join(timeout=1)
        assert attempts == ["owner event loop has pending deliveries", "bound"]

    asyncio.run(scenario())


def test_stale_closed_loop_callback_cannot_reach_new_generation_subscriber():
    hub = StreamHub(max_batches_per_client=2)
    scheduled = []

    async def old_service_loop():
        client = hub.subscribe()
        loop = asyncio.get_running_loop()
        original_schedule = loop.call_soon_threadsafe

        def capture(callback, *args):
            scheduled.append((callback, args))

        loop.call_soon_threadsafe = capture
        try:
            hub.publish(b"stale", item_count=1)
        finally:
            loop.call_soon_threadsafe = original_schedule
        assert hub.unsubscribe(client) is True

    async def rebuilt_service_loop():
        client = hub.subscribe()
        stale_callback, stale_args = scheduled.pop()
        asyncio.get_running_loop().call_soon(stale_callback, *stale_args)
        hub.publish(b"fresh", item_count=1)

        assert await asyncio.wait_for(client.get(), timeout=0.1) == b"fresh"
        await asyncio.sleep(0)
        assert client.empty()
        assert hub.stats().delivered_batches == 1
        assert hub.unsubscribe(client) is True

    asyncio.run(old_service_loop())
    asyncio.run(rebuilt_service_loop())


def test_threadsafe_publish_without_subscribers_does_not_bind_loop():
    hub = StreamHub(max_batches_per_client=1)

    async def producer_only_loop():
        assert hub.publish_threadsafe(
            asyncio.get_running_loop(), b"unobserved", item_count=4
        ) == 1

    async def later_subscriber_loop():
        client = hub.subscribe()
        assert hub.stats().produced_items == 4
        assert hub.unsubscribe(client) is True

    asyncio.run(producer_only_loop())
    asyncio.run(later_subscriber_loop())


@pytest.mark.parametrize("max_batches", [0, -1, True, 1.5])
def test_rejects_invalid_queue_capacity(max_batches):
    with pytest.raises((TypeError, ValueError)):
        StreamHub(max_batches_per_client=max_batches)


@pytest.mark.parametrize("item_count", [-1, True, 1.5])
def test_rejects_invalid_item_count(item_count):
    hub = StreamHub(max_batches_per_client=1)
    with pytest.raises((TypeError, ValueError)):
        hub.publish(b"data", item_count=item_count)


def test_rejects_non_bytes_batches():
    hub = StreamHub(max_batches_per_client=1)
    with pytest.raises(TypeError):
        hub.publish("not bytes", item_count=1)
