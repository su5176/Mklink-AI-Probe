import pytest
from elftools.dwarf.structs import DWARFStructs

from mklink.elf_backend import (
    ElfParseError,
    ElfSection,
    list_writable_object_symbols,
    writable_memory_ranges,
)
from mklink.elf_builtin import BuiltinElfBackend, _fixed_address, _member_offset


class FakeSymbol:
    def __init__(
        self,
        name,
        *,
        kind,
        address,
        size,
        section=1,
        binding="STB_GLOBAL",
        visibility="STV_DEFAULT",
    ):
        self.name = name
        self.values = {
            "st_info": {"type": kind, "bind": binding},
            "st_other": {"visibility": visibility},
            "st_value": address,
            "st_size": size,
            "st_shndx": section,
        }

    def __getitem__(self, key):
        return self.values[key]


class FakeSymbolTable:
    def __init__(self, symbols):
        self._symbols = symbols

    def iter_symbols(self):
        return iter(self._symbols)


class FakeSection:
    def __init__(self, name, *, address, size, flags, section_type):
        self.name = name
        self.values = {
            "sh_addr": address,
            "sh_size": size,
            "sh_flags": flags,
            "sh_type": section_type,
        }

    def __getitem__(self, key):
        return self.values[key]


class FakeElf:
    def __init__(self, symbols=(), sections=(), dwarf=None):
        self._symtab = FakeSymbolTable(symbols)
        self._sections = list(sections)
        self._dwarf = dwarf

    def get_section_by_name(self, name):
        return self._symtab if name == ".symtab" else None

    def iter_sections(self):
        return iter(self._sections)

    def has_dwarf_info(self):
        return self._dwarf is not None

    def get_dwarf_info(self):
        return self._dwarf


class FakeAttribute:
    def __init__(self, form, value):
        self.form = form
        self.value = value


class FakeDie:
    def __init__(self, tag, offset, *, attributes=None, children=(), refs=None):
        self.tag = tag
        self.offset = offset
        self.attributes = attributes or {}
        self._children = list(children)
        self._refs = refs or {}

    def iter_children(self):
        return iter(self._children)

    def get_DIE_from_attribute(self, key):
        return self._refs[key]


class FakeCu:
    def __init__(self, top):
        self._top = top

    def get_top_DIE(self):
        return self._top


class FakeDwarf:
    def __init__(self, top):
        self._cus = [FakeCu(top)]
        self.structs = DWARFStructs(
            little_endian=True,
            dwarf_format=32,
            address_size=4,
            dwarf_version=4,
        )

    def iter_CUs(self):
        return iter(self._cus)


def make_backend(tmp_path, elf):
    source = tmp_path / "firmware.axf"
    source.write_bytes(b"fixture")
    return BuiltinElfBackend(elf_factory=lambda _stream: elf), str(source)


def test_builtin_symbols_normalize_defined_objects_and_functions(tmp_path):
    elf = FakeElf(
        symbols=[
            FakeSymbol(
                "g_counter", kind="STT_OBJECT", address=0x20000010, size=4
            ),
            FakeSymbol(
                "HardFault_Handler",
                kind="STT_FUNC",
                address=0x08000101,
                size=12,
            ),
            FakeSymbol(
                "missing", kind="STT_OBJECT", address=0, size=4, section="SHN_UNDEF"
            ),
            FakeSymbol("source.c", kind="STT_FILE", address=0, size=0),
        ]
    )
    backend, source = make_backend(tmp_path, elf)

    symbols = backend.symbols(source)

    assert [(item.name, item.kind, item.address, item.size) for item in symbols] == [
        ("g_counter", "object", 0x20000010, 4),
        ("HardFault_Handler", "function", 0x08000101, 12),
    ]
    assert symbols[0].binding == "global"
    assert symbols[0].visibility == "default"


def test_builtin_symbols_keep_static_and_unicode_names(tmp_path):
    elf = FakeElf(
        symbols=[
            FakeSymbol(
                "static_value",
                kind="STT_OBJECT",
                address=0x20000020,
                size=2,
                binding="STB_LOCAL",
            ),
            FakeSymbol(
                "sensor_\u6e29\u5ea6",
                kind="STT_OBJECT",
                address=0x20000024,
                size=4,
            ),
        ]
    )
    backend, source = make_backend(tmp_path, elf)

    symbols = backend.symbols(source)

    assert [item.name for item in symbols] == ["static_value", "sensor_\u6e29\u5ea6"]
    assert symbols[0].binding == "local"


def test_builtin_sections_preserve_header_fields(tmp_path):
    elf = FakeElf(
        sections=[
            FakeSection(
                ".text",
                address=0x08000000,
                size=0x120,
                flags=0x6,
                section_type="SHT_PROGBITS",
            ),
            FakeSection(
                ".bss",
                address=0x20000000,
                size=0x40,
                flags=0x3,
                section_type="SHT_NOBITS",
            ),
            FakeSection(
                "", address=0, size=0, flags=0, section_type="SHT_NULL"
            ),
        ]
    )
    backend, source = make_backend(tmp_path, elf)

    sections = backend.sections(source)

    assert sections == [
        ElfSection(".text", 0x08000000, 0x120, 0x6, "SHT_PROGBITS"),
        ElfSection(".bss", 0x20000000, 0x40, 0x3, "SHT_NOBITS"),
    ]


def test_builtin_sections_cover_uncompressed_symbol_execution_range(
    tmp_path, monkeypatch
):
    elf = FakeElf(
        symbols=[
            FakeSymbol(
                "compressed_data",
                kind="STT_OBJECT",
                address=0x20000018,
                size=0x100,
                section=1,
            ),
            FakeSymbol(
                "code_alias",
                kind="STT_FUNC",
                address=0x20000010,
                size=0x1F0,
                section=1,
            ),
            FakeSymbol(
                "before_section",
                kind="STT_OBJECT",
                address=0x1FFFFFF0,
                size=0x20,
                section=1,
            ),
            FakeSymbol(
                "crosses_next_section",
                kind="STT_OBJECT",
                address=0x200001F0,
                size=0x20,
                section=1,
            ),
        ],
        sections=[
            FakeSection("", address=0, size=0, flags=0, section_type="SHT_NULL"),
            FakeSection(
                "RW_IRAM1",
                address=0x20000000,
                size=0x20,
                flags=0x3,
                section_type="SHT_PROGBITS",
            ),
            FakeSection(
                ".bss",
                address=0x20000200,
                size=0x40,
                flags=0x3,
                section_type="SHT_NOBITS",
            ),
        ],
    )
    backend, source = make_backend(tmp_path, elf)

    assert backend.sections(source) == [
        ElfSection("RW_IRAM1", 0x20000000, 0x118, 0x3, "SHT_PROGBITS"),
        ElfSection(".bss", 0x20000200, 0x40, 0x3, "SHT_NOBITS"),
    ]
    monkeypatch.setattr(
        "mklink.elf_backend.get_elf_backend", lambda *_args, **_kwargs: backend
    )
    assert writable_memory_ranges(source) == (
        (0x20000000, 0x20000118),
        (0x20000200, 0x20000240),
    )
    assert [item.name for item in list_writable_object_symbols(source)] == [
        "compressed_data"
    ]


def test_builtin_invalid_input_has_clear_error(tmp_path):
    source = tmp_path / "not-elf.axf"
    source.write_bytes(b"not an elf")

    with pytest.raises(ElfParseError, match="Invalid ELF/AXF"):
        BuiltinElfBackend().symbols(str(source))


def test_fixed_address_and_member_offset_accept_only_constant_expressions():
    structs = DWARFStructs(
        little_endian=True, dwarf_format=32, address_size=4, dwarf_version=4
    )
    address = FakeAttribute(
        "DW_FORM_exprloc", bytes([0x03, 0x20, 0x00, 0x00, 0x20])
    )
    address_with_offset = FakeAttribute(
        "DW_FORM_exprloc", bytes([0x03, 0x20, 0x00, 0x00, 0x20, 0x23, 0x04])
    )
    dynamic = FakeAttribute("DW_FORM_exprloc", bytes([0x91, 0x00]))
    member = FakeAttribute("DW_FORM_exprloc", bytes([0x23, 0x04]))
    dynamic_member = FakeAttribute("DW_FORM_exprloc", bytes([0x91, 0x00]))
    composite = FakeAttribute(
        "DW_FORM_exprloc",
        bytes([0x03, 0x20, 0x00, 0x00, 0x20, 0x94, 0x01, 0x9F]),
    )

    assert _fixed_address(address, structs) == 0x20000020
    assert _fixed_address(address_with_offset, structs) == 0x20000024
    assert _fixed_address(composite, structs) is None
    assert _fixed_address(dynamic, structs) is None
    assert _member_offset(member, structs) == 4
    assert _member_offset(dynamic_member, structs) is None


def test_builtin_dwarf_skips_members_with_nonconstant_offsets(tmp_path):
    int16 = FakeDie(
        "DW_TAG_base_type",
        0x10,
        attributes={
            "DW_AT_name": FakeAttribute("DW_FORM_string", b"int16_t"),
            "DW_AT_byte_size": FakeAttribute("DW_FORM_data1", 2),
        },
    )
    dynamic_member = FakeDie(
        "DW_TAG_member",
        0x21,
        attributes={
            "DW_AT_name": FakeAttribute("DW_FORM_string", b"dynamic"),
            "DW_AT_type": FakeAttribute("DW_FORM_ref4", 0x10),
            "DW_AT_data_member_location": FakeAttribute(
                "DW_FORM_exprloc", bytes([0x91, 0x00])
            ),
        },
        refs={"DW_AT_type": int16},
    )
    record = FakeDie(
        "DW_TAG_structure_type",
        0x20,
        attributes={"DW_AT_name": FakeAttribute("DW_FORM_string", b"Record")},
        children=[dynamic_member],
    )
    top = FakeDie("DW_TAG_compile_unit", 0, children=[int16, record])
    backend, source = make_backend(tmp_path, FakeElf(dwarf=FakeDwarf(top)))

    info = backend.dwarf_info(source)

    assert info.records_by_offset[0x20].members == []


def test_builtin_dwarf_excludes_arrays_with_dynamic_dimensions(tmp_path):
    int16 = FakeDie(
        "DW_TAG_base_type",
        0x10,
        attributes={
            "DW_AT_name": FakeAttribute("DW_FORM_string", b"int16_t"),
            "DW_AT_byte_size": FakeAttribute("DW_FORM_data1", 2),
        },
    )
    dynamic = FakeDie(
        "DW_TAG_subrange_type",
        0x31,
        attributes={"DW_AT_count": FakeAttribute("DW_FORM_ref4", 0x99)},
    )
    fixed = FakeDie(
        "DW_TAG_subrange_type",
        0x32,
        attributes={"DW_AT_count": FakeAttribute("DW_FORM_data1", 4)},
    )
    array = FakeDie(
        "DW_TAG_array_type",
        0x30,
        attributes={"DW_AT_type": FakeAttribute("DW_FORM_ref4", 0x10)},
        children=[dynamic, fixed],
        refs={"DW_AT_type": int16},
    )
    top = FakeDie("DW_TAG_compile_unit", 0, children=[int16, array])
    backend, source = make_backend(tmp_path, FakeElf(dwarf=FakeDwarf(top)))

    info = backend.dwarf_info(source)

    assert 0x30 not in info.arrays


def test_builtin_dwarf_normalizes_records_arrays_and_global_addresses(tmp_path):
    int16 = FakeDie(
        "DW_TAG_base_type",
        0x10,
        attributes={
            "DW_AT_name": FakeAttribute("DW_FORM_string", b"int16_t"),
            "DW_AT_byte_size": FakeAttribute("DW_FORM_data1", 2),
            "DW_AT_encoding": FakeAttribute("DW_FORM_data1", 5),
        },
    )
    member = FakeDie(
        "DW_TAG_member",
        0x21,
        attributes={
            "DW_AT_name": FakeAttribute("DW_FORM_string", b"value"),
            "DW_AT_type": FakeAttribute("DW_FORM_ref4", 0x10),
            "DW_AT_data_member_location": FakeAttribute("DW_FORM_data1", 0),
        },
        refs={"DW_AT_type": int16},
    )
    record = FakeDie(
        "DW_TAG_structure_type",
        0x20,
        attributes={
            "DW_AT_name": FakeAttribute("DW_FORM_string", b"Point"),
            "DW_AT_byte_size": FakeAttribute("DW_FORM_data1", 2),
        },
        children=[member],
    )
    subrange = FakeDie(
        "DW_TAG_subrange_type",
        0x31,
        attributes={"DW_AT_count": FakeAttribute("DW_FORM_data1", 2)},
    )
    array = FakeDie(
        "DW_TAG_array_type",
        0x30,
        attributes={"DW_AT_type": FakeAttribute("DW_FORM_ref4", 0x20)},
        children=[subrange],
        refs={"DW_AT_type": record},
    )
    fixed = FakeDie(
        "DW_TAG_variable",
        0x40,
        attributes={
            "DW_AT_name": FakeAttribute("DW_FORM_string", b"points"),
            "DW_AT_type": FakeAttribute("DW_FORM_ref4", 0x30),
            "DW_AT_location": FakeAttribute(
                "DW_FORM_exprloc", bytes([0x03, 0x00, 0x00, 0x00, 0x20])
            ),
        },
        refs={"DW_AT_type": array},
    )
    linked = FakeDie(
        "DW_TAG_variable",
        0x50,
        attributes={
            "DW_AT_name": FakeAttribute("DW_FORM_string", b"linked_only"),
            "DW_AT_type": FakeAttribute("DW_FORM_ref4", 0x10),
        },
        refs={"DW_AT_type": int16},
    )
    local_duplicate = FakeDie(
        "DW_TAG_variable",
        0x61,
        attributes={
            "DW_AT_name": FakeAttribute("DW_FORM_string", b"linked_only"),
            "DW_AT_type": FakeAttribute("DW_FORM_ref4", 0x10),
        },
        refs={"DW_AT_type": int16},
    )
    subprogram = FakeDie(
        "DW_TAG_subprogram", 0x60, children=[local_duplicate]
    )
    top = FakeDie(
        "DW_TAG_compile_unit",
        0,
        children=[int16, record, array, fixed, linked, subprogram],
    )
    elf = FakeElf(
        symbols=[
            FakeSymbol(
                "linked_only", kind="STT_OBJECT", address=0x20000010, size=2
            )
        ],
        dwarf=FakeDwarf(top),
    )
    backend, source = make_backend(tmp_path, elf)

    info = backend.dwarf_info(source)

    assert info.records_by_offset[0x20].members[0].type_name == "int16_t"
    assert info.arrays[0x30].dimensions == (2,)
    assert info.arrays[0x30].size == 4
    assert info.variables["points"].address == 0x20000000
    assert info.variables["points"].type_name == "Point[]"
    assert info.variables["linked_only"].address == 0x20000010
