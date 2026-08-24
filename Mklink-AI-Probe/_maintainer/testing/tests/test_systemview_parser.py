from mklink.systemview_parser import (
    EVTID_INIT,
    EVTID_OVERFLOW,
    EVTID_STACK_INFO,
    EVTID_TASK_INFO,
    EVTID_TASK_START_EXEC,
    EVTID_TASK_STOP_EXEC,
    SystemViewParser,
)


def _encode_u32(value: int) -> bytes:
    encoded = bytearray()
    while value > 0x7F:
        encoded.append((value & 0x7F) | 0x80)
        value >>= 7
    encoded.append(value)
    return bytes(encoded)


def test_task_info_uses_segger_task_id_priority_name_order():
    task_id_raw = 0x123
    priority = 6
    name = b"svuser"
    timestamp_delta = 7
    packet = b"".join(
        (
            bytes((EVTID_TASK_INFO,)),
            _encode_u32(task_id_raw),
            _encode_u32(priority),
            bytes((len(name),)),
            name,
            _encode_u32(timestamp_delta),
        )
    )

    parser = SystemViewParser()
    events = parser.feed(packet)

    assert events == [
        {
            "kind": "task_info",
            "task_id_raw": task_id_raw,
            "task_id": task_id_raw << 2,
            "prio": priority,
            "name": "svuser",
            "delta_ticks": timestamp_delta,
            "t_ticks": timestamp_delta,
            "task_name": "svuser",
        }
    ]
    assert parser.task_name(task_id_raw << 2) == "svuser"


def test_hpm_init_preserves_zero_ram_base_and_id_shift():
    parser = SystemViewParser()

    parser._post_process({
        "kind": "init",
        "cpu_freq": 360_000_000,
        "ram_base": 0,
        "id_shift": 0,
        "delta_ticks": 0,
        "t_ticks": 0,
    })

    assert parser._ram_base == 0
    assert parser._id_shift == 0


def test_runtime_cpu_clock_override_survives_stale_init_packet():
    parser = SystemViewParser()
    parser.set_cpu_freq(360_000_000, lock=True)
    payload = b"".join(
        (
            _encode_u32(816_000_000),
            _encode_u32(816_000_000),
            _encode_u32(0),
            _encode_u32(2),
        )
    )
    packet = bytes((EVTID_INIT, len(payload))) + payload + _encode_u32(1)

    events = parser.feed(packet)

    assert events[0]["kind"] == "init"
    assert parser.cpu_freq == 360_000_000
    assert events[0]["t_us"] == 1_000_000 / 360_000_000


def test_stack_info_consumes_stack_end_before_timestamp_delta():
    task_id_raw = 0x123
    stack_base = 0x20001000
    stack_size = 1024
    stack_end = 0
    timestamp_delta = 7
    packet = b"".join(
        (
            bytes((EVTID_STACK_INFO,)),
            _encode_u32(task_id_raw),
            _encode_u32(stack_base),
            _encode_u32(stack_size),
            _encode_u32(stack_end),
            _encode_u32(timestamp_delta),
        )
    )

    parser = SystemViewParser()
    events = parser.feed(packet)

    assert events == [
        {
            "kind": "stack_info",
            "task_id_raw": task_id_raw,
            "task_id": task_id_raw << 2,
            "stack_base": stack_base,
            "stack_size": stack_size,
            "stack_end": stack_end,
            "delta_ticks": timestamp_delta,
            "t_ticks": timestamp_delta,
            "task_name": None,
        }
    ]


def test_task_info_rejects_non_printable_names_from_false_packet_alignment():
    task_id_raw = 6
    priority = 5
    name = b"n_rx\x00corrupt"
    invalid_packet = b"".join(
        (
            bytes((EVTID_TASK_INFO,)),
            _encode_u32(task_id_raw),
            _encode_u32(priority),
            bytes((len(name),)),
            name,
            _encode_u32(7),
        )
    )
    valid_packet = b"".join(
        (
            bytes((4,)),
            _encode_u32(0x123),
            _encode_u32(11),
        )
    )

    parser = SystemViewParser()
    events = parser.feed(invalid_packet + valid_packet)

    assert events == [
        {
            "kind": "task_start_exec",
            "task_id_raw": 0x123,
            "task_id": 0x123 << 2,
            "delta_ticks": 11,
            "t_ticks": 11,
            "task_name": None,
        }
    ]
    assert parser.task_name(task_id_raw << 2) is None
    assert parser.dropped_packets == 1


def test_overflow_recovery_does_not_swallow_stack_info_as_a_phantom_module_event():
    stack_packet = b"".join(
        (
            bytes((EVTID_STACK_INFO,)),
            _encode_u32(0x123),
            _encode_u32(0x20001000),
            _encode_u32(1024),
            _encode_u32(0),
            _encode_u32(7),
        )
    )
    # d1 05 is the tail of a packet timestamp seen at a recovery boundary.  If
    # accepted as module event 721, its following length byte (STACK_INFO=21)
    # causes the parser to consume the task metadata as opaque payload.
    stream = _encode_u32(721) + stack_packet

    parser = SystemViewParser()
    events = parser.feed(stream)

    assert [event["kind"] for event in events] == ["stack_info"]
    assert events[0]["task_id"] == 0x123 << 2
    assert events[0]["stack_size"] == 1024
    assert parser.dropped_bytes == 2


def test_overflow_clears_cached_task_for_following_stop_event():
    stream = b"".join(
        (
            bytes((EVTID_TASK_START_EXEC,)),
            _encode_u32(7),
            _encode_u32(1),
            bytes((EVTID_OVERFLOW,)),
            _encode_u32(3),
            _encode_u32(2),
            bytes((EVTID_TASK_STOP_EXEC,)),
            _encode_u32(1),
        )
    )

    events = SystemViewParser().feed(stream)

    assert [event["kind"] for event in events] == [
        "task_start_exec", "overflow", "task_stop_exec",
    ]
    assert events[1]["drop_count"] == 3
    assert "task_id" not in events[2]
