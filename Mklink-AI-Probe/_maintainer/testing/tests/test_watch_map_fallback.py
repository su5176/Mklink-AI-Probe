from pathlib import Path

from mklink.watch import resolve_map_source_variable


def test_resolve_map_source_variable_reads_hpm_ses_float(tmp_path: Path):
    elf = tmp_path / "demo.elf"
    map_file = tmp_path / "demo.map"
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    source = src_dir / "hello_world.c"

    elf.write_bytes(b"")
    map_file.write_text(
        "  vofa_test_sin              0x00080330  tp-0x07D0           4      4  Zero  Gb  hello_world.c.o\n",
        encoding="utf-8",
    )
    source.write_text("volatile float vofa_test_sin = 0.0f;\n", encoding="utf-8")

    resolved = resolve_map_source_variable(str(elf), "vofa_test_sin")

    assert resolved == (0x00080330, "float", 4)


def test_resolve_map_source_variable_reads_hpm_gcc_split_symbol(tmp_path: Path):
    elf = tmp_path / "demo.elf"
    map_file = tmp_path / "demo.map"
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    source = src_dir / "hello_world.c"

    elf.write_bytes(b"")
    map_file.write_text(
        "\n".join([
            " .sbss.vofa_test_char",
            "                0x012001d8        0x1 CMakeFiles/app.dir/src/hello_world.c.obj",
            "                0x012001d8                vofa_test_char",
            "                0x01280000        0x8 CMakeFiles/app.dir/src/hello_world.c.obj",
            "                0x01280000                vofa_test_sin",
            "                0x01280004                vofa_test_tri",
            "",
        ]),
        encoding="utf-8",
    )
    source.write_text(
        "\n".join([
            "volatile uint8_t vofa_test_char = 0;",
            "volatile float vofa_test_sin;",
            "volatile float vofa_test_tri;",
            "",
        ]),
        encoding="utf-8",
    )

    assert resolve_map_source_variable(str(elf), "vofa_test_char") == (
        0x012001D8, "uint8_t", 1,
    )
    assert resolve_map_source_variable(str(elf), "vofa_test_sin") == (
        0x01280000, "float", 4,
    )
