"""Built-in ELF/DWARF backend powered by the bundled pyelftools package."""

from __future__ import annotations

import ntpath
import posixpath
from contextlib import contextmanager
from importlib.metadata import version
from typing import Callable, Iterable, Iterator

from elftools.common.exceptions import DWARFError, ELFError
from elftools.dwarf.dwarf_expr import DWARFExprParser
from elftools.elf.elffile import ELFFile

from mklink.elf_backend import ElfParseError, ElfSection, ElfSymbol


_SYMBOL_KINDS = {
    "STT_OBJECT": "object",
    "STT_FUNC": "function",
}


def _enum_name(value: object, prefix: str) -> str:
    text = str(value)
    if text.startswith(prefix):
        text = text[len(prefix):]
    return text.lower()


def _normalize_symbol(symbol) -> ElfSymbol | None:
    kind = _SYMBOL_KINDS.get(symbol["st_info"]["type"])
    if kind is None or symbol["st_shndx"] == "SHN_UNDEF":
        return None
    name = str(symbol.name or "")
    if not name:
        return None
    return ElfSymbol(
        name=name,
        address=int(symbol["st_value"]),
        size=int(symbol["st_size"]),
        kind=kind,
        binding=_enum_name(symbol["st_info"]["bind"], "STB_"),
        visibility=_enum_name(symbol["st_other"]["visibility"], "STV_"),
        section=symbol["st_shndx"],
    )


def _symbols_from_elf(elf) -> list[ElfSymbol]:
    symtab = elf.get_section_by_name(".symtab")
    if symtab is None:
        return []
    symbols = []
    for symbol in symtab.iter_symbols():
        normalized = _normalize_symbol(symbol)
        if normalized is not None:
            symbols.append(normalized)
    return symbols


def _decode_value(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _attribute_name(attributes: dict, *keys: str) -> str:
    for key in keys:
        attribute = attributes.get(key)
        if attribute is not None:
            return _decode_value(attribute.value)
    return ""


def _attribute_int(attributes: dict, key: str, default: int = 0) -> int:
    attribute = attributes.get(key)
    if attribute is None or not isinstance(attribute.value, int):
        return default
    return int(attribute.value)


def _type_offset(die, key: str = "DW_AT_type") -> int | None:
    if key not in die.attributes:
        return None
    try:
        target = die.get_DIE_from_attribute(key)
    except (KeyError, TypeError, ValueError, DWARFError):
        return None
    return int(target.offset) if target is not None else None


def _expression_operations(attribute, structs):
    if attribute is None or not str(attribute.form).startswith("DW_FORM_block") \
            and attribute.form != "DW_FORM_exprloc":
        return []
    try:
        return DWARFExprParser(structs).parse_expr(attribute.value)
    except (KeyError, TypeError, ValueError, DWARFError):
        return []


def _fixed_address(attribute, structs) -> int | None:
    operations = _expression_operations(attribute, structs)
    if not operations or operations[0].op_name != "DW_OP_addr":
        return None
    address = int(operations[0].args[0])
    for operation in operations[1:]:
        if operation.op_name != "DW_OP_plus_uconst":
            return None
        address += int(operation.args[0])
    return address


_CONSTANT_INTEGER_FORMS = {
    "DW_FORM_data1",
    "DW_FORM_data2",
    "DW_FORM_data4",
    "DW_FORM_data8",
    "DW_FORM_implicit_const",
    "DW_FORM_sdata",
    "DW_FORM_udata",
}


def _constant_attribute_int(attribute) -> int | None:
    if attribute is None or attribute.form not in _CONSTANT_INTEGER_FORMS:
        return None
    if not isinstance(attribute.value, int):
        return None
    return int(attribute.value)


def _member_offset(attribute, structs) -> int | None:
    if attribute is None:
        return 0
    constant = _constant_attribute_int(attribute)
    if constant is not None:
        return constant if constant >= 0 else None
    operations = _expression_operations(attribute, structs)
    if len(operations) == 1 and operations[0].op_name in {
        "DW_OP_plus_uconst",
        "DW_OP_constu",
        "DW_OP_consts",
    }:
        value = int(operations[0].args[0])
        return value if value >= 0 else None
    return None


def _walk_dies(die, parent_tags: tuple[str, ...] = ()):
    yield die, parent_tags
    child_parents = parent_tags + (die.tag,)
    for child in die.iter_children():
        yield from _walk_dies(child, child_parents)


def _unique_object_addresses(symbols: Iterable[ElfSymbol]) -> dict[str, int]:
    addresses: dict[str, set[int]] = {}
    for symbol in symbols:
        if symbol.kind != "object":
            continue
        addresses.setdefault(symbol.name, set()).add(symbol.address)
    return {
        name: next(iter(values))
        for name, values in addresses.items()
        if len(values) == 1
    }


def _is_global_scope(parent_tags: tuple[str, ...]) -> bool:
    local_tags = {
        "DW_TAG_subprogram",
        "DW_TAG_lexical_block",
        "DW_TAG_inlined_subroutine",
    }
    return not any(tag in local_tags for tag in parent_tags)


def _entry_value(entry, key: str, default=None):
    if hasattr(entry, key):
        return getattr(entry, key)
    try:
        return entry[key]
    except (KeyError, TypeError):
        return default


def _line_file_path(cu, line_program, state) -> str | None:
    file_index = int(state.file or 0) - 1
    files = line_program.header.file_entry
    if file_index < 0 or file_index >= len(files):
        return None
    file_entry = files[file_index]
    filename = _decode_value(_entry_value(file_entry, "name", ""))
    if not filename:
        return None

    top = cu.get_top_DIE()
    comp_dir = _attribute_name(top.attributes, "DW_AT_comp_dir")
    directory = ""
    dir_index = int(_entry_value(file_entry, "dir_index", 0) or 0)
    directories = line_program.header.include_directory
    if 0 < dir_index <= len(directories):
        directory = _decode_value(directories[dir_index - 1])

    parts = [comp_dir, directory, filename]
    windows_style = any("\\" in part or ntpath.splitdrive(part)[0] for part in parts)
    path_module = ntpath if windows_style else posixpath
    if path_module.isabs(filename):
        return path_module.normpath(filename)
    base = directory
    if base and not path_module.isabs(base):
        base = path_module.join(comp_dir, base) if comp_dir else base
    elif not base:
        base = comp_dir
    return path_module.normpath(path_module.join(base, filename)) if base else filename


def _line_ranges_from_program(cu, line_program) -> list[tuple[int, int, str]]:
    ranges = []
    previous = None
    for entry in line_program.get_entries():
        state = entry.state
        if state is None:
            continue
        if previous is not None:
            start = int(previous.address)
            end = int(state.address)
            location_path = _line_file_path(cu, line_program, previous)
            if (
                end > start
                and location_path
                and previous.line is not None
                and int(previous.line) > 0
            ):
                ranges.append(
                    (start, end, f"{location_path}:{int(previous.line)}")
                )
        previous = None if state.end_sequence else state
    return ranges


class BuiltinElfBackend:
    name = "builtin"
    parser_version = f"pyelftools-{version('pyelftools')}-v1"

    def __init__(self, *, elf_factory: Callable | None = None) -> None:
        self._elf_factory = elf_factory or ELFFile

    @contextmanager
    def _open_elf(self, source: str) -> Iterator:
        try:
            with open(source, "rb") as stream:
                try:
                    yield self._elf_factory(stream)
                except ElfParseError:
                    raise
                except (
                    DWARFError,
                    ELFError,
                    IndexError,
                    KeyError,
                    TypeError,
                    ValueError,
                ) as exc:
                    raise ElfParseError(f"Invalid ELF/AXF file {source}: {exc}") from exc
        except ElfParseError:
            raise
        except OSError as exc:
            raise ElfParseError(f"Cannot open ELF/AXF file {source}: {exc}") from exc

    def symbols(self, source: str) -> list[ElfSymbol]:
        with self._open_elf(source) as elf:
            return _symbols_from_elf(elf)

    def sections(self, source: str) -> list[ElfSection]:
        with self._open_elf(source) as elf:
            # Arm Compiler may store a compressed RW load image in an AXF while
            # keeping symbols at their uncompressed execution addresses.  In
            # that format sh_size is the compressed byte count, so section-only
            # writable-range checks can incorrectly reject valid RAM objects.
            # A defined symbol still carries its execution address and section
            # index; extend the normalized section to cover those symbols.
            raw_sections = list(elf.iter_sections())
            allocated_addresses = sorted({
                int(section["sh_addr"])
                for section in raw_sections
                if int(section["sh_flags"]) & 0x2
            })
            symbol_ends: dict[int, int] = {}
            for symbol in _symbols_from_elf(elf):
                if (
                    symbol.kind != "object"
                    or not isinstance(symbol.section, int)
                    or not 0 <= symbol.section < len(raw_sections)
                    or symbol.size <= 0
                ):
                    continue
                section = raw_sections[symbol.section]
                address = int(section["sh_addr"])
                flags = int(section["sh_flags"])
                symbol_end = symbol.address + symbol.size
                next_address = next(
                    (candidate for candidate in allocated_addresses if candidate > address),
                    None,
                )
                if (
                    flags & 0x3 != 0x3
                    or symbol.address < address
                    or (next_address is not None and symbol_end > next_address)
                ):
                    continue
                symbol_ends[symbol.section] = max(
                    symbol_ends.get(symbol.section, 0),
                    symbol_end,
                )

            sections = []
            for index, section in enumerate(raw_sections):
                name = str(section.name or "")
                if not name:
                    continue
                address = int(section["sh_addr"])
                size = int(section["sh_size"])
                symbol_end = symbol_ends.get(index, 0)
                if symbol_end > address + size and symbol_end >= address:
                    size = symbol_end - address
                sections.append(
                    ElfSection(
                        name=name,
                        address=address,
                        size=size,
                        flags=int(section["sh_flags"]),
                        section_type=str(section["sh_type"]),
                    )
                )
            return sections

    def dwarf_info(self, source: str):
        from mklink.dwarf_parser import (
            DwarfArray,
            DwarfEnum,
            DwarfInfo,
            DwarfMember,
            DwarfStruct,
            DwarfVariable,
            _finalize_array_size,
            get_array_type,
            resolve_type_name,
        )

        with self._open_elf(source) as elf:
            if not elf.has_dwarf_info():
                raise ElfParseError(f"No DWARF info found in {source}")
            dwarf = elf.get_dwarf_info()
            info = DwarfInfo()
            object_addresses = _unique_object_addresses(_symbols_from_elf(elf))

            entries = []
            for cu in dwarf.iter_CUs():
                entries.extend(_walk_dies(cu.get_top_DIE()))

            for die, parent_tags in entries:
                attrs = die.attributes
                tag = die.tag
                offset = int(die.offset)
                name = _attribute_name(attrs, "DW_AT_name")
                size = _attribute_int(attrs, "DW_AT_byte_size")
                type_offset = _type_offset(die)

                if tag == "DW_TAG_base_type" and name:
                    info.base_types[offset] = (name, size)
                    if "DW_AT_encoding" in attrs:
                        info.base_type_encodings[offset] = _attribute_int(
                            attrs, "DW_AT_encoding"
                        )
                elif tag == "DW_TAG_typedef" and name:
                    info.typedefs[offset] = (name, type_offset)
                elif tag == "DW_TAG_pointer_type":
                    info.pointers[offset] = (type_offset, size or 4)
                elif tag == "DW_TAG_array_type":
                    dimensions = []
                    complete = True
                    for child in die.iter_children():
                        if child.tag != "DW_TAG_subrange_type":
                            continue
                        child_attrs = child.attributes
                        count_attr = child_attrs.get("DW_AT_count")
                        upper_attr = child_attrs.get("DW_AT_upper_bound")
                        if count_attr is not None:
                            count = _constant_attribute_int(count_attr)
                        elif upper_attr is not None:
                            upper = _constant_attribute_int(upper_attr)
                            lower_attr = child_attrs.get("DW_AT_lower_bound")
                            lower = (
                                0
                                if lower_attr is None
                                else _constant_attribute_int(lower_attr)
                            )
                            count = (
                                upper - lower + 1
                                if upper is not None and lower is not None
                                else None
                            )
                        else:
                            count = None
                        if count is None or count <= 0:
                            complete = False
                            break
                        dimensions.append(count)
                    if complete:
                        info.arrays[offset] = DwarfArray(
                            offset=offset,
                            element_type_offset=type_offset,
                            dimensions=tuple(dimensions),
                            size=size,
                        )
                elif tag in {
                    "DW_TAG_atomic_type",
                    "DW_TAG_const_type",
                    "DW_TAG_restrict_type",
                    "DW_TAG_volatile_type",
                }:
                    info.qualifiers[offset] = type_offset
                elif tag in {"DW_TAG_structure_type", "DW_TAG_union_type"}:
                    record_name = name or f"<anonymous@0x{offset:x}>"
                    record = DwarfStruct(
                        name=record_name,
                        offset=offset,
                        size=size,
                        kind=(
                            "union" if tag == "DW_TAG_union_type" else "struct"
                        ),
                    )
                    for child in die.iter_children():
                        if child.tag != "DW_TAG_member":
                            continue
                        child_attrs = child.attributes
                        member_offset = _member_offset(
                            child_attrs.get("DW_AT_data_member_location"),
                            dwarf.structs,
                        )
                        if member_offset is None:
                            continue
                        member = DwarfMember(
                            name=_attribute_name(child_attrs, "DW_AT_name"),
                            offset=member_offset,
                            type_offset=_type_offset(child),
                            bit_offset=(
                                _attribute_int(child_attrs, "DW_AT_bit_offset")
                                if "DW_AT_bit_offset" in child_attrs
                                else (
                                    _attribute_int(
                                        child_attrs, "DW_AT_data_bit_offset"
                                    )
                                    if "DW_AT_data_bit_offset" in child_attrs
                                    else None
                                )
                            ),
                            bit_size=(
                                _attribute_int(child_attrs, "DW_AT_bit_size")
                                if "DW_AT_bit_size" in child_attrs
                                else None
                            ),
                        )
                        record.members.append(member)
                    info.records_by_offset[offset] = record
                    if name:
                        info.structs.setdefault(name, record)
                elif tag == "DW_TAG_enumeration_type":
                    enum_name = name or f"<anonymous@0x{offset:x}>"
                    enum = DwarfEnum(
                        name=enum_name, offset=offset, size=size or 4
                    )
                    for child in die.iter_children():
                        if child.tag != "DW_TAG_enumerator":
                            continue
                        value = child.attributes.get("DW_AT_const_value")
                        if value is None or not isinstance(value.value, int):
                            continue
                        enum.values[int(value.value)] = _attribute_name(
                            child.attributes, "DW_AT_name"
                        )
                    info.enums_by_offset[offset] = enum
                    info.enums.setdefault(enum_name, enum)
                elif tag == "DW_TAG_variable" and name:
                    location = attrs.get("DW_AT_location")
                    address = _fixed_address(location, dwarf.structs)
                    if address is None and _is_global_scope(parent_tags):
                        linkage_name = _attribute_name(
                            attrs,
                            "DW_AT_linkage_name",
                            "DW_AT_MIPS_linkage_name",
                        )
                        address = object_addresses.get(
                            linkage_name or name, object_addresses.get(name)
                        )
                    variable = DwarfVariable(
                        name=name,
                        offset=offset,
                        type_offset=type_offset,
                        address=address,
                    )
                    existing = info.variables.get(name)
                    existing_quality = (
                        0 if existing is None or existing.address is None
                        else 1 if existing.address == 0 else 2
                    )
                    variable_quality = (
                        0 if variable.address is None
                        else 1 if variable.address == 0 else 2
                    )
                    if existing is None or variable_quality > existing_quality:
                        info.variables[name] = variable

            for record in info.records_by_offset.values():
                for member in record.members:
                    member.type_name, member.size = resolve_type_name(
                        info, member.type_offset
                    )
            for array_offset in tuple(info.arrays):
                array = get_array_type(info, array_offset)
                if array is not None:
                    _finalize_array_size(info, array)
            for variable in info.variables.values():
                variable.type_name, variable.size = resolve_type_name(
                    info, variable.type_offset
                )
            return info

    def source_locations(
        self, source: str, addresses: Iterable[int]
    ) -> dict[int, str]:
        requested = [int(address) for address in addresses]
        if not requested:
            return {}
        with self._open_elf(source) as elf:
            if not elf.has_dwarf_info():
                return {}
            dwarf = elf.get_dwarf_info()
            ranges = []
            for cu in dwarf.iter_CUs():
                line_program = dwarf.line_program_for_CU(cu)
                if line_program is not None:
                    ranges.extend(_line_ranges_from_program(cu, line_program))

        locations = {}
        for original in requested:
            address = original & ~1
            best = None
            for start, end, location in ranges:
                if start <= address < end and (best is None or start >= best[0]):
                    best = (start, location)
            if best is not None:
                locations[original] = best[1]
        return locations
