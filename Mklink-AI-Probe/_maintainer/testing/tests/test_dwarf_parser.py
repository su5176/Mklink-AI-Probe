from mklink.dwarf_parser import (
    DwarfCache,
    DwarfInfo,
    _default_dwarf_cache_dir,
    _info_from_json,
    _info_to_json,
    load_dwarf_info,
    parse_dwarf_info_output,
)


def test_default_dwarf_cache_follows_project_root(tmp_path, monkeypatch):
    monkeypatch.delenv("MKLINK_CACHE_DIR", raising=False)

    assert _default_dwarf_cache_dir(str(tmp_path)) == (
        tmp_path / ".mklink" / "cache" / "dwarf"
    )


def test_explicit_cache_root_overrides_project_root(tmp_path, monkeypatch):
    cache_root = tmp_path / "task-cache"
    monkeypatch.setenv("MKLINK_CACHE_DIR", str(cache_root))

    assert _default_dwarf_cache_dir(str(tmp_path / "project")) == (
        cache_root / "dwarf"
    )


def test_build_workspace_cache_is_used_without_project_root(tmp_path, monkeypatch):
    monkeypatch.delenv("MKLINK_CACHE_DIR", raising=False)
    monkeypatch.setenv("MKLINK_BUILD_ROOT", str(tmp_path / "build"))

    assert _default_dwarf_cache_dir() == tmp_path / "build" / "cache" / "dwarf"


def test_load_dwarf_info_caches_under_project_root(tmp_path, monkeypatch):
    class Backend:
        name = "fixture"
        parser_version = "fixture-v1"

        def __init__(self):
            self.calls = 0

        def dwarf_info(self, _source):
            self.calls += 1
            return DwarfInfo()

    backend = Backend()
    source = tmp_path / "firmware.axf"
    source.write_bytes(b"fixture")
    project_root = tmp_path / "project"
    monkeypatch.delenv("MKLINK_CACHE_DIR", raising=False)
    monkeypatch.delenv("MKLINK_BUILD_ROOT", raising=False)
    monkeypatch.setattr(
        "mklink.elf_backend.get_elf_backend",
        lambda *_args, **_kwargs: backend,
    )

    load_dwarf_info(str(source), project_root=str(project_root))
    load_dwarf_info(str(source), project_root=str(project_root))

    cache_files = list(
        (project_root / ".mklink" / "cache" / "dwarf").glob("*.json")
    )
    assert len(cache_files) == 1
    assert backend.calls == 1


def test_load_dwarf_info_without_cache_creates_no_directory(tmp_path, monkeypatch):
    class Backend:
        name = "fixture"
        parser_version = "fixture-v1"

        @staticmethod
        def dwarf_info(_source):
            return DwarfInfo()

    source = tmp_path / "firmware.axf"
    source.write_bytes(b"fixture")
    project_root = tmp_path / "project"
    monkeypatch.delenv("MKLINK_CACHE_DIR", raising=False)
    monkeypatch.setattr(
        "mklink.elf_backend.get_elf_backend",
        lambda *_args, **_kwargs: Backend(),
    )

    load_dwarf_info(str(source), use_cache=False, project_root=str(project_root))

    assert not (project_root / ".mklink").exists()


def test_unwritable_cache_does_not_fall_back_or_break_parsing(tmp_path, monkeypatch):
    class Backend:
        name = "fixture"
        parser_version = "fixture-v1"

        @staticmethod
        def dwarf_info(_source):
            return DwarfInfo()

    source = tmp_path / "firmware.axf"
    source.write_bytes(b"fixture")
    monkeypatch.setattr(
        "mklink.elf_backend.get_elf_backend",
        lambda *_args, **_kwargs: Backend(),
    )
    monkeypatch.setattr(
        "mklink.dwarf_parser.DwarfCache",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            PermissionError("read-only")
        ),
    )

    assert isinstance(
        load_dwarf_info(str(source), project_root=str(tmp_path / "project")),
        DwarfInfo,
    )


def test_variable_type_resolves_through_volatile_and_typedefs():
    output = """
 <1><10>: Abbrev Number: 1 (DW_TAG_base_type)
    <11>   DW_AT_byte_size   : 4
    <12>   DW_AT_name        : unsigned int
 <1><20>: Abbrev Number: 2 (DW_TAG_typedef)
    <21>   DW_AT_type        : <0x10>
    <22>   DW_AT_name        : uint32_t
 <1><30>: Abbrev Number: 2 (DW_TAG_typedef)
    <31>   DW_AT_type        : <0x20>
    <32>   DW_AT_name        : rt_uint32_t
 <1><40>: Abbrev Number: 3 (DW_TAG_volatile_type)
    <41>   DW_AT_type        : <0x30>
 <1><50>: Abbrev Number: 4 (DW_TAG_variable)
    <51>   DW_AT_name        : mklink_rtt_test_arm
    <52>   DW_AT_type        : <0x40>
    <53>   DW_AT_location    : 5 byte block: 3 1c b0 0 20 (DW_OP_addr: 2000b01c)
"""

    variable = parse_dwarf_info_output(output).variables["mklink_rtt_test_arm"]

    assert variable.type_name == "rt_uint32_t"
    assert variable.size == 4
    assert variable.address == 0x2000B01C


def test_parser_preserves_record_offsets_and_fixed_array_dimensions():
    output = """
 <1><10>: Abbrev Number: 1 (DW_TAG_base_type)
    <11>   DW_AT_byte_size   : 2
    <12>   DW_AT_name        : int16_t
 <1><20>: Abbrev Number: 2 (DW_TAG_structure_type)
    <21>   DW_AT_name        : Point
    <22>   DW_AT_byte_size   : 4
 <2><23>: Abbrev Number: 3 (DW_TAG_member)
    <24>   DW_AT_name        : x
    <25>   DW_AT_type        : <0x10>
    <26>   DW_AT_data_member_location: 0
 <2><27>: Abbrev Number: 0
 <1><30>: Abbrev Number: 2 (DW_TAG_structure_type)
    <31>   DW_AT_name        : Point
    <32>   DW_AT_byte_size   : 4
 <2><33>: Abbrev Number: 3 (DW_TAG_member)
    <34>   DW_AT_name        : y
    <35>   DW_AT_type        : <0x10>
    <36>   DW_AT_data_member_location: 2
 <2><37>: Abbrev Number: 0
 <1><40>: Abbrev Number: 4 (DW_TAG_array_type)
    <41>   DW_AT_type        : <0x30>
 <2><42>: Abbrev Number: 5 (DW_TAG_subrange_type)
    <43>   DW_AT_count       : 2
 <2><44>: Abbrev Number: 0
 <1><50>: Abbrev Number: 2 (DW_TAG_structure_type)
    <51>   DW_AT_byte_size   : 2
 <2><52>: Abbrev Number: 3 (DW_TAG_member)
    <53>   DW_AT_name        : value
    <54>   DW_AT_type        : <0x10>
    <55>   DW_AT_data_member_location: 0
 <2><56>: Abbrev Number: 0
 <1><60>: Abbrev Number: 6 (DW_TAG_variable)
    <61>   DW_AT_name        : points
    <62>   DW_AT_type        : <0x40>
    <63>   DW_AT_location    : 5 byte block: 3 00 00 00 20 (DW_OP_addr: 20000000)
 <1><70>: Abbrev Number: 6 (DW_TAG_variable)
    <71>   DW_AT_name        : anonymous_value
    <72>   DW_AT_type        : <0x50>
    <73>   DW_AT_location    : 5 byte block: 3 10 00 00 20 (DW_OP_addr: 20000010)
"""

    info = parse_dwarf_info_output(output)

    assert info.structs["Point"].offset == 0x20
    assert set(info.records_by_offset) == {0x20, 0x30, 0x50}
    assert info.records_by_offset[0x30].members[0].name == "y"
    assert info.records_by_offset[0x50].name == "<anonymous@0x50>"
    assert info.arrays[0x40].element_type_offset == 0x30
    assert info.arrays[0x40].dimensions == (2,)
    assert info.arrays[0x40].size == 8
    assert info.variables["points"].type_name == "Point[]"
    assert info.variables["points"].size == 8
    assert info.variables["anonymous_value"].type_name == "<anonymous@0x50>"
    assert info.variables["anonymous_value"].size == 2

    restored = _info_from_json(_info_to_json(info))
    assert restored.records_by_offset[0x30].members[0].name == "y"
    assert restored.records_by_offset[0x50].name == "<anonymous@0x50>"
    assert restored.arrays[0x40].dimensions == (2,)
    assert restored.arrays[0x40].size == 8


def test_parser_preserves_duplicate_named_enums_by_type_offset():
    output = """
 <1><10>: Abbrev Number: 1 (DW_TAG_enumeration_type)
    <11>   DW_AT_name        : Mode
    <12>   DW_AT_byte_size   : 4
 <2><13>: Abbrev Number: 2 (DW_TAG_enumerator)
    <14>   DW_AT_name        : OLD
    <15>   DW_AT_const_value : 0
 <2><16>: Abbrev Number: 0
 <1><20>: Abbrev Number: 1 (DW_TAG_enumeration_type)
    <21>   DW_AT_name        : Mode
    <22>   DW_AT_byte_size   : 4
 <2><23>: Abbrev Number: 2 (DW_TAG_enumerator)
    <24>   DW_AT_name        : NEW
    <25>   DW_AT_const_value : 1
 <2><26>: Abbrev Number: 0
 <1><30>: Abbrev Number: 3 (DW_TAG_variable)
    <31>   DW_AT_name        : old_mode
    <32>   DW_AT_type        : <0x10>
    <33>   DW_AT_location    : 5 byte block: 3 00 00 00 20 (DW_OP_addr: 20000000)
 <1><40>: Abbrev Number: 3 (DW_TAG_variable)
    <41>   DW_AT_name        : new_mode
    <42>   DW_AT_type        : <0x20>
    <43>   DW_AT_location    : 5 byte block: 3 04 00 00 20 (DW_OP_addr: 20000004)
"""

    info = parse_dwarf_info_output(output)

    assert info.enums["Mode"].offset == 0x10
    assert info.enums_by_offset[0x10].values == {0: "OLD"}
    assert info.enums_by_offset[0x20].values == {1: "NEW"}
    assert info.variables["old_mode"].type_name == "Mode"
    assert info.variables["old_mode"].size == 4

    restored = _info_from_json(_info_to_json(info))
    assert restored.enums_by_offset[0x10].values == {0: "OLD"}
    assert restored.enums_by_offset[0x20].values == {1: "NEW"}


def test_dwarf_cache_is_backend_parser_and_source_aware(tmp_path):
    source = tmp_path / "firmware.axf"
    source.write_bytes(b"first")
    cache = DwarfCache(str(tmp_path / "cache"))
    info = DwarfInfo()

    cache.save(
        str(source), info, backend="builtin", parser_version="pyelftools-test"
    )

    assert cache.load(
        str(source), backend="builtin", parser_version="pyelftools-test"
    ) is not None
    assert cache.load(
        str(source), backend="external", parser_version="pyelftools-test"
    ) is None
    assert cache.load(
        str(source), backend="builtin", parser_version="pyelftools-other"
    ) is None

    source.write_bytes(b"changed")
    assert cache.load(
        str(source), backend="builtin", parser_version="pyelftools-test"
    ) is None
