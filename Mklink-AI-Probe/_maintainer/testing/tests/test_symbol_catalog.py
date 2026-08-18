from __future__ import annotations

import math
import struct

import pytest

from mklink.dwarf_parser import (
    DwarfEnum,
    DwarfArray,
    DwarfInfo,
    DwarfMember,
    DwarfStruct,
    DwarfVariable,
)
from mklink.device import Device
from mklink.elf_backend import ElfSection
from mklink.c_layout import CLayoutError
from mklink.symbol_catalog import (
    SymbolCatalog,
    SymbolCatalogError,
    SymbolValueError,
    decode_descriptor,
    encode_descriptor,
    rebind_paths,
)


def _dwarf_fixture() -> DwarfInfo:
    info = DwarfInfo()
    info.base_types = {
        1: ("float", 4),
        2: ("int16_t", 2),
        3: ("uint32_t", 4),
        4: ("bool", 1),
    }
    info.typedefs = {10: ("gain_t", 1)}
    info.pointers = {20: (3, 4)}
    info.arrays = {
        21: (2, 16),
        22: (30, 56),
    }
    info.structs = {
        "Controller": DwarfStruct(
            name="Controller",
            offset=30,
            size=28,
            members=[
                DwarfMember("target", offset=0, type_offset=1, type_name="float", size=4),
                DwarfMember("enabled", offset=4, type_offset=4, type_name="bool", size=1),
                DwarfMember("samples", offset=6, type_offset=21, type_name="int16_t[]", size=16),
                DwarfMember("next", offset=24, type_offset=20, type_name="uint32_t*", size=4),
            ],
        )
    }
    info.enums = {
        "Mode": DwarfEnum("Mode", offset=40, size=4, values={0: "OFF", 1: "RUN"})
    }
    info.variables = {
        "gain": DwarfVariable("gain", 100, type_offset=10, address=0x20000010, size=4, type_name="gain_t"),
        "controller": DwarfVariable("controller", 101, type_offset=30, address=0x20000020, size=28, type_name="Controller"),
        "mode": DwarfVariable("mode", 102, type_offset=40, address=0x20000040, size=4, type_name="Mode"),
        "flash_constant": DwarfVariable("flash_constant", 103, type_offset=3, address=0x08001000, size=4, type_name="uint32_t"),
        "local_temp": DwarfVariable("local_temp", 104, type_offset=2, address=0x00000120, size=2, type_name="int16_t"),
        "unresolved": DwarfVariable("unresolved", 105, type_offset=3, address=None, size=4, type_name="uint32_t"),
        "buffer": DwarfVariable("buffer", 106, type_offset=21, address=0x20000060, size=16, type_name="int16_t[]"),
        "next": DwarfVariable("next", 107, type_offset=20, address=0x20000080, size=4, type_name="uint32_t*"),
        "controllers": DwarfVariable("controllers", 108, type_offset=22, address=0x20000100, size=56, type_name="Controller[]"),
    }
    return info


def test_catalog_keeps_ram_scalars_and_expands_struct_members(tmp_path):
    axf = tmp_path / "app.axf"
    axf.write_bytes(b"axf")

    catalog = SymbolCatalog.from_dwarf(
        _dwarf_fixture(),
        axf_path=str(axf),
        generation=3,
        ram_ranges=[(0x20000000, 0x20010000)],
    )

    paths = {item.path for item in catalog.items}
    assert {"controller.enabled", "controller.target", "gain", "mode"} <= paths
    assert {f"buffer[{index}]" for index in range(8)} <= paths
    assert {f"controller.samples[{index}]" for index in range(8)} <= paths
    assert "controllers[1].target" in paths
    assert "controllers[1].samples[7]" in paths
    assert catalog.by_path("gain").type_name == "gain_t"
    assert catalog.by_path("gain").scalar_kind == "float"
    assert catalog.by_path("controller.target").address == 0x20000020
    assert catalog.by_path("controller.enabled").parent_path == "controller"
    assert catalog.by_path("buffer[0]").parent_path == "buffer"
    assert catalog.by_path("controllers[1].target").address == 0x2000011C
    assert catalog.by_path("mode").enum_values == {"OFF": 0, "RUN": 1}
    assert catalog.generation == 3


def test_catalog_rejects_flash_locals_unresolved_and_pointers(tmp_path):
    axf = tmp_path / "app.axf"
    axf.write_bytes(b"axf")
    catalog = SymbolCatalog.from_dwarf(
        _dwarf_fixture(),
        axf_path=str(axf),
        ram_ranges=[(0x20000000, 0x20010000)],
    )

    for path in ("flash_constant", "local_temp", "unresolved", "next"):
        assert catalog.by_path(path) is None
    assert catalog.by_path("controller.next") is None


def test_catalog_pages_large_array_without_losing_later_elements(tmp_path):
    axf = tmp_path / "app.axf"
    axf.write_bytes(b"axf")
    info = _dwarf_fixture()
    info.arrays[23] = (3, 300 * 4)
    info.variables["values"] = DwarfVariable(
        "values", 109, type_offset=23, address=0x20001000, size=300 * 4,
        type_name="uint32_t[]",
    )

    catalog = SymbolCatalog.from_dwarf(
        info,
        axf_path=str(axf),
        ram_ranges=[(0x20000000, 0x20010000)],
        max_leaves_per_root=256,
    )

    values = [item for item in catalog.items if item.parent_path == "values"]
    assert len(values) == 256
    assert catalog.by_path("values[255]") is not None
    assert catalog.by_path("values[256]").address == 0x20001000 + 256 * 4
    assert catalog.by_path("values[299]").address == 0x20001000 + 299 * 4
    assert catalog.truncated_roots == ()
    assert [item.path for item in values[:12]] == [
        f"values[{index}]" for index in range(12)
    ]
    root = next(item for item in catalog.browse_roots() if item.path == "values")
    assert root.kind == "branch"
    assert root.child_count == 300
    assert root.array_dimensions == (300,)
    assert root.snapshot_eligible is True
    ranges = catalog.browse_children("values")
    assert [(item.label, item.range_start, item.range_end) for item in ranges] == [
        ("[0..255]", 0, 255),
        ("[256..299]", 256, 299),
    ]
    tail = catalog.browse_children("values", offset=256)
    assert len(tail) == 44
    assert tail[0].descriptor.path == "values[256]"
    assert tail[-1].descriptor.path == "values[299]"


def test_catalog_resolves_one_dimensional_scalar_array_snapshots(tmp_path):
    axf = tmp_path / "app.axf"
    axf.write_bytes(b"axf")
    catalog = SymbolCatalog.from_dwarf(
        _dwarf_fixture(),
        axf_path=str(axf),
        ram_ranges=[(0x20000000, 0x20010000)],
    )

    descriptors = catalog.array_descriptors("buffer")

    assert [item.path for item in descriptors] == [
        f"buffer[{index}]" for index in range(8)
    ]
    assert [item.address for item in descriptors] == [
        0x20000060 + index * 2 for index in range(8)
    ]
    with pytest.raises(SymbolCatalogError, match="not an array"):
        catalog.array_descriptors("buffer[0]")
    with pytest.raises(SymbolCatalogError, match="scalar numeric"):
        catalog.array_descriptors("controllers")


def test_catalog_pages_struct_arrays_by_direct_element(tmp_path):
    axf = tmp_path / "app.axf"
    axf.write_bytes(b"axf")
    info = _dwarf_fixture()
    info.arrays[24] = DwarfArray(24, element_type_offset=30, dimensions=(300,), size=300 * 28)
    info.variables["many_controllers"] = DwarfVariable(
        "many_controllers", 110, 24, 0x20001000, 300 * 28, "Controller[]",
    )
    catalog = SymbolCatalog.from_dwarf(
        info,
        axf_path=str(axf),
        ram_ranges=[(0x20000000, 0x20010000)],
    )

    ranges = catalog.browse_children("many_controllers")
    assert [item.label for item in ranges] == ["[0..255]", "[256..299]"]
    tail = catalog.browse_children("many_controllers", offset=256)
    assert len(tail) == 44
    assert all(item.kind == "branch" for item in tail)
    members = catalog.browse_children("many_controllers[299]")
    assert {item.label for item in members} >= {"target", "enabled", "samples"}
    samples = catalog.browse_children("many_controllers[299].samples")
    assert samples[-1].descriptor.path == "many_controllers[299].samples[7]"
    assert catalog.by_path("many_controllers[299].target").address == 0x20001000 + 299 * 28


def test_catalog_pages_each_dimension_of_nested_arrays(tmp_path):
    axf = tmp_path / "app.axf"
    axf.write_bytes(b"axf")
    info = DwarfInfo(
        base_types={1: ("uint32_t", 4)},
        arrays={2: DwarfArray(2, element_type_offset=1, dimensions=(4, 300), size=4 * 300 * 4)},
        variables={
            "matrix": DwarfVariable(
                "matrix", 100, 2, 0x20001000, 4 * 300 * 4, "uint32_t[][]",
            ),
        },
    )
    catalog = SymbolCatalog.from_dwarf(
        info,
        axf_path=str(axf),
        ram_ranges=[(0x20000000, 0x20010000)],
    )

    rows = catalog.browse_children("matrix")
    root = next(item for item in catalog.browse_roots() if item.path == "matrix")
    assert root.array_dimensions == (4, 300)
    assert root.snapshot_eligible is False
    assert [item.path for item in rows] == [f"matrix[{index}]" for index in range(4)]
    assert all(item.child_count == 300 for item in rows)
    ranges = catalog.browse_children("matrix[2]")
    assert [item.label for item in ranges] == ["[0..255]", "[256..299]"]
    tail = catalog.browse_children("matrix[2]", offset=256)
    assert tail[-1].descriptor.path == "matrix[2][299]"
    assert catalog.by_path("matrix[2][299]").address == 0x20001000 + (2 * 300 + 299) * 4


def test_device_reads_and_writes_array_elements_beyond_initial_catalog_page(tmp_path):
    axf = tmp_path / "app.axf"
    axf.write_bytes(b"axf")
    info = DwarfInfo(
        base_types={1: ("uint32_t", 4)},
        arrays={2: DwarfArray(2, element_type_offset=1, dimensions=(1000,), size=4000)},
        variables={
            "values": DwarfVariable(
                "values", 100, 2, 0x20001000, 4000, "uint32_t[]",
            ),
        },
    )
    catalog = SymbolCatalog.from_dwarf(
        info,
        axf_path=str(axf),
        ram_ranges=[(0x20000000, 0x20010000)],
    )
    device = Device(axf=str(axf))
    device._connected = True
    device._bridge = object()
    device._dwarf_info = info
    device._symbol_catalog = catalog
    writes = []
    device.read_memory = lambda address, size: struct.pack("<I", 0x12345678)
    device.write_memory = lambda address, data: writes.append((address, data))

    assert device.read_variable("values[999]") == 0x12345678
    device.write_variable("values[999]", 0xAABBCCDD)

    assert writes == [(0x20001000 + 999 * 4, struct.pack("<I", 0xAABBCCDD))]


def test_catalog_does_not_report_truncation_for_unsupported_tail_after_exact_limit(tmp_path):
    axf = tmp_path / "app.axf"
    axf.write_bytes(b"axf")
    info = DwarfInfo(
        base_types={1: ("uint32_t", 4)},
        arrays={2: (1, 256 * 4)},
        pointers={3: (1, 4)},
    )
    holder = DwarfStruct(
        name="Holder",
        offset=4,
        size=256 * 4 + 4,
        members=[
            DwarfMember("values", 0, 2, "uint32_t[]", 256 * 4),
            DwarfMember("next", 256 * 4, 3, "uint32_t*", 4),
        ],
    )
    info.structs = {"Holder": holder}
    info.records_by_offset = {4: holder}
    info.variables = {
        "holder": DwarfVariable(
            "holder", 100, 4, 0x20000000, holder.size, "Holder",
        ),
    }

    catalog = SymbolCatalog.from_dwarf(
        info,
        axf_path=str(axf),
        ram_ranges=[(0x20000000, 0x20010000)],
        max_leaves_per_root=256,
    )

    assert len(catalog.items) == 256
    assert catalog.truncated_roots == ()


def test_catalog_lists_union_aliases_and_marks_overlapping_storage(tmp_path):
    axf = tmp_path / "app.axf"
    axf.write_bytes(b"axf")
    info = DwarfInfo(base_types={1: ("uint32_t", 4)})
    union = DwarfStruct(
        name="Alias",
        offset=50,
        size=4,
        members=[
            DwarfMember("word", offset=0, type_offset=1, type_name="uint32_t", size=4),
            DwarfMember("same_word", offset=0, type_offset=1, type_name="uint32_t", size=4),
        ],
    )
    union.kind = "union"
    info.structs = {"Alias": union}
    info.records_by_offset = {50: union}
    info.variables = {
        "alias": DwarfVariable("alias", 100, 50, 0x20000020, 4, "Alias"),
    }

    catalog = SymbolCatalog.from_dwarf(
        info, axf_path=str(axf), ram_ranges=[(0x20000000, 0x20010000)]
    )

    assert [item.path for item in catalog.items] == ["alias.same_word", "alias.word"]
    assert {item.address for item in catalog.items} == {0x20000020}
    assert all(item.overlapping for item in catalog.items)


def test_catalog_transparently_expands_anonymous_struct_and_union_members(tmp_path):
    axf = tmp_path / "app.axf"
    axf.write_bytes(b"axf")
    info = DwarfInfo(base_types={1: ("uint32_t", 4), 2: ("float", 4)})
    inner = DwarfStruct(
        name="",
        offset=51,
        size=8,
        members=[
            DwarfMember("mileage", 0, 2, "float", 4),
            DwarfMember("zero", 4, 1, "uint32_t", 4),
        ],
    )
    union = DwarfStruct(
        name="",
        offset=52,
        size=8,
        members=[
            DwarfMember("odo", 0, 1, "uint32_t", 4),
            DwarfMember("", 0, 51, "anonymous struct", 8),
        ],
        kind="union",
    )
    outer = DwarfStruct(
        name="DATASAVE_TYPEDEF",
        offset=53,
        size=8,
        members=[DwarfMember("", 0, 52, "anonymous union", 8)],
    )
    info.records_by_offset = {51: inner, 52: union, 53: outer}
    info.structs = {"DATASAVE_TYPEDEF": outer}
    info.variables = {
        "data_save": DwarfVariable(
            "data_save", 100, 53, 0x20000648, 8, "DATASAVE_TYPEDEF",
        ),
    }

    catalog = SymbolCatalog.from_dwarf(
        info, axf_path=str(axf), ram_ranges=[(0x20000000, 0x20010000)]
    )

    assert [item.path for item in catalog.items] == [
        "data_save.mileage", "data_save.odo", "data_save.zero",
    ]
    assert catalog.require("data_save.mileage", 1).address == 0x20000648
    assert catalog.require("data_save.zero", 1).address == 0x2000064C
    assert all(item.overlapping for item in catalog.items)


def test_catalog_lists_unexpanded_aggregate_as_container(tmp_path):
    axf = tmp_path / "app.axf"
    axf.write_bytes(b"axf")
    opaque = DwarfStruct(
        name="Opaque",
        offset=54,
        size=4,
        members=[DwarfMember("bits", 0, 1, "uint32_t", 4, bit_size=3)],
    )
    info = DwarfInfo(
        base_types={1: ("uint32_t", 4)},
        structs={"Opaque": opaque},
        records_by_offset={54: opaque},
        variables={
            "opaque": DwarfVariable("opaque", 100, 54, 0x20000020, 4, "Opaque"),
        },
    )

    catalog = SymbolCatalog.from_dwarf(
        info, axf_path=str(axf), ram_ranges=[(0x20000000, 0x20010000)]
    )

    assert catalog.items == ()
    assert [item.to_dict() for item in catalog.containers] == [{
        "path": "opaque",
        "address": 0x20000020,
        "type_name": "Opaque",
        "size": 4,
        "reason": "unsupported_layout",
    }]
    assert catalog.to_page(query="opaque")["containers"][0]["path"] == "opaque"


def test_catalog_accepts_common_gcc_signed_integer_base_type_names(tmp_path):
    axf = tmp_path / "app.axf"
    axf.write_bytes(b"axf")
    info = DwarfInfo(
        base_types={
            1: ("short int", 2),
            2: ("long int", 4),
            3: ("long long int", 8),
        },
        variables={
            "short_value": DwarfVariable("short_value", 10, 1, 0x20000010, 2, "short int"),
            "long_value": DwarfVariable("long_value", 11, 2, 0x20000020, 4, "long int"),
            "wide_value": DwarfVariable("wide_value", 12, 3, 0x20000030, 8, "long long int"),
        },
    )

    catalog = SymbolCatalog.from_dwarf(
        info, axf_path=str(axf), ram_ranges=[(0x20000000, 0x20010000)]
    )

    assert [(item.path, item.scalar_kind, item.size) for item in catalog.items] == [
        ("long_value", "signed", 4),
        ("short_value", "signed", 2),
        ("wide_value", "signed", 8),
    ]


def test_catalog_uses_dwarf_encoding_for_plain_char_signedness(tmp_path):
    axf = tmp_path / "app.axf"
    axf.write_bytes(b"axf")
    info = DwarfInfo(
        base_types={1: ("char", 1), 2: ("char", 1)},
        base_type_encodings={1: 8, 2: 6},
        variables={
            "unsigned_char": DwarfVariable("unsigned_char", 10, 1, 0x20000010, 1, "char"),
            "signed_char": DwarfVariable("signed_char", 11, 2, 0x20000011, 1, "char"),
        },
    )

    catalog = SymbolCatalog.from_dwarf(
        info, axf_path=str(axf), ram_ranges=[(0x20000000, 0x20010000)]
    )

    assert catalog.require("unsigned_char", 1).scalar_kind == "unsigned"
    assert decode_descriptor(catalog.require("unsigned_char", 1), b"\xff") == 255
    assert catalog.require("signed_char", 1).scalar_kind == "signed"
    assert decode_descriptor(catalog.require("signed_char", 1), b"\xff") == -1


@pytest.mark.parametrize(
    ("path", "value", "expected"),
    [
        ("gain", 1.5, struct.pack("<f", 1.5)),
        ("controller.enabled", True, b"\x01"),
        ("mode", "RUN", b"\x01\x00\x00\x00"),
    ],
)
def test_descriptor_encoding_and_decoding_is_little_endian(tmp_path, path, value, expected):
    axf = tmp_path / "app.axf"
    axf.write_bytes(b"axf")
    catalog = SymbolCatalog.from_dwarf(
        _dwarf_fixture(), axf_path=str(axf), ram_ranges=[(0x20000000, 0x20010000)]
    )
    descriptor = catalog.require(path, catalog.generation)

    encoded = encode_descriptor(descriptor, value)

    assert encoded == expected
    decoded = decode_descriptor(descriptor, encoded)
    if isinstance(value, float):
        assert decoded == pytest.approx(value)
    elif path == "mode":
        assert decoded == 1
    else:
        assert decoded is value


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_float_encoding_rejects_non_finite_values(tmp_path, value):
    axf = tmp_path / "app.axf"
    axf.write_bytes(b"axf")
    descriptor = SymbolCatalog.from_dwarf(
        _dwarf_fixture(), axf_path=str(axf), ram_ranges=[(0x20000000, 0x20010000)]
    ).by_path("gain")

    with pytest.raises(SymbolValueError, match="finite"):
        encode_descriptor(descriptor, value)


def test_enum_encoding_rejects_unknown_name(tmp_path):
    axf = tmp_path / "app.axf"
    axf.write_bytes(b"axf")
    descriptor = SymbolCatalog.from_dwarf(
        _dwarf_fixture(), axf_path=str(axf), ram_ranges=[(0x20000000, 0x20010000)]
    ).by_path("mode")

    with pytest.raises(SymbolValueError, match="enum"):
        encode_descriptor(descriptor, "INVALID")


def test_negative_enum_values_round_trip_as_signed(tmp_path):
    axf = tmp_path / "app.axf"
    axf.write_bytes(b"axf")
    info = DwarfInfo(
        enums={
            "State": DwarfEnum("State", offset=40, size=4, values={-1: "ERROR", 0: "IDLE"}),
        },
        variables={
            "state": DwarfVariable("state", 100, 40, 0x20000020, 4, "State"),
        },
    )
    descriptor = SymbolCatalog.from_dwarf(
        info, axf_path=str(axf), ram_ranges=[(0x20000000, 0x20010000)]
    ).require("state", 1)

    encoded = encode_descriptor(descriptor, "ERROR")

    assert descriptor.enum_signed is True
    assert encoded == b"\xff\xff\xff\xff"
    assert decode_descriptor(descriptor, encoded) == -1


def test_rebind_paths_reports_preserved_updated_and_removed(tmp_path):
    first_axf = tmp_path / "first.axf"
    second_axf = tmp_path / "second.axf"
    first_axf.write_bytes(b"first")
    second_axf.write_bytes(b"second")
    old = SymbolCatalog.from_dwarf(
        _dwarf_fixture(), axf_path=str(first_axf), generation=1, ram_ranges=[(0x20000000, 0x20010000)]
    )
    new_info = _dwarf_fixture()
    new_info.variables["gain"].address = 0x20000100
    del new_info.variables["mode"]
    new = SymbolCatalog.from_dwarf(
        new_info, axf_path=str(second_axf), generation=2, ram_ranges=[(0x20000000, 0x20010000)]
    )

    summary = rebind_paths(old, new, ["gain", "controller.target", "mode"])

    assert summary.preserved == ("controller.target",)
    assert summary.updated == ("gain",)
    assert summary.removed == ("mode",)


def test_device_reparse_publishes_catalog_atomically(tmp_path, monkeypatch):
    first_axf = tmp_path / "first.axf"
    second_axf = tmp_path / "second.axf"
    first_axf.write_bytes(b"first")
    second_axf.write_bytes(b"second")
    loads = iter([_dwarf_fixture(), RuntimeError("bad DWARF")])

    def load_dwarf(_path, **_kwargs):
        result = next(loads)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr("mklink.dwarf_parser.load_dwarf_info", load_dwarf)
    device = Device(axf=str(first_axf))

    first = device.reparse_axf_atomically()
    old_catalog = device.symbol_catalog
    failed = device.parse_axf(str(second_axf))

    assert first.generation == 1
    assert failed["loaded"] is False
    assert "bad DWARF" in failed["error"]
    assert device.symbol_catalog is old_catalog
    assert device.axf_status["axf_path"] == str(first_axf)
    assert device.axf_status["variable_count"] == len(first.items)
    assert device.axf_status["elf_backend"] == "builtin"
    assert device.axf_status["builtin_elf_available"] is True
    assert len(first.items) > 4


def test_device_reparse_uses_writable_elf_sections_for_hpm_dlm(tmp_path, monkeypatch):
    axf = tmp_path / "demo.elf"
    axf.write_bytes(b"elf")
    info = DwarfInfo(
        base_types={1: ("float", 4)},
        base_type_encodings={1: 4},
        variables={
            "vofa_test_sin": DwarfVariable(
                "vofa_test_sin", 100, 1, 0x0008035C, 4, "float"
            ),
            "flash_constant": DwarfVariable(
                "flash_constant", 101, 1, 0x80003000, 4, "float"
            ),
        },
    )
    monkeypatch.setattr("mklink.dwarf_parser.load_dwarf_info", lambda *_args, **_kwargs: info)
    monkeypatch.setattr(
        "mklink.elf_backend.list_elf_sections",
        lambda *_args, **_kwargs: [
            ElfSection(".sbss", 0x00080300, 0x100, 0x3, "SHT_NOBITS"),
            ElfSection(".rodata", 0x80003000, 0x100, 0x2, "SHT_PROGBITS"),
        ],
    )

    catalog = Device(axf=str(axf)).reparse_axf_atomically()

    assert catalog.by_path("vofa_test_sin").address == 0x0008035C
    assert catalog.by_path("flash_constant") is None


def _opaque_layout_fixture() -> DwarfInfo:
    opaque = DwarfStruct(
        name="Opaque",
        offset=60,
        size=8,
        members=[DwarfMember("bits", 0, 1, "uint32_t", 4, bit_size=3)],
    )
    return DwarfInfo(
        base_types={1: ("uint32_t", 4)},
        structs={"Opaque": opaque},
        records_by_offset={60: opaque},
        variables={
            "opaque": DwarfVariable("opaque", 100, 60, 0x20000020, 8, "Opaque"),
        },
    )


def test_device_applies_and_reuses_c_layout_for_the_same_axf(tmp_path, monkeypatch):
    axf = tmp_path / "app.axf"
    axf.write_bytes(b"axf")
    info = _opaque_layout_fixture()
    catalog = SymbolCatalog.from_dwarf(
        info, axf_path=str(axf), ram_ranges=[(0x20000000, 0x20010000)]
    )
    device = Device(axf=str(axf))
    device._dwarf_info = info
    device._symbol_catalog = catalog
    definition = "typedef struct { uint32_t low; uint32_t high; } Opaque;"

    applied, layout = device.apply_c_definition("opaque", definition)

    assert layout.size == 8
    assert [item.path for item in applied.items] == ["opaque.high", "opaque.low"]
    assert all(item.source == "c_override" for item in applied.items)
    assert [item.path for item in applied.browse_children("opaque")] == [
        "opaque.high", "opaque.low",
    ]
    monkeypatch.setattr("mklink.dwarf_parser.load_dwarf_info", lambda *_args, **_kwargs: info)
    reparsed = device.reparse_axf_atomically()
    assert [item.path for item in reparsed.items] == ["opaque.high", "opaque.low"]

    axf.write_bytes(b"rebuilt-axf")
    rebuilt = device.reparse_axf_atomically()
    assert rebuilt.items == ()
    assert [item.path for item in rebuilt.containers] == ["opaque"]


def test_device_rejects_c_layout_whose_size_differs_from_axf(tmp_path):
    axf = tmp_path / "app.axf"
    axf.write_bytes(b"axf")
    info = _opaque_layout_fixture()
    device = Device(axf=str(axf))
    device._dwarf_info = info
    device._symbol_catalog = SymbolCatalog.from_dwarf(
        info, axf_path=str(axf), ram_ranges=[(0x20000000, 0x20010000)]
    )

    with pytest.raises(CLayoutError, match="does not match AXF variable size"):
        device.apply_c_definition(
            "opaque", "typedef struct { uint32_t only; } Opaque;"
        )

    assert device.symbol_catalog.items == ()
    assert [item.path for item in device.symbol_catalog.containers] == ["opaque"]
