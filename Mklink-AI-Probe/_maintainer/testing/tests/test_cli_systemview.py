import json

from mklink import cli


def test_systemview_symbol_source_uses_configured_hpm_elf(tmp_path):
    elf = tmp_path / "hpm_build" / "output" / "demo.elf"
    elf.parent.mkdir(parents=True)
    elf.write_bytes(b"elf")
    config_dir = tmp_path / ".mklink"
    config_dir.mkdir()
    (config_dir / "project_info.json").write_text(
        json.dumps({"axf_path": str(elf)}), encoding="utf-8"
    )

    assert cli._systemview_symbol_source(str(tmp_path)) == str(elf)
