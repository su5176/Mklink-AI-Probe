from pathlib import Path
import hashlib
import io
import os
from urllib.error import URLError

from mklink import firmware_check as fc


def _valid_uf2(payload: bytes = b"firmware") -> bytes:
    block = bytearray(512)
    block[0:4] = (0x0A324655).to_bytes(4, "little")
    block[4:8] = (0x9E5D5157).to_bytes(4, "little")
    block[16:20] = len(payload).to_bytes(4, "little")
    block[20:24] = (0).to_bytes(4, "little")
    block[24:28] = (1).to_bytes(4, "little")
    block[32:32 + len(payload)] = payload
    block[508:512] = (0x0AB16F30).to_bytes(4, "little")
    return bytes(block)


def _readme(path: Path, version: str, *, family: str = "microlink") -> None:
    marker = "HPM Firmware Build Date" if family == "hpmlink" else "Firmware Build Date"
    (path / "readme.txt").write_text(f"{marker}:test\n{version}\n", encoding="utf-8")


def test_read_microkeen_version_uses_first_changelog_version(tmp_path):
    _readme(tmp_path, "V3.3.7\nV3.3.6")
    assert fc.read_microkeen_version(tmp_path) == fc.Version(3, 3, 7)


def test_parse_hpmlink_firmware_accepts_only_v4():
    info = fc.parse_firmware_filename("HPMLink_V4.3.7.uf2")

    assert info is not None
    assert info.family == "hpmlink"
    assert info.model == "V4"
    assert info.version == fc.Version(4, 3, 7)
    assert fc.parse_firmware_filename("HPMLink_V3.3.7.uf2") is None


def test_manifest_parser_requires_integrity_and_exact_hardware_identity():
    content = _valid_uf2()
    payload = {
        "schema": fc.FIRMWARE_MANIFEST_SCHEMA,
        "firmwares": [{
            "family": "hpmlink",
            "model": "V4",
            "version": "V4.3.7",
            "name": "HPMLink_V4.3.7.uf2",
            "size": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
            "urls": {
                "github": "https://github.example/HPMLink_V4.3.7.uf2",
                "gitee": "https://gitee.example/HPMLink_V4.3.7.uf2",
            },
        }],
    }

    entries = fc._parse_manifest(payload)

    assert len(entries) == 1
    assert entries[0].family == "hpmlink"
    assert entries[0].size == 512
    assert entries[0].download_urls == payload["firmwares"][0]["urls"]


def test_materialize_manifest_firmware_validates_and_falls_back(monkeypatch):
    content = _valid_uf2(b"remote")
    info = fc._parse_manifest({
        "schema": fc.FIRMWARE_MANIFEST_SCHEMA,
        "firmwares": [{
            "family": "microlink", "model": "V3", "version": "V3.3.7",
            "name": "MicroLink_V3.3.7.uf2", "size": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
            "urls": {
                "github": "https://github.example/firmware.uf2",
                "gitee": "https://gitee.example/firmware.uf2",
            },
        }],
    })[0]
    requested = []

    def download(request, timeout):
        requested.append(request.full_url)
        if "github" in request.full_url:
            raise URLError("unavailable")
        return io.BytesIO(content)

    monkeypatch.setattr(fc, "urlopen", download)
    path, temporary, source = fc._materialize_firmware(info)
    try:
        assert temporary is True
        assert source == "gitee"
        assert path.read_bytes() == content
        assert requested == [info.download_urls["github"], info.download_urls["gitee"]]
    finally:
        path.unlink(missing_ok=True)


def test_find_bootloader_disk_uses_uf2_marker(monkeypatch, tmp_path):
    (tmp_path / "INFO_UF2.TXT").write_text("UF2 Bootloader", encoding="ascii")
    monkeypatch.setenv("MKLINK_BOOTLOADER_DISK", str(tmp_path))
    assert fc._find_bootloader_disk() == str(tmp_path).rstrip("\\/") + ("\\" if os.name == "nt" else "/")


def test_upgrade_probe_firmware_returns_up_to_date_without_reboot(monkeypatch, tmp_path):
    disk = tmp_path / "disk"
    disk.mkdir()
    _readme(disk, "V3.3.7")
    firmware_root = tmp_path / "firmware"
    firmware_root.mkdir()
    (firmware_root / "MicroLink_V3.3.7.uf2").write_bytes(b"uf2")
    monkeypatch.setattr(fc, "_probe_disk", lambda: str(disk))
    monkeypatch.setattr(fc, "_remote_firmware", lambda model, **kwargs: None)

    class Device:
        def enter_bootloader(self):
            raise AssertionError("up-to-date firmware must not reboot")

    result = fc.upgrade_probe_firmware(Device(), firmware_root, confirm=True)
    assert result["status"] == "up_to_date"


def test_upgrade_probe_firmware_copies_and_verifies(monkeypatch, tmp_path):
    disk = tmp_path / "disk"
    disk.mkdir()
    _readme(disk, "V3.3.7")
    boot = tmp_path / "boot"
    boot.mkdir()
    (boot / "INFO_UF2.TXT").write_text("UF2 Bootloader", encoding="ascii")
    firmware_root = tmp_path / "firmware"
    firmware_root.mkdir()
    firmware = firmware_root / "MicroLink_V3.3.8.uf2"
    firmware.write_bytes(b"new-uf2")
    sequence = iter([str(disk), str(boot), str(disk)])
    monkeypatch.setattr(fc, "_probe_disk", lambda: next(sequence, str(disk)))
    monkeypatch.setattr(fc, "_find_bootloader_disk", lambda: str(boot))
    monkeypatch.setattr(
        fc,
        "_remote_firmware",
        lambda model, **kwargs: fc.FirmwareInfo("MicroLink_V3.3.8.uf2", fc.Version(3, 3, 8), "V3", firmware),
    )
    entered = []

    class Device:
        def enter_bootloader(self):
            entered.append(True)
            _readme(disk, "V3.3.8")

    result = fc.upgrade_probe_firmware(
        Device(), firmware_root, confirm=True, bootloader_timeout=0.1, verify_timeout=0.6
    )
    assert entered == [True]
    assert result["status"] == "updated"
    assert (boot / firmware.name).read_bytes() == b"new-uf2"


def test_upgrade_probe_firmware_selects_hpmlink_for_hpm_readme(
    monkeypatch, tmp_path,
):
    disk = tmp_path / "disk"
    disk.mkdir()
    _readme(disk, "V4.3.6", family="hpmlink")
    (disk / "STARTUP_ANIMATION.zhrgb").write_bytes(b"animation")
    boot = tmp_path / "boot"
    boot.mkdir()
    (boot / "INFO_UF2.TXT").write_text("UF2 Bootloader", encoding="ascii")
    firmware_root = tmp_path / "firmware"
    firmware_root.mkdir()
    hpmlink = firmware_root / "HPMLink_V4.3.7.uf2"
    hpmlink.write_bytes(b"hpm-uf2")
    (firmware_root / "MicroLink_V4.3.8.uf2").write_bytes(b"micro-uf2")
    sequence = iter([str(disk), str(boot), str(disk)])
    monkeypatch.setattr(fc, "_probe_disk", lambda: next(sequence, str(disk)))
    monkeypatch.setattr(fc, "_find_bootloader_disk", lambda: str(boot))
    monkeypatch.setattr(fc, "_remote_firmware", lambda model, **kwargs: None)

    class Device:
        def enter_bootloader(self):
            _readme(disk, "V4.3.7", family="hpmlink")

    result = fc.upgrade_probe_firmware(
        Device(), firmware_root, confirm=True,
        bootloader_timeout=0.1, verify_timeout=0.6,
    )

    assert result["status"] == "updated"
    assert result["family"] == "hpmlink"
    assert result["firmware"] == hpmlink.name
    assert (boot / hpmlink.name).read_bytes() == b"hpm-uf2"
    assert not (boot / "MicroLink_V4.3.8.uf2").exists()


def test_upgrade_probe_firmware_keeps_microlink_for_unmarked_v4_disk(
    monkeypatch, tmp_path,
):
    disk = tmp_path / "disk"
    disk.mkdir()
    _readme(disk, "V4.3.6")
    firmware_root = tmp_path / "firmware"
    firmware_root.mkdir()
    microlink = firmware_root / "MicroLink_V4.3.6.uf2"
    microlink.write_bytes(b"micro-uf2")
    (firmware_root / "HPMLink_V4.3.7.uf2").write_bytes(b"hpm-uf2")
    monkeypatch.setattr(fc, "_probe_disk", lambda: str(disk))
    monkeypatch.setattr(fc, "_remote_firmware", lambda model, **kwargs: None)

    result = fc.upgrade_probe_firmware(object(), firmware_root, confirm=True)

    assert result["status"] == "up_to_date"
    assert result["firmware"] == microlink.name


def test_probe_firmware_family_ignores_animation_without_hpm_readme(tmp_path):
    _readme(tmp_path, "V4.3.7")
    (tmp_path / "STARTUP_ANIMATION.zhrgb").write_bytes(b"animation")
    assert fc.probe_firmware_family(tmp_path, fc.Version(4, 3, 7)) == "microlink"


def test_probe_firmware_family_requires_v4_even_with_hpm_readme(tmp_path):
    _readme(tmp_path, "V3.3.7", family="hpmlink")
    assert fc.probe_firmware_family(tmp_path, fc.Version(3, 3, 7)) == "microlink"


def test_latest_firmware_prefers_newer_local_release_candidate(
    monkeypatch, tmp_path,
):
    remote = fc.FirmwareInfo(
        "HPMLink_V4.3.7.uf2",
        fc.Version(4, 3, 7),
        "V4",
        Path("HPMLink_V4.3.7.uf2"),
        family="hpmlink",
        download_url="https://github.example/HPMLink_V4.3.7.uf2",
    )
    local = tmp_path / "HPMLink_V4.3.8.uf2"
    local.write_bytes(b"candidate")
    monkeypatch.setattr(fc, "_remote_firmware", lambda *args, **kwargs: remote)

    candidate = fc.latest_firmware("V4", tmp_path, family="hpmlink")

    assert candidate is not None
    assert candidate.version == fc.Version(4, 3, 8)
    assert candidate.path == local


def test_firmware_check_filters_v4_candidates_by_probe_family(
    monkeypatch, tmp_path,
):
    disk = tmp_path / "disk"
    disk.mkdir()
    _readme(disk, "V4.3.7", family="hpmlink")
    firmware_root = tmp_path / "firmware"
    firmware_root.mkdir()
    (firmware_root / "MicroLink_V4.3.8.uf2").write_bytes(b"micro-uf2")
    hpmlink = firmware_root / "HPMLink_V4.3.7.uf2"
    hpmlink.write_bytes(b"hpm-uf2")
    monkeypatch.setattr(fc, "_probe_disk", lambda: str(disk))

    result = fc.check_probe_firmware(None, firmware_root)

    assert result.status == "ok"
    assert result.recommended_uf2 is not None
    assert result.recommended_uf2.name == hpmlink.name
    assert [item.name for item in result.all_uf2s] == [hpmlink.name]


def test_firmware_check_includes_newer_local_release_candidate(
    monkeypatch, tmp_path,
):
    disk = tmp_path / "disk"
    disk.mkdir()
    _readme(disk, "V4.3.7", family="hpmlink")
    local = tmp_path / "HPMLink_V4.3.8.uf2"
    local.write_bytes(b"candidate")
    remote = fc.FirmwareInfo(
        "HPMLink_V4.3.7.uf2",
        fc.Version(4, 3, 7),
        "V4",
        Path("HPMLink_V4.3.7.uf2"),
        family="hpmlink",
        download_url="https://github.example/HPMLink_V4.3.7.uf2",
    )
    monkeypatch.setattr(fc, "_probe_disk", lambda: str(disk))
    monkeypatch.setattr(fc, "_remote_firmwares", lambda: [remote])

    result = fc.check_probe_firmware(None, tmp_path)

    assert result.status == "upgrade_required"
    assert result.recommended_uf2 is not None
    assert result.recommended_uf2.path == local


def test_materialize_firmware_falls_back_from_github_to_gitee(monkeypatch):
    github = fc.FirmwareInfo(
        "MicroLink_V3.3.7.uf2",
        fc.Version(3, 3, 7),
        "V3",
        Path("MicroLink_V3.3.7.uf2"),
        download_url="https://github.example/MicroLink_V3.3.7.uf2",
        download_source="github",
    )
    gitee = fc.FirmwareInfo(
        github.name,
        github.version,
        github.model,
        github.path,
        download_url="https://gitee.example/MicroLink_V3.3.7.uf2",
        download_source="gitee",
    )
    monkeypatch.setattr(
        fc,
        "_remote_firmware_from_source",
        lambda model, source, **kwargs: gitee,
    )
    requested = []

    def open_download(request, timeout):
        requested.append(request.full_url)
        if "github" in request.full_url:
            raise URLError("github unavailable")
        return io.BytesIO(b"gitee-uf2")

    monkeypatch.setattr(fc, "urlopen", open_download)

    path, temporary, source = fc._materialize_firmware(github)
    try:
        assert temporary is True
        assert source == "gitee"
        assert path.read_bytes() == b"gitee-uf2"
        assert requested == [github.download_url, gitee.download_url]
    finally:
        path.unlink(missing_ok=True)


def test_materialize_firmware_does_not_query_gitee_when_github_succeeds(monkeypatch):
    github = fc.FirmwareInfo(
        "MicroLink_V3.3.7.uf2",
        fc.Version(3, 3, 7),
        "V3",
        Path("MicroLink_V3.3.7.uf2"),
        download_url="https://github.example/MicroLink_V3.3.7.uf2",
        download_source="github",
    )
    monkeypatch.setattr(
        fc,
        "_remote_firmware_from_source",
        lambda model, source, **kwargs: (_ for _ in ()).throw(AssertionError("Gitee must stay lazy")),
    )
    monkeypatch.setattr(fc, "urlopen", lambda request, timeout: io.BytesIO(b"github-uf2"))

    path, temporary, source = fc._materialize_firmware(github)
    try:
        assert temporary is True
        assert source == "github"
        assert path.read_bytes() == b"github-uf2"
    finally:
        path.unlink(missing_ok=True)


def test_manual_upgrade_result_includes_latest_download_details(monkeypatch, tmp_path):
    disk = tmp_path / "disk"
    disk.mkdir()
    _readme(disk, "V3.3.6")
    firmware_root = tmp_path / "firmware"
    firmware_root.mkdir()
    candidate = fc.FirmwareInfo(
        "MicroLink_V3.3.7.uf2",
        fc.Version(3, 3, 7),
        "V3",
        Path("MicroLink_V3.3.7.uf2"),
        download_url="https://github.example/MicroLink_V3.3.7.uf2",
        download_source="github",
    )
    monkeypatch.setattr(fc, "_probe_disk", lambda: str(disk))
    monkeypatch.setattr(fc, "_remote_firmware", lambda model, **kwargs: candidate)

    result = fc.upgrade_probe_firmware(object(), firmware_root, confirm=True)

    assert result == {
        "status": "manual_required",
        "message": "未连接调试会话，请按住升级键进入 Bootloader 后复制该 UF2 文件",
        "current_version": "V3.3.6",
        "latest_version": "V3.3.7",
        "firmware": "MicroLink_V3.3.7.uf2",
        "model": "V3",
        "family": "microlink",
        "download_available": True,
    }


def test_firmware_download_endpoint_returns_binary_and_source(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from mklink.remote.api import create_app

    firmware = tmp_path / "MicroLink_V3.3.7.uf2"
    firmware.write_bytes(b"release-uf2")
    candidate = fc.FirmwareInfo(
        firmware.name,
        fc.Version(3, 3, 7),
        "V3",
        firmware,
    )
    monkeypatch.setattr(fc, "latest_firmware", lambda model, root, **kwargs: candidate)
    monkeypatch.setattr(fc, "_materialize_firmware", lambda info: (firmware, False, "gitee"))
    app = create_app(auth_token=None, project_root=str(tmp_path))

    with TestClient(app) as client:
        response = client.get("/api/probe/firmware-download?model=V3")

    assert response.status_code == 200
    assert response.content == b"release-uf2"
    assert response.headers["x-mklink-firmware-name"] == firmware.name
    assert response.headers["x-mklink-firmware-version"] == "V3.3.7"
    assert response.headers["x-mklink-firmware-source"] == "gitee"
    assert response.headers["x-mklink-firmware-family"] == "microlink"


def test_firmware_download_endpoint_selects_hpmlink_family(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from mklink.remote.api import create_app

    firmware = tmp_path / "HPMLink_V4.3.7.uf2"
    firmware.write_bytes(b"hpm-release-uf2")
    candidate = fc.FirmwareInfo(
        firmware.name,
        fc.Version(4, 3, 7),
        "V4",
        firmware,
        family="hpmlink",
    )
    requested = []

    def latest(model, root, *, family="microlink"):
        requested.append((model, family))
        return candidate

    monkeypatch.setattr(fc, "latest_firmware", latest)
    monkeypatch.setattr(fc, "_materialize_firmware", lambda info: (firmware, False, "local"))
    app = create_app(auth_token=None, project_root=str(tmp_path))

    with TestClient(app) as client:
        response = client.get(
            "/api/probe/firmware-download?model=V4&family=hpmlink"
        )

    assert response.status_code == 200
    assert response.content == b"hpm-release-uf2"
    assert requested == [("V4", "hpmlink")]
    assert response.headers["x-mklink-firmware-name"] == firmware.name
    assert response.headers["x-mklink-firmware-family"] == "hpmlink"


def test_firmware_download_endpoint_rejects_hpmlink_v3(tmp_path):
    from fastapi.testclient import TestClient
    from mklink.remote.api import create_app

    app = create_app(auth_token=None, project_root=str(tmp_path))
    with TestClient(app) as client:
        response = client.get(
            "/api/probe/firmware-download?model=V3&family=hpmlink"
        )

    assert response.status_code == 400
    assert response.json()["detail"] == "HPMLink firmware is only available for V4"
