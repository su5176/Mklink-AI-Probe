"""
MKLink Serial Bridge — CLI 入口。

依赖检查 → 命令分发。
"""

from __future__ import annotations

import argparse
import sys

from mklink._deps import require_dependencies
from mklink._types import DEFAULT_BAUDRATE


def _cli_test(port: str):
    """基本 CLI 测试：连接、获取 IDCODE、断开。"""
    from mklink.bridge import MKLinkSerialBridge
    from mklink.discovery import list_available_ports

    print(f"[*] 连接 {port} ...")
    bridge = MKLinkSerialBridge(port)

    if not bridge.connect():
        print("[FAIL] 连接失败，请检查设备和端口")
        available = list_available_ports()
        if available:
            print("可用端口:")
            for p in available:
                print(f"  {p['device']} — {p['description']}")
        return

    print("[OK] 连接成功")

    # 读取 IDCODE
    try:
        resp = bridge.send_command("cmd.get_idcode()", timeout=5.0)
        print(f"IDCODE 响应: {resp.strip()}")
    except Exception as e:
        print(f"[FAIL] 读取 IDCODE 失败: {e}")

    bridge.close()
    print("[*] 已断开连接")


def _cli_keil_parse(project_root: str):
    """解析 Keil .uvprojx 工程文件并显示配置。"""
    import json
    from mklink.keil_parser import find_uvprojx, parse_uvprojx

    uvp = find_uvprojx(project_root)
    if not uvp:
        print("[FAIL] 未找到 .uvprojx 文件")
        return

    print(f"[OK] 找到工程文件: {uvp}")
    info = parse_uvprojx(uvp)
    if not info:
        print("[FAIL] 解析工程文件失败")
        return

    # 格式化输出（排除 groups 以保持简洁）
    display = {k: v for k, v in info.items() if k != "groups"}
    print(json.dumps(display, indent=2, ensure_ascii=False))


def _cli_iar_parse(project_root: str):
    """解析 IAR .ewp 工程文件并显示配置。"""
    import json
    from mklink.iar_parser import find_ewp, parse_ewp

    ewp = find_ewp(project_root)
    if not ewp:
        print("[FAIL] 未找到 .ewp 文件")
        return

    print(f"[OK] 找到工程文件: {ewp}")
    info = parse_ewp(ewp)
    if not info:
        print("[FAIL] 解析工程文件失败")
        return

    # 格式化输出（排除冗余字段）
    exclude_keys = {"groups"}
    display = {k: v for k, v in info.items() if k not in exclude_keys}
    print(json.dumps(display, indent=2, ensure_ascii=False))


def _cli_project_init(project_root: str):
    """初始化项目配置：自动检测 IAR/Keil → 解析工程 → 匹配 MCU → 保存配置。"""
    from pathlib import Path
    from mklink.keil_parser import find_uvprojx, parse_uvprojx
    from mklink.iar_parser import find_ewp, parse_ewp
    from mklink.profiles import load_mcu_profiles, match_mcu_by_device
    from mklink.project_config import (
        save_config, save_project_info, save_rtt_config, is_configured,
    )
    from mklink.discovery import (
        check_flm_on_microkeen, copy_flm_to_microkeen, resolve_keil_flm_path,
    )

    if is_configured(project_root):
        print("[*] 项目已配置，将更新配置...")

    # 1. 自动检测工程类型（IAR vs Keil）
    uvp = find_uvprojx(project_root)
    ewp = find_ewp(project_root)

    ide_type = None
    project_info = None

    if ewp and not uvp:
        # IAR 项目
        ide_type = "IAR"
        print(f"[OK] 找到 IAR 工程文件: {ewp}")
        project_info = parse_ewp(ewp)
    elif uvp and not ewp:
        # Keil 项目
        ide_type = "Keil"
        print(f"[OK] 找到 Keil 工程文件: {uvp}")
        project_info = parse_uvprojx(uvp)
    elif ewp and uvp:
        # 两者都存在，优先使用 Keil
        print("[*] 检测到同时存在 IAR 和 Keil 工程文件")
        ide_type = "Keil"
        print(f"[OK] 找到 Keil 工程文件: {uvp}")
        project_info = parse_uvprojx(uvp)
        print(f"    自动选择: Keil")
    else:
        print("[FAIL] 未找到 .uvprojx 或 .ewp 工程文件")
        return

    if not project_info:
        print(f"[FAIL] 解析 {ide_type} 工程文件失败")
        return

    project_info["ide_type"] = ide_type

    # --- FLM 处理（Keil 和 IAR 都通过 mcu_profiles 获取 FLM） ---
    flm_name = project_info.get("flm_name", "")
    flm_on_keil = None
    flm_on_microkeen = None
    flm_copied = False

    if ide_type == "Keil" and flm_name:
        # flm_name 已经是纯文件名（如 "N32G43x"），自动加上 .FLM 扩展名
        if not flm_name.upper().endswith(".FLM"):
            flm_name_with_ext = flm_name + ".FLM"
        else:
            flm_name_with_ext = flm_name
        # 设备上的 FLM 路径格式
        project_info["flm_path_on_device"] = f"FLM/{flm_name_with_ext}"

        # 从 Keil 安装目录查找 FLM
        flm_on_keil = resolve_keil_flm_path(flm_name)

        # 检查 MICROKEEN 磁盘
        exists_on_microkeen, path_on_microkeen = check_flm_on_microkeen(flm_name)
        if exists_on_microkeen:
            flm_on_microkeen = path_on_microkeen

        # 如果 MICROKEEN 上没有，自动拷贝
        if not exists_on_microkeen:
            success, dest = copy_flm_to_microkeen(flm_name)
            if success:
                flm_on_microkeen = dest
                flm_copied = True

    elif ide_type == "IAR" and flm_name:
        # IAR 也需要 FLM 文件，从 mcu_profiles.json 获取 flm_name
        # flm_name 格式为 "FLM/STM32F10x_1024.FLM"，需要提取纯文件名
        flm_base = flm_name.replace("FLM/", "").replace("flm/", "")
        if not flm_base.upper().endswith(".FLM"):
            flm_base = flm_base + ".FLM"

        # 设备上的 FLM 路径格式
        project_info["flm_path_on_device"] = f"FLM/{flm_base}"

        # 从 Keil 安装目录查找 FLM（IAR 自己不带 FLM，但可能同时安装了 Keil）
        flm_on_keil = resolve_keil_flm_path(flm_name.replace("FLM/", ""))

        # 检查 MICROKEEN 磁盘
        exists_on_microkeen, path_on_microkeen = check_flm_on_microkeen(flm_name.replace("FLM/", ""))
        if exists_on_microkeen:
            flm_on_microkeen = path_on_microkeen

        # 如果 MICROKEEN 上没有，自动拷贝
        if not exists_on_microkeen and flm_on_keil:
            success, dest = copy_flm_to_microkeen(flm_name.replace("FLM/", ""))
            if success:
                flm_on_microkeen = dest
                flm_copied = True

    # 保存 project_info.json（排除 groups）
    if flm_on_keil:
        project_info["flm_path"] = flm_on_keil
    project_save = {k: v for k, v in project_info.items() if k != "groups"}
    save_project_info(project_root, project_save)

    # --- 打印完整初始化结果 ---
    # 匹配 MCU 配置
    profiles = load_mcu_profiles()
    device_name = project_info.get("device", "")
    mcu_key = match_mcu_by_device(device_name, profiles)
    mcu_name = profiles[mcu_key]["name"] if mcu_key and mcu_key != "custom" else "custom"

    print()
    print("=" * 50)
    print("  项目初始化结果")
    print("=" * 50)
    print(f"  IDE:      {ide_type}")
    print(f"  设备:     {device_name} ({project_info.get('vendor', '?')})")
    print(f"  编译器:   {project_info.get('compiler', '?').upper()}")
    print(f"  Flash:    {project_info.get('flash_base', '?')} ({project_info.get('flash_size', 0)} bytes)")
    print(f"  RAM:      {project_info.get('ram_base', '?')} ({project_info.get('ram_size', 0)} bytes)")
    print(f"  HEX:      {project_info.get('hex_path', '?')}")
    print(f"  MAP:      {project_info.get('map_path', '?')}")

    if ide_type == "Keil":
        print()
        print(f"  FLM 名称: {flm_name}")
        if flm_on_keil:
            print(f"  FLM (Keil安装目录):  {flm_on_keil}")
        else:
            print(f"  FLM (Keil安装目录):  未找到")
        if flm_on_microkeen:
            status = " (已拷贝)" if flm_copied else " (已存在)"
            print(f"  FLM (MICROKEEN磁盘): {flm_on_microkeen}{status}")
        else:
            print(f"  FLM (MICROKEEN磁盘): 未找到")
    elif ide_type == "IAR":
        # IAR 也需要 FLM，显示 FLM 相关信息
        print()
        print(f"  FLM 名称: {flm_name}")
        if flm_on_keil:
            print(f"  FLM (Keil安装目录):  {flm_on_keil}")
        else:
            print(f"  FLM (Keil安装目录):  未找到")
        if flm_on_microkeen:
            status = " (已拷贝)" if flm_copied else " (已存在)"
            print(f"  FLM (MICROKEEN磁盘): {flm_on_microkeen}{status}")
        else:
            print(f"  FLM (MICROKEEN磁盘): 未找到")
        fl = project_info.get("flash_loader_path", "")
        if fl:
            print(f"  Flash Loader: {fl}")

    print(f"  MCU:      {mcu_key or 'custom'} ({mcu_name})")
    print(f"  RTT:      将在首次使用时检测")

    print("=" * 50)

    # 自动检测 COM 口
    from mklink.discovery import find_mklink_cdc_port
    com_port = find_mklink_cdc_port()
    if com_port:
        print(f"  COM 口:   {com_port} (自动检测)")
    else:
        print(f"  COM 口:   未检测到（稍后可手动配置）")

    # 保存 config.json
    save_config(project_root, {
        "com_port": com_port or "",
        "baudrate": DEFAULT_BAUDRATE,
        "mcu_key": mcu_key,
        "ide_type": ide_type,
        "swd_clock": 1000000,
    })
    # 保存默认 rtt_config（RTT 地址在首次使用时再检测）
    save_rtt_config(project_root, {
        "integrated": False,
        "rtt_addr": "",
        "search_size": 1024,
        "channel": 0,
        "autostart": False,
    })
    print("[OK] 项目配置已保存到 .mklink/")

    # 检查可选依赖
    from mklink._deps import check_readelf_available
    readelf_ok, readelf_info = check_readelf_available()
    if readelf_ok:
        print(f"  readelf:  {readelf_info} (符号解析可用)")
    else:
        print(f"  readelf:  未安装（符号解析/VOFA变量名/HardFault分析不可用）")
        print(f"            安装: winget install --id Arm.GnuArmEmbeddedToolchain")


def _cli_flash(project_root: str, port: str | None, hex_path: str | None):
    """一站式烧录：连接 → IDCODE → FLM → 烧录。"""
    from mklink.flash import burn_hex_file
    from mklink.project_config import (
        check_project_config, format_config_status, load_config, load_project_info, save_config,
    )

    # 1. 配置检查（提示用户如果配置不完整）
    status = check_project_config(project_root)
    if not status.is_valid:
        print(format_config_status(status))
        return
    if status.needs_init():
        print("[WARN] 项目未初始化或配置不完整:")
        print(format_config_status(status))
        print()

    # 如果未指定端口，加载配置中的端口并验证连通性
    resolved_port = port
    config = load_config(project_root) or {}
    project = load_project_info(project_root) or {}
    if not resolved_port:
        if config and config.get("com_port"):
            from mklink.bridge import MKLinkSerialBridge
            test_bridge = MKLinkSerialBridge(config["com_port"])
            if test_bridge.connect():
                resolved_port = config["com_port"]
                test_bridge.close()
            else:
                # 配置的端口无效，自动重新检测
                from mklink.discovery import find_mklink_cdc_port
                detected = find_mklink_cdc_port()
                if detected and detected != config["com_port"]:
                    print(f"[WARN] 保存的端口 {config['com_port']} 无法连接，检测到新端口 {detected}")
                    print(f"[AUTO] 更新配置端口为 {detected}")
                    config["com_port"] = detected
                    save_config(project_root, config)
                    resolved_port = detected
                else:
                    test_bridge.close()
                    if detected is None:
                        print("[FAIL] 未找到 MKLink 设备，请检查连接")
                        return
                    resolved_port = detected

    # BIN 文件安全检查
    resolved_hex = hex_path or project.get("hex_path", "")
    if resolved_hex and resolved_hex.lower().endswith(".bin"):
        print("[WARN] 指定的文件是 .bin 格式。BIN 文件不包含地址信息，烧录有风险。")
        print("       建议使用 .hex 文件（包含完整地址映射）。")
        print()

    try:
        result = burn_hex_file(
            hex_path=hex_path,
            port=resolved_port,
            mcu_key=config.get("mcu_key"),
            flash_base=project.get("flash_base") or "0x08000000",
            swd_clock=config.get("swd_clock"),
            project_root=project_root,
        )
        if result["success"]:
            print(f"\n[OK] 烧录成功 ({result['time_ms']}ms)")
        else:
            print(f"\n[FAIL] 烧录失败")
            print(f"  响应: {result.get('response', '')[:200]}")
    except Exception as e:
        print(f"[FAIL] {e}")


def _cli_rtt(
    project_root: str,
    port: str | None,
    duration: float,
    visualize: bool = False,
    host: str = "127.0.0.1",
    port_http: int = 0,
    no_browser: bool = False,
):
    """一站式 RTT 捕获：连接 → 启动 RTT → 读取输出。

    支持两种模式：
      - 控制台模式（默认）：实时打印原始 RTT 数据到终端
      - RTT View 模式（--visualize）：启动 Web RAW 终端
    """
    import time
    from mklink.bridge import MKLinkSerialBridge
    from mklink.rtt import RTTSession
    from mklink.discovery import find_mklink_cdc_port
    from mklink.project_config import load_rtt_config, ensure_rtt_config_updated, load_config, save_config

    resolved_port = port
    if not resolved_port:
        config = load_config(project_root)
        if config and config.get("com_port"):
            test_bridge = MKLinkSerialBridge(config["com_port"])
            if test_bridge.connect():
                resolved_port = config["com_port"]
                test_bridge.close()
            else:
                test_bridge.close()
                detected = find_mklink_cdc_port()
                if detected and detected != config.get("com_port"):
                    print(f"[WARN] 保存的端口 {config['com_port']} 无法连接，检测到新端口 {detected}")
                    print(f"[AUTO] 更新配置端口为 {detected}")
                    config["com_port"] = detected
                    save_config(project_root, config)
                    resolved_port = detected
                elif detected:
                    resolved_port = detected
                else:
                    print("[FAIL] 未找到 MKLink 设备，请检查连接")
                    return
        else:
            resolved_port = find_mklink_cdc_port()
            if not resolved_port:
                print("[FAIL] 未找到 MKLink 设备")
                return

    # 自动更新 RTT 地址（如果 MAP 文件比配置新）
    ensure_rtt_config_updated(project_root)

    rtt_cfg = load_rtt_config(project_root)
    if not rtt_cfg or not rtt_cfg.get("rtt_addr"):
        print("[FAIL] RTT 地址未配置，请先运行:")
        print("  python -m mklink rtt-integrate --project-root .")
        return

    print(f"[*] 连接 {resolved_port} ...")
    bridge = MKLinkSerialBridge(resolved_port)
    if not bridge.connect():
        print("[FAIL] 连接失败")
        return
    print("[OK] 连接成功")

    session = RTTSession(bridge, channel=rtt_cfg.get("channel", 0))
    result = session.start("", project_root=project_root)

    if not result.get("control_block_addr"):
        print("[FAIL] 未找到 RTT 控制块")
        bridge.close()
        return

    print(f"[OK] RTT 已启动 (控制块: {result['control_block_addr']})")

    if visualize:
        # --- RTT View 模式（Web RAW 终端）---
        from mklink.rtt_viewer import run_rtt_raw_viewer
        try:
            run_rtt_raw_viewer(
                session=session,
                bridge=bridge,
                host=host,
                port=port_http,
                no_browser=no_browser,
                duration=duration,
            )
        except Exception as e:
            print(f"[FAIL] RTT RAW 终端启动失败: {e}")
            session.stop()
            bridge.close()
    else:
        # --- 控制台模式（原有行为）---
        print(f"[*] 读取 RTT 输出 {duration} 秒...\n")
        start = time.time()
        try:
            while time.time() - start < duration:
                data = session.read_output(0.5)
                if data:
                    print(data, end="", flush=True)
        except KeyboardInterrupt:
            print("\n[*] 用户中断")
        finally:
            session.stop()
            bridge.close()
            print(f"\n[OK] RTT 会话结束")


def _cli_copy_flm(project_root: str):
    """拷贝 FLM 文件到 MICROKEEN 磁盘。"""
    from mklink.project_config import load_project_info
    from mklink.discovery import check_flm_on_microkeen, copy_flm_to_microkeen

    project = load_project_info(project_root)
    if project is None:
        print("[FAIL] 项目未配置，先运行 `python -m mklink project-init`")
        return

    ide_type = project.get("ide_type", "Keil")
    if ide_type == "IAR":
        print("[FAIL] IAR 项目使用 .board flash loader，不需要拷贝 FLM")
        flash_loader = project.get("flash_loader_path", "")
        if flash_loader:
            print(f"  Flash Loader: {flash_loader}")
        return

    flm_name = project.get("flm_name", "")
    if not flm_name:
        print("[FAIL] 未找到 FLM 配置")
        return

    # 检查是否已存在
    exists, path = check_flm_on_microkeen(flm_name)
    if exists:
        print(f"[OK] FLM 已存在: {path}")
        return

    # 执行拷贝
    success, dest = copy_flm_to_microkeen(flm_name)
    if not success:
        print(f"[FAIL] FLM '{flm_name}' 拷贝失败")
        print("请确保：")
        print("  1. [MICROKEEN] 磁盘已插入")
        print("  2. Keil 已安装且包含该芯片的 FLM")


def _cli_project_info(project_root: str):
    """显示项目已缓存的配置。"""
    import json
    from mklink.project_config import (
        load_config, load_project_info, load_rtt_config, is_configured,
    )
    from mklink.discovery import check_flm_on_microkeen

    if not is_configured(project_root):
        print("[*] 项目未配置，运行 `python -m mklink project-init` 初始化")
        return

    config = load_config(project_root)
    project = load_project_info(project_root)
    rtt = load_rtt_config(project_root)

    print("=== 基本配置 ===")
    if config:
        print(json.dumps(config, indent=2, ensure_ascii=False))

    if project:
        ide_type = project.get("ide_type", "Keil")
        print(f"\n=== {ide_type} 工程 ===")
        print(f"  设备: {project.get('device', '?')} ({project.get('vendor', '?')})")
        if ide_type == "Keil":
            print(f"  FLM: {project.get('flm_name', '?')}")
        elif ide_type == "IAR":
            print(f"  Flash Loader: {project.get('flash_loader_path', '?')}")
            print(f"  CPU: {project.get('cpu', '?')}")
        print(f"  HEX: {project.get('hex_path', '?')}")
        print(f"  MAP: {project.get('map_path', '?')}")

        # 检查 MICROKEEN 磁盘上的 FLM（仅 Keil）
        if ide_type == "Keil":
            flm_name = project.get("flm_name", "")
            if flm_name:
                exists, path = check_flm_on_microkeen(flm_name)
                if exists:
                    print(f"  MICROKEEN FLM: {path}")
                else:
                    print(f"  MICROKEEN FLM: 未找到 '{flm_name}'")

    if rtt:
        print("\n=== RTT 配置 ===")
        print(json.dumps(rtt, indent=2, ensure_ascii=False))

    # Modbus 配置（从 config.json 中提取）
    if config and config.get("modbus_port"):
        print("\n=== Modbus 配置 ===")
        print(f"  串口: {config['modbus_port']}")
        print(f"  波特率: {config.get('modbus_baud', 9600)}")
        print(f"  校验位: {config.get('modbus_parity', 'N')}")
        print(f"  停止位: {config.get('modbus_stopbits', 1)}")


def _cli_rtt_integrate(project_root: str, src_dir: str | None, inc_dir: str | None, force: bool = False):
    """集成 SEGGER RTT 源文件到项目。

    自动完成:
    1. 复制 RTT 源文件到项目 src/ 和 inc/
    2. 将文件添加到 Keil 或 IAR 工程
    3. 在 main.c 中添加 SEGGER_RTT_Init() 调用（USE_RTT 宏保护）
    4. 在工程中添加 USE_RTT 宏定义
    5. 查找并保存 RTT 控制块地址到配置文件
    """
    from pathlib import Path
    from mklink.project_config import load_keil_project, save_rtt_config
    from mklink.rtt_integration import (
        check_rtt_in_project, full_rtt_integrate,
        check_rtt_sources_bundled, generate_rtt_usage_example,
    )
    from mklink.rtt_addr import diagnose_rtt_addr, find_rtt_addr_from_map

    if not check_rtt_sources_bundled():
        print("[FAIL] 技能目录中缺少 RTT 源文件 (rtt_sources/)")
        return

    # 确定 src/inc 目录
    root = Path(project_root).resolve()
    if not src_dir or not inc_dir:
        # 尝试 IAR 项目
        from mklink.iar_parser import find_ewp, parse_ewp, resolve_iar_path
        ewp_path = find_ewp(root)
        if ewp_path:
            ewp_info = parse_ewp(ewp_path)
            if ewp_info and ewp_info.get("include_paths"):
                for inc_path in ewp_info["include_paths"]:
                    resolved = resolve_iar_path(ewp_path, inc_path)
                    if Path(resolved).exists() and not inc_dir:
                        inc_dir = resolved
                        break

        # 尝试 Keil 项目
        if not inc_dir:
            keil = load_keil_project(project_root)
            if keil and keil.get("include_paths"):
                uvp_dir = Path(keil["uvprojx_path"]).parent
                for inc_path in keil["include_paths"]:
                    resolved = (uvp_dir / inc_path).resolve()
                    if resolved.exists() and str(resolved).startswith(str(root)):
                        if not inc_dir:
                            inc_dir = str(resolved)
                        break

        # 通用回退：扫描常见头文件目录
        if not inc_dir:
            for candidate in ["inc", "Core/Inc", "User", "App", "include"]:
                candidate_path = root / candidate
                if candidate_path.exists() and any(candidate_path.glob("*.h")):
                    inc_dir = str(candidate_path)
                    break

    if not src_dir:
        src_dir = str(root / "src")
    if not inc_dir:
        inc_dir = str(root / "inc")

    if not Path(inc_dir).exists():
        print(f"[WARN] 头文件目录不存在: {inc_dir}")
        print("       RTT 头文件将被复制到该目录（目录将自动创建）")
        print("       建议确认该目录是否在项目的 Include Path 中")

    print(f"[*] 源文件目录: {src_dir}")
    print(f"[*] 头文件目录: {inc_dir}")

    # 检查当前状态（跳过 force 时）
    if not force:
        status = check_rtt_in_project(src_dir, inc_dir)
        if status["integrated"]:
            print("[OK] RTT 源文件已存在于项目中（使用 --force 强制重新集成）")
            return

    # 执行完整集成流程
    print("\n[*] 开始 RTT 集成...")
    result = full_rtt_integrate(
        project_root=project_root,
        uvprojx_path=None,  # 自动查找
        ewp_path=None,     # 自动查找
        src_dir=src_dir,
        inc_dir=inc_dir,
        main_c_path=None,  # 自动查找
    )

    print()
    print("=" * 50)
    print("  RTT 集成结果")
    print("=" * 50)

    # 复制源文件
    copy = result.get("copy")
    if copy:
        if copy["success"]:
            print("\n[1] 复制源文件:")
            for f in copy.get("copied", []):
                print(f"  + {f}")
            for f in copy.get("skipped", []):
                print(f"  ~ {f} (已存在)")
        else:
            print(f"\n[FAIL] 复制源文件失败: {copy.get('errors', [])}")

    # 添加到 IAR 工程
    iar = result.get("iar")
    if iar:
        print(f"\n[2] 添加到 IAR 工程:")
        if iar["success"]:
            print(f"  + 已添加 SEGGER_RTT 文件到 .ewp")
            print(f"  + 工程文件已备份到: {iar.get('backup_path', 'N/A')}")
        else:
            for e in iar.get("errors", []):
                print(f"  ! {e}")

    # 添加到 Keil 工程
    keil = result.get("keil")
    if keil:
        print(f"\n[2] 添加到 Keil 工程:")
        if keil["success"]:
            print(f"  + 已添加 SEGGER_RTT 分组到 .uvprojx")
            print(f"  + 工程文件已备份到: {keil.get('backup_path', 'N/A')}")
        else:
            for e in keil.get("errors", []):
                print(f"  ! {e}")

    # 添加初始化代码
    main_res = result.get("main")
    if main_res:
        print(f"\n[3] 添加初始化代码到 main.c:")
        if main_res["success"]:
            if main_res.get("added_include"):
                print(f"  + 已添加 #ifdef USE_RTT / #include \"SEGGER_RTT.h\" / #endif")
            if main_res.get("added_init"):
                print(f"  + 已添加 USE_RTT 宏保护的 SEGGER_RTT_Init() 调用")
            if main_res.get("verified"):
                print(f"  + 初始化验证通过")
            print(f"  + main.c 已备份到: {main_res.get('backup_path', 'N/A')}")
        else:
            for e in main_res.get("errors", []):
                print(f"  ! {e}")
        for w in main_res.get("warnings", []):
            print(f"  ~ {w}")
    elif result.get("main_error"):
        print(f"\n[3] 添加初始化代码到 main.c:")
        print(f"  ! {result['main_error']}")

    # 添加 USE_RTT 宏
    macro_res = result.get("macro")
    if macro_res:
        print(f"\n[4] 添加 USE_RTT 宏:")
        if macro_res["success"] and macro_res.get("macro_added"):
            print(f"  + 已添加 USE_RTT 到 {macro_res.get('ide_type', '?')} 工程定义")
        elif macro_res["success"] and not macro_res.get("macro_added"):
            print(f"  ~ USE_RTT 宏已存在于 {macro_res.get('ide_type', '?')} 工程定义中，跳过")
        else:
            for e in macro_res.get("errors", []):
                print(f"  ! {e}")

    # 最终结果
    print("\n" + "=" * 50)
    if result.get("success"):
        print("  RTT 集成全部成功")
    else:
        print("  RTT 集成存在失败项，请检查上方输出")
    print("=" * 50)

    # 查找 RTT 地址并更新配置
    keil = load_keil_project(project_root)
    rtt_addr = ""
    if keil and keil.get("map_path"):
        map_path = keil["map_path"]
        print(f"\n[*] 正在从 MAP 文件查找 RTT 地址: {map_path}")
        rtt_result = diagnose_rtt_addr(map_path)
        rtt_addr = rtt_result.addr
        if rtt_addr:
            source = f" ({rtt_result.source})" if rtt_result.source else ""
            print(f"[OK] 找到 _SEGGER_RTT 地址: {rtt_addr}{source}")
        else:
            print("[WARN] 未能解析出 _SEGGER_RTT 地址")
            for detail in rtt_result.details:
                print(f"      - {detail}")
            for warning in rtt_result.warnings:
                print(f"      - {warning}")
            print("      请确保已重新编译项目后运行此命令")

    # 更新 rtt_config
    save_rtt_config(project_root, {
        "integrated": result.get("success", False),
        "rtt_addr": rtt_addr or "",
        "search_size": 1024,
        "channel": 0,
        "autostart": False,
    })

    if rtt_addr:
        print("\n[OK] RTT 配置已更新到 .mklink/rtt_config.json")

    if result.get("success"):
        print("\n--- 使用示例 ---")
        print(generate_rtt_usage_example())
    elif not rtt_addr:
        print("\n[!] RTT 地址未找到，请重新编译项目后再次运行 `python -m mklink rtt-integrate`")


def _resolve_port(port: str | None, project_root: str = ".") -> str:
    """解析 COM 端口。优先级：
    1. 显式指定的 --port 参数
    2. config.json 中保存的 com_port（验证连通性）
    3. 自动扫描发现新端口（更新 config）
    """
    if port:
        return port

    from mklink.discovery import find_mklink_cdc_port
    from mklink.project_config import load_config, save_config

    config = load_config(project_root)

    # 优先使用配置中的端口
    if config and config.get("com_port"):
        saved_port = config["com_port"]
        from mklink.bridge import MKLinkSerialBridge
        bridge = MKLinkSerialBridge(saved_port)
        if bridge.connect():
            bridge.close()
            return saved_port
        bridge.close()
        # 配置端口失败，fallback 到扫描

    found = find_mklink_cdc_port()
    if not found:
        print("[FAIL] 未找到 MKLink 设备，请用 --port 指定")
        raise SystemExit(1)

    # 更新配置
    if config and config.get("com_port") != found:
        print(f"[WARN] 保存的端口 {config.get('com_port', '(空)')} 不可用，检测到新端口 {found}")
        print(f"[AUTO] 更新配置端口为 {found}")
        config["com_port"] = found
        save_config(project_root, config)
    elif not config:
        # 没有配置文件时也尝试保存
        try:
            save_config(project_root, {"com_port": found})
        except Exception:
            pass

    return found


def _cli_read_ram(port: str | None, addr: str, size: int, save: str | None):
    """读取目标芯片 RAM 数据。"""
    from mklink.bridge import MKLinkSerialBridge

    port = _resolve_port(port)
    print(f"[*] 连接 {port} ...")
    bridge = MKLinkSerialBridge(port)
    if not bridge.connect():
        print("[FAIL] 连接失败")
        return

    try:
        if save:
            cmd = f'cmd.read_ram({addr}, {size}, "{save}")'
        else:
            cmd = f'cmd.read_ram({addr}, {size})'
        print(f"[*] {cmd}")
        resp = bridge.send_command(cmd, timeout=10.0)
        print(resp.strip())
        if save:
            print(f"\n[OK] 数据已保存到设备文件: {save}")
            print("     重启下载器后可在 U 盘中查看")
    except Exception as e:
        print(f"[FAIL] {e}")
    finally:
        bridge.close()


def _format_reg_value(data: bytes, width: int) -> int | None:
    if width <= 8 and len(data) >= 1:
        return data[0]
    if width <= 16 and len(data) >= 2:
        return int.from_bytes(data[:2], "little")
    if len(data) >= 4:
        return int.from_bytes(data[:4], "little")
    return None


def _cli_read_reg(
    port: str | None,
    register: str | None,
    addr: str | None,
    width: int,
    count: int,
    output_format: str,
    raw: bool,
):
    """读取内存映射寄存器。"""
    from mklink.memory_access import read_memory
    from mklink.registers import resolve_register

    target = register or addr
    if not target:
        print("[FAIL] 请指定寄存器名或 --addr")
        return
    try:
        reg = resolve_register(target, width=width)
    except KeyError as e:
        print(f"[FAIL] {e}")
        return

    bytes_per = max(1, width // 8)
    data, raw_resp = read_memory(port, reg.address, bytes_per * count)
    if raw:
        print(raw_resp.strip())
        return
    print(f"{reg.name} @ 0x{reg.address:08X}")
    for i in range(count):
        chunk = data[i * bytes_per:(i + 1) * bytes_per]
        value = _format_reg_value(chunk, width)
        if value is None:
            print(raw_resp.strip())
            return
        suffix = f"[{i}]" if count > 1 else ""
        if output_format == "hex":
            display = f"0x{value:0{bytes_per * 2}X}"
        elif output_format == "dec":
            display = str(value)
        elif output_format == "bin":
            display = f"0b{value:0{width}b}"
        else:
            display = f"0x{value:0{bytes_per * 2}X} ({value})"
        print(f"  {reg.name}{suffix} = {display}")


def _cli_write_ram(port: str | None, addr: str, data_bytes: list[str]):
    """写入数据到目标芯片 RAM 并回读验证。"""
    from mklink.bridge import MKLinkSerialBridge

    if not data_bytes:
        print("[FAIL] 未指定写入数据，用法: python -m mklink write-ram --addr 0x20001000 0xDE 0xAD 0xBE 0xEF")
        return

    port = _resolve_port(port)
    print(f"[*] 连接 {port} ...")
    bridge = MKLinkSerialBridge(port)
    if not bridge.connect():
        print("[FAIL] 连接失败")
        return

    try:
        byte_args = ", ".join(data_bytes)
        write_cmd = f'cmd.write_ram({addr}, {byte_args})'
        print(f"[*] 写入: {write_cmd}")
        resp = bridge.send_command(write_cmd, timeout=10.0)
        print(resp.strip())

        # 回读验证
        n = len(data_bytes)
        read_cmd = f'cmd.read_ram({addr}, {n})'
        print(f"\n[*] 回读验证: {read_cmd}")
        resp = bridge.send_command(read_cmd, timeout=10.0)
        print(resp.strip())
    except Exception as e:
        print(f"[FAIL] {e}")
    finally:
        bridge.close()


def _cli_read_flash(port: str | None, addr: str, size: int, save: str | None, project_root: str):
    """读取目标芯片 Flash 数据（自动加载 FLM）。"""
    from mklink.bridge import MKLinkSerialBridge
    from mklink.profiles import load_mcu_profiles
    from mklink.project_config import load_config, load_keil_project

    port = _resolve_port(port)
    print(f"[*] 连接 {port} ...")
    bridge = MKLinkSerialBridge(port)
    if not bridge.connect():
        print("[FAIL] 连接失败")
        return

    try:
        # 自动加载 FLM（读 Flash 需要 FLM 算法）
        config = load_config(project_root)
        keil = load_keil_project(project_root)
        mcu_key = config.get("mcu_key", "") if config else ""
        profiles = load_mcu_profiles()
        mcu = profiles.get(mcu_key, {})

        flm_path = mcu.get("flm_path", "")
        if keil and keil.get("flm_path_on_device"):
            flm_path = keil["flm_path_on_device"]
        if flm_path and not flm_path.startswith("/"):
            flm_path = "/" + flm_path

        flash_base = mcu.get("flash_base", "0x08000000")
        ram_base = mcu.get("ram_base", "0x20000000")

        if flm_path:
            loaded = bridge.require_flm_loaded({
                "flm_path": flm_path.lstrip("/"),
                "flash_base": flash_base,
                "ram_base": ram_base,
                "name": mcu.get("name", mcu_key),
            })
            if loaded:
                print("[OK] FLM 已加载")
            else:
                print("[WARN] FLM 加载失败，read_flash 可能不可用")
        else:
            print("[WARN] 未找到 FLM 配置，跳过 FLM 加载")

        if save:
            cmd = f'cmd.read_flash({addr}, {size}, "{save}")'
        else:
            cmd = f'cmd.read_flash({addr}, {size})'
        print(f"[*] {cmd}")
        resp = bridge.send_command(cmd, timeout=10.0)
        print(resp.strip())
        if save:
            print(f"\n[OK] 数据已保存到设备文件: {save}")
            print("     重启下载器后可在 U 盘中查看")
    except Exception as e:
        print(f"[FAIL] {e}")
    finally:
        bridge.close()


def _cli_symbols(source: str, filter_pattern: str | None):
    """Browse symbols from ELF/AXF file using arm-none-eabi-readelf.

    Usage:
        python -m mklink symbols --source <axf>
        python -m mklink symbols --source <axf> --filter <regex>
    """
    import subprocess

    from mklink.symbol_parser import parse_readelf_output, filter_symbols

    # Run readelf to get symbol table
    cmd = ["arm-none-eabi-readelf", "-s", source]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except FileNotFoundError:
        print("[FAIL] arm-none-eabi-readelf not found on PATH")
        print("       Install ARM GCC toolchain or add to PATH")
        return
    except subprocess.TimeoutExpired:
        print("[FAIL] readelf command timed out")
        return

    if result.returncode != 0:
        print(f"[FAIL] readelf failed: {result.stderr.strip()}")
        return

    # Parse the readelf output
    symbols = parse_readelf_output(result.stdout)

    if not symbols:
        if not result.stdout.strip():
            print("No symbols found (empty readelf output)")
        else:
            print("No RAM OBJECT symbols found")
        return

    # Apply filter if specified
    if filter_pattern:
        try:
            import re
            re.compile(filter_pattern)  # Validate regex first
        except re.error as e:
            print(f"[FAIL] Invalid regex pattern: {e}")
            return

        symbols = filter_symbols(symbols, filter_pattern)

        if not symbols:
            print("No matching symbols found")
            return

    # Print table
    # Format: Name  Address  Size
    name_w = max(len(s["name"]) for s in symbols)
    name_w = max(name_w, 4)  # at least "Name" width

    print(f"{'Name':<{name_w}}  {'Address':<12}  {'Size':>4}")
    print(f"{'-' * name_w}  {'-' * 12}  {'-' * 4}")
    for sym in symbols:
        print(f"{sym['name']:<{name_w}}  {sym['address']:<12}  {sym['size']:>4}")


def _default_axf_from_project(project_root: str) -> str | None:
    from mklink.project_config import load_project_info, load_keil_project

    project = load_project_info(project_root) or load_keil_project(project_root)
    if not project:
        return None
    return project.get("axf_path") or project.get("out_path")


def _project_root_from_args(args) -> str:
    project_root = getattr(args, "project_root", ".")
    positional = getattr(args, "project_root_positional", None)
    return project_root if project_root != "." else positional or "."


def _profile_sizes(project_root: str) -> tuple[int, int]:
    from mklink.project_config import load_config, load_project_info, load_keil_project
    from mklink.profiles import load_mcu_profiles

    flash_size = 0
    ram_size = 0
    project = load_project_info(project_root) or load_keil_project(project_root) or {}
    flash_size = int(project.get("flash_size") or 0)
    ram_size = int(project.get("ram_size") or 0)
    config = load_config(project_root) or {}
    mcu_key = config.get("mcu_key")
    if mcu_key:
        profile = load_mcu_profiles().get(mcu_key, {})
        for region in profile.get("regions", []):
            if region.get("name") == "flash" and not flash_size:
                flash_size = int(str(region.get("size", "0")), 0)
            if region.get("name") == "ram" and not ram_size:
                ram_size = int(str(region.get("size", "0")), 0)
    return flash_size, ram_size


def _cli_hardfault(port: str | None, source: str | None, sp: str | None):
    from mklink.hardfault import FAULT_REGISTERS, addr2line, format_hardfault_report, parse_exception_stack_frame
    from mklink.memory_access import read_memory
    from mklink.registers import resolve_register

    fault_values: dict[str, int] = {}
    for reg_name in FAULT_REGISTERS:
        reg = resolve_register(reg_name)
        data, raw = read_memory(port, reg.address, 4)
        if len(data) >= 4:
            fault_values[reg_name] = int.from_bytes(data[:4], "little")
        else:
            print(f"[WARN] 无法解析 {reg_name} 响应:")
            print(raw.strip())

    frame = None
    locations = {}
    if sp:
        sp_addr = int(sp, 0)
        data, raw = read_memory(port, sp_addr, 32)
        if len(data) >= 32:
            frame = parse_exception_stack_frame(data)
            if source:
                locations = addr2line(source, frame["pc"], frame["lr"])
        else:
            print("[WARN] 无法解析异常栈帧:")
            print(raw.strip())
    else:
        print("[INFO] 未指定 --sp；只读取 Fault 寄存器。CPU MSP/PSP 不是普通内存地址，不能用 read_ram 自动读取。")

    print(format_hardfault_report(fault_values, frame=frame, locations=locations))


def _cli_typeinfo(args):
    from mklink.typeinfo import run_typeinfo

    if not args.source:
        args.source = _default_axf_from_project(_project_root_from_args(args))
    if not args.source:
        print("[FAIL] 请指定 --source 或先运行 project-init")
        return
    try:
        print(run_typeinfo(args))
    except Exception as e:
        print(f"[FAIL] {e}")


def _cli_memmap(args):
    from mklink.memmap import analyze_memmap, format_memmap, format_memmap_json

    project_root = _project_root_from_args(args)
    source = args.source or _default_axf_from_project(project_root)
    if not source:
        print("[FAIL] 请指定 --source 或先运行 project-init")
        return
    flash_size, ram_size = _profile_sizes(project_root)
    try:
        summary = analyze_memmap(source, flash_size=flash_size, ram_size=ram_size)
    except Exception as e:
        print(f"[FAIL] {e}")
        return
    print(format_memmap_json(summary) if args.json else format_memmap(summary))


def _cli_watch(args):
    from mklink.watch import format_watch_rows, read_watch_values

    source = args.source or _default_axf_from_project(args.project_root)
    if not source:
        print("[FAIL] 请指定 --source 或先运行 project-init")
        return
    names: list[str] = []
    if args.variables:
        for item in args.variables:
            names.extend([p.strip() for p in item.split(",") if p.strip()])
    if args.profile:
        import json
        with open(args.profile, "r", encoding="utf-8") as f:
            profile = json.load(f)
        names.extend(profile.get("variables", []))
    if not names:
        print("[FAIL] 请指定变量名或 --profile")
        return
    import time
    try:
        if args.period and args.period > 0:
            while True:
                print(format_watch_rows(read_watch_values(names, source=source, port=args.port), as_json=args.json))
                time.sleep(args.period)
        else:
            print(format_watch_rows(read_watch_values(names, source=source, port=args.port), as_json=args.json))
    except KeyboardInterrupt:
        pass
    except Exception as e:
        print(f"[FAIL] {e}")


def _cli_superwatch(args):
    """Start SuperWatch read_ram-based variable/register visualization."""
    import json

    from mklink.superwatch import (
        build_read_blocks,
        find_project_svd,
        load_svd_registers,
        poll_blocks,
        resolve_watch_items,
        run_superwatch_visualizer,
    )

    svd_registers = {}
    svd_path = args.svd or find_project_svd(args.project_root)
    if svd_path:
        try:
            svd_registers = load_svd_registers(svd_path)
            print(f"[OK] SVD loaded: {svd_path}")
        except Exception as e:
            print(f"[WARN] SVD unavailable: {e}")

    dwarf_info = None
    if args.source:
        try:
            from mklink.dwarf_parser import load_dwarf_info
            dwarf_info = load_dwarf_info(args.source)
        except Exception as e:
            print(f"[WARN] DWARF unavailable: {e}")

    try:
        items = resolve_watch_items(
            args.variables,
            source=args.source,
            dwarf_info=dwarf_info,
            svd_registers=svd_registers,
        )
    except Exception as e:
        print(f"[FAIL] {e}")
        return
    if not items and not args.visualize:
        print("[FAIL] No SuperWatch variables or registers specified")
        return

    if args.visualize:
        run_superwatch_visualizer(
            items=items,
            period=args.period,
            port=args.port,
            host=args.host,
            port_http=args.port_http,
            no_browser=args.no_browser,
            max_points=args.max_points,
            duration=args.duration,
            dwarf_info=dwarf_info,
            svd_registers=svd_registers,
            dump_mem=getattr(args, 'dump_mem', False),
        )
        return

    try:
        points = poll_blocks(
            build_read_blocks(items),
            port=args.port,
            duration=args.duration,
            period=args.period,
        )
        print(json.dumps(points, ensure_ascii=False, indent=2))
    except Exception as e:
        print(f"[FAIL] {e}")


def _cli_vofa(
    port: str | None,
    variables: list[str],
    period: float,
    stop: bool,
    visualize: bool = False,
    host: str = "127.0.0.1",
    port_http: int = 0,
    no_browser: bool = False,
    max_points: int = 500,
    duration: float = 30.0,
    names: str | None = None,
    source: str | None = None,
):
    """启动或停止 VOFA+ 实时变量观测。"""
    from mklink.bridge import MKLinkSerialBridge
    from mklink._types import DeviceState

    port = _resolve_port(port)
    print(f"[*] 连接 {port} ...")
    bridge = MKLinkSerialBridge(port)
    if not bridge.connect():
        print("[FAIL] 连接失败")
        return

    try:
        if stop:
            # 停止：用上次的变量列表发送 period=0，或直接发空命令
            # 简单方案：发送一个 period=0 的 vofa.send
            if variables:
                var_args = ", ".join(variables)
                cmd = f'vofa.send({var_args}, 0)'
            else:
                cmd = 'vofa.send(0x20000000, "uint8_t", 0)'
            print(f"[*] 停止 VOFA: {cmd}")
            resp = bridge.send_command(cmd, timeout=5.0)
            print(resp.strip())
            print("[OK] VOFA 已停止")
        else:
            if not variables:
                print("[FAIL] 请指定观测变量，用法:")
                print('  python -m mklink vofa 0x20000030 uint8_t 0x2000154c float --period 0.00001')
                return

            original_variables = list(variables)
            if source:
                from mklink.vofa_viewer import resolve_variable_names
                variables = resolve_variable_names(variables, source)

            var_args = ", ".join(f'"{v}"' if not v.startswith("0x") and not v.replace(".", "").isdigit() else v for v in variables)
            cmd = f'vofa.send({var_args}, {period})'
            print(f"[*] 启动 VOFA: {cmd}")

            # 切换到流模式
            bridge._enter_stream(DeviceState.VOFA_STREAM)
            bridge._write_raw((cmd + "\n").encode("utf-8"))

            if visualize:
                # --- 可视化模式 ---
                from mklink.vofa_viewer import run_vofa_visualizer
                channel_names = [n.strip() for n in names.split(",")] if names else None
                run_vofa_visualizer(
                    bridge,
                    variables=variables,
                    var_args=var_args,
                    period=period,
                    duration=duration,
                    host=host,
                    port=port_http,
                    no_browser=no_browser,
                    max_points=max_points,
                    channel_names=channel_names,
                    source=source,
                    original_variables=original_variables,
                )
            else:
                # --- 控制台模式 ---
                from mklink.vofa_viewer import JustFloatParser, _infer_channel_count, _infer_channel_names
                channel_count = _infer_channel_count(variables)
                ch_names = _infer_channel_names(variables, channel_count)
                parser = JustFloatParser(channel_count, ch_names)

                print(f"[OK] VOFA 已启动，采样周期 {period}s，通道数 {channel_count}")
                if duration > 0:
                    print(f"[*] 采集 {duration}s ...")
                else:
                    print("[*] 按 Ctrl+C 停止...")

                import time
                start = time.time()
                frame_count = 0
                try:
                    while True:
                        if duration > 0 and time.time() - start >= duration:
                            break
                        time.sleep(0.05)
                        raw = bridge.drain_stream_bytes()
                        if raw:
                            frames = parser.feed(raw)
                            for f in frames:
                                frame_count += 1
                                vals = " | ".join(f"{k}={f[k]:.4g}" for k in ch_names if k in f)
                                print(f"[{frame_count}] {vals}")
                except KeyboardInterrupt:
                    print("\n[*] 用户中断")

                print(f"[*] 共接收 {frame_count} 帧")
                if parser.dropped_frames:
                    print(f"[WARN] 丢弃 {parser.dropped_frames} 帧 ({parser.dropped_bytes} bytes)")

                # 停止
                bridge._exit_stream()
                stop_cmd = f'vofa.send({var_args}, 0)'
                bridge.send_command(stop_cmd, timeout=5.0)
                print("[OK] VOFA 已停止")
    except Exception as e:
        print(f"[FAIL] {e}")
        # 异常时尝试停止 VOFA 流，防止设备锁死在流模式
        if bridge.state in (DeviceState.VOFA_STREAM, DeviceState.READY):
            try:
                bridge._exit_stream()
                bridge.send_command('vofa.send(0x20000000, "uint8_t", 0)', timeout=3.0)
            except Exception:
                pass
    finally:
        bridge.close()


def _enable_utf8_console():
    """Windows 控制台 UTF-8 模式支持。"""
    import os
    if os.name == "nt":
        import sys
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Modbus RTU CLI 处理函数
# ---------------------------------------------------------------------------

def _modbus_resolve_defaults(args) -> bool:
    """用 config.json 中的 Modbus 默认值填充未指定的参数。

    --port 未指定时从 config 读取 modbus_port。
    --baud 未指定时（仍为默认 9600）从 config 读取 modbus_baud。
    --parity/--stopbits 同理。

    Returns:
        True 表示 port 已解析（来自参数或配置），False 表示无法解析。
    """
    if args.port:
        return True

    from mklink.project_config import load_config
    config = load_config(".")
    if config and config.get("modbus_port"):
        args.port = config["modbus_port"]
        if not hasattr(args, "_baud_explicit") and config.get("modbus_baud"):
            args.baud = config["modbus_baud"]
        if config.get("modbus_parity"):
            args.parity = config["modbus_parity"]
        if config.get("modbus_stopbits"):
            args.stopbits = config["modbus_stopbits"]
        print(f"[AUTO] 从配置读取 Modbus 串口: {args.port} @ {args.baud}bps")
        return True

    print("[FAIL] 未指定 --port 且 config.json 中无 modbus_port 配置")
    print("  用法: python -m mklink modbus read --port COM8 --slave 1 --fc 3 --start 0 --quantity 10")
    print("  配置后会自动记住串口参数，后续无需再指定 --port")
    return False


def _modbus_save_config(args):
    """Modbus 通信成功后，将串口参数持久化到 config.json。"""
    from mklink.project_config import load_config, save_config
    config = load_config(".")
    if config is None:
        config = {}
    updated = False
    for key, val in [("modbus_port", args.port), ("modbus_baud", args.baud),
                     ("modbus_parity", args.parity), ("modbus_stopbits", args.stopbits)]:
        if config.get(key) != val:
            config[key] = val
            updated = True
    if updated:
        save_config(".", config)


def _modbus_open_client(args):
    """从 argparse args 创建并打开 ModbusClient。"""
    if not _modbus_resolve_defaults(args):
        return None
    from mklink.modbus._client import ModbusClient
    client = ModbusClient(
        port=args.port,
        baudrate=args.baud,
        parity=args.parity,
        stopbits=args.stopbits,
        timeout=args.timeout,
        retries=args.retries,
    )
    if not client.open():
        return None
    return client


def _cli_modbus_scan(args):
    from mklink.modbus._scanner import scan_slaves
    client = _modbus_open_client(args)
    if not client:
        return
    try:
        print(f"[*] Modbus 从站扫描: {args.port} @ {args.baud}bps (地址 {args.start}-{args.end})")

        def on_progress(current, total, msg):
            pct = current * 100 // total
            status = f" {msg}" if msg else ""
            print(f"\r  [{pct:3d}%] {current}/{total}{status}", end="", flush=True)

        found = scan_slaves(
            client,
            start_addr=args.start,
            end_addr=args.end,
            on_progress=on_progress,
        )
        print()  # 换行
        if found:
            print(f"[OK] 发现 {len(found)} 个从站: {', '.join(str(a) for a in found)}")
            _modbus_save_config(args)
        else:
            print("[WARN] 未发现任何从站")
    finally:
        client.close()


def _cli_modbus_read(args):
    from mklink.modbus._format import format_registers, registers_to_values
    from mklink.modbus._client import ModbusError
    from pymodbus import ModbusException as PymodbusException

    client = _modbus_open_client(args)
    if not client:
        return
    try:
        fc = args.fc
        slave = args.slave
        start = args.start
        qty = args.quantity
        fmt = args.format

        try:
            if fc == 1:
                bits = client.read_coils(start, qty, slave)
                print(f"[OK] FC01 读 {qty} 个线圈 (从站 {slave}, 地址 {start}):")
                for i, b in enumerate(bits):
                    print(f"  {start + i:>6}: {fmt_on_off(b, fmt)}")
            elif fc == 2:
                bits = client.read_discrete_inputs(start, qty, slave)
                print(f"[OK] FC02 读 {qty} 个离散输入 (从站 {slave}, 地址 {start}):")
                for i, b in enumerate(bits):
                    print(f"  {start + i:>6}: {fmt_on_off(b, fmt)}")
            elif fc == 3:
                regs = client.read_holding_registers(start, qty, slave)
                print(f"[OK] FC03 读 {qty} 个保持寄存器 (从站 {slave}, 地址 {start}):")
                for i, v in enumerate(regs):
                    print(f"  {start + i:>6}: {_fmt_val(v, fmt)}")
                _modbus_save_config(args)
            elif fc == 4:
                regs = client.read_input_registers(start, qty, slave)
                print(f"[OK] FC04 读 {qty} 个输入寄存器 (从站 {slave}, 地址 {start}):")
                for i, v in enumerate(regs):
                    print(f"  {start + i:>6}: {_fmt_val(v, fmt)}")
                _modbus_save_config(args)
        except ModbusError as e:
            print(f"[FAIL] {e}")
        except PymodbusException as e:
            print(f"[FAIL] Modbus 通信错误: {e}")
    finally:
        client.close()


def _cli_modbus_write(args):
    from mklink.modbus._client import ModbusError
    from pymodbus import ModbusException as PymodbusException

    client = _modbus_open_client(args)
    if not client:
        return
    try:
        fc = args.fc
        slave = args.slave
        start = args.start
        values = args.values

        try:
            if fc == 5:
                v = _parse_bool(values[0])
                client.write_coil(start, v, slave)
                print(f"[OK] FC05 写单个线圈 (从站 {slave}, 地址 {start}): {'ON' if v else 'OFF'}")
            elif fc == 6:
                v = int(values[0], 0)
                client.write_register(start, v, slave)
                print(f"[OK] FC06 写单个寄存器 (从站 {slave}, 地址 {start}): {v} (0x{v:04X})")
            elif fc == 15:
                bits = [_parse_bool(v) for v in values]
                client.write_coils(start, bits, slave)
                print(f"[OK] FC15 写 {len(bits)} 个线圈 (从站 {slave}, 地址 {start})")
            elif fc == 16:
                regs = [int(v, 0) for v in values]
                client.write_registers(start, regs, slave)
                print(f"[OK] FC16 写 {len(regs)} 个寄存器 (从站 {slave}, 地址 {start})")
            _modbus_save_config(args)
        except ModbusError as e:
            print(f"[FAIL] {e}")
        except PymodbusException as e:
            print(f"[FAIL] Modbus 通信错误: {e}")
    finally:
        client.close()


def _cli_modbus_poll(args):
    from mklink.modbus._poller import poll_registers
    from mklink.modbus._format import parse_register_spec

    client = _modbus_open_client(args)
    if not client:
        return
    try:
        specs = parse_register_spec(args.registers)
        poll_registers(
            client, slave=args.slave, specs=specs,
            interval=args.interval, fmt=args.format, count=args.count,
        )
    finally:
        client.close()


def _cli_modbus_monitor(args):
    from mklink.modbus._monitor import monitor_traffic

    client = _modbus_open_client(args)
    if not client:
        return
    try:
        monitor_traffic(
            client, slave=args.slave,
            interval=args.interval, output_format=args.output_format,
            save_file=args.save,
        )
    finally:
        client.close()


def _cli_modbus_diag(args):
    from mklink.modbus._client import ModbusError
    from pymodbus import ModbusException as PymodbusException

    client = _modbus_open_client(args)
    if not client:
        return
    try:
        try:
            if args.subfunc == "exception-status":
                status = client.read_exception_status(args.slave)
                print(f"[OK] FC07 异常状态 (从站 {args.slave}): 0x{status:02X} (二进制: {status:08b})")
            elif args.subfunc == "mask-write":
                client.mask_write_register(
                    args.addr, args.and_mask, args.or_mask, args.slave,
                )
                print(f"[OK] FC22 掩码写寄存器 (从站 {args.slave}, 地址 {args.addr}):")
                print(f"  AND=0x{args.and_mask:04X}, OR=0x{args.or_mask:04X}")
            elif args.subfunc == "read-write":
                write_vals = args.write_values or []
                regs = client.read_write_registers(
                    read_address=args.addr, read_count=args.read_count,
                    write_address=args.addr, write_values=write_vals,
                    slave=args.slave,
                )
                print(f"[OK] FC23 读写多寄存器 (从站 {args.slave}, 读地址 {args.addr}, {args.read_count} 个):")
                for i, v in enumerate(regs):
                    print(f"  {args.addr + i:>6}: {v} (0x{v:04X})")
        except ModbusError as e:
            print(f"[FAIL] {e}")
        except PymodbusException as e:
            print(f"[FAIL] Modbus 通信错误: {e}")
    finally:
        client.close()


def _cli_modbus_dashboard(args):
    """启动 Modbus Web 可视化仪表盘。"""
    from mklink.modbus._profile import load_profile
    from mklink.modbus._dashboard import run_modbus_dashboard

    client = _modbus_open_client(args)
    if not client:
        return

    try:
        profile = load_profile(getattr(args, "profile", None))
        slave = getattr(args, "slave", 1)

        # Use profile baudrate if not overridden
        fast_interval = profile.get("poll_groups", {}).get("fast", {}).get("interval", 1.0)
        slow_interval = profile.get("poll_groups", {}).get("slow", {}).get("interval", 5.0)

        run_modbus_dashboard(
            client=client,
            slave=slave,
            profile=profile,
            host=args.host if hasattr(args, "host") else "127.0.0.1",
            port=args.port_http if hasattr(args, "port_http") else 0,
            no_browser=args.no_browser if hasattr(args, "no_browser") else False,
            max_points=args.max_points if hasattr(args, "max_points") else 500,
            fast_interval=fast_interval,
            slow_interval=slow_interval,
            duration=args.duration if hasattr(args, "duration") else 0,
            html_path=getattr(args, "html", None),
            allow_arbitrary_writes=getattr(args, "allow_arbitrary_writes", False),
        )
    except FileNotFoundError as e:
        print(f"[FAIL] {e}")
    except Exception as e:
        print(f"[FAIL] 启动仪表盘失败: {e}")
        client.close()


def _cli_modbus_pointmap_detect(args):
    """Detect a Modbus point table without writing generated files."""
    import json
    from mklink.modbus._pointmap import detect_pointmap

    pointmap = detect_pointmap(
        project_root=getattr(args, "project_root", "."),
        source=getattr(args, "source", None),
        fmt=getattr(args, "format", "auto"),
    )
    if getattr(args, "json", False):
        print(json.dumps(pointmap.to_jsonable(), ensure_ascii=False, indent=2))
        return

    summary = pointmap.summary()
    print("[OK] Modbus point table detection")
    print(f"  Source:   {summary['source_format']}")
    print(f"  Files:    {', '.join(summary['source_files']) if summary['source_files'] else 'none'}")
    print(f"  Points:   {summary['points']}")
    print(f"  Writable: {summary['writable']}")
    print(f"  Bits:     {summary['bitfields']}")
    print(f"  Commands: {summary['commands']}")
    for warning in summary.get("warnings", []):
        print(f"  [WARN] {warning}")


def _cli_modbus_pointmap_generate(args):
    """Generate dashboard profile and Markdown docs from a detected point table."""
    from pathlib import Path
    from mklink.modbus._pointmap import detect_pointmap, generate_pointmap_artifacts

    project_root = Path(getattr(args, "project_root", "."))
    output = Path(getattr(args, "output", None) or project_root / ".mklink" / "modbus_profile.json")
    doc = Path(getattr(args, "doc", None) or project_root / "docs" / "modbus_pointmap.md")
    pointmap = detect_pointmap(
        project_root=project_root,
        source=getattr(args, "source", None),
        fmt=getattr(args, "format", "auto"),
    )
    summary = pointmap.summary()
    print("[INFO] Modbus point table summary")
    print(f"  Source:   {summary['source_format']}")
    print(f"  Files:    {', '.join(summary['source_files']) if summary['source_files'] else 'none'}")
    print(f"  Points:   {summary['points']}")
    print(f"  Writable: {summary['writable']}")
    print(f"  Commands: {summary['commands']}")
    for warning in summary.get("warnings", []):
        print(f"  [WARN] {warning}")

    if not getattr(args, "yes", False):
        answer = input(f"Write {output} and {doc}? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("[CANCEL] Generation cancelled")
            return

    profile_path, doc_path = generate_pointmap_artifacts(pointmap, output, doc)
    print(f"[OK] Wrote profile: {profile_path}")
    print(f"[OK] Wrote document: {doc_path}")


def _cli_modbus_dispatch(args):
    """分发 modbus 子命令。"""
    if not hasattr(args, "modbus_command") or not args.modbus_command:
        from mklink.cli import _get_modbus_parser_help
        _get_modbus_parser_help()
        return
    dispatch = {
        "scan": _cli_modbus_scan,
        "read": _cli_modbus_read,
        "write": _cli_modbus_write,
        "poll": _cli_modbus_poll,
        "monitor": _cli_modbus_monitor,
        "diag": _cli_modbus_diag,
        "dashboard": _cli_modbus_dashboard,
        "pointmap": lambda ns: _cli_modbus_pointmap_detect(ns)
        if getattr(ns, "pointmap_command", None) == "detect"
        else _cli_modbus_pointmap_generate(ns)
        if getattr(ns, "pointmap_command", None) == "generate"
        else print("Usage: python -m mklink modbus pointmap <detect|generate> [options]"),
    }
    handler = dispatch.get(args.modbus_command)
    if handler:
        handler(args)
    else:
        print("用法: python -m mklink modbus <scan|read|write|poll|monitor|diag|dashboard> [选项]")


def _get_modbus_parser_help():
    """打印 modbus 帮助信息。"""
    print("用法: python -m mklink modbus <命令> [选项]")
    print()
    print("Modbus RTU 调试命令:")
    print("  scan      扫描从站地址 (1-247)")
    print("  read      读取寄存器/线圈 (FC01-04)")
    print("  write     写入寄存器/线圈 (FC05/06/15/16)")
    print("  poll      实时轮询寄存器（表格刷新）")
    print("  monitor   监控通信流量")
    print("  diag      诊断功能 (FC07/22/23)")
    print("  dashboard Web 可视化仪表盘（实时图表 + 交互控制）")
    print()
    print("示例:")
    print("  python -m mklink modbus scan --port COM7")
    print("  python -m mklink modbus read --port COM7 --slave 1 --fc 3 --start 0 --quantity 10")
    print("  python -m mklink modbus write --port COM7 --slave 1 --fc 6 --start 0 100")
    print("  python -m mklink modbus poll --port COM7 --slave 1 --registers \"0:uint16:Temp 1:float\"")
    print("  python -m mklink modbus dashboard --port COM7 --slave 1 --baud 57600")


def _fmt_val(v: int, fmt: str) -> str:
    """格式化寄存器值。"""
    if fmt == "hex":
        return f"0x{v:04X}"
    elif fmt == "bin":
        return f"{v:016b}"
    else:
        return str(v)


def _fmt_on_off(b: bool, fmt: str) -> str:
    """格式化线圈/离散输入值。"""
    if fmt == "hex":
        return "0xFF00" if b else "0x0000"
    return "ON" if b else "OFF"


def _parse_bool(s: str) -> bool:
    """解析布尔值字符串。"""
    return s.lower() in ("1", "on", "true", "yes", "0xff00")


def _cli_serial_dispatch(args):
    """串口调试命令分发。"""
    from mklink.serial._port import SerialPort, list_uart_ports, is_mklink_port
    from mklink.serial._profile import load_profile, find_profile, ProfileError
    from mklink.serial._monitor import SerialMonitor
    from mklink.serial._logger import FileLogger
    from mklink.serial._cli_mode import CLIMode
    from mklink.serial._dashboard import SerialDashboardServer
    from mklink.serial._profile_from_c import generate_profile_from_c
    from mklink.serial._autoreply import AutoReplyEngine, load_rules_from_file

    cmd = getattr(args, "serial_command", None)
    if not cmd:
        print("用法: python -m mklink serial <命令> [选项]")
        print()
        print("串口调试命令:")
        print("  list       列出可用 UART 端口")
        print("  open       交互式串口终端")
        print("  send       发送数据后退出")
        print("  monitor    多端口被动监听")
        print("  log        无头模式日志记录")
        print("  dashboard  Web 可视化 Dashboard")
        print("  profile    Profile 管理")
        print()
        print("示例:")
        print("  python -m mklink serial list")
        print("  python -m mklink serial open --port COM3 --baud 115200")
        print("  python -m mklink serial send --port COM3 --hex 01030000000A")
        print("  python -m mklink serial monitor --port COM3 --port COM4")
        print("  python -m mklink serial dashboard --port COM3 --profile frame.json")
        return

    if cmd == "list":
        ports = list_uart_ports()
        if not ports:
            print("[*] 未发现可用 UART 端口")
            return
        print(f"[OK] 发现 {len(ports)} 个 UART 端口:")
        for p in ports:
            tag = " [MKLink]" if p.get("is_mklink") else ""
            print(f"  {p['device']} — {p['description']}{tag}")

    elif cmd == "open":
        # Load profile if specified
        profile = None
        if args.profile:
            try:
                profile = load_profile(args.profile)
            except ProfileError as e:
                print(f"[FAIL] Profile 加载失败: {e}")
                return

        # Build port config
        port_config = [{
            "port": args.port,
            "baudrate": args.baud,
            "databits": args.databits,
            "stopbits": args.stop,
            "parity": args.parity,
        }]

        # Auto-reply rules
        auto_reply_rules = None
        if args.auto_reply:
            try:
                rules = load_rules_from_file(args.auto_reply)
                auto_reply_rules = [{"match_hex": r.match_hex, "match_regex": r.match_regex,
                                     "match_contains": r.match_contains, "reply_hex": r.reply_hex,
                                     "reply_ascii": r.reply_ascii, "delay": r.delay}
                                    for r in rules]
            except Exception as e:
                print(f"[WARN] 自动应答规则加载失败: {e}")

        # Logger
        logger = None
        if args.log:
            log_format = "csv" if args.log.endswith(".csv") else "txt"
            logger = FileLogger(args.log, format=log_format)
            logger.start()

        # Create monitor and run CLI mode
        monitor = SerialMonitor(
            ports=port_config,
            profile=profile,
            auto_reply_rules=auto_reply_rules,
            logger=logger,
        )

        cli = CLIMode(
            monitor=monitor,
            mode=args.mode,
            filter_pattern=args.filter,
        )

        try:
            monitor.start()
            cli.run()
        except KeyboardInterrupt:
            pass
        finally:
            monitor.stop()
            if logger:
                logger.close()

    elif cmd == "send":
        port = SerialPort(args.port, baudrate=args.baud)
        if not port.open():
            print(f"[FAIL] 无法打开端口 {args.port}")
            return
        try:
            if args.hex:
                data = bytes.fromhex(args.send_data.replace(" ", ""))
            else:
                data = args.send_data.encode("utf-8")

            import time
            for i in range(args.count):
                port.write(data)
                if args.count > 1:
                    print(f"[TX] #{i+1}/{args.count}: {data.hex(' ') if args.hex else args.send_data}")
                    if i < args.count - 1:
                        time.sleep(args.delay)
                else:
                    print(f"[TX] {data.hex(' ') if args.hex else args.send_data}")
        finally:
            port.close()

    elif cmd == "monitor":
        profile = None
        if args.profile:
            try:
                profile = load_profile(args.profile)
            except ProfileError as e:
                print(f"[FAIL] Profile 加载失败: {e}")
                return

        port_configs = [{"port": p, "baudrate": args.baud, "databits": args.databits,
                         "stopbits": args.stop, "parity": args.parity} for p in args.port]

        logger = None
        if args.log:
            log_format = "csv" if args.log.endswith(".csv") else "txt"
            logger = FileLogger(args.log, format=log_format)
            logger.start()

        monitor = SerialMonitor(
            ports=port_configs,
            profile=profile,
            logger=logger,
        )

        cli = CLIMode(
            monitor=monitor,
            mode=args.mode,
            filter_pattern=args.filter,
        )

        try:
            monitor.start()
            cli.run()
        except KeyboardInterrupt:
            pass
        finally:
            monitor.stop()
            if logger:
                logger.close()

    elif cmd == "log":
        port_config = [{"port": args.port, "baudrate": args.baud,
                        "databits": args.databits, "stopbits": args.stop, "parity": args.parity}]

        log_format = args.format if args.format else ("csv" if args.output.endswith(".csv") else "txt")
        logger = FileLogger(args.output, format=log_format)
        logger.start()

        profile = None
        if args.profile:
            try:
                profile = load_profile(args.profile)
            except ProfileError as e:
                print(f"[FAIL] Profile 加载失败: {e}")
                return

        monitor = SerialMonitor(ports=port_config, profile=profile, logger=logger)

        import time
        try:
            monitor.start()
            print(f"[OK] 日志记录中: {args.output} (Ctrl+C 停止)")
            if args.duration > 0:
                time.sleep(args.duration)
            else:
                while True:
                    time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            monitor.stop()
            logger.close()
            print(f"[OK] 日志已保存: {args.output}")

    elif cmd == "dashboard":
        profile = None
        if args.profile:
            try:
                profile = load_profile(args.profile)
            except ProfileError as e:
                print(f"[FAIL] Profile 加载失败: {e}")
                return

        port_configs = [{"port": p, "baudrate": args.baud, "databits": args.databits,
                         "stopbits": args.stop, "parity": args.parity} for p in args.port]

        monitor = SerialMonitor(ports=port_configs, profile=profile)

        dashboard = SerialDashboardServer(
            monitor=monitor,
            host=args.host,
            port=args.port_http,
            open_browser=not args.no_browser,
        )

        try:
            monitor.start()
            dashboard.run_forever()
        except KeyboardInterrupt:
            pass
        finally:
            dashboard.stop()
            monitor.stop()

    elif cmd == "profile":
        pcmd = getattr(args, "profile_command", None)
        if pcmd == "detect":
            try:
                profile = generate_profile_from_c(args.source, struct_name=args.struct)
                import json
                print(json.dumps(profile, indent=2, ensure_ascii=False))
            except (ValueError, FileNotFoundError) as e:
                print(f"[FAIL] {e}")
        elif pcmd == "generate":
            try:
                profile = generate_profile_from_c(args.source, struct_name=args.struct)
                from mklink.serial._profile import save_profile
                output = args.output or ".mklink/serial_profile.json"
                import os
                os.makedirs(os.path.dirname(output) or ".", exist_ok=True)
                save_profile(profile, output)
                print(f"[OK] Profile 已生成: {output}")
            except (ValueError, FileNotFoundError) as e:
                print(f"[FAIL] {e}")
        elif pcmd == "show":
            import json
            path = args.profile or find_profile(".")
            if not path:
                print("[FAIL] 未找到 Profile 文件")
                return
            try:
                profile = load_profile(path)
                print(f"Profile: {path}")
                print(json.dumps(profile, indent=2, ensure_ascii=False))
            except ProfileError as e:
                print(f"[FAIL] {e}")
        else:
            print("[FAIL] 请指定 profile 子命令: detect, generate, show")
    else:
        print("[FAIL] 未知的 serial 子命令，使用 --help 查看帮助")


# --- CPU Debug Control CLI handlers ---

def _cli_halt(port: str | None):
    from mklink.bridge import MKLinkSerialBridge
    from mklink.debug_control import halt_cpu, read_debug_state
    port = port or _auto_detect_port()
    if not port:
        return
    bridge = MKLinkSerialBridge(port)
    try:
        bridge.connect()
        state = halt_cpu(bridge)
        if state.halted:
            print(f"[OK] CPU 已停止 (DHCSR=0x{state.dhcsr_raw:08X})")
        else:
            print(f"[WARN] 写入 halt 命令但 S_HALT 未置位 (DHCSR=0x{state.dhcsr_raw:08X})")
    finally:
        bridge.close()


def _cli_resume(port: str | None):
    from mklink.bridge import MKLinkSerialBridge
    from mklink.debug_control import resume_cpu
    port = port or _auto_detect_port()
    if not port:
        return
    bridge = MKLinkSerialBridge(port)
    try:
        bridge.connect()
        state = resume_cpu(bridge)
        if not state.halted:
            print(f"[OK] CPU 已恢复运行 (DHCSR=0x{state.dhcsr_raw:08X})")
        else:
            print(f"[WARN] 写入 resume 命令但 CPU 仍处于 halt (DHCSR=0x{state.dhcsr_raw:08X})")
    finally:
        bridge.close()


def _cli_step(port: str | None):
    from mklink.bridge import MKLinkSerialBridge
    from mklink.debug_control import step_cpu
    port = port or _auto_detect_port()
    if not port:
        return
    bridge = MKLinkSerialBridge(port)
    try:
        bridge.connect()
        state = step_cpu(bridge)
        print(f"[OK] 单步执行完成 (DHCSR=0x{state.dhcsr_raw:08X}, halted={state.halted})")
    finally:
        bridge.close()


def _cli_break(args):
    from mklink.bridge import MKLinkSerialBridge
    from mklink.debug_control import (
        set_breakpoint, clear_breakpoint, clear_all_breakpoints,
        read_debug_state, get_num_breakpoints,
    )
    port = args.port or _auto_detect_port()
    if not port:
        return
    bridge = MKLinkSerialBridge(port)
    try:
        bridge.connect()

        # --status: show debug state
        if args.status:
            state = read_debug_state(bridge)
            print(f"CPU: {'HALTED' if state.halted else 'RUNNING'} (DHCSR=0x{state.dhcsr_raw:08X})")
            print(f"FPB: {state.num_breakpoints} 个比较器")
            if state.breakpoints:
                for bp in state.breakpoints:
                    print(f"  [{bp.index}] 0x{bp.address:08X} (enabled)")
            else:
                print("  无活跃断点")
            return

        # --list: list breakpoints
        if args.list:
            state = read_debug_state(bridge)
            print(f"FPB 硬件断点 ({state.num_breakpoints} 个槽位):")
            if state.breakpoints:
                for bp in state.breakpoints:
                    print(f"  [{bp.index}] 0x{bp.address:08X}")
            else:
                print("  无活跃断点")
            return

        # --clear: clear breakpoints
        if args.clear is not None:
            if args.clear == "all":
                n = clear_all_breakpoints(bridge)
                print(f"[OK] 已清除 {n} 个断点")
            else:
                try:
                    slot = int(args.clear)
                except ValueError:
                    print(f"[FAIL] 无效槽位号: {args.clear}")
                    return
                clear_breakpoint(bridge, slot)
                print(f"[OK] 已清除断点 [{slot}]")
            return

        # Set breakpoint
        if not args.target:
            print("[FAIL] 请指定断点目标（函数名或地址），或使用 --list / --clear / --status")
            return

        target = args.target
        # Try parsing as hex address
        address = None
        if target.startswith("0x") or target.startswith("0X"):
            try:
                address = int(target, 16)
            except ValueError:
                pass

        # If not an address, resolve as function symbol
        if address is None:
            source = args.source
            if not source:
                # Try to find AXF from project config
                from mklink.project_config import load_config
                config = load_config(".")
                if config and config.get("axf_path"):
                    source = config["axf_path"]
            if not source:
                print(f"[FAIL] 需要 --source 指定 AXF 文件来解析函数名 '{target}'")
                return

            import subprocess
            from mklink.symbol_parser import resolve_function_address
            try:
                result = subprocess.run(
                    ["arm-none-eabi-readelf", "-s", source],
                    capture_output=True, text=True, timeout=10
                )
                if result.returncode != 0:
                    print(f"[FAIL] readelf 失败: {result.stderr.strip()}")
                    return
                address = resolve_function_address(result.stdout, target)
            except FileNotFoundError:
                # Try fromelf for ARM Compiler
                try:
                    result = subprocess.run(
                        ["fromelf", "--text", "-s", source],
                        capture_output=True, text=True, timeout=10
                    )
                    if result.returncode == 0:
                        address = resolve_function_address(result.stdout, target)
                except FileNotFoundError:
                    print("[FAIL] 未找到 arm-none-eabi-readelf 或 fromelf")
                    return

            if address is None:
                # Try fuzzy match
                from mklink.symbol_parser import parse_readelf_functions
                funcs = parse_readelf_functions(result.stdout)
                similar = [f["name"] for f in funcs if target.lower() in f["name"].lower()][:5]
                print(f"[FAIL] 未找到函数 '{target}'")
                if similar:
                    print(f"  相似函数: {', '.join(similar)}")
                return

        try:
            slot = set_breakpoint(bridge, address, args.slot)
            print(f"[OK] 断点已设置: [{slot}] @ 0x{address:08X}", end="")
            if target and not target.startswith("0x"):
                print(f" ({target})")
            else:
                print()
        except (ValueError, RuntimeError) as e:
            print(f"[FAIL] {e}")

    finally:
        bridge.close()


def _cli_gui(args):
    """启动 MKLink GUI（构建前端 + FastAPI 服务器 + 浏览器）。"""
    from mklink._deps import require_gui_dependencies
    require_gui_dependencies()

    import os
    import subprocess
    import webbrowser
    from pathlib import Path

    skill_dir = Path(__file__).resolve().parent.parent
    gui_dir = skill_dir / "gui"
    dist_dir = gui_dir / "dist"

    # 解析项目目录：优先用 --project-root 参数，否则用当前工作目录
    project_root = os.path.abspath(args.project_root) if args.project_root != "." else os.getcwd()

    # 1. 检查/构建前端
    if not dist_dir.is_dir() or not (dist_dir / "index.html").exists():
        print("[MKLink] 前端未构建，正在构建...")
        if not (gui_dir / "node_modules").is_dir():
            print("[MKLink] 安装 npm 依赖...")
            subprocess.run(["npm", "install"], cwd=str(gui_dir), check=True)
        subprocess.run(["npm", "run", "build"], cwd=str(gui_dir), check=True)
        print("[MKLink] 前端构建完成")

    # 2. 创建 FastAPI app（挂载了 Vue 静态文件）
    from mklink.remote.api import create_app, run_server
    app = create_app(project_root=project_root)

    # 3. 打开浏览器
    url = f"http://{args.host}:{args.port}"
    if not args.no_browser:
        import threading
        def _open():
            import time
            time.sleep(1.5)
            webbrowser.open(url)
        threading.Thread(target=_open, daemon=True).start()

    print(f"[MKLink] GUI 启动中...")
    print(f"[MKLink] 项目目录: {project_root}")
    print(f"[MKLink] 浏览器地址: {url}")
    print(f"[MKLink] API 文档: {url}/docs")
    print(f"[MKLink] 按 Ctrl+C 退出")

    run_server(
        app, host=args.host, port=args.port,
        project_root=project_root,
    )


def _cli_serve(args):
    """启动远程调试服务器。"""
    from mklink._deps import require_gui_dependencies
    backend = getattr(args, "backend", "fastapi")
    if backend == "fastapi":
        require_gui_dependencies()
        from mklink.remote.api import create_app, run_server
        app = create_app(
            auth_token=args.token,
            project_root=args.project_root,
        )
        print(f"[MKLink] Starting FastAPI server on {args.host}:{args.port}")
        print(f"[MKLink] Backend: fastapi | Auth: {'enabled' if args.token else 'disabled'}")
        print(f"[MKLink] API docs: http://{args.host}:{args.port}/docs")
        run_server(
            app, host=args.host, port=args.port,
            device_port=args.device_port, axf=args.axf,
            project_root=args.project_root,
        )
    else:
        from mklink.remote.server import serve
        serve(
            host=args.host, port=args.port,
            auth_token=args.token,
            device_port=args.device_port, axf=args.axf,
        )


def main():
    """CLI 入口，首先执行依赖检查。"""
    # Windows 控制台 UTF-8 支持
    _enable_utf8_console()

    # 第一步：依赖预检
    require_dependencies()

    # 延迟导入（依赖检查通过后再导入 pyserial 相关模块）
    from mklink.discovery import find_mklink_cdc_port, list_available_ports
    from mklink.rtt_addr import diagnose_rtt_addr, find_rtt_addr_from_map
    from mklink.autostart import generate_autostart_config

    parser = argparse.ArgumentParser(
        prog="mklink",
        description="MKLink Flash Programmer CLI",
    )
    subparsers = parser.add_subparsers(dest="command")

    # test 子命令
    test_parser = subparsers.add_parser("test", help="基本连接测试")
    test_parser.add_argument("--port", required=True)
    test_parser.add_argument("--baud", type=int, default=DEFAULT_BAUDRATE)

    # discover 子命令
    disc_parser = subparsers.add_parser("discover", help="查找 MKLink CDC 端口")
    disc_parser.add_argument("--list", action="store_true", help="列出所有端口")

    # rtt-find 子命令
    rtt_parser = subparsers.add_parser("rtt-find", help="从 map/elf 查找 RTT 地址")
    rtt_parser.add_argument("path", help=".map 或 .elf 文件路径")

    # autostart 子命令
    auto_parser = subparsers.add_parser("autostart", help="生成上电自动启动 RTT 配置")
    auto_parser.add_argument("--addr", required=True)
    auto_parser.add_argument("--size", type=int, default=1024)
    auto_parser.add_argument("--channel", type=int, default=0)

    def _add_project_root_arg(parser):
        """为子命令添加 project-root 参数（同时支持选项和位置参数）。"""
        parser.add_argument("--project-root", default=".", help="项目根目录")
        parser.add_argument("project_root_positional", nargs="?", default=None, help="项目根目录（位置参数，等同于 --project-root）")

    def _resolve_project_root(args):
        """从参数中解析 project_root，优先使用位置参数。"""
        return args.project_root if args.project_root != "." else args.project_root_positional or "."

    # keil-parse 子命令
    keil_parser_cmd = subparsers.add_parser("keil-parse", help="解析 Keil .uvprojx 工程文件")
    _add_project_root_arg(keil_parser_cmd)

    # iar-parse 子命令
    iar_parser_cmd = subparsers.add_parser("iar-parse", help="解析 IAR .ewp 工程文件")
    _add_project_root_arg(iar_parser_cmd)

    # project-init 子命令
    init_parser = subparsers.add_parser("project-init", help="初始化项目配置（解析 Keil 工程 + 检测 RTT）")
    _add_project_root_arg(init_parser)

    # project-info 子命令
    info_parser = subparsers.add_parser("project-info", help="显示项目已缓存的配置")
    _add_project_root_arg(info_parser)

    # rtt-integrate 子命令
    rtt_int_parser = subparsers.add_parser("rtt-integrate", help="集成 SEGGER RTT 源文件到项目")
    _add_project_root_arg(rtt_int_parser)
    rtt_int_parser.add_argument("--src-dir", help="源文件目录（默认自动检测）")
    rtt_int_parser.add_argument("--inc-dir", help="头文件目录（默认自动检测）")
    rtt_int_parser.add_argument("--force", action="store_true", help="强制重新集成（即使已集成）")

    # copy-flm 子命令
    copy_flm_parser = subparsers.add_parser("copy-flm", help="拷贝 FLM 文件到 MICROKEEN 磁盘")
    _add_project_root_arg(copy_flm_parser)

    # flash 子命令（一站式烧录）
    flash_parser = subparsers.add_parser("flash", help="一站式烧录（自动连接 → IDCODE → FLM → 烧录）")
    _add_project_root_arg(flash_parser)
    flash_parser.add_argument("--port", help="COM 端口（默认自动检测）")
    flash_parser.add_argument("--hex", help="HEX 文件路径（默认从 .mklink/ 配置读取）")

    # rtt 子命令（一站式 RTT 捕获）
    rtt_cmd_parser = subparsers.add_parser("rtt", help="一站式 RTT 捕获（自动连接 → 启动 RTT → 读取输出）")
    _add_project_root_arg(rtt_cmd_parser)
    rtt_cmd_parser.add_argument("--port", help="COM 端口（默认自动检测）")
    rtt_cmd_parser.add_argument("--duration", type=float, default=10.0, help="读取时长（秒，默认 10）")
    # --visualize 及相关选项
    rtt_cmd_parser.add_argument("--visualize", action="store_true", help="启用 Web RAW 终端模式")
    rtt_cmd_parser.add_argument("--host", default="127.0.0.1", help="HTTP 服务器绑定地址（默认 127.0.0.1）")
    rtt_cmd_parser.add_argument("--port-http", type=int, default=0, help="HTTP 服务器端口（默认 0 = 随机）")
    rtt_cmd_parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    rtt_cmd_parser.add_argument("--source", help="ELF/AXF path accepted for HIL smoke compatibility")

    # read-ram 子命令
    read_ram_parser = subparsers.add_parser("read-ram", help="读取目标芯片 RAM 数据")
    read_ram_parser.add_argument("--port", help="COM 端口（默认自动检测）")
    read_ram_parser.add_argument("--addr", required=True, help="读取地址（如 0x20000000）")
    read_ram_parser.add_argument("--size", type=int, default=256, help="读取字节数（默认 256）")
    read_ram_parser.add_argument("--save", help="保存到设备文件（如 ram.bin）")

    # read-reg 子命令
    read_reg_parser = subparsers.add_parser("read-reg", help="读取内存映射寄存器（复用 cmd.read_ram）")
    read_reg_parser.add_argument("register", nargs="?", help="寄存器名（如 SCB.CFSR）")
    read_reg_parser.add_argument("--port", help="COM 端口（默认自动检测）")
    read_reg_parser.add_argument("--addr", help="寄存器地址（如 0xE000ED28）")
    read_reg_parser.add_argument("--width", type=int, choices=[8, 16, 32], default=32, help="位宽（默认 32）")
    read_reg_parser.add_argument("--count", type=int, default=1, help="连续读取数量（默认 1）")
    read_reg_parser.add_argument("--format", choices=["hex", "dec", "bin", "both"], default="both", help="显示格式")
    read_reg_parser.add_argument("--raw", action="store_true", help="直接输出设备原始响应")

    # write-ram 子命令
    write_ram_parser = subparsers.add_parser("write-ram", help="写入数据到目标芯片 RAM 并回读验证")
    write_ram_parser.add_argument("--port", help="COM 端口（默认自动检测）")
    write_ram_parser.add_argument("--addr", required=True, help="写入地址（如 0x20001000）")
    write_ram_parser.add_argument("data", nargs="+", help="待写入的字节（如 0xDE 0xAD 0xBE 0xEF）")

    # read-flash 子命令
    read_flash_parser = subparsers.add_parser("read-flash", help="读取目标芯片 Flash 数据（自动加载 FLM）")
    _add_project_root_arg(read_flash_parser)
    read_flash_parser.add_argument("--port", help="COM 端口（默认自动检测）")
    read_flash_parser.add_argument("--addr", default="0x08000000", help="读取地址（默认 0x08000000）")
    read_flash_parser.add_argument("--size", type=int, default=128, help="读取字节数（默认 128）")
    read_flash_parser.add_argument("--save", help="保存到设备文件（如 flash.bin）")

    # vofa 子命令
    vofa_parser = subparsers.add_parser("vofa", help="VOFA+ 实时变量观测（启动/停止）")
    vofa_parser.add_argument("--port", help="COM 端口（默认自动检测）")
    vofa_parser.add_argument("--period", type=float, default=0.001, help="采样周期（秒，默认 0.001）")
    vofa_parser.add_argument("--stop", action="store_true", help="停止 VOFA 观测")
    vofa_parser.add_argument("--visualize", action="store_true", help="启动 Web 可视化仪表盘")
    vofa_parser.add_argument("--host", default="127.0.0.1", help="HTTP 服务器绑定地址（默认 127.0.0.1）")
    vofa_parser.add_argument("--port-http", type=int, default=0, help="HTTP 端口（默认随机可用端口）")
    vofa_parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    vofa_parser.add_argument("--max-points", type=int, default=500, help="图表最大数据点数（默认 500）")
    vofa_parser.add_argument("--duration", type=float, default=30.0, help="可视化运行时长（秒，默认 30）")
    vofa_parser.add_argument("--names", help="通道名称，逗号分隔（如 ntc_temp,comp_coeff）")
    vofa_parser.add_argument("--source", help="ELF/AXF 文件路径，用于变量名/struct.field 解析")
    vofa_parser.add_argument("variables", nargs="*", help="变量列表: 地址 类型 地址 类型 ...（如 0x20000030 uint8_t 0x2000154c float）")

    # symbols 子命令（符号浏览器）
    symbols_parser = subparsers.add_parser("symbols", help="Browse symbols from ELF/AXF file")
    symbols_parser.add_argument("--source", required=True, help="ELF/AXF file path")
    symbols_parser.add_argument("--filter", default=None, help="Regex pattern to filter symbol names")

    # hardfault 子命令
    hardfault_parser = subparsers.add_parser("hardfault", help="读取并解码 Cortex-M HardFault 寄存器/栈帧")
    hardfault_parser.add_argument("--port", help="COM 端口（默认自动检测）")
    hardfault_parser.add_argument("--source", help="ELF/AXF 文件路径，用于 addr2line")
    hardfault_parser.add_argument("--sp", help="异常栈帧地址（MSP/PSP 值）；未指定时只读 Fault 寄存器")

    # typeinfo 子命令
    typeinfo_parser = subparsers.add_parser("typeinfo", help="查询 AXF DWARF 类型信息")
    _add_project_root_arg(typeinfo_parser)
    typeinfo_parser.add_argument("--source", help="ELF/AXF 文件路径")
    typeinfo_parser.add_argument("--var", help="变量名")
    typeinfo_parser.add_argument("--struct", help="结构体名")
    typeinfo_parser.add_argument("--enum", help="枚举名")
    typeinfo_parser.add_argument("--list-structs", action="store_true", help="列出结构体")
    typeinfo_parser.add_argument("--list-enums", action="store_true", help="列出枚举")
    typeinfo_parser.add_argument("--json", action="store_true", help="JSON 输出")

    # memmap 子命令
    memmap_parser = subparsers.add_parser("memmap", help="分析 AXF 段表和 RAM/Flash 占用")
    _add_project_root_arg(memmap_parser)
    memmap_parser.add_argument("--source", help="ELF/AXF 文件路径")
    memmap_parser.add_argument("--top", type=int, default=0, help="预留：显示前 N 个大符号")
    memmap_parser.add_argument("--json", action="store_true", help="JSON 输出")

    # watch 子命令
    watch_parser = subparsers.add_parser("watch", help="读取变量快照，支持 DWARF 类型解码")
    watch_parser.add_argument("--project-root", default=".", help="项目根目录")
    watch_parser.add_argument("variables", nargs="*", help="变量名，支持逗号分隔和 struct.field")
    watch_parser.add_argument("--port", help="COM 端口（默认自动检测）")
    watch_parser.add_argument("--source", help="ELF/AXF 文件路径")
    watch_parser.add_argument("--period", type=float, default=0.0, help="周期刷新秒数；0 表示单次读取")
    watch_parser.add_argument("--profile", help="watch profile JSON，格式: {\"variables\": [...]}")
    watch_parser.add_argument("--struct", action="store_true", help="预留：展开结构体字段")
    watch_parser.add_argument("--json", action="store_true", help="JSON 输出")

    # ---- Modbus RTU 子命令组 ----
    superwatch_parser = subparsers.add_parser("superwatch", help="SuperWatch read_ram timestamped viewer")
    superwatch_parser.add_argument("variables", nargs="*", help="variables, struct.field paths, or registers")
    superwatch_parser.add_argument("--project-root", default=".", help="project root")
    superwatch_parser.add_argument("--port", help="COM port")
    superwatch_parser.add_argument("--source", help="ELF/AXF path for DWARF variable resolution")
    superwatch_parser.add_argument("--svd", help="CMSIS-SVD path; auto-detected from Keil Pack when omitted")
    superwatch_parser.add_argument("--period", type=float, default=0.1, help="sampling period in seconds")
    superwatch_parser.add_argument("--visualize", action="store_true", help="start Web visualizer")
    superwatch_parser.add_argument("--host", default="127.0.0.1", help="HTTP bind host")
    superwatch_parser.add_argument("--port-http", type=int, default=0, help="HTTP port, 0=random")
    superwatch_parser.add_argument("--no-browser", action="store_true", help="do not open browser")
    superwatch_parser.add_argument("--max-points", type=int, default=500, help="maximum chart points")
    superwatch_parser.add_argument("--duration", type=float, default=30.0, help="run duration seconds; 0=forever")
    superwatch_parser.add_argument("--dump-mem", action="store_true",
        help="use dump_mem binary streaming protocol for higher throughput")

    modbus_parser = subparsers.add_parser(
        "modbus", help="Modbus RTU 调试（扫描、读写、轮询、监控）"
    )
    modbus_sub = modbus_parser.add_subparsers(dest="modbus_command")

    def _add_modbus_serial_args(p):
        """添加 Modbus 共用串口参数。"""
        p.add_argument("--port", default=None, help="Modbus 串口（如 COM8）；未指定时从 config.json 读取 modbus_port")
        p.add_argument("--baud", type=int, default=9600, help="波特率（默认 9600）")
        p.add_argument("--parity", choices=["N", "E", "O"], default="N", help="校验位（N=无 E=偶 O=奇，默认 N）")
        p.add_argument("--stopbits", type=int, choices=[1, 2], default=1, help="停止位（默认 1）")
        p.add_argument("--timeout", type=float, default=1.0, help="响应超时秒数（默认 1.0）")
        p.add_argument("--retries", type=int, default=3, help="超时重试次数（默认 3）")

    # modbus scan
    modbus_scan = modbus_sub.add_parser("scan", help="扫描 Modbus 从站地址")
    _add_modbus_serial_args(modbus_scan)
    modbus_scan.add_argument("--start", type=int, default=1, help="起始地址（默认 1）")
    modbus_scan.add_argument("--end", type=int, default=247, help="结束地址（默认 247）")

    # modbus read
    modbus_read = modbus_sub.add_parser("read", help="读取寄存器/线圈")
    _add_modbus_serial_args(modbus_read)
    modbus_read.add_argument("--slave", type=int, required=True, help="从站地址 (1-247)")
    modbus_read.add_argument("--fc", type=int, required=True, choices=[1, 2, 3, 4],
                             help="功能码: 1=线圈 2=离散输入 3=保持寄存器 4=输入寄存器")
    modbus_read.add_argument("--start", type=int, required=True, help="起始地址")
    modbus_read.add_argument("--quantity", type=int, default=1, help="数量（默认 1）")
    modbus_read.add_argument("--format", choices=["dec", "hex", "bin", "float"], default="dec",
                             help="显示格式（默认 dec）")

    # modbus write
    modbus_write = modbus_sub.add_parser("write", help="写入寄存器/线圈")
    _add_modbus_serial_args(modbus_write)
    modbus_write.add_argument("--slave", type=int, required=True, help="从站地址 (1-247)")
    modbus_write.add_argument("--fc", type=int, required=True, choices=[5, 6, 15, 16],
                              help="功能码: 5=单线圈 6=单寄存器 15=多线圈 16=多寄存器")
    modbus_write.add_argument("--start", type=int, required=True, help="起始地址")
    modbus_write.add_argument("values", nargs="+", help="写入值（如 100 或 0x64）")

    # modbus poll
    modbus_poll = modbus_sub.add_parser("poll", help="轮询寄存器（实时表格）")
    _add_modbus_serial_args(modbus_poll)
    modbus_poll.add_argument("--slave", type=int, required=True, help="从站地址 (1-247)")
    modbus_poll.add_argument("--registers", required=True,
                             help="寄存器列表: 地址:类型[:名称] 空格分隔（如 0:uint16:Temp 1:float）")
    modbus_poll.add_argument("--interval", type=float, default=1.0, help="轮询间隔秒数（默认 1.0）")
    modbus_poll.add_argument("--format", choices=["dec", "hex", "bin", "float"], default="dec",
                             help="显示格式（默认 dec）")
    modbus_poll.add_argument("--count", type=int, default=None, help="轮询次数（默认无限，Ctrl+C 停止）")

    # modbus monitor
    modbus_monitor = modbus_sub.add_parser("monitor", help="监控 Modbus 通信流量")
    _add_modbus_serial_args(modbus_monitor)
    modbus_monitor.add_argument("--slave", type=int, default=1, help="监控的从站地址（默认 1）")
    modbus_monitor.add_argument("--interval", type=float, default=2.0, help="探测间隔秒数（默认 2.0）")
    modbus_monitor.add_argument("--output-format", choices=["decoded", "hex", "both"], default="decoded",
                                help="输出格式（默认 decoded）")
    modbus_monitor.add_argument("--save", help="保存日志到文件")

    # modbus diag
    modbus_diag = modbus_sub.add_parser("diag", help="Modbus 诊断（FC07/08/22/23）")
    _add_modbus_serial_args(modbus_diag)
    modbus_diag.add_argument("--slave", type=int, required=True, help="从站地址 (1-247)")
    modbus_diag.add_argument("--subfunc", choices=["exception-status", "mask-write", "read-write"],
                             default="exception-status", help="诊断功能（默认 exception-status）")
    modbus_diag.add_argument("--addr", type=int, default=0, help="寄存器地址（mask-write/read-write 用）")
    modbus_diag.add_argument("--and-mask", type=lambda x: int(x, 0), default=0xFFFF, help="AND 掩码（默认 0xFFFF）")
    modbus_diag.add_argument("--or-mask", type=lambda x: int(x, 0), default=0x0000, help="OR 掩码（默认 0x0000）")
    modbus_diag.add_argument("--write-values", type=lambda x: [int(v.strip(), 0) for v in x.split(",")],
                             default=None, help="写入值，逗号分隔（read-write 用）")
    modbus_diag.add_argument("--read-count", type=int, default=10, help="读取数量（read-write 用，默认 10）")

    # modbus dashboard
    modbus_dashboard = modbus_sub.add_parser("dashboard", help="Web 可视化仪表盘（实时图表 + 交互控制）")
    _add_modbus_serial_args(modbus_dashboard)
    modbus_dashboard.add_argument("--slave", type=int, default=1, help="从站地址（默认 1）")
    modbus_dashboard.add_argument("--profile", default=None, help="寄存器配置文件路径（默认自动加载）")
    modbus_dashboard.add_argument("--host", default="127.0.0.1", help="HTTP 绑定地址（默认 127.0.0.1）")
    modbus_dashboard.add_argument("--port-http", type=int, default=0, help="HTTP 端口（默认随机）")
    modbus_dashboard.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    modbus_dashboard.add_argument("--max-points", type=int, default=500, help="图表最大数据点（默认 500）")
    modbus_dashboard.add_argument("--duration", type=float, default=0, help="运行时长秒（默认无限，Ctrl+C 停止）")
    modbus_dashboard.add_argument("--html", default=None, help="自定义仪表盘 HTML 文件路径（默认从 .mklink/modbus_dashboard.html 加载）")

    modbus_dashboard.add_argument("--allow-arbitrary-writes", action="store_true",
                                  help="Allow dashboard debug writes outside profile-writable addresses")

    # modbus pointmap
    modbus_pointmap = modbus_sub.add_parser("pointmap", help="Detect or generate Modbus point-table profile/docs")
    pointmap_sub = modbus_pointmap.add_subparsers(dest="pointmap_command")
    pm_detect = pointmap_sub.add_parser("detect", help="Detect point table without writing files")
    pm_detect.add_argument("--project-root", default=".", help="Project root")
    pm_detect.add_argument("--source", default=None, help="Explicit C/Markdown/CSV source file")
    pm_detect.add_argument("--format", choices=["auto", "c", "markdown", "csv"], default="auto")
    pm_detect.add_argument("--json", action="store_true", help="Print JSON detection result")
    pm_generate = pointmap_sub.add_parser("generate", help="Generate .mklink/modbus_profile.json and docs")
    pm_generate.add_argument("--project-root", default=".", help="Project root")
    pm_generate.add_argument("--source", default=None, help="Explicit C/Markdown/CSV source file")
    pm_generate.add_argument("--format", choices=["auto", "c", "markdown", "csv"], default="auto")
    pm_generate.add_argument("--output", default=None, help="Profile output path")
    pm_generate.add_argument("--doc", default=None, help="Markdown documentation output path")
    pm_generate.add_argument("--yes", action="store_true", help="Write files without prompting")

    # ─── serial 串口调试 ───────────────────────────────────────────────
    serial_parser = subparsers.add_parser(
        "serial", help="通用串口调试（收发、监控、Dashboard）"
    )
    serial_sub = serial_parser.add_subparsers(dest="serial_command")

    def _add_serial_port_args(p, multi=False):
        """添加串口调试共用参数。"""
        if multi:
            p.add_argument("--port", action="append", required=True, help="串口号（可多次指定）")
        else:
            p.add_argument("--port", required=True, help="串口号（如 COM3）")
        p.add_argument("--baud", type=int, default=115200, help="波特率（默认 115200）")
        p.add_argument("--databits", type=int, choices=[5, 6, 7, 8], default=8, help="数据位（默认 8）")
        p.add_argument("--stop", type=int, choices=[1, 2], default=1, help="停止位（默认 1）")
        p.add_argument("--parity", choices=["N", "E", "O"], default="N", help="校验位（默认 N）")

    # serial list
    serial_sub.add_parser("list", help="列出可用 UART 端口")

    # serial open
    serial_open = serial_sub.add_parser("open", help="交互式串口终端")
    _add_serial_port_args(serial_open)
    serial_open.add_argument("--mode", choices=["ascii", "hex"], default="ascii", help="显示模式")
    serial_open.add_argument("--profile", default=None, help="协议 Profile 文件路径")
    serial_open.add_argument("--filter", default=None, help="过滤正则表达式")
    serial_open.add_argument("--log", default=None, help="日志输出文件路径")
    serial_open.add_argument("--auto-reply", default=None, help="自动应答规则文件路径")

    # serial send
    serial_send = serial_sub.add_parser("send", help="发送数据后退出")
    _add_serial_port_args(serial_send)
    serial_send.add_argument("send_data", help="要发送的数据")
    serial_send.add_argument("--hex", action="store_true", help="以 HEX 格式发送")
    serial_send.add_argument("--count", type=int, default=1, help="发送次数（默认 1）")
    serial_send.add_argument("--delay", type=float, default=1.0, help="多次发送间隔秒数（默认 1.0）")

    # serial monitor
    serial_monitor = serial_sub.add_parser("monitor", help="多端口被动监听")
    _add_serial_port_args(serial_monitor, multi=True)
    serial_monitor.add_argument("--mode", choices=["ascii", "hex"], default="ascii", help="显示模式")
    serial_monitor.add_argument("--profile", default=None, help="协议 Profile 文件路径")
    serial_monitor.add_argument("--filter", default=None, help="过滤正则表达式")
    serial_monitor.add_argument("--log", default=None, help="日志输出文件路径")

    # serial log
    serial_log = serial_sub.add_parser("log", help="无头模式日志记录")
    _add_serial_port_args(serial_log)
    serial_log.add_argument("--output", required=True, help="输出文件路径")
    serial_log.add_argument("--format", choices=["txt", "csv"], default=None, help="日志格式（默认按扩展名）")
    serial_log.add_argument("--profile", default=None, help="协议 Profile 文件路径")
    serial_log.add_argument("--duration", type=float, default=0, help="记录时长秒（默认无限）")

    # serial dashboard
    serial_dashboard = serial_sub.add_parser("dashboard", help="Web 可视化 Dashboard")
    _add_serial_port_args(serial_dashboard, multi=True)
    serial_dashboard.add_argument("--profile", default=None, help="协议 Profile 文件路径")
    serial_dashboard.add_argument("--host", default="127.0.0.1", help="HTTP 绑定地址")
    serial_dashboard.add_argument("--port-http", type=int, default=0, help="HTTP 端口（默认随机）")
    serial_dashboard.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")

    # serial profile (nested subcommands)
    serial_profile = serial_sub.add_parser("profile", help="Profile 管理")
    profile_sub = serial_profile.add_subparsers(dest="profile_command")

    sp_detect = profile_sub.add_parser("detect", help="从 C 源码检测帧结构")
    sp_detect.add_argument("--source", required=True, help="C 源文件路径")
    sp_detect.add_argument("--struct", default=None, help="指定 struct 名称")

    sp_generate = profile_sub.add_parser("generate", help="生成 Profile JSON")
    sp_generate.add_argument("--source", required=True, help="C 源文件路径")
    sp_generate.add_argument("--struct", default=None, help="指定 struct 名称")
    sp_generate.add_argument("--output", default=None, help="输出路径（默认 .mklink/serial_profile.json）")

    sp_show = profile_sub.add_parser("show", help="显示 Profile 内容")
    sp_show.add_argument("--profile", default=None, help="Profile 文件路径（默认自动查找）")

    # --- CPU Debug Control ---
    halt_parser = subparsers.add_parser("halt", help="停止 CPU 执行（写 DHCSR）")
    halt_parser.add_argument("--port", help="COM 端口（默认自动检测）")

    resume_parser = subparsers.add_parser("resume", help="恢复 CPU 执行")
    resume_parser.add_argument("--port", help="COM 端口（默认自动检测）")

    step_parser = subparsers.add_parser("step", help="单步执行一条指令")
    step_parser.add_argument("--port", help="COM 端口（默认自动检测）")

    break_parser = subparsers.add_parser("break", help="设置/管理 FPB 硬件断点")
    break_parser.add_argument("target", nargs="?", help="函数名或 Flash 地址（如 main 或 0x08001234）")
    break_parser.add_argument("--port", help="COM 端口（默认自动检测）")
    break_parser.add_argument("--source", help="ELF/AXF 文件路径（用于符号解析）")
    break_parser.add_argument("--slot", type=int, default=None, help="指定断点槽位 (0-5)")
    break_parser.add_argument("--list", action="store_true", help="列出当前已设置的断点")
    break_parser.add_argument("--clear", nargs="?", const="all", help="清除断点：指定槽位号或 all")
    break_parser.add_argument("--status", action="store_true", help="显示 CPU 调试状态")

    # serve 子命令
    serve_parser = subparsers.add_parser("serve", help="启动远程调试服务器（REST API + WebSocket JSON-RPC）")
    serve_parser.add_argument("--host", default="127.0.0.1", help="绑定地址（默认 127.0.0.1）")
    serve_parser.add_argument("--port", type=int, default=8765, help="绑定端口（默认 8765）")
    serve_parser.add_argument("--token", default=None, help="客户端认证 Token")
    serve_parser.add_argument("--device-port", default=None, help="MKLink COM 端口（默认自动检测）")
    serve_parser.add_argument("--axf", default=None, help="AXF/ELF 文件路径")
    serve_parser.add_argument("--backend", choices=["legacy", "fastapi"], default="fastapi",
                              help="服务器后端（默认 fastapi，legacy 使用原始 socket）")
    serve_parser.add_argument("--project-root", default=".", help="项目根目录")

    # gui 子命令
    gui_parser = subparsers.add_parser("gui", help="启动 MKLink GUI（FastAPI + Vue 3 浏览器界面）")
    gui_parser.add_argument("--host", default="127.0.0.1", help="绑定地址（默认 127.0.0.1）")
    gui_parser.add_argument("--port", type=int, default=8765, help="绑定端口（默认 8765）")
    gui_parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    gui_parser.add_argument("--device-port", default=None, help="MKLink COM 端口（默认自动检测）")
    gui_parser.add_argument("--axf", default=None, help="AXF/ELF 文件路径")
    gui_parser.add_argument("--project-root", default=".", help="项目根目录")

    # 向后兼容：--test 标志
    parser.add_argument("--port", help="COM 端口（兼容旧版）")
    parser.add_argument("--baud", type=int, default=DEFAULT_BAUDRATE)
    parser.add_argument("--test", action="store_true", help="运行基本测试（兼容旧版）")

    args = parser.parse_args()

    # 兼容旧版 --test 模式
    if args.test and args.port:
        _cli_test(args.port)
        return

    if args.command == "test":
        _cli_test(args.port)
    elif args.command == "discover":
        if args.list:
            for p in list_available_ports():
                print(f"  {p['device']} — {p['description']}")
        else:
            port = find_mklink_cdc_port()
            if port:
                print(f"[OK] 发现 MKLink CDC 端口: {port}")
                # 自动保存到 .mklink/config.json（如果配置已存在）
                from mklink.project_config import load_config, save_config
                config = load_config(".")
                if config is not None:
                    if config.get("com_port") != port:
                        config["com_port"] = port
                        save_config(".", config)
                        print(f"[AUTO] 已更新配置中的端口为 {port}")
                    else:
                        print(f"[OK] 配置中的端口已是 {port}")
            else:
                print("[FAIL] 未找到 MKLink CDC 端口")
    elif args.command == "rtt-find":
        result = diagnose_rtt_addr(args.path)
        if result.addr:
            source = f" ({result.source})" if result.source else ""
            print(f"[OK] _SEGGER_RTT 地址: {result.addr}{source}")
        else:
            print("[FAIL] 未找到 RTT 地址")
            for detail in result.details:
                print(f"  - {detail}")
            for warning in result.warnings:
                print(f"  - {warning}")
            if result.path_checked:
                print("  - 已检查文件:")
                for checked in result.path_checked:
                    print(f"    {checked}")
    elif args.command == "autostart":
        print(generate_autostart_config(args.addr, args.size, args.channel))
    elif args.command == "keil-parse":
        _cli_keil_parse(_resolve_project_root(args))
    elif args.command == "iar-parse":
        _cli_iar_parse(_resolve_project_root(args))
    elif args.command == "project-init":
        _cli_project_init(_resolve_project_root(args))
    elif args.command == "project-info":
        _cli_project_info(_resolve_project_root(args))
    elif args.command == "rtt-integrate":
        _cli_rtt_integrate(_resolve_project_root(args), args.src_dir, args.inc_dir, args.force)
    elif args.command == "copy-flm":
        _cli_copy_flm(_resolve_project_root(args))
    elif args.command == "flash":
        _cli_flash(_resolve_project_root(args), args.port, args.hex)
    elif args.command == "rtt":
        _cli_rtt(
            _resolve_project_root(args),
            port=args.port,
            duration=args.duration,
            visualize=args.visualize,
            host=args.host,
            port_http=args.port_http,
            no_browser=args.no_browser,
        )
    elif args.command == "read-ram":
        _cli_read_ram(args.port, args.addr, args.size, args.save)
    elif args.command == "read-reg":
        _cli_read_reg(args.port, args.register, args.addr, args.width, args.count, args.format, args.raw)
    elif args.command == "write-ram":
        _cli_write_ram(args.port, args.addr, args.data)
    elif args.command == "read-flash":
        _cli_read_flash(args.port, args.addr, args.size, args.save, _resolve_project_root(args))
    elif args.command == "vofa":
        _cli_vofa(
            args.port, args.variables, args.period, args.stop,
            visualize=args.visualize,
            host=args.host,
            port_http=args.port_http,
            no_browser=args.no_browser,
            max_points=args.max_points,
            duration=args.duration,
            names=args.names,
            source=args.source,
        )
    elif args.command == "symbols":
        _cli_symbols(args.source, args.filter)
    elif args.command == "hardfault":
        _cli_hardfault(args.port, args.source, args.sp)
    elif args.command == "typeinfo":
        _cli_typeinfo(args)
    elif args.command == "memmap":
        _cli_memmap(args)
    elif args.command == "watch":
        _cli_watch(args)
    elif args.command == "superwatch":
        _cli_superwatch(args)
    elif args.command == "modbus":
        _cli_modbus_dispatch(args)
    elif args.command == "serial":
        _cli_serial_dispatch(args)
    elif args.command == "halt":
        _cli_halt(args.port)
    elif args.command == "resume":
        _cli_resume(args.port)
    elif args.command == "step":
        _cli_step(args.port)
    elif args.command == "break":
        _cli_break(args)
    elif args.command == "serve":
        _cli_serve(args)
    elif args.command == "gui":
        _cli_gui(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    raise SystemExit(main())
