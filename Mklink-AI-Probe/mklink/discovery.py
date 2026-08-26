"""
MKLink Serial Bridge — COM 口发现和磁盘管理。

依赖: pyserial
内部依赖: mklink._types
"""

from __future__ import annotations

import os
import serial
from serial.tools import list_ports

from mklink._types import (
    DEFAULT_BAUDRATE,
    KNOWN_MKLINK_VID_PIDS,
    MKLINK_IDENTITY_COMMAND,
    MKLINK_IDENTITY_TOKEN,
    PROMPT,
)
from mklink.usb_interfaces import (
    MKLINK_COMMAND_INTERFACE,
    is_mklink_usb_port,
    usb_interface_number,
)

# MICROKEEN 磁盘名称
_MICROKEEN_DISK_NAME = "MICROKEEN"
_FLM_DIR_NAME = "FLM"
_PROBE_SYNC_TIMEOUT = 0.15
_PROBE_IDENTITY_TIMEOUT = 0.35


def _normalize_flm_name(flm_name: str) -> str:
    """Return a plain FLM filename from a device path or filename."""
    flm_name = flm_name.replace("\\", "/").rstrip("/")
    flm_name = os.path.basename(flm_name)
    if flm_name and not flm_name.upper().endswith(".FLM"):
        flm_name = flm_name + ".FLM"
    return flm_name


def _is_mklink_identity_response(response: bytes) -> bool:
    token = MKLINK_IDENTITY_TOKEN.encode("ascii")
    return b">>>" in response and any(
        line.strip() == token for line in response.splitlines()
    )


def _probe_port(port_device: str) -> bool:
    """快速确认端口是否为 MKLink CMD 接口。"""
    ser = None
    try:
        ser = serial.Serial(
            port_device,
            DEFAULT_BAUDRATE,
            timeout=_PROBE_SYNC_TIMEOUT,
        )
        ser.reset_input_buffer()
        ser.reset_output_buffer()

        # 主机接收缓存重置不会清除探针 REPL 中尚未换行的输入。
        # 先结束残留行，避免身份命令被拼接到半条旧命令后面。
        ser.write(b"\n")
        ser.flush()
        sync_response = ser.read_until(PROMPT.encode("ascii"), 1024)
        if b"hello microkeen" in sync_response.lower():
            return True

        # 只看到通用的提示符不足以识别 CMD 口，目标板 UART 也可能
        # 输出相同文本；必须执行唯一、无副作用的身份命令。
        ser.reset_input_buffer()
        ser.timeout = _PROBE_IDENTITY_TIMEOUT
        ser.write((MKLINK_IDENTITY_COMMAND + "\n").encode("ascii"))
        ser.flush()
        response = ser.read_until(PROMPT.encode("ascii"), 1024)
        return _is_mklink_identity_response(response)
    except (serial.SerialException, OSError):
        return False
    finally:
        if ser and ser.is_open:
            try:
                ser.close()
            except Exception:
                pass


def find_mklink_cdc_port(
    serial_number: object = None,
    *,
    exclude_ports: set[str] | None = None,
) -> str | None:
    """自动扫描并识别 MicroLink 的 USB CDC 虚拟串口。

    新版 V2/V3/V4 固件固定使用 MI_04 作为命令接口，因此有完整 USB
    接口元数据时直接返回该端口。旧固件或元数据缺失时仍逐端口执行
    唯一身份命令，不并发探测。
    """
    excluded = {str(port).strip().casefold() for port in (exclude_ports or set())}
    ports = [
        port_info
        for port_info in list_ports.comports()
        if str(port_info.device).strip().casefold() not in excluded
    ]
    requested_serial = str(serial_number or "").strip().casefold()
    if requested_serial:
        matching_ports = [
            port_info for port_info in ports
            if str(getattr(port_info, "serial_number", "") or "").strip().casefold()
            == requested_serial
        ]
        if matching_ports:
            ports = matching_ports

    command_ports = [
        port_info for port_info in ports
        if is_mklink_usb_port(port_info)
        and usb_interface_number(port_info) == MKLINK_COMMAND_INTERFACE
    ]
    if command_ports:
        return command_ports[0].device

    # 单轮扫描，每端口执行残留行清理和身份确认
    # Probe physical USB serial ports first. Bluetooth RFCOMM opens can block
    # for tens of seconds and cannot be an MKLink CDC interface.
    probe_candidates = [
        port_info for port_info in ports
        if not str(getattr(port_info, "hwid", "") or "").upper().startswith("BTHENUM")
    ]
    def probe_priority(port_info: object) -> int:
        mfr = str(getattr(port_info, "manufacturer", "") or "").lower()
        if any(name in mfr for name in ("microkeen", "microlink", "mklink")):
            return 0
        if (
            getattr(port_info, "vid", None),
            getattr(port_info, "pid", None),
        ) in KNOWN_MKLINK_VID_PIDS:
            return 0
        if (
            getattr(port_info, "vid", None) is not None
            or str(getattr(port_info, "hwid", "") or "").upper().startswith("USB")
        ):
            return 1
        return 2

    probe_candidates.sort(key=probe_priority)

    for port_info in probe_candidates:
        if _probe_port(port_info.device):
            return port_info.device

    return None


def list_available_ports() -> list[dict]:
    """列出所有可用的串行端口。"""
    return [
        {
            "device": p.device,
            "description": p.description,
            "manufacturer": p.manufacturer or "",
            "vid": p.vid,
            "pid": p.pid,
        }
        for p in list_ports.comports()
    ]


def _windows_volume_label(root: str) -> str | None:
    """Read a Windows volume label without spawning a console process."""
    if os.name != "nt":
        return None
    try:
        import ctypes

        label = ctypes.create_unicode_buffer(261)
        available = ctypes.windll.kernel32.GetVolumeInformationW(
            root, label, len(label), None, None, None, None, 0
        )
        return label.value if available else None
    except (AttributeError, OSError, ValueError):
        return None


def find_microkeen_disk() -> str | None:
    """查找 MICROKEEN 磁盘路径。

    在 Windows 上查找名为 [MICROKEEN] 的可移动磁盘。
    返回磁盘根路径，如 'D:\\'，未找到返回 None。
    """
    if os.name != "nt":
        return None

    # Service and scheduled-task sessions can enumerate removable volumes
    # differently from an interactive shell.  An operator may provide a
    # concrete root, but it is accepted only after the same MICROKEEN label
    # verification used by automatic discovery.
    configured_root = os.environ.get("MKLINK_MICROKEEN_DISK", "").strip()
    if configured_root:
        root = configured_root.rstrip("\\/") + "\\"
        try:
            if os.path.isdir(root) and (
                (_windows_volume_label(root) or "").casefold()
                == _MICROKEEN_DISK_NAME.casefold()
            ):
                return root
        except Exception:
            pass
        return None

    # 尝试通过 drivedddata 注册表查找
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SYSTEM\\MountedDevices") as key:
            i = 0
            while True:
                try:
                    name, value, _ = winreg.EnumValue(key, i)
                    i += 1
                    # 检查名称是否包含 MICROKEEN
                    if isinstance(name, str) and "microkeen" in name.lower():
                        # 从注册表值提取盘符（如 \\?\Volume{...}\ -> D:）
                        if "\\??\\" in value:
                            drive_letter = value.split("\\??\\")[1].split(":")[0]
                            return f"{drive_letter}:\\"
                except OSError:
                    break
    except Exception:
        pass

    # 后备方案：检查常见盘符
    import string
    for letter in string.ascii_uppercase:
        path = f"{letter}:\\"
        try:
            if os.path.exists(path):
                if (_windows_volume_label(path) or "").casefold() == _MICROKEEN_DISK_NAME.casefold():
                    return path
        except Exception:
            continue

    return None


def get_microkeen_flm_path() -> str | None:
    """获取 MICROKEEN 磁盘的 FLM 目录路径。

    返回 FLM 目录的完整路径，如 'D:\\FLM\\'，未找到磁盘返回 None。
    """
    disk = find_microkeen_disk()
    if disk is None:
        return None
    flm_path = os.path.join(disk, _FLM_DIR_NAME)
    if os.path.isdir(flm_path):
        return flm_path
    return None


def check_flm_on_microkeen(flm_name: str) -> tuple[bool, str | None]:
    """检查指定 FLM 文件是否存在于 MICROKEEN 磁盘的 FLM 目录中。

    Args:
        flm_name: FLM 文件名（如 'N32G43x.FLM' 或 'N32G43x'）

    Returns:
        (exists, full_path): 文件是否存在，以及完整路径（如果存在）
    """
    flm_name = _normalize_flm_name(flm_name)
    if not flm_name:
        return False, None

    flm_dir = get_microkeen_flm_path()
    if flm_dir is None:
        return False, None

    flm_path = os.path.join(flm_dir, flm_name)
    if os.path.isfile(flm_path):
        return True, flm_path
    return False, None


def _get_all_user_profile_dirs() -> list[str]:
    """获取所有可能的用户目录路径。

    Windows 用户目录可能在不同盘符（如 C:, D:），
    且 USERPROFILE 环境变量可能与实际用户目录不同。
    """
    dirs = []

    # 从环境变量获取
    userprofile = os.environ.get("USERPROFILE", "")
    if userprofile:
        dirs.append(userprofile)

    # 尝试从注册表获取真实用户目录（适用于用户目录在 D: 盘的情况）
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders"
        )
        value, _ = winreg.QueryValueEx(key, "Local AppData")
        dirs.append(value)
        winreg.CloseKey(key)
    except Exception:
        pass

    # 尝试获取 USERPROFILE 的所有可能盘符位置
    if userprofile:
        # 从 C: 到 Z: 遍历可能的用户目录
        import string
        userdrive = os.path.splitdrive(userprofile)[0]  # 如 "C:"
        username = os.path.split(userprofile)[1]  # 如 "Tony"
        for letter in string.ascii_uppercase:
            potential = f"{letter}:\\Users\\{username}"
            if potential != userprofile and os.path.isdir(potential):
                dirs.append(potential)

    # 去重
    seen = set()
    result = []
    for d in dirs:
        norm = os.path.normpath(d).lower()
        if norm not in seen:
            seen.add(norm)
            result.append(d)

    return result


def resolve_keil_flm_path(flm_name: str) -> str | None:
    """从 Keil 安装目录解析 FLM 文件的完整路径。

    在以下位置查找 FLM 文件：
    - Keil 安装目录: C:\\Keil_v5\\ARM\\Flash\\, D:\\Keil_v5\\ARM\\Flash\\
    - 用户目录 AppData\\Local\\Arm\\Packs\\...\\Flash\\
      （支持用户目录在不同盘符的情况）

    Args:
        flm_name: FLM 文件名（如 'N32G43x.FLM' 或 'N32G43x'）

    Returns:
        完整路径，如果未找到返回 None
    """
    flm_name = _normalize_flm_name(flm_name)
    if not flm_name:
        return None

    keil_paths = [
        r"C:\Keil_v5\ARM\Flash",
        r"C:\Keil_v5\ARM\Pack\Flash",
        r"D:\Keil_v5\ARM\Flash",
        r"D:\Keil_v5\ARM\Pack\Flash",
    ]

    for base_dir in keil_paths:
        if not os.path.isdir(base_dir):
            continue
        flm_path = os.path.join(base_dir, flm_name)
        if os.path.isfile(flm_path):
            return flm_path

    # 搜索用户目录的 Arm Packs
    user_dirs = _get_all_user_profile_dirs()

    for userprofile in user_dirs:
        packs_paths = [
            os.path.join(userprofile, "AppData", "Local", "Arm", "Packs"),
            os.path.join(userprofile, "AppData", "Roaming", "Arm", "Packs"),
        ]

        for packs_dir in packs_paths:
            if not os.path.isdir(packs_dir):
                continue
            # 搜索所有子目录中的 Flash 子目录
            for root, dirs, files in os.walk(packs_dir):
                if os.path.basename(root) == "Flash" and flm_name in files:
                    return os.path.join(root, flm_name)

    return None


def copy_flm_to_microkeen(flm_name: str) -> tuple[bool, str | None]:
    """将 FLM 文件拷贝到 MICROKEEN 磁盘的 FLM 目录。

    Args:
        flm_name: FLM 文件名（如 'N32G43x.FLM' 或 'N32G43x'）

    Returns:
        (success, dest_path): 是否成功，以及目标路径
    """
    import shutil

    flm_name_with_ext = _normalize_flm_name(flm_name)
    if not flm_name_with_ext:
        print("[FAIL] 未找到 FLM 配置")
        return False, None

    # 获取 MICROKEEN FLM 目录
    flm_dir = get_microkeen_flm_path()
    if flm_dir is None:
        print("[FAIL] 未找到 MICROKEEN 磁盘")
        return False, None

    # 解析源 FLM 路径（自动处理无后缀情况）
    src_path = resolve_keil_flm_path(flm_name_with_ext)
    if src_path is None:
        print(f"[FAIL] 未在 Keil 安装目录中找到 '{flm_name_with_ext}'")
        return False, None

    # 目标路径（设备上使用带扩展名的文件名）
    dest_path = os.path.join(flm_dir, flm_name_with_ext)

    # 检查目标文件是否存在
    if os.path.isfile(dest_path):
        src_size = os.path.getsize(src_path)
        dest_size = os.path.getsize(dest_path)
        if src_size == dest_size:
            print(f"[OK] FLM 已存在且大小相同，跳过拷贝: {dest_path} ({src_size} bytes)")
            return True, dest_path
        else:
            print(f"[INFO] FLM 文件大小不同，将重新拷贝: {dest_path} (源:{src_size} vs 目标:{dest_size})")

    try:
        shutil.copy2(src_path, dest_path)
        print(f"[OK] 已拷贝 FLM: {src_path} -> {dest_path}")
        return True, dest_path
    except Exception as e:
        print(f"[FAIL] 拷贝失败: {e}")
        return False, None
