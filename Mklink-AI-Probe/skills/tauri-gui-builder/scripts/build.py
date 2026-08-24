#!/usr/bin/env python3
"""Build Mklink AI Probe Tauri desktop GUI exe.

Usage:
    python build.py              # Build exe only (no bundle)
    python build.py --bundle     # Standard NSIS bundle with sidecar
    python build.py --check      # Check prerequisites only
    python build.py --clean      # Clean build artifacts

The built exe is at: gui/src-tauri/target/release/mklink-ai-probe.exe
"""

import subprocess
import sys
import os
import shutil
import argparse
import platform
import json
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

IS_WINDOWS = platform.system() == "Windows"


def find_project_root():
    """Find mklink-ai-probe project root by walking up from CWD."""
    d = Path.cwd()
    for _ in range(10):
        if (d / "mklink" / "__main__.py").exists() and (d / "gui" / "src-tauri").is_dir():
            return d
        parent = d.parent
        if parent == d:
            break
        d = parent
    print("[FAIL] Cannot find mklink-ai-probe project root. Run from the project directory or use --project-dir.")
    sys.exit(1)

IS_WINDOWS = platform.system() == "Windows"


def run(cmd, cwd=None, check=True, env=None):
    """Run a command and stream output."""
    print(f"\n> {' '.join(cmd) if isinstance(cmd, list) else cmd}")
    use_shell = IS_WINDOWS and isinstance(cmd, list) and cmd[0] in ("npm", "npx", "cargo", "rustc")
    result = subprocess.run(
        cmd, cwd=cwd, check=False,
        text=True, shell=use_shell, env=env,
    )
    if check and result.returncode != 0:
        print(f"[FAIL] Command exited with code {result.returncode}")
        sys.exit(result.returncode)
    return result.returncode


def check_rust():
    """Check Rust toolchain."""
    rc = run(["rustc", "--version"], check=False)
    if rc != 0:
        print("[FAIL] Rust not found. Install with:")
        print("  Invoke-WebRequest -Uri https://win.rustup.rs/x86_64 -OutFile $env:TEMP\\rustup-init.exe")
        print("  & $env:TEMP\\rustup-init.exe -y --default-toolchain stable")
        return False
    return True


def check_node():
    """Check Node.js and install frontend deps."""
    rc = run(["node", "--version"], check=False)
    if rc != 0:
        print("[FAIL] Node.js not found. Install with: winget install OpenJS.NodeJS.LTS")
        return False
    node_modules = GUI_DIR / "node_modules"
    if not node_modules.exists():
        print("[INFO] Installing Node.js dependencies...")
        run(["npm", "install"], cwd=str(GUI_DIR))
    return True


def check_python_deps():
    """Check Python mklink package with [gui] extras."""
    rc = run(
        [sys.executable, "-c", "import mklink, fastapi, uvicorn; print('Python deps OK')"],
        check=False,
    )
    if rc != 0:
        print("[INFO] Installing Python dependencies...")
        run([sys.executable, "-m", "pip", "install", "-e", ".[gui]"], cwd=str(SKILL_DIR))
    return True


def build_builtin_pack_bundle(config_path, roots, output):
    """Load the colocated bundle builder without making scripts a package."""
    import importlib.util

    builder_path = Path(__file__).resolve().with_name("builtin_packs.py")
    spec = importlib.util.spec_from_file_location("mklink_builtin_pack_builder", builder_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load builtin Pack builder")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.build_bundle(Path(config_path), [Path(root) for root in roots], Path(output))


def build_daplinkutility_flm_bundle(executable, output):
    """Load the pinned Qt resource extractor without making scripts a package."""
    import importlib.util

    builder_path = Path(__file__).resolve().with_name("daplinkutility_resources.py")
    spec = importlib.util.spec_from_file_location("mklink_daplinkutility_resources", builder_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load DAPLinkUtility FLM builder")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.build_bundle(Path(executable), Path(output))


def builtin_pack_roots():
    value = os.environ.get("MKLINK_BUILTIN_PACK_ROOTS", "")
    roots = [Path(item).resolve() for item in value.split(os.pathsep) if item.strip()]
    missing = [root for root in roots if not root.is_dir()]
    if missing:
        raise RuntimeError("builtin Pack root does not exist: {}".format(missing[0]))
    return roots


def daplinkutility_executable():
    value = os.environ.get("MKLINK_DAPLINKUTILITY_EXE", "").strip()
    if not value:
        return None
    executable = Path(value).resolve()
    if not executable.is_file():
        raise RuntimeError("DAPLinkUtility executable does not exist: {}".format(executable))
    return executable


def build_sidecar(force=False):
    """Build Python sidecar exe with PyInstaller."""
    sidecar_dir = TAURI_DIR / "binaries"
    sidecar_exe = sidecar_dir / "mklink-sidecar-x86_64-pc-windows-msvc.exe"
    dist_dir = SKILL_DIR / "dist"
    built = dist_dir / "mklink-sidecar.exe"

    if force:
        sidecar_exe.unlink(missing_ok=True)
        built.unlink(missing_ok=True)

    if sidecar_exe.exists():
        print(f"[OK] Sidecar already exists: {sidecar_exe}")
        return True

    print("[INFO] Building Python sidecar with PyInstaller...")
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        run([sys.executable, "-m", "pip", "install", "pyinstaller"])

    with TemporaryDirectory(prefix="mklink-builtin-packs-") as temporary:
        roots = builtin_pack_roots()
        builtin_args = []
        if roots:
            bundle_dir = Path(temporary) / "builtin_packs"
            manifest = build_builtin_pack_bundle(
                SKILL_DIR / "skills" / "tauri-gui-builder" / "builtin-packs.json",
                roots,
                bundle_dir,
            )
            print("[OK] Built builtin Pack bundle: {} targets".format(manifest["target_count"]))
            builtin_args = [
                "--add-data",
                "{}{}mklink/builtin_packs".format(bundle_dir, os.pathsep),
            ]
        else:
            print("[WARN] MKLINK_BUILTIN_PACK_ROOTS is not set; builtin Pack bundle omitted")
        daplink_exe = daplinkutility_executable()
        if daplink_exe is not None:
            try:
                import pefile  # noqa: F401
            except ImportError:
                run([sys.executable, "-m", "pip", "install", "pefile"])
            flm_dir = Path(temporary) / "builtin_flm"
            flm_manifest = build_daplinkutility_flm_bundle(daplink_exe, flm_dir)
            print(
                "[OK] Built DAPLinkUtility FLM bundle: {} targets, {} blobs".format(
                    flm_manifest["target_count"], flm_manifest["blob_count"]
                )
            )
            builtin_args.extend([
                "--add-data",
                "{}{}mklink/builtin_flm".format(flm_dir, os.pathsep),
            ])
        else:
            print("[WARN] MKLINK_DAPLINKUTILITY_EXE is not set; builtin FLM bundle omitted")
        run([
            sys.executable, "-m", "PyInstaller",
            "--noconfirm", "--clean", "--onefile", "--name", "mklink-sidecar",
            "--collect-all", "mklink",
            "--collect-all", "elftools",
            "--collect-all", "pyocd",
            "--copy-metadata", "pyocd",
            "--collect-all", "cmsis_pack_manager",
            "--collect-all", "hid",
        ] + builtin_args + [
            "-p", str(SKILL_DIR),
            str(SKILL_DIR / "mklink" / "__main__.py"),
        ], cwd=str(SKILL_DIR))

    sidecar_dir.mkdir(parents=True, exist_ok=True)
    if built.exists():
        shutil.copy2(str(built), str(sidecar_exe))
        print(f"[OK] Sidecar copied to {sidecar_exe}")
        return True
    print("[FAIL] PyInstaller did not produce mklink-sidecar.exe")
    return False


def stage_stcp_library():
    """Stage the validated in-process STCP bridge for the NSIS bundle."""
    source = (
        SKILL_DIR / "native" / "stcp_bridge" / "build" / "mklink-stcp.dll"
    ).resolve()
    try:
        header = source.read_bytes()[:2]
    except OSError:
        header = b""
    if not source.is_file() or header != b"MZ":
        raise RuntimeError(
            "a valid native/stcp_bridge/build/mklink-stcp.dll is required"
        )

    destination = TAURI_DIR / "resources" / "mklink-stcp.dll"
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    return destination


def add_stcp_bundle_resource(bundle):
    """Map the staged bridge beside the installed sidecar on Windows."""
    resources = bundle.get("resources", [])
    if isinstance(resources, list):
        resources = {name: name for name in resources}
    elif isinstance(resources, dict):
        resources = dict(resources)
    else:
        raise RuntimeError("bundle resources must be a list or mapping")
    resources["resources/mklink-stcp.dll"] = "mklink-stcp.dll"
    bundle["resources"] = resources


@contextmanager
def temporary_external_bin(config_path):
    """Add the release sidecar config and restore the exact original bytes."""
    config_path = Path(config_path)
    original = config_path.read_bytes()
    data = json.loads(original.decode("utf-8"))
    bundle = data.setdefault("bundle", {})
    bundle["externalBin"] = [
        "binaries/mklink-sidecar"
    ]
    add_stcp_bundle_resource(bundle)
    config_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    try:
        yield
    finally:
        config_path.write_bytes(original)


@contextmanager
def temporary_bundle_config(config_path):
    """Apply the standard NSIS-only sidecar bundle settings."""
    config_path = Path(config_path)
    original = config_path.read_bytes()
    data = json.loads(original.decode("utf-8"))
    bundle = data.setdefault("bundle", {})
    bundle["targets"] = ["nsis"]
    # LZMA's solid dictionary can fail to mmap on Windows machines with a
    # constrained system drive; zlib keeps the standard NSIS bundle reliable.
    bundle.setdefault("windows", {}).setdefault("nsis", {})["compression"] = "zlib"
    bundle["externalBin"] = [
        "binaries/mklink-sidecar"
    ]
    add_stcp_bundle_resource(bundle)
    config_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    try:
        yield
    finally:
        config_path.write_bytes(original)


def load_updater_private_key(env=None, home=None):
    """Load the updater key without adding it to the process environment."""
    values = os.environ if env is None else env
    configured = values.get("MKLINK_TAURI_UPDATER_KEY", "").strip()
    if configured:
        configured_path = Path(configured).expanduser()
        try:
            is_file = configured_path.is_file()
        except OSError:
            is_file = False
        if is_file:
            configured = configured_path.read_text(encoding="utf-8").strip()
        if configured:
            return configured

    key_path = (
        (Path.home() if home is None else Path(home))
        / ".config"
        / "mklink-ai-probe"
        / "updater.key"
    )
    if not key_path.is_file():
        raise RuntimeError(
            "updater private key is required; set MKLINK_TAURI_UPDATER_KEY "
            "or create ~/.config/mklink-ai-probe/updater.key"
        )
    key = key_path.read_text(encoding="utf-8").strip()
    if not key:
        raise RuntimeError("updater private key is empty")
    return key


def collect_signed_bundle_outputs(bundle_dir):
    """Return the standard NSIS installer and its Tauri updater signature."""
    nsis_dir = Path(bundle_dir) / "nsis"
    setups = sorted(nsis_dir.glob("*.exe"))
    if not setups:
        raise RuntimeError("standard NSIS setup executable was not produced")
    if len(setups) != 1:
        raise RuntimeError("expected exactly one standard NSIS setup executable")
    signature = Path(f"{setups[0]}.sig")
    if not signature.is_file():
        raise RuntimeError("Tauri updater signature was not produced")
    return {
        "setup": setups[0],
        "updater_signature": signature,
    }


def build_release_bundle():
    """Build a bundle from the current source with a temporary sidecar config."""
    signing_key = load_updater_private_key()
    if not build_sidecar(force=True):
        raise SystemExit(1)
    staged_stcp = stage_stcp_library()
    try:
        bundle_dir = TAURI_DIR / "target" / "release" / "bundle"
        if bundle_dir.exists():
            try:
                shutil.rmtree(bundle_dir)
            except OSError as exc:
                raise RuntimeError(
                    f"failed to remove stale bundle outputs: {bundle_dir}"
                ) from exc
            if bundle_dir.exists():
                raise RuntimeError(
                    f"stale bundle outputs still exist: {bundle_dir}"
                )
        with temporary_bundle_config(TAURI_DIR / "tauri.conf.json"):
            return build_tauri(bundle=True, signing_key=signing_key)
    finally:
        staged_stcp.unlink(missing_ok=True)


def build_tauri(bundle=False, signing_key=None):
    """Build Tauri application."""
    cmd = ["npx", "tauri", "build"]
    if not bundle:
        cmd.append("--no-bundle")

    env = os.environ.copy()
    env["VITE_MKLINK_API"] = "http://127.0.0.1:8765"
    if bundle:
        if not signing_key:
            raise RuntimeError("updater private key is required for a release bundle")
        env["TAURI_SIGNING_PRIVATE_KEY"] = signing_key
        env["TAURI_SIGNING_PRIVATE_KEY_PASSWORD"] = os.environ.get(
            "MKLINK_TAURI_UPDATER_KEY_PASSWORD", ""
        )
    run(cmd, cwd=str(GUI_DIR), env=env)

    exe = TAURI_DIR / "target" / "release" / "mklink-ai-probe.exe"
    if exe.exists():
        size_mb = exe.stat().st_size / (1024 * 1024)
        print(f"\n[OK] Built: {exe} ({size_mb:.1f} MB)")
    else:
        print(f"\n[FAIL] Expected exe not found: {exe}")
        sys.exit(1)

    if bundle:
        outputs = collect_signed_bundle_outputs(
            TAURI_DIR / "target" / "release" / "bundle"
        )
        for label, path in outputs.items():
            size_mb = path.stat().st_size / (1024 * 1024)
            print(f"[OK] {label}: {path} ({size_mb:.1f} MB)")
        return outputs
    return {}


def clean():
    """Remove build artifacts."""
    targets = [
        TAURI_DIR / "target",
        GUI_DIR / "dist",
        SKILL_DIR / "dist",
        SKILL_DIR / "build",
    ]
    for t in targets:
        if t.exists():
            print(f"  Removing {t}")
            shutil.rmtree(t, ignore_errors=True)
    print("[OK] Clean complete")


def main():
    global SKILL_DIR, GUI_DIR, TAURI_DIR

    parser = argparse.ArgumentParser(description="Build Mklink AI Probe Tauri GUI")
    parser.add_argument("--bundle", action="store_true", help="Standard NSIS bundle with sidecar")
    parser.add_argument("--check", action="store_true", help="Check prerequisites only")
    parser.add_argument("--clean", action="store_true", help="Remove build artifacts")
    parser.add_argument("--project-dir", type=str, default=None, help="mklink-ai-probe project root directory")
    args = parser.parse_args()

    # Resolve project root
    if args.project_dir:
        SKILL_DIR = Path(args.project_dir).resolve()
    else:
        SKILL_DIR = find_project_root()
    GUI_DIR = SKILL_DIR / "gui"
    TAURI_DIR = GUI_DIR / "src-tauri"
    print(f"[INFO] Project root: {SKILL_DIR}")

    # Ensure PATH includes cargo bin
    cargo_bin = Path.home() / ".cargo" / "bin"
    env_path = os.environ.get("PATH", "")
    if str(cargo_bin) not in env_path:
        os.environ["PATH"] = f"{env_path};{cargo_bin}"

    if args.clean:
        clean()
        return

    print("=== Mklink AI Probe Tauri Builder ===\n")

    # Prerequisites
    print("--- Checking prerequisites ---")
    if not check_rust():
        sys.exit(1)
    if not check_node():
        sys.exit(1)
    if not check_python_deps():
        sys.exit(1)

    if args.check:
        print("\n[OK] All prerequisites met")
        return

    # Build
    if args.bundle:
        print("\n--- Building sidecar (PyInstaller) ---")
        build_release_bundle()
        return

    print("\n--- Building Tauri application ---")
    build_tauri(bundle=args.bundle)


if __name__ == "__main__":
    main()
