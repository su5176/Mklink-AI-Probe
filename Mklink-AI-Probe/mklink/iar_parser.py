"""
MKLink Serial Bridge — IAR EWP/EWT 工程文件解析。

零外部依赖（仅 stdlib xml/re/pathlib），零内部依赖。
从 .ewp XML 中提取设备、输出路径、配置等。
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path


def _fmt_hex(hex_str: str) -> str:
    """将原始 hex 字符串转换为标准格式 "0x" + 大写字符。"""
    s = hex_str.strip().upper()
    if s.startswith("0X"):
        s = s[2:]
    return "0x" + s.zfill(8)


def find_ewp(project_root: str) -> str | None:
    """在项目目录中查找 .ewp 文件。

    支持的输入形式：
    - 项目根目录（如 "D:\\Projects\\project"）
    - 直接传入 .ewp 文件路径
    - settings 子目录（IAR 通常将配置放在 settings/ 下）

    搜索顺序：直接文件 → settings/ → 一级子目录。
    """
    root = Path(project_root)

    # 如果传入的是 .ewp 文件，直接返回
    if root.is_file() and root.suffix == ".ewp":
        return str(root.resolve())

    # 如果是绝对路径且目录存在
    if root.is_absolute() and root.is_dir():
        for f in sorted(root.glob("*.ewp")):
            return str(f)
        for f in sorted(root.glob("settings/*.ewp")):
            return str(f)
        return None

    # 相对路径：优先 settings/ 子目录
    for f in sorted(root.glob("settings/*.ewp")):
        return str(f)

    # 项目根目录
    for f in sorted(root.glob("*.ewp")):
        return str(f)

    # 一级子目录
    for f in sorted(root.glob("*/*.ewp")):
        return str(f)

    return None


def _get_option_value(data_elem: ET.Element, option_name: str) -> str:
    """从配置 data 元素中查找指定名称的选项值。"""
    for option in data_elem.findall("option"):
        name_el = option.find("name")
        if name_el is not None and name_el.text == option_name:
            state_el = option.find("state")
            if state_el is not None and state_el.text:
                return state_el.text.strip()
    return ""


def _get_state_from_option(option_elem: ET.Element) -> str:
    """从 option 元素中获取 state 值（可能直接是文本或包含在 state 子元素中）。"""
    state_el = option_elem.find("state")
    if state_el is not None and state_el.text:
        return state_el.text.strip()
    return ""


def _get_all_states_from_option(option_elem: ET.Element) -> list[str]:
    """从 option 元素中获取所有 state 值（多值选项如 CCIncludePath2、CCDefines）。"""
    return [
        state.text.strip()
        for state in option_elem.findall("state")
        if state is not None and state.text
    ]


def resolve_iar_path(ewp_path: str, iar_path: str) -> str:
    """将 IAR $PROJ_DIR$ 路径解析为绝对路径。

    Args:
        ewp_path: .ewp 文件路径
        iar_path: 含 $PROJ_DIR$ 前缀的 IAR 路径

    Returns:
        解析后的绝对路径字符串
    """
    ewp_dir = Path(ewp_path).parent.resolve()
    if "$PROJ_DIR$" in iar_path:
        rel = iar_path.replace("$PROJ_DIR$", "").lstrip("\\/").replace("\\", "/")
        return str((ewp_dir / rel).resolve())
    return iar_path


def _find_config_by_name(configs: list, name: str) -> ET.Element | None:
    """查找指定名称的配置。"""
    for cfg in configs:
        name_el = cfg.find("name")
        if name_el is not None and name_el.text == name:
            return cfg
    return None


def _parse_iar_device_from_chip_menu(chip_str: str) -> tuple[str, str]:
    """从 OGChipSelectEditMenu 字符串解析设备名称。

    示例输入: "STM32F105VC\tST STM32F105VC"
    返回: ("STM32F105VC", "ST")
    """
    if not chip_str:
        return "", ""
    parts = chip_str.split("\t")
    device = parts[0].strip() if parts else ""
    vendor = parts[1].strip() if len(parts) > 1 else ""
    return device, vendor


def _parse_xcl_memory(xcl_path: str) -> dict:
    """从 IAR .xcl 链接配置文件解析内存布局。

    示例:
    - "--cpu=Cortex-M3"
    - "-p" "D:\\IAR\\arm\\CONFIG\\debugger\\ST\\STM32F105VC.ddf"
    """
    result = {
        "cpu": "",
        "fpu": "",
        "flash_base": "0x08000000",
        "flash_size": 0,
        "ram_base": "0x20000000",
        "ram_size": 0,
        "flash_loader": "",
        "ddf_path": "",
    }

    if not xcl_path or not Path(xcl_path).exists():
        return result

    try:
        content = Path(xcl_path).read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return result

    # 解析 --cpu=Cortex-M3
    m = re.search(r'"--cpu=([^"]+)"', content)
    if m:
        result["cpu"] = m.group(1).strip()

    # 解析 --fpu=None 或 --fpu=Cortex-M4
    m = re.search(r'"--fpu=([^"]+)"', content)
    if m:
        result["fpu"] = m.group(1).strip()

    # 解析 -p "DDF路径"
    m = re.search(r'"-p"\s+"([^"]+)"', content)
    if m:
        result["ddf_path"] = m.group(1).strip()

    # 解析 --flash_loader（支持正斜杠和反斜杠）
    m = re.search(r'"--flash_loader"\s+"([^"]+)"', content)
    if not m:
        m = re.search(r"--flash_loader\s+\"([^\"]+)\"", content)
    if m:
        result["flash_loader"] = m.group(1).strip()

    # 从 DDF 路径推断 Flash 大小（ST STM32F105VC.ddf → 256KB flash for F105VC）
    # DDF 文件通常包含内存范围定义，但直接解析 DDF 太复杂
    # 根据芯片型号推断
    ddf = result["ddf_path"]
    if "STM32F105xC" in ddf or "STM32F105VC" in ddf:
        result["flash_size"] = 0x40000  # 256KB
        result["ram_size"] = 0x8000     # 32KB
    elif "STM32F105x" in ddf:
        result["flash_size"] = 0x20000  # 128KB
        result["ram_size"] = 0x8000     # 32KB

    return result


def parse_ewp(ewp_path: str, config_name: str | None = "Debug") -> dict | None:
    """解析 .ewp 文件，提取完整工程配置。

    Args:
        ewp_path: .ewp 文件路径
        config_name: 指定配置名称（如 "Debug" 或 "Release"），None 则取第一个

    Returns:
        配置字典，解析失败返回 None
    """
    path = Path(ewp_path)
    if not path.exists():
        return None

    try:
        tree = ET.parse(str(path))
    except ET.ParseError:
        return None

    root = tree.getroot()
    if root.tag != "project":
        return None

    # 查找所有 configuration 元素
    configs = list(root.findall("configuration"))
    if not configs:
        return None

    # 查找指定配置，None 则取第一个
    config = _find_config_by_name(configs, config_name) if config_name else configs[0]
    if config is None:
        config = configs[0]

    config_name_found = config.find("name")
    config_name_str = config_name_found.text if config_name_found is not None else "Unknown"

    # 收集所有 settings 组的 data 元素
    all_options = {}
    for settings in config.findall("settings"):
        settings_name = settings.find("name")
        sname = settings_name.text if settings_name is not None else ""
        for data in settings.findall("data"):
            for option in data.findall("option"):
                name_el = option.find("name")
                if name_el is not None and name_el.text:
                    val = _get_state_from_option(option)
                    # 用 settings 名 + option 名作为 key 以避免冲突
                    key = f"{sname}.{name_el.text}" if sname else name_el.text
                    all_options[key] = val

    # 设备信息
    chip_str = all_options.get("General.OGChipSelectEditMenu", "")
    device, vendor = _parse_iar_device_from_chip_menu(chip_str)

    # 输出路径
    exe_path = all_options.get("General.ExePath", "Debug\\Exe")
    obj_path = all_options.get("General.ObjPath", "Debug\\Obj")
    list_path = all_options.get("General.ListPath", "Debug\\List")

    # 输出文件名（OOCOutputFile 在 OBJCOPY 组，IlinkOutputFile 在 ILINK 组）
    hex_file = all_options.get("OBJCOPY.OOCOutputFile", "") or all_options.get("General.OOCOutputFile", "")
    out_file = all_options.get("ILINK.IlinkOutputFile", "") or all_options.get("General.IlinkOutputFile", "")

    base_dir = path.parent.resolve()

    result = {
        "ewp_path": str(path.resolve()),
        "config_name": config_name_str,
        "device": device,
        "vendor": vendor,
        "chip_str": chip_str,
        "exe_path": exe_path,
        "obj_path": obj_path,
        "list_path": list_path,
        "hex_file": hex_file,
        "out_file": out_file,
        "compiler": "IAR ARM",
    }

    # 解析绝对路径
    result["hex_path"] = str((base_dir / exe_path / hex_file).resolve()) if hex_file and exe_path else ""
    result["map_path"] = str((base_dir / list_path / (out_file.replace(".out", ".map") if out_file else "project.map")).resolve())
    result["out_path"] = str((base_dir / exe_path / out_file).resolve()) if out_file and exe_path else ""

    # 查找对应的 .xcl 文件以获取内存布局
    # IAR 使用多个 xcl 文件：driver.xcl（CPU/调试配置）和 general.xcl（flash loader）
    settings_dir = base_dir / "settings"
    xcl_candidates = [
        settings_dir / f"{path.stem}.{config_name_str}.driver.xcl",
        settings_dir / f"{path.stem}.{config_name_str}.general.xcl",
        settings_dir / f"{path.stem}.Debug.driver.xcl",
        settings_dir / f"{path.stem}.Debug.general.xcl",
    ]

    # 合并多个 xcl 文件的结果（flash_loader 在 general.xcl，CPU 配置在 driver.xcl）
    memory_config = {"flash_base": "0x08000000", "flash_size": 0, "ram_base": "0x20000000", "ram_size": 0}
    for xcl_path in xcl_candidates:
        if xcl_path.exists():
            partial = _parse_xcl_memory(str(xcl_path))
            # 只合并有值的字段（避免用空值覆盖已有值）
            for key in ["flash_loader", "ddf_path", "cpu", "fpu", "flash_size", "ram_size"]:
                if partial.get(key):
                    memory_config[key] = partial[key]

    result.update(memory_config)

    # IAR flash 算法通过 flash_loader 指定
    # 对于 FLM 文件：需要通过 device 名称匹配 mcu_profiles.json 获取
    # 注意：.board 文件不是 FLM，不要从 board 路径提取 flm_name
    flash_loader = memory_config.get("flash_loader", "")
    result["flash_loader_path"] = flash_loader if flash_loader else ""

    # 提取 include_paths 和 defines（从 ICCARM settings）
    include_paths = []
    defines = []
    for settings in config.findall("settings"):
        sname_el = settings.find("name")
        if sname_el is not None and sname_el.text == "ICCARM":
            for data in settings.findall("data"):
                for option in data.findall("option"):
                    name_el = option.find("name")
                    if name_el is not None and name_el.text == "CCIncludePath2":
                        include_paths = _get_all_states_from_option(option)
                    elif name_el is not None and name_el.text == "CCDefines":
                        defines = _get_all_states_from_option(option)
    result["include_paths"] = include_paths
    result["defines"] = defines

    # 根据 device 名称从 mcu_profiles.json 获取 FLM 路径
    device_name = result.get("device", "")
    if device_name:
        result["flm_name"] = get_iar_flm_from_device(device_name)
    else:
        result["flm_name"] = ""

    return result


def get_iar_flm_from_device(device: str) -> str:
    """根据 IAR 设备名称从 mcu_profiles.json 获取 FLM 路径。

    Args:
        device: IAR 设备名称（如 "STM32F105VC"）

    Returns:
        FLM 文件名（如 "STM32F10x_1024.FLM"），未找到返回空字符串
    """
    from mklink.profiles import load_mcu_profiles, match_mcu_by_device

    profiles = load_mcu_profiles()
    mcu_key = match_mcu_by_device(device, profiles)
    if mcu_key and mcu_key != "custom":
        mcu = profiles.get(mcu_key, {})
        flm_path = mcu.get("flm_path", "")
        if flm_path:
            return flm_path
    return ""


def find_xcl_for_config(project_root: str, ewp_stem: str, config_name: str) -> str | None:
    """查找指定配置的 .xcl 文件路径。"""
    root = Path(project_root)
    settings_dir = root / "settings"

    if not settings_dir.exists():
        return None

    # 尝试 driver.xcl（包含 flash loader 配置）
    driver_xcl = settings_dir / f"{ewp_stem}.{config_name}.driver.xcl"
    if driver_xcl.exists():
        return str(driver_xcl)

    # 尝试 general.xcl
    general_xcl = settings_dir / f"{ewp_stem}.{config_name}.general.xcl"
    if general_xcl.exists():
        return str(general_xcl)

    # 回退到 Debug
    driver_xcl = settings_dir / f"{ewp_stem}.Debug.driver.xcl"
    if driver_xcl.exists():
        return str(driver_xcl)

    return None


def get_output_dir_map(ewp_info: dict) -> dict:
    """从 ewp 解析结果返回标准化的输出目录映射。

    Returns:
        {hex_path, map_path, out_path, exe_dir, list_dir}
    """
    return {
        "hex_path": ewp_info.get("hex_path", ""),
        "map_path": ewp_info.get("map_path", ""),
        "out_path": ewp_info.get("out_path", ""),
        "exe_dir": ewp_info.get("exe_path", ""),
        "list_dir": ewp_info.get("list_path", ""),
        "obj_dir": ewp_info.get("obj_path", ""),
    }
