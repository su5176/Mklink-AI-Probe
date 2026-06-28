"""
MKLink Serial Bridge — 烧录功能模块。

提供芯片擦除、固件烧录、验证等操作。
依赖: pyserial (通过 MKLinkSerialBridge)
"""

from __future__ import annotations

import os
import shutil
import sys
import time
from pathlib import Path

# Windows 终端默认 GBK 编码，设备返回的特殊字符会导致 encode 失败
# 透明替换无法编码的字符，避免 UnicodeEncodeError
if sys.platform == "win32":
    sys.stdout.reconfigure(errors="replace", encoding="utf-8")
    sys.stderr.reconfigure(errors="replace", encoding="utf-8")

from mklink.bridge import MKLinkSerialBridge
from mklink.discovery import find_microkeen_disk
from mklink.profiles import load_mcu_profiles
from mklink.utils import parse_download_progress, parse_load_result
from mklink._types import FLM_LOAD_TIMEOUT

HPM_BOARD_FLASH_CFG = {
    "hpm5e00evk": ("0xfcf90002U", "0x00000005U", "0x00001000U"),
    "hpm6e00evk": ("0xfcf90001U", "0x00000005U", "0x00001000U"),
    "hpm6p00evk": ("0xfcf90002U", "0x00000005U", "0x00001000U"),
    "hpm5300evk": ("0xfcf90002U", "0x00000005U", "0x00001000U"),
    "hpm5301evklite": ("0xfcf90002U", "0x00000005U", "0x00001000U"),
    "hpm6200evk": ("0xfcf90001U", "0x00000005U", "0x00001000U"),
    "hpm6300evk": ("0xfcf90001U", "0x00000005U", "0x00001000U"),
    "hpm6750evk2": ("0xfcf90002U", "0x00000005U", "0x0000000EU"),
    "hpm6750evkmini": ("0xfcf90002U", "0x00000005U", "0x0000000EU"),
    "hpm6800evk": ("0xfcf90001U", "0x00000005U", "0x00001000U"),
}


def parse_hpm_program_result(output: str) -> dict:
    """Parse hpm.program output.

    HPM programming is successful only when the device reports a final program
    marker. A standalone "0" can also come from setup commands such as
    hpm.board(), so it is not enough to prove that the BIN was downloaded.
    """
    progress = parse_download_progress(output)
    lower = output.lower()
    has_failure = (
        "error" in lower
        or "failed" in lower
        or ("open filename" in lower and "fail" in lower)
    )
    has_loaded_success = "loaded successfully" in lower
    has_100_percent = any(p["percent"] == 100 for p in progress)
    return {
        "success": (has_loaded_success or has_100_percent) and not has_failure,
        "progress": progress,
        "loaded_successfully": has_loaded_success,
        "download_100_percent": has_100_percent,
    }


# ---------------------------------------------------------------------------
# 错误类
# ---------------------------------------------------------------------------

class FlashError(Exception):
    """烧录操作错误。"""
    pass


class DeviceNotReadyError(FlashError):
    """设备未连接或未就绪。"""
    pass


class FLMLoadError(FlashError):
    """FLM 加载失败。"""
    pass


class IDCODEError(FlashError):
    """获取芯片 IDCODE 失败。"""
    pass


# ---------------------------------------------------------------------------
# 烧录器类
# ---------------------------------------------------------------------------

class MKLinkFlash:
    """MKLink 烧录器封装，提供高级烧录接口。"""

    def __init__(self, bridge: MKLinkSerialBridge):
        self._bridge = bridge

    @classmethod
    def connect(cls, port: str | None = None, baudrate: int = 115200) -> "MKLinkFlash":
        """连接到 MKLink 设备并返回烧录器实例。

        Args:
            port: COM 端口号，如 "COM6"。None 则自动查找。
        Returns:
            MKLinkFlash 实例
        Raises:
            DeviceNotReadyError: 无法连接设备
        """
        from mklink.discovery import find_mklink_cdc_port

        if port is None:
            port = find_mklink_cdc_port()
            if not port:
                raise DeviceNotReadyError("未找到 MKLink 设备，请检查连接")

        bridge = MKLinkSerialBridge(port, baudrate)
        if not bridge.connect():
            raise DeviceNotReadyError(f"无法连接到 {port}")

        return cls(bridge)

    def close(self):
        """断开连接。"""
        self._bridge.close()

    # ------------------------------------------------------------------
    # 基础操作
    # ------------------------------------------------------------------

    def get_idcode(self, timeout: float = 10.0) -> int:
        """获取芯片 IDCODE。

        Args:
            timeout: 超时时间（秒）
        Returns:
            芯片 IDCODE（整数）
        Raises:
            IDCODEError: 获取失败
        """
        start = time.time()
        while time.time() - start < timeout:
            resp = self._bridge.send_command("cmd.get_idcode()", echo=True)
            # 解析多种格式:
            # - idcode = 0X2BA01477 (hex with prefix)
            # - idcode = 732195447 (decimal)
            # - 732195447 (raw decimal)
            # - 0x2BA01477 (raw hex)
            for line in resp.split("\n"):
                line = line.strip()
                if not line:
                    continue
                # 格式1: idcode = xxx
                if line.startswith("idcode"):
                    parts = line.split("=", 1)
                    if len(parts) == 2:
                        value_str = parts[1].strip()
                    else:
                        continue
                else:
                    # 格式2: 直接是数字
                    value_str = line

                # 尝试解析为十六进制 (0x 或 0X 前缀)
                try:
                    if value_str.startswith(("0x", "0X")):
                        idcode = int(value_str, 16)
                    else:
                        # 尝试解析为十进制
                        idcode = int(value_str, 10)
                    # 验证有效性
                    if idcode != 0 and idcode != 0xFFFFFFFF:
                        return idcode
                except ValueError:
                    continue
            time.sleep(0.5)

        raise IDCODEError(f"获取 IDCODE 失败（超时 {timeout}s）")

    def set_swd_clock(self, clock_hz: int) -> None:
        """设置 SWD 时钟频率。

        Args:
            clock_hz: 时钟频率（Hz），如 10000000 表示 10MHz
        """
        self._bridge.send_command(f"cmd.set_swd_clock({clock_hz})", echo=True)

    def load_flm(self, flm_path: str, flash_base: str, ram_base: str) -> bool:
        """加载 Flash 算法文件。

        Args:
            flm_path: FLM 文件路径（设备端路径，如 /FLM/N32G43x.FLM）
            flash_base: Flash 基地址（如 0x08000000）
            ram_base: RAM 基地址（如 0x20000000）
        Returns:
            True 成功，False 失败
        """
        resp = self._bridge.send_command(
            f'load.flm("{flm_path}",{flash_base},{ram_base})',
            echo=True,
        )
        has_zero = False
        for line in resp.strip().split("\n"):
            line = line.strip()
            if "fail" in line.lower() or "error" in line.lower() or "open failed" in line.lower():
                return False
            if line == "0":
                has_zero = True
        return has_zero

    def erase_chip(self, flash_base: str = "0x08000000") -> bool:
        """擦除芯片 Flash。

        Args:
            flash_base: Flash 基地址
        Returns:
            True 成功，False 失败
        """
        resp = self._bridge.send_command(f"cmd.erase_chip_flash({flash_base})", echo=True)
        # 逐行检查返回 0 表示成功
        for line in resp.strip().split("\n"):
            line = line.strip()
            if line == "0":
                return True
        return False

    def erase_sector(self, addr: str) -> bool:
        """擦除指定扇区。

        Args:
            addr: 扇区地址
        Returns:
            True 成功，False 失败
        """
        resp = self._bridge.send_command(f"cmd.erase_sector_flash({addr})", echo=True)
        return "0" in resp

    # ------------------------------------------------------------------
    # 文件烧录
    # ------------------------------------------------------------------

    def burn_hex(
        self,
        hex_path: str,
        microkeen_filename: str | None = None,
        progress_callback=None,
    ) -> dict:
        """烧录 HEX 文件（自动处理 MICROKEEN 文件拷贝）。

        Args:
            hex_path: 本地 HEX 文件路径
            microkeen_filename: MICROKEEN 磁盘上的文件名，None 则使用 hex_path 的文件名
            progress_callback: 进度回调函数，签名为 callback(percent: int)
        Returns:
            烧录结果 dict: {"success": bool, "progress": list, "time_ms": int}
        Raises:
            FlashError: 烧录失败
        """
        # 1. 拷贝到 MICROKEEN（如果需要）
        filename = self._copy_to_microkeen(hex_path, microkeen_filename)
        if not filename:
            raise FlashError("无法将文件拷贝到 MICROKEEN 磁盘")

        # 2. 发送烧录命令
        start = time.time()
        resp = self._bridge.send_command(
            f'load.hex("{filename}")',
            timeout=FLM_LOAD_TIMEOUT,
            echo=True,
        )

        # 3. 等待完成（烧录输出会分多行返回）
        time.sleep(0.5)

        elapsed_ms = int((time.time() - start) * 1000)

        # 4. 检查返回值（设备端返回 "0" 表示成功）
        load_success = False
        for line in resp.strip().split("\n"):
            line = line.strip()
            if line == "0":
                load_success = True
                break
            if "loaded" in line.lower() and "success" in line.lower():
                load_success = True
                break

        # 5. 解析进度
        progress_list = parse_download_progress(resp)

        # 6. 更新进度回调
        if progress_callback and progress_list:
            last_percent = 0
            for p in progress_list:
                if p["percent"] > last_percent:
                    progress_callback(p["percent"])
                    last_percent = p["percent"]

        # 7. 解析最终结果（优先使用返回值检查，其次使用 parse_load_result）
        if not load_success:
            result = parse_load_result(resp)
            load_success = result.get("success", False)

        return {
            "success": load_success,
            "filename": filename,
            "progress": progress_list,
            "time_ms": elapsed_ms,
            "response": resp,
        }

    def burn_bin(
        self,
        bin_path: str,
        addr: str,
        microkeen_filename: str | None = None,
        progress_callback=None,
    ) -> dict:
        """烧录 BIN 文件。

        Args:
            bin_path: 本地 BIN 文件路径
            addr: 烧录地址（如 0x08000000）
            microkeen_filename: MICROKEEN 磁盘上的文件名
            progress_callback: 进度回调函数
        Returns:
            烧录结果 dict
        """
        filename = self._copy_to_microkeen(bin_path, microkeen_filename)
        if not filename:
            raise FlashError("无法将文件拷贝到 MICROKEEN 磁盘")

        start = time.time()
        resp = self._bridge.send_command(f'load.bin("{filename}",{addr})', echo=True)
        time.sleep(0.5)

        elapsed_ms = int((time.time() - start) * 1000)
        progress_list = parse_download_progress(resp)

        if progress_callback and progress_list:
            last_percent = 0
            for p in progress_list:
                if p["percent"] > last_percent:
                    progress_callback(p["percent"])
                    last_percent = p["percent"]

        result = parse_load_result(resp)
        return {
            "success": result.get("success", False),
            "filename": filename,
            "addr": addr,
            "progress": progress_list,
            "time_ms": elapsed_ms,
            "response": resp,
        }

    def burn_hpm_bin(
        self,
        bin_path: str,
        addr: str,
        board: str | None = None,
        flash_cfg: tuple[str, str, str] | list[str] | None = None,
        microkeen_filename: str | None = None,
        progress_callback=None,
    ) -> dict:
        """Program an HPMicro BIN image with the HPM device-side API."""
        filename = self._copy_to_microkeen(bin_path, microkeen_filename)
        if not filename:
            raise FlashError("无法将文件拷贝到 MICROKEEN 磁盘")

        board_key = board.lower() if board else ""
        commands: list[str] = []
        if board_key:
            commands.append(f'hpm.board("{board_key}")')
        elif flash_cfg:
            cfg = [str(v) for v in flash_cfg]
            if len(cfg) != 3:
                raise FlashError("HPM flash_cfg 需要 3 个参数")
            commands.append(f"hpm.flash_cfg({cfg[0]},{cfg[1]},{cfg[2]})")

        commands.append(f'hpm.program("{filename}",{addr})')

        responses: list[str] = []
        start = time.time()
        for index, cmd in enumerate(commands):
            timeout = FLM_LOAD_TIMEOUT if cmd.startswith("hpm.program") else 10.0
            responses.append(self._bridge.send_command(cmd, timeout=timeout, echo=True))
            if index < len(commands) - 1:
                time.sleep(0.1)
        time.sleep(0.5)

        resp = "\n".join(responses)
        elapsed_ms = int((time.time() - start) * 1000)
        progress_list = parse_download_progress(resp)

        if progress_callback and progress_list:
            last_percent = 0
            for p in progress_list:
                if p["percent"] > last_percent:
                    progress_callback(p["percent"])
                    last_percent = p["percent"]

        result = parse_hpm_program_result(resp)
        return {
            "success": result.get("success", False),
            "filename": filename,
            "addr": addr,
            "board": board_key or None,
            "progress": progress_list,
            "loaded_successfully": result.get("loaded_successfully", False),
            "download_100_percent": result.get("download_100_percent", False),
            "time_ms": elapsed_ms,
            "response": resp,
        }

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    def _copy_to_microkeen(
        self, local_path: str, microkeen_filename: str | None = None
    ) -> str | None:
        """拷贝文件到 MICROKEEN 磁盘（仅当目标文件不存在或时间不同时）。

        Args:
            local_path: 本地文件路径
            microkeen_filename: 指定在 MICROKEEN 上的文件名，None 则用原文件名
        Returns:
            MICROKEEN 上的文件名（不含路径），失败返回 None
        """
        disk = find_microkeen_disk()
        if not disk:
            return None

        src = Path(local_path)
        if not src.exists():
            return None

        filename = microkeen_filename or src.name
        dest = Path(disk) / filename

        # 检查目标文件是否存在且修改时间相同，避免不必要的拷贝
        if dest.exists():
            src_mtime = src.stat().st_mtime
            dest_mtime = dest.stat().st_mtime
            # 比较时间戳，允许 2 秒误差
            if abs(src_mtime - dest_mtime) < 2:
                print(f"[SKIP] {filename} 已存在且时间相同，跳过拷贝")
                return filename

        try:
            shutil.copy2(src, dest)
            print(f"[COPY] {src} -> {dest}")
            return filename  # 返回纯文件名，MKLINK 设备端只需要文件名
        except OSError:
            return None

    def beep(self) -> None:
        """发送提示音。"""
        # 设备端不一定实现蜂鸣器命令，失败时静默跳过，避免污染烧录结果。
        try:
            self._bridge.send_command("cmd.set_beep_on()", timeout=1.0)
            time.sleep(0.1)
            self._bridge.send_command("cmd.set_beep_off()", timeout=1.0)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 便捷函数
# ---------------------------------------------------------------------------

def burn_hex_file(
    hex_path: str | None = None,
    port: str | None = None,
    mcu_key: str | None = None,
    flash_base: str | None = None,
    swd_clock: int | None = None,
    progress_callback=None,
    project_root: str = ".",
) -> dict:
    """一站式烧录固件文件。

    自动完成：连接 → 设置时钟 → 获取 IDCODE → 加载 FLM → 烧录
    load.hex() / load.bin() 内部按扇区边擦边写，无需单独擦除。

    Args:
        hex_path: 本地固件文件路径，None 则自动从 .mklink/project_info.json 读取
        port: COM 端口，None 则自动查找
        mcu_key: MCU 配置键名（如 "n32g435"）
        flash_base: Flash 基地址
        swd_clock: SWD 时钟频率（Hz）
        progress_callback: 进度回调函数
        project_root: 项目根目录（用于查找 .mklink/keil_project.json）
    Returns:
        烧录结果 dict
    Raises:
        FlashError: 如果 hex_path 是 MKLinkSerialBridge 对象（误用 API）
    """
    # 防止常见误用：传入 bridge 对象而非 hex_path 字符串
    if hasattr(hex_path, "_serial") or hasattr(hex_path, "send_command"):
        raise FlashError(
            f"参数错误：第一个参数应该是 HEX 文件路径字符串，"
            f"而不是 bridge 对象。请使用：burn_hex_file('{hex_path}', port='{port or 'COM6'}')"
        )

    from mklink.project_config import load_config, load_keil_project

    config = load_config(project_root) or {}
    project_info = load_keil_project(project_root) or {}

    # 如果未显式传入文件路径，优先读取 project_info.json 里的 bin_path，
    # 其次兼容旧字段 hex_path。
    if hex_path is None:
        if project_info:
            hex_path = project_info.get("bin_path") or project_info.get("hex_path")
            if hex_path:
                print(f"[AUTO] 从 project_info.json 自动获取 firmware_path: {hex_path}")
        if not hex_path:
            raise FlashError("firmware_path 为空且无法从 .mklink/project_info.json 读取")

    if not Path(hex_path).exists():
        raise FlashError(f"HEX 文件不存在: {hex_path}")

    # 检测 .bin 文件，自动路由到 burn_bin
    is_bin_file = hex_path.lower().endswith(".bin")
    if is_bin_file:
        print("[WARN] 检测到 .bin 文件，将使用 burn_bin 模式")

    if flash_base is None:
        flash_base = project_info.get("flash_base") or "0x08000000"
    bin_base = (
        project_info.get("bin_base")
        or project_info.get("download_base")
        or flash_base
    )
    board = project_info.get("board", "")
    is_hpm_project = (
        str(project_info.get("vendor", "")).lower() == "hpmicro"
        or str(board).lower().startswith("hpm")
    )
    hpm_flash_cfg = project_info.get("hpm_flash_cfg")
    if not hpm_flash_cfg and board:
        hpm_flash_cfg = HPM_BOARD_FLASH_CFG.get(str(board).lower())

    if mcu_key is None:
        mcu_key = config.get("mcu_key")
    if not mcu_key:
        if is_hpm_project:
            mcu_key = "custom"
        else:
            raise FlashError("mcu_key 未配置，请先运行 `python -m mklink project-init`")

    flash = MKLinkFlash.connect(port)
    try:
        # 0. 如果未指定 SWD 时钟，从 config.json 读取
        if swd_clock is None:
            swd_clock = config.get("swd_clock", 1000000) if config else 1000000

        print(f"[*] 烧录配置: MCU={mcu_key}, Flash={flash_base}, SWD={swd_clock}Hz")

        # 1. 设置 SWD 时钟
        flash.set_swd_clock(swd_clock)

        # 2. 获取 IDCODE（验证连接）
        idcode = flash.get_idcode()
        print(f"[OK] IDCODE: 0x{idcode:08X}")

        # 3. 加载 FLM。若 profile 没有配置 flm_path，则跳过下载算法，
        # 直接依赖设备端通用烧录接口。
        profiles = load_mcu_profiles()
        mcu = profiles.get(mcu_key, {})
        if not mcu:
            raise FlashError(f"未知 MCU 配置: {mcu_key}，请检查 .mklink/config.json")
        flm_path_from_profile = mcu.get("flm_path", "")
        if flm_path_from_profile:
            # flm_path 来自 profile，格式如 "FLM/N32G43x.FLM"，设备端需要 "/FLM/..." 格式
            if not flm_path_from_profile.startswith("/"):
                flm_path = "/" + flm_path_from_profile
            else:
                flm_path = flm_path_from_profile
            ram_base = mcu.get("ram_base", "0x20000000")

            print(f"[*] 使用 FLM: {flm_path} (RAM={ram_base})")

            if not flash.load_flm(flm_path, flash_base, ram_base):
                raise FLMLoadError(f"FLM 加载失败: {flm_path}")

            print(f"[OK] FLM 加载成功")
        else:
            print("[AUTO] MCU profile 未配置 flm_path，跳过 FLM 加载")

        # 4. 烧录（load.hex 内部自动按扇区擦写，无需单独擦除）
        if is_bin_file:
            print(f"开始烧录 {Path(hex_path).name} (BIN 模式, 地址: {bin_base}) ...")
            if is_hpm_project:
                print(f"[AUTO] HPM 工程使用 hpm.program 下载，板卡: {board or '未指定'}")
                result = flash.burn_hpm_bin(
                    hex_path,
                    addr=bin_base,
                    board=board if board else None,
                    flash_cfg=hpm_flash_cfg,
                    progress_callback=progress_callback,
                )
            else:
                result = flash.burn_bin(hex_path, addr=bin_base, progress_callback=progress_callback)
        else:
            print(f"开始烧录 {Path(hex_path).name} ...")
            result = flash.burn_hex(hex_path, progress_callback=progress_callback)

        # 5. 提示音
        if result["success"]:
            flash.beep()

        return result

    finally:
        flash.close()
