import importlib.util
import hashlib
import json
from pathlib import Path
import tomllib
from zipfile import ZipFile

import pytest


BUILDER_PATH = (
    Path(__file__).resolve().parents[3]
    / "skills"
    / "tauri-gui-builder"
    / "scripts"
    / "build.py"
)
BUILTIN_PACK_BUILDER_PATH = BUILDER_PATH.with_name("builtin_packs.py")
PROJECT_ROOT = Path(__file__).resolve().parents[3]
GITEE_UPDATER_ENDPOINT = (
    "https://gitee.com/Aladdin-Wang/Mklink-AI-Probe/raw/updates/latest.json"
)


def source_tree_digest(source: Path, relative_names: list[str]) -> str:
    digest = hashlib.sha256()
    for name in sorted(relative_names, key=str.casefold):
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update((source / name).read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


@pytest.fixture
def builder():
    spec = importlib.util.spec_from_file_location("mklink_tauri_builder", BUILDER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def builtin_pack_builder():
    spec = importlib.util.spec_from_file_location(
        "mklink_builtin_pack_builder", BUILTIN_PACK_BUILDER_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def configure_stub_stcp(builder, root):
    builder.SKILL_DIR = root
    library = root / "native" / "stcp_bridge" / "build" / "mklink-stcp.dll"
    library.parent.mkdir(parents=True, exist_ok=True)
    library.write_bytes(b"MZnative")


def test_external_bin_patch_restores_exact_config(builder, tmp_path):
    config = tmp_path / "tauri.conf.json"
    original = b'{"bundle":{"active":true,"icon":[]}}\n'
    config.write_bytes(original)

    with builder.temporary_external_bin(config):
        patched = json.loads(config.read_text(encoding="utf-8"))
        assert "externalBin" in patched["bundle"]
        assert patched["bundle"]["resources"]["resources/mklink-stcp.dll"] == (
            "mklink-stcp.dll"
        )

    assert config.read_bytes() == original


def test_external_bin_patch_restores_after_failure(builder, tmp_path):
    config = tmp_path / "tauri.conf.json"
    original = b'{"bundle":{"active":true,"icon":[]}}'
    config.write_bytes(original)

    with pytest.raises(RuntimeError, match="build failed"):
        with builder.temporary_external_bin(config):
            raise RuntimeError("build failed")

    assert config.read_bytes() == original


def test_release_bundle_forces_sidecar_rebuild(builder, monkeypatch, tmp_path):
    calls = []
    builder.TAURI_DIR = tmp_path
    configure_stub_stcp(builder, tmp_path)
    (tmp_path / "tauri.conf.json").write_text(
        '{"bundle":{"active":true,"icon":[]}}', encoding="utf-8"
    )
    monkeypatch.setattr(
        builder,
        "build_sidecar",
        lambda force=False: calls.append(force) or True,
    )
    monkeypatch.setattr(builder, "load_updater_private_key", lambda: "private-key")
    monkeypatch.setattr(
        builder, "build_tauri", lambda bundle=False, signing_key=None: None
    )

    builder.build_release_bundle()

    assert calls == [True]


def test_tauri_build_injects_packaged_api_origin(builder, monkeypatch, tmp_path):
    builder.GUI_DIR = tmp_path
    builder.TAURI_DIR = tmp_path / "src-tauri"
    executable = builder.TAURI_DIR / "target" / "release" / "mklink-ai-probe.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"exe")
    calls = []
    monkeypatch.setattr(
        builder,
        "run",
        lambda command, **kwargs: calls.append((command, kwargs)) or 0,
    )

    builder.build_tauri(bundle=False)

    assert calls[0][1]["env"]["VITE_MKLINK_API"] == "http://127.0.0.1:8765"


def test_release_bundle_removes_stale_bundle_outputs(builder, monkeypatch, tmp_path):
    builder.TAURI_DIR = tmp_path
    configure_stub_stcp(builder, tmp_path)
    (tmp_path / "tauri.conf.json").write_text(
        '{"version":"0.1.0-rc.2","bundle":{"active":true}}',
        encoding="utf-8",
    )
    stale_bundle = tmp_path / "target" / "release" / "bundle"
    stale_msi = stale_bundle / "msi" / "stale.msi"
    stale_msi.parent.mkdir(parents=True)
    stale_msi.write_bytes(b"stale")
    observed = []
    monkeypatch.setattr(builder, "build_sidecar", lambda force=False: True)
    monkeypatch.setattr(builder, "load_updater_private_key", lambda: "private-key")
    monkeypatch.setattr(
        builder,
        "build_tauri",
        lambda bundle=False, signing_key=None: observed.append(
            (bundle, stale_bundle.exists(), signing_key)
        ),
    )

    builder.build_release_bundle()

    assert observed == [(True, False, "private-key")]


def test_release_bundle_aborts_when_stale_outputs_cannot_be_removed(
    builder, monkeypatch, tmp_path,
):
    builder.TAURI_DIR = tmp_path
    configure_stub_stcp(builder, tmp_path)
    (tmp_path / "tauri.conf.json").write_text(
        '{"version":"0.1.0-rc.2","bundle":{"active":true}}',
        encoding="utf-8",
    )
    stale_bundle = tmp_path / "target" / "release" / "bundle"
    stale_bundle.mkdir(parents=True)
    built = []
    monkeypatch.setattr(builder, "build_sidecar", lambda force=False: True)
    monkeypatch.setattr(builder, "load_updater_private_key", lambda: "private-key")
    monkeypatch.setattr(builder.shutil, "rmtree", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        builder,
        "build_tauri",
        lambda bundle=False, signing_key=None: built.append((bundle, signing_key)),
    )

    with pytest.raises(RuntimeError, match="stale bundle"):
        builder.build_release_bundle()

    assert built == []


def test_bundle_config_preserves_product_version_and_builds_only_nsis(builder, tmp_path):
    config = tmp_path / "tauri.conf.json"
    original = b'{"version":"0.1.0-rc.1","bundle":{"active":true}}\n'
    config.write_bytes(original)

    with builder.temporary_bundle_config(config):
        patched = config.read_text(encoding="utf-8")
        assert '"version": "0.1.0-rc.1"' in patched
        assert '"targets": [' in patched
        assert '"nsis"' in patched
        assert '"msi"' not in patched
        assert '"externalBin"' in patched
        assert '"resources/mklink-stcp.dll": "mklink-stcp.dll"' in patched

    assert config.read_bytes() == original


def test_updater_private_key_is_required_outside_the_repository(
    builder, monkeypatch, tmp_path,
):
    monkeypatch.delenv("MKLINK_TAURI_UPDATER_KEY", raising=False)

    with pytest.raises(RuntimeError, match="updater private key"):
        builder.load_updater_private_key(env={}, home=tmp_path)


def test_signed_tauri_bundle_passes_private_key_only_to_child_environment(
    builder, monkeypatch, tmp_path,
):
    builder.GUI_DIR = tmp_path
    builder.TAURI_DIR = tmp_path / "src-tauri"
    executable = builder.TAURI_DIR / "target" / "release" / "mklink-ai-probe.exe"
    nsis = builder.TAURI_DIR / "target" / "release" / "bundle" / "nsis"
    executable.parent.mkdir(parents=True)
    nsis.mkdir(parents=True)
    executable.write_bytes(b"exe")
    setup = nsis / "Mklink AI Probe_0.1.0_x64-setup.exe"
    setup.write_bytes(b"setup")
    (nsis / "Mklink AI Probe_0.1.0_x64-setup.exe.sig").write_text(
        "signature", encoding="ascii"
    )
    calls = []
    monkeypatch.delenv("TAURI_SIGNING_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("TAURI_SIGNING_PRIVATE_KEY_PASSWORD", raising=False)
    monkeypatch.delenv("MKLINK_TAURI_UPDATER_KEY_PASSWORD", raising=False)
    monkeypatch.setattr(
        builder,
        "run",
        lambda command, **kwargs: calls.append((command, kwargs)) or 0,
    )

    outputs = builder.build_tauri(bundle=True, signing_key="private-key-value")

    assert calls[0][1]["env"]["TAURI_SIGNING_PRIVATE_KEY"] == "private-key-value"
    assert calls[0][1]["env"]["TAURI_SIGNING_PRIVATE_KEY_PASSWORD"] == ""
    assert "TAURI_SIGNING_PRIVATE_KEY" not in builder.os.environ
    assert "TAURI_SIGNING_PRIVATE_KEY_PASSWORD" not in builder.os.environ
    assert set(outputs) == {"setup", "updater_signature"}


def test_signed_bundle_outputs_reject_missing_signature_and_ignore_msi(
    builder, tmp_path,
):
    bundle = tmp_path / "bundle"
    nsis = bundle / "nsis"
    msi = bundle / "msi"
    nsis.mkdir(parents=True)
    msi.mkdir()
    setup = nsis / "app-setup.exe"
    setup.write_bytes(b"setup")
    (msi / "app.msi").write_bytes(b"msi")

    with pytest.raises(RuntimeError, match="signature"):
        builder.collect_signed_bundle_outputs(bundle)

    signature = nsis / "app-setup.exe.sig"
    signature.write_text("signature", encoding="ascii")
    outputs = builder.collect_signed_bundle_outputs(bundle)

    assert outputs == {
        "setup": setup,
        "updater_signature": signature,
    }


def test_tauri_bundle_includes_complete_third_party_license_texts():
    tauri_dir = PROJECT_ROOT / "gui" / "src-tauri"
    config = json.loads((tauri_dir / "tauri.conf.json").read_text(encoding="utf-8"))
    notices_relative = "resources/THIRD-PARTY-NOTICES.txt"

    assert notices_relative in config["bundle"]["resources"]
    notices = (tauri_dir / notices_relative).read_text(encoding="utf-8")
    assert "Apache License" in notices
    assert "Version 2.0, January 2004" in notices
    assert "END OF TERMS AND CONDITIONS" in notices
    assert "BSD 3-Clause License" in notices
    assert "Neither the name of Nordic Semiconductor ASA" in notices
    assert "pyelftools 0.32" in notices
    assert "free and unencumbered software released into the public domain" in notices
    assert "http://unlicense.org/" in notices


def test_tauri_bundle_integrates_mklink_usb_port_naming():
    tauri_dir = PROJECT_ROOT / "gui" / "src-tauri"
    config = json.loads((tauri_dir / "tauri.conf.json").read_text(encoding="utf-8"))
    nsis = config["bundle"]["windows"]["nsis"]
    hook = (tauri_dir / "installer-hooks.nsh").read_text(encoding="utf-8")

    assert nsis["installMode"] == "perMachine"
    assert nsis["installerHooks"] == "installer-hooks.nsh"
    assert "NSIS_HOOK_PREINSTALL" in hook
    assert "$LOCALAPPDATA\\Mklink AI Probe\\uninstall.exe" in hook
    assert 'HKCU "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\Mklink AI Probe"' in hook
    assert 'HKLM "Software\\WOW6432Node\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\Mklink AI Probe"' in hook
    assert 'ReadRegStr $0 ${ROOT} "${KEY}" "UninstallString"' in hook
    assert "NSIS_HOOK_POSTINSTALL" in hook
    assert "Initializing MKLink USB serial port naming" in hook
    assert "the helper still runs successfully" in hook
    assert "--manage-usb-port-names apply" in hook
    assert "$INSTDIR\\mklink-ai-probe.exe" in hook


def test_stable_product_version_and_signed_updater_are_configured():
    pyproject = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    cargo = (PROJECT_ROOT / "gui" / "src-tauri" / "Cargo.toml").read_text(
        encoding="utf-8"
    )
    config = json.loads(
        (PROJECT_ROOT / "gui" / "src-tauri" / "tauri.conf.json").read_text(
            encoding="utf-8"
        )
    )
    package = json.loads(
        (PROJECT_ROOT / "gui" / "package.json").read_text(encoding="utf-8")
    )
    lib_rs = (PROJECT_ROOT / "gui" / "src-tauri" / "src" / "lib.rs").read_text(
        encoding="utf-8"
    )
    capability = json.loads(
        (
            PROJECT_ROOT
            / "gui"
            / "src-tauri"
            / "capabilities"
            / "default.json"
        ).read_text(encoding="utf-8")
    )

    python_version = tomllib.loads(pyproject)["project"]["version"]
    cargo_version = tomllib.loads(cargo)["package"]["version"]
    assert python_version == cargo_version == config["version"]
    assert len(config["version"].split(".")) == 3
    assert all(part.isdigit() for part in config["version"].split("."))
    assert config["bundle"]["createUpdaterArtifacts"] is True
    assert config["plugins"]["updater"]["endpoints"] == [GITEE_UPDATER_ENDPOINT]
    assert config["plugins"]["updater"]["pubkey"].strip()

    assert 'tauri-plugin-updater = "2"' in cargo
    assert 'tauri-plugin-process = "2"' in cargo
    assert ".plugin(tauri_plugin_updater::Builder::new().build())" in lib_rs
    assert ".plugin(tauri_plugin_process::init())" in lib_rs
    assert "@tauri-apps/plugin-updater" in package["dependencies"]
    assert "@tauri-apps/plugin-process" in package["dependencies"]
    assert "updater:default" in capability["permissions"]
    assert "process:allow-restart" in capability["permissions"]


def test_sidecar_collects_pyocd_plugins_metadata_and_hid_binary(builder, monkeypatch, tmp_path):
    builder.SKILL_DIR = tmp_path
    builder.TAURI_DIR = tmp_path / "gui" / "src-tauri"
    commands = []

    def fake_run(command, **_kwargs):
        commands.append(command)
        output = tmp_path / "dist" / "mklink-sidecar.exe"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"sidecar")
        return 0

    monkeypatch.setattr(builder, "run", fake_run)

    assert builder.build_sidecar(force=True) is True

    pairs = [commands[0][index:index + 2] for index in range(len(commands[0]) - 1)]
    assert ["--collect-all", "elftools"] in pairs
    assert ["--collect-all", "pyocd"] in pairs
    assert ["--copy-metadata", "pyocd"] in pairs
    assert ["--collect-all", "cmsis_pack_manager"] in pairs
    assert ["--collect-all", "hid"] in pairs


def test_skill_defaults_axf_to_builtin_parser():
    text = (PROJECT_ROOT / "SKILL.md").read_text(encoding="utf-8")

    assert "默认使用内置 pyelftools" in text
    assert "仅在用户明确指定" in text


def test_builtin_pack_builder_keeps_only_descriptor_algorithms_and_licenses(
    builtin_pack_builder, monkeypatch, tmp_path,
):
    pack_root = tmp_path / "packs"
    source = pack_root / "Keil" / "Test_DFP" / "1.0.0"
    (source / "Flash").mkdir(parents=True)
    (source / "Keil.Test_DFP.pdsc").write_text(
        '<package><vendor>Keil</vendor><name>Test_DFP</name>'
        '<license>LICENSE</license>'
        '<releases><release version="1.0.0"/></releases>'
        '<devices><family Dfamily="Test"><device Dname="TEST123">'
        '<algorithm name="Flash/Test.FLM" start="0x08000000" size="0x1000"/>'
        '</device></family></devices></package>',
        encoding="utf-8",
    )
    (source / "Flash" / "Test.FLM").write_bytes(b"algorithm")
    (source / "LICENSE").write_text("Apache-2.0", encoding="utf-8")
    (source / "example.bin").write_bytes(b"do-not-bundle")
    selected_names = ["Flash/Test.FLM", "Keil.Test_DFP.pdsc", "LICENSE"]
    tree_digest = source_tree_digest(source, selected_names)
    license_digest = hashlib.sha256(b"Apache-2.0").hexdigest()
    config = tmp_path / "builtin-packs.json"
    config.write_text(json.dumps({
        "schema": 1,
        "packs": [{
            "pack_id": "Keil.Test_DFP",
            "version": "1.0.0",
            "source_url": "https://vendor.example/Keil.Test_DFP.1.0.0.pack",
            "source_tree_sha256": tree_digest,
            "redistribution_authorized": True,
            "redistribution_basis": "Apache-2.0",
            "license_files": [{"path": "LICENSE", "sha256": license_digest}],
        }],
    }), encoding="utf-8")
    monkeypatch.setattr(
        builtin_pack_builder,
        "_read_targets",
        lambda _path: [{"part_number": "TEST123", "vendor": "Keil"}],
    )
    output = tmp_path / "bundle"

    manifest = builtin_pack_builder.build_bundle(config, [pack_root], output)

    slim_pack = output / manifest["packs"][0]["file"]
    with ZipFile(slim_pack) as archive:
        assert sorted(archive.namelist()) == selected_names
    assert manifest["target_count"] == 1
    record = manifest["packs"][0]
    assert record["source_url"] == "https://vendor.example/Keil.Test_DFP.1.0.0.pack"
    assert record["source_tree_sha256"] == tree_digest
    assert record["redistribution_basis"] == "Apache-2.0"
    assert record["licenses"] == [{"path": "LICENSE", "sha256": license_digest}]
    assert json.loads((output / "manifest.json").read_text(encoding="utf-8")) == manifest


def test_builtin_pack_directory_rejects_changed_source_tree(
    builtin_pack_builder, monkeypatch, tmp_path,
):
    pack_root = tmp_path / "packs"
    source = pack_root / "Keil" / "Test_DFP" / "1.0.0"
    (source / "Flash").mkdir(parents=True)
    (source / "Keil.Test_DFP.pdsc").write_text(
        '<package><vendor>Keil</vendor><name>Test_DFP</name>'
        '<license>LICENSE</license>'
        '<releases><release version="1.0.0"/></releases>'
        '<devices><family Dfamily="Test"><device Dname="TEST123">'
        '<algorithm name="Flash/Test.FLM" start="0x08000000" size="0x1000"/>'
        '</device></family></devices></package>',
        encoding="utf-8",
    )
    (source / "Flash" / "Test.FLM").write_bytes(b"algorithm")
    (source / "LICENSE").write_bytes(b"Apache-2.0")
    config = tmp_path / "builtin-packs.json"
    config.write_text(json.dumps({
        "schema": 1,
        "packs": [{
            "pack_id": "Keil.Test_DFP",
            "version": "1.0.0",
            "source_url": "https://vendor.example/Keil.Test_DFP.1.0.0.pack",
            "source_tree_sha256": "0" * 64,
            "redistribution_authorized": True,
            "redistribution_basis": "Apache-2.0",
            "license_files": [{
                "path": "LICENSE",
                "sha256": hashlib.sha256(b"Apache-2.0").hexdigest(),
            }],
        }],
    }), encoding="utf-8")
    monkeypatch.setattr(
        builtin_pack_builder,
        "_read_targets",
        lambda _path: [{"part_number": "TEST123", "vendor": "Keil"}],
    )

    with pytest.raises(ValueError, match="source tree SHA-256"):
        builtin_pack_builder.build_bundle(config, [pack_root], tmp_path / "out")


@pytest.mark.parametrize(
    ("pack_id", "version"),
    [
        ("Vendor.Bad/Name", "1.0.0"),
        ("Vendor.Device_DFP", "../1.0.0"),
    ],
)
def test_builtin_pack_directory_rejects_unsafe_identity_segments(
    builtin_pack_builder, tmp_path, pack_id, version,
):
    config = tmp_path / "builtin-packs.json"
    config.write_text(json.dumps({
        "schema": 1,
        "packs": [{"pack_id": pack_id, "version": version}],
    }), encoding="utf-8")

    with pytest.raises(ValueError, match="safe path segment"):
        builtin_pack_builder.build_bundle(config, [tmp_path], tmp_path / "out")


@pytest.mark.parametrize(
    ("vendor", "name", "release", "license_xml", "message"),
    [
        ("Other", "Test_DFP", "1.0.0", "<license>LICENSE</license>", "identity"),
        ("Keil", "Other_DFP", "1.0.0", "<license>LICENSE</license>", "identity"),
        ("Keil", "Test_DFP", "2.0.0", "<license>LICENSE</license>", "version"),
        ("Keil", "Test_DFP", "1.0.0", "", "declare a license"),
        ("Keil", "Test_DFP", "1.0.0", "<license>OTHER.txt</license>", "pinned"),
    ],
)
def test_builtin_pack_directory_validates_descriptor_identity_and_licenses(
    builtin_pack_builder, monkeypatch, tmp_path,
    vendor, name, release, license_xml, message,
):
    pack_root = tmp_path / "packs"
    source = pack_root / "Keil" / "Test_DFP" / "1.0.0"
    (source / "Flash").mkdir(parents=True)
    descriptor = (
        f"<package><vendor>{vendor}</vendor><name>{name}</name>{license_xml}"
        f'<releases><release version="{release}"/></releases>'
        '<devices><family Dfamily="Test"><device Dname="TEST123">'
        '<algorithm name="Flash/Test.FLM" start="0x08000000" size="0x1000"/>'
        '</device></family></devices></package>'
    )
    (source / "Keil.Test_DFP.pdsc").write_text(descriptor, encoding="utf-8")
    (source / "Flash" / "Test.FLM").write_bytes(b"algorithm")
    (source / "LICENSE").write_bytes(b"Apache-2.0")
    names = ["Flash/Test.FLM", "Keil.Test_DFP.pdsc", "LICENSE"]
    config = tmp_path / "builtin-packs.json"
    config.write_text(json.dumps({
        "schema": 1,
        "packs": [{
            "pack_id": "Keil.Test_DFP",
            "version": "1.0.0",
            "source_url": "https://vendor.example/Keil.Test_DFP.1.0.0.pack",
            "source_tree_sha256": source_tree_digest(source, names),
            "redistribution_authorized": True,
            "redistribution_basis": "Apache-2.0",
            "license_files": [{
                "path": "LICENSE",
                "sha256": hashlib.sha256(b"Apache-2.0").hexdigest(),
            }],
        }],
    }), encoding="utf-8")
    monkeypatch.setattr(
        builtin_pack_builder,
        "_read_targets",
        lambda _path: [{"part_number": "TEST123", "vendor": "Keil"}],
    )

    with pytest.raises(ValueError, match=message):
        builtin_pack_builder.build_bundle(config, [pack_root], tmp_path / "out")


def test_builtin_pack_builder_accepts_explicit_authorized_slim_archives(
    builtin_pack_builder, monkeypatch, tmp_path,
):
    pack_root = tmp_path / "pyocd-packs"
    pack_root.mkdir()
    archive_path = pack_root / "Vendor.Device_DFP.1.0.0-small.pack"
    descriptor = (
        '<package><vendor>Vendor</vendor><name>Device_DFP</name>'
        '<license>LICENSE.txt</license>'
        '<releases><release version="1.0.0"/></releases>'
        '<devices><family Dfamily="Test"><device Dname="DEVICE_A">'
        '<algorithm name="Flash/Test.FLM" start="0x08000000" size="0x1000"/>'
        '</device></family></devices></package>'
    )
    with ZipFile(archive_path, "w") as archive:
        archive.writestr("Vendor.Device_DFP.pdsc", descriptor)
        archive.writestr("Flash/Test.FLM", b"algorithm")
        archive.writestr("LICENSE.txt", "Redistribution terms")
        archive.writestr("Examples/large.bin", b"do-not-bundle")
    source_digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    license_digest = hashlib.sha256(b"Redistribution terms").hexdigest()
    config = tmp_path / "builtin-packs.json"
    config.write_text(json.dumps({
        "schema": 1,
        "packs": [],
        "archives": [{
            "file": archive_path.name,
            "sha256": source_digest,
            "source_url": "https://vendor.example/packs/Vendor.Device_DFP.1.0.0.pack",
            "redistribution_authorized": True,
            "redistribution_basis": "Vendor terms permit redistribution",
            "license_files": [{"path": "LICENSE.txt", "sha256": license_digest}],
            "provenance": "local pyOCD resource bundle",
        }],
    }), encoding="utf-8")
    monkeypatch.setattr(
        builtin_pack_builder,
        "_read_targets",
        lambda _path: [{"part_number": "DEVICE_A", "vendor": "Vendor"}],
    )

    manifest = builtin_pack_builder.build_bundle(config, [pack_root], tmp_path / "out")

    record = manifest["packs"][0]
    assert record["pack_id"] == "Vendor.Device_DFP"
    assert record["version"] == "1.0.0"
    assert record["provenance"] == "local pyOCD resource bundle"
    assert record["source_sha256"] == source_digest
    assert record["source_url"] == "https://vendor.example/packs/Vendor.Device_DFP.1.0.0.pack"
    assert record["redistribution_basis"] == "Vendor terms permit redistribution"
    assert record["licenses"] == [{"path": "LICENSE.txt", "sha256": license_digest}]
    with ZipFile(tmp_path / "out" / record["file"]) as archive:
        assert sorted(archive.namelist()) == ["Flash/Test.FLM", "LICENSE.txt", "Vendor.Device_DFP.pdsc"]


def test_builtin_archive_requires_allowlisted_digest_and_descriptor_license(
    builtin_pack_builder, tmp_path,
):
    pack_root = tmp_path / "packs"
    pack_root.mkdir()
    archive_path = pack_root / "Vendor.Unlicensed.1.0.0.pack"
    with ZipFile(archive_path, "w") as archive:
        archive.writestr(
            "Vendor.Unlicensed.pdsc",
            '<package><vendor>Vendor</vendor><name>Unlicensed</name>'
            '<releases><release version="1.0.0"/></releases></package>',
        )
    config = tmp_path / "builtin-packs.json"
    config.write_text(json.dumps({
        "schema": 1,
        "packs": [],
        "archives": [{
            "file": archive_path.name,
            "sha256": "0" * 64,
            "source_url": "https://vendor.example/Unlicensed.pack",
            "redistribution_authorized": True,
            "redistribution_basis": "fixture terms permit redistribution",
            "license_files": [{
                "path": "LICENSE.txt",
                "sha256": hashlib.sha256(b"missing").hexdigest(),
            }],
            "provenance": "fixture",
        }],
    }), encoding="utf-8")

    with pytest.raises(ValueError, match="SHA-256"):
        builtin_pack_builder.build_bundle(config, [pack_root], tmp_path / "bad-hash")

    payload = json.loads(config.read_text(encoding="utf-8"))
    payload["archives"][0]["sha256"] = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    config.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="license"):
        builtin_pack_builder.build_bundle(config, [pack_root], tmp_path / "no-license")


def test_builtin_archive_rejects_unsafe_descriptor_path(
    builtin_pack_builder, tmp_path,
):
    pack_root = tmp_path / "packs"
    pack_root.mkdir()
    archive_path = pack_root / "Vendor.Device_DFP.1.0.0.pack"
    descriptor = (
        '<package><vendor>Vendor</vendor><name>Device_DFP</name>'
        '<license>LICENSE.txt</license>'
        '<releases><release version="1.0.0"/></releases></package>'
    )
    with ZipFile(archive_path, "w") as archive:
        archive.writestr("../Vendor.Device_DFP.pdsc", descriptor)
        archive.writestr("LICENSE.txt", b"Redistribution terms")
    config = tmp_path / "builtin-packs.json"
    config.write_text(json.dumps({
        "schema": 1,
        "packs": [],
        "archives": [{
            "file": archive_path.name,
            "sha256": hashlib.sha256(archive_path.read_bytes()).hexdigest(),
            "source_url": "https://vendor.example/Device.pack",
            "redistribution_authorized": True,
            "redistribution_basis": "Redistribution terms",
            "license_files": [{
                "path": "LICENSE.txt",
                "sha256": hashlib.sha256(b"Redistribution terms").hexdigest(),
            }],
            "provenance": "fixture",
        }],
    }), encoding="utf-8")

    with pytest.raises(ValueError, match="safe relative path"):
        builtin_pack_builder.build_bundle(config, [pack_root], tmp_path / "out")


def test_builtin_archive_rejects_duplicate_pack_identity(
    builtin_pack_builder, monkeypatch, tmp_path,
):
    pack_root = tmp_path / "packs"
    pack_root.mkdir()
    descriptor = (
        '<package><vendor>Vendor</vendor><name>Device_DFP</name>'
        '<license>LICENSE.txt</license>'
        '<releases><release version="1.0.0"/></releases>'
        '<devices><family Dfamily="Test"><device Dname="DEVICE_A">'
        '<algorithm name="Flash/Test.FLM" start="0x08000000" size="0x1000"/>'
        '</device></family></devices></package>'
    )
    records = []
    for index in (1, 2):
        archive_path = pack_root / f"copy-{index}.pack"
        with ZipFile(archive_path, "w") as archive:
            archive.writestr("Vendor.Device_DFP.pdsc", descriptor)
            archive.writestr("Flash/Test.FLM", b"algorithm")
            archive.writestr("LICENSE.txt", b"Redistribution terms")
        records.append({
            "file": archive_path.name,
            "sha256": hashlib.sha256(archive_path.read_bytes()).hexdigest(),
            "source_url": f"https://vendor.example/copy-{index}.pack",
            "redistribution_authorized": True,
            "redistribution_basis": "Redistribution terms",
            "license_files": [{
                "path": "LICENSE.txt",
                "sha256": hashlib.sha256(b"Redistribution terms").hexdigest(),
            }],
            "provenance": "fixture",
        })
    config = tmp_path / "builtin-packs.json"
    config.write_text(json.dumps({
        "schema": 1,
        "packs": [],
        "archives": records,
    }), encoding="utf-8")
    monkeypatch.setattr(
        builtin_pack_builder,
        "_read_targets",
        lambda _path: [{"part_number": "DEVICE_A", "vendor": "Vendor"}],
    )

    with pytest.raises(ValueError, match="unique"):
        builtin_pack_builder.build_bundle(config, [pack_root], tmp_path / "out")


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("source_url", None, "source_url"),
        ("source_url", "http://vendor.example/Device.pack", "HTTPS"),
        ("redistribution_basis", None, "redistribution basis"),
        ("redistribution_authorized", False, "redistribution_authorized"),
        ("license_files", None, "license_files"),
    ],
)
def test_builtin_archive_requires_public_redistribution_metadata(
    builtin_pack_builder, monkeypatch, tmp_path, field, value, message,
):
    pack_root = tmp_path / "packs"
    pack_root.mkdir()
    archive_path = pack_root / "Vendor.Device_DFP.1.0.0.pack"
    descriptor = (
        '<package><vendor>Vendor</vendor><name>Device_DFP</name>'
        '<license>LICENSE.txt</license>'
        '<releases><release version="1.0.0"/></releases>'
        '<devices><family Dfamily="Test"><device Dname="DEVICE_A">'
        '<algorithm name="Flash/Test.FLM" start="0x08000000" size="0x1000"/>'
        '</device></family></devices></package>'
    )
    with ZipFile(archive_path, "w") as archive:
        archive.writestr("Vendor.Device_DFP.pdsc", descriptor)
        archive.writestr("Flash/Test.FLM", b"algorithm")
        archive.writestr("LICENSE.txt", b"Redistribution terms")
    record = {
        "file": archive_path.name,
        "sha256": hashlib.sha256(archive_path.read_bytes()).hexdigest(),
        "source_url": "https://vendor.example/Device.pack",
        "redistribution_authorized": True,
        "redistribution_basis": "Vendor terms permit redistribution",
        "license_files": [{
            "path": "LICENSE.txt",
            "sha256": hashlib.sha256(b"Redistribution terms").hexdigest(),
        }],
        "provenance": "official vendor Pack",
    }
    if value is None:
        record.pop(field)
    else:
        record[field] = value
    config = tmp_path / "builtin-packs.json"
    config.write_text(
        json.dumps({"schema": 1, "packs": [], "archives": [record]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        builtin_pack_builder,
        "_read_targets",
        lambda _path: [{"part_number": "DEVICE_A", "vendor": "Vendor"}],
    )

    with pytest.raises(ValueError, match=message):
        builtin_pack_builder.build_bundle(config, [pack_root], tmp_path / "out")


def test_builtin_archive_rejects_changed_license_bytes(
    builtin_pack_builder, monkeypatch, tmp_path,
):
    pack_root = tmp_path / "packs"
    pack_root.mkdir()
    archive_path = pack_root / "Vendor.Device_DFP.1.0.0.pack"
    descriptor = (
        '<package><vendor>Vendor</vendor><name>Device_DFP</name>'
        '<license>LICENSE.txt</license>'
        '<releases><release version="1.0.0"/></releases>'
        '<devices><family Dfamily="Test"><device Dname="DEVICE_A">'
        '<algorithm name="Flash/Test.FLM" start="0x08000000" size="0x1000"/>'
        '</device></family></devices></package>'
    )
    with ZipFile(archive_path, "w") as archive:
        archive.writestr("Vendor.Device_DFP.pdsc", descriptor)
        archive.writestr("Flash/Test.FLM", b"algorithm")
        archive.writestr("LICENSE.txt", b"changed terms")
    config = tmp_path / "builtin-packs.json"
    config.write_text(json.dumps({
        "schema": 1,
        "packs": [],
        "archives": [{
            "file": archive_path.name,
            "sha256": hashlib.sha256(archive_path.read_bytes()).hexdigest(),
            "source_url": "https://vendor.example/Device.pack",
            "redistribution_authorized": True,
            "redistribution_basis": "Vendor terms permit redistribution",
            "license_files": [{
                "path": "LICENSE.txt",
                "sha256": hashlib.sha256(b"original terms").hexdigest(),
            }],
            "provenance": "official vendor Pack",
        }],
    }), encoding="utf-8")
    monkeypatch.setattr(
        builtin_pack_builder,
        "_read_targets",
        lambda _path: [{"part_number": "DEVICE_A", "vendor": "Vendor"}],
    )

    with pytest.raises(ValueError, match="license SHA-256"):
        builtin_pack_builder.build_bundle(config, [pack_root], tmp_path / "out")


def test_sidecar_collects_generated_builtin_pack_bundle(builder, monkeypatch, tmp_path):
    builder.SKILL_DIR = tmp_path
    builder.TAURI_DIR = tmp_path / "gui" / "src-tauri"
    config = tmp_path / "skills" / "tauri-gui-builder" / "builtin-packs.json"
    config.parent.mkdir(parents=True)
    config.write_text('{"schema":1,"packs":[]}', encoding="utf-8")
    pack_root = tmp_path / "pack-root"
    pack_root.mkdir()
    monkeypatch.setenv("MKLINK_BUILTIN_PACK_ROOTS", str(pack_root))
    generated = []
    commands = []

    def fake_bundle(_config, roots, output):
        generated.append((list(roots), output))
        output.mkdir(parents=True)
        (output / "manifest.json").write_text('{"schema":1,"packs":[]}', encoding="utf-8")
        return {"target_count": 0, "packs": []}

    def fake_run(command, **_kwargs):
        commands.append(command)
        output = tmp_path / "dist" / "mklink-sidecar.exe"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"sidecar")
        return 0

    monkeypatch.setattr(builder, "build_builtin_pack_bundle", fake_bundle)
    monkeypatch.setattr(builder, "run", fake_run)

    assert builder.build_sidecar(force=True) is True

    command = commands[0]
    add_data_index = command.index("--add-data")
    assert command[add_data_index + 1].endswith(";mklink/builtin_packs")
    assert generated[0][0] == [pack_root]


def test_sidecar_collects_generated_daplinkutility_flm_bundle(
    builder, monkeypatch, tmp_path
):
    builder.SKILL_DIR = tmp_path
    builder.TAURI_DIR = tmp_path / "gui" / "src-tauri"
    executable = tmp_path / "DAPLinkUtility.exe"
    executable.write_bytes(b"pinned-source")
    monkeypatch.setenv("MKLINK_DAPLINKUTILITY_EXE", str(executable))
    generated = []
    commands = []

    def fake_bundle(source, output):
        generated.append((source, output))
        output.mkdir(parents=True)
        (output / "manifest.json").write_text(
            '{"schema":1,"targets":[]}', encoding="utf-8"
        )
        return {"target_count": 8137, "blob_count": 1428}

    def fake_run(command, **_kwargs):
        commands.append(command)
        output = tmp_path / "dist" / "mklink-sidecar.exe"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"sidecar")
        return 0

    monkeypatch.setattr(builder, "build_daplinkutility_flm_bundle", fake_bundle)
    monkeypatch.setattr(builder, "run", fake_run)

    assert builder.build_sidecar(force=True) is True

    command = commands[0]
    add_data = [
        command[index + 1]
        for index, value in enumerate(command[:-1])
        if value == "--add-data"
    ]
    assert any(value.endswith(";mklink/builtin_flm") for value in add_data)
    assert generated[0][0] == executable


def test_daplinkutility_executable_rejects_missing_source(builder, monkeypatch, tmp_path):
    monkeypatch.setenv("MKLINK_DAPLINKUTILITY_EXE", str(tmp_path / "missing.exe"))

    with pytest.raises(RuntimeError, match="does not exist"):
        builder.daplinkutility_executable()
