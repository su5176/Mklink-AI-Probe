"""
MKLink Serial Bridge — Keil uvprojx 工程文件解析。

零外部依赖（仅 stdlib xml/re/pathlib），零内部依赖。
从 .uvprojx XML 中提取设备、Flash 算法、内存布局、输出路径等配置。
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path


def _fmt_hex(hex_str: str) -> str:
    """将原始 hex 字符串转换为标准格式 "0x" + 大写字符。

    兼容 "08000000"、"0x08000000"、等输入。
    """
    s = hex_str.strip().upper()
    if s.startswith("0X"):
        s = s[2:]
    return "0x" + s.zfill(8)


def find_uvprojx(project_root: str) -> str | None:
    """在项目目录中查找 .uvprojx 文件。

    支持的输入形式：
    - 项目根目录（如 "D:\\Projects\\project" 或 "MDK-ARM"）
    - 直接传入 .uvprojx 文件路径
    - MDK-ARM 子目录（如 "D:\\Projects\\project\\MDK-ARM"）

    搜索顺序：直接文件 → MDK-ARM/ 子目录 → 项目根目录 → 一级子目录。
    """
    root = Path(project_root)

    # 如果传入的是 .uvprojx 文件，直接返回
    if root.is_file() and root.suffix == ".uvprojx":
        return str(root.resolve())

    # 如果是绝对路径且目录存在，直接在该目录下搜索
    if root.is_absolute() and root.is_dir():
        # 直接搜索该目录下的 .uvprojx 文件
        for f in sorted(root.glob("*.uvprojx")):
            return str(f)
        # 搜索 MDK-ARM/ 子目录
        for f in sorted(root.glob("MDK-ARM/*.uvprojx")):
            return str(f)
        return None

    # 相对路径：优先搜索 MDK-ARM/ 子目录
    for f in sorted(root.glob("MDK-ARM/*.uvprojx")):
        return str(f)

    # 项目根目录
    for f in sorted(root.glob("*.uvprojx")):
        return str(f)

    # 一级子目录
    for f in sorted(root.glob("*/*.uvprojx")):
        return str(f)

    return None


def parse_uvprojx(uvprojx_path: str, target_name: str | None = None) -> dict | None:
    """解析 .uvprojx 文件，提取完整工程配置。

    Args:
        uvprojx_path: .uvprojx 文件路径
        target_name: 指定 Target 名称，None 则取第一个

    Returns:
        配置字典，解析失败返回 None
    """
    path = Path(uvprojx_path)
    if not path.exists():
        return None

    try:
        tree = ET.parse(str(path))
    except ET.ParseError:
        return None

    root = tree.getroot()
    base_dir = path.parent

    # 查找目标 Target
    target = _find_target(root, target_name)
    if target is None:
        return None

    tco = target.find("TargetOption/TargetCommonOption")
    if tco is None:
        return None

    # 基本信息
    result = {
        "uvprojx_path": str(path.resolve()),
        "target_name": _text(target, "TargetName", ""),
        "device": _text(tco, "Device", ""),
        "vendor": _text(tco, "Vendor", ""),
        "pack_id": _text(tco, "PackID", ""),
    }

    # 编译器类型
    uac6 = _text(tco, "..//uAC6", "")
    if uac6 == "":
        uac6 = _text(target, "ToolsetNumber", "")
    result["compiler"] = "ac6" if uac6 == "1" else "ac5"

    # Flash 算法配置
    flash_dll = _text(tco, "FlashDriverDll", "")
    flash_info = _parse_flash_driver_dll(flash_dll)
    result.update(flash_info)

    # 内存布局（从 Cpu 字符串 + OnChipMemories）
    cpu_str = _text(tco, "Cpu", "")
    mem = _parse_memory_layout(cpu_str, target)
    result["ram_base"] = mem.get("ram_base", "0x20000000")
    result["ram_size"] = mem.get("ram_size", 0)
    # flash_base/flash_size 优先用 FlashDriverDll 解析结果
    if not result.get("flash_base"):
        result["flash_base"] = mem.get("flash_base", "0x08000000")
    if not result.get("flash_size"):
        result["flash_size"] = mem.get("flash_size", 0)

    # Scatter file 优先级最高（umfTarg="0" 时生效）
    ldads = target.find(".//LDads")
    if ldads is not None:
        umf = ldads.find("umfTarg")
        if umf is not None and umf.text and umf.text.strip() == "0":
            scatter = ldads.find("ScatterFile")
            if scatter is not None and scatter.text and scatter.text.strip():
                sct_rel = scatter.text.strip()
                sct_abs = (base_dir / sct_rel).resolve()
                if sct_abs.exists():
                    sct_mem = parse_scatter_file(str(sct_abs))
                    if sct_mem:
                        result["scatter_file"] = str(sct_abs)
                        if sct_mem.get("flash_base"):
                            result["flash_base"] = sct_mem["flash_base"]
                        if sct_mem.get("flash_size"):
                            result["flash_size"] = sct_mem["flash_size"]
                        if sct_mem.get("ram_base"):
                            result["ram_base"] = sct_mem["ram_base"]
                        if sct_mem.get("ram_size"):
                            result["ram_size"] = sct_mem["ram_size"]

    # 输出路径
    output_dir = _text(tco, "OutputDirectory", ".\\Objects\\")
    output_name = _text(tco, "OutputName", "")
    listing_dir = _text(tco, "ListingPath", ".\\Listings\\")

    result["output_dir"] = output_dir
    result["output_name"] = output_name
    result["listing_dir"] = listing_dir

    # 解析绝对路径
    result["hex_path"] = str((base_dir / output_dir / (output_name + ".hex")).resolve())
    result["map_path"] = str((base_dir / listing_dir / (output_name + ".map")).resolve())
    result["axf_path"] = str((base_dir / output_dir / (output_name + ".axf")).resolve())

    # Include 路径和宏定义
    cads = target.find("TargetOption/TargetArmAds/Cads/VariousControls")
    if cads is not None:
        inc_str = _text(cads, "IncludePath", "")
        result["include_paths"] = [p.strip() for p in inc_str.split(";") if p.strip()]
        def_str = _text(cads, "Define", "")
        result["defines"] = [d.strip() for d in def_str.split(",") if d.strip()]
    else:
        result["include_paths"] = []
        result["defines"] = []

    # 源文件组
    result["groups"] = _parse_groups(target)

    return result


def _find_target(root: ET.Element, target_name: str | None) -> ET.Element | None:
    """查找指定名称的 Target，或返回第一个。"""
    targets = root.findall(".//Target")
    if not targets:
        return None

    if target_name is None:
        return targets[0]

    for t in targets:
        name_el = t.find("TargetName")
        if name_el is not None and name_el.text == target_name:
            return t

    return targets[0]


def _text(parent: ET.Element, tag: str, default: str = "") -> str:
    """安全获取子元素文本。"""
    el = parent.find(tag)
    if el is not None and el.text:
        return el.text.strip()
    return default


def _parse_flash_driver_dll(dll_str: str) -> dict:
    """解析 FlashDriverDll 字符串。

    示例输入:
    UL2CM3(-S0 -C0 -P0 -FD20000000 -FC1000 -FN1 -FF0N32G43x -FS08000000 -FL020000
           -FP0($$Device:N32G435CB$Flash\\N32G43x.FLM))
    """
    result = {
        "flm_name": "",
        "flm_path": "",
        "flash_base": "",
        "flash_size": 0,
        "flash_ram_base": "",
        "page_size": 0,
        "flash_region_count": 1,
    }

    if not dll_str:
        return result

    # -FD: RAM base for flash programming
    m = re.search(r"-FD([0-9a-fA-F]+)", dll_str)
    if m:
        result["flash_ram_base"] = _fmt_hex(m.group(1))

    # -FC: page size
    m = re.search(r"-FC([0-9a-fA-F]+)", dll_str)
    if m:
        result["page_size"] = int(m.group(1), 16)

    # -FN: flash region count
    m = re.search(r"-FN(\d+)", dll_str)
    if m:
        result["flash_region_count"] = int(m.group(1))

    # -FF0: FLM algorithm name
    m = re.search(r"-FF0(\S+?)(?:\s|-)", dll_str)
    if m:
        result["flm_name"] = m.group(1)

    # -FS0: flash start address (always 8 hex digits for N32G435: 0x08000000)
    m = re.search(r"-FS0([0-9a-fA-F]{8})", dll_str)
    if not m:
        m = re.search(r"-FS([0-9a-fA-F]+)", dll_str)
    if m:
        result["flash_base"] = _fmt_hex(m.group(1))

    # -FL0: flash length (always 8 hex digits for N32G435: 0x00080000 = 512KB)
    m = re.search(r"-FL0([0-9a-fA-F]{8})", dll_str)
    if not m:
        m = re.search(r"-FL([0-9a-fA-F]+)", dll_str)
    if m:
        result["flash_size"] = int(m.group(1), 16)

    # -FP0(...): full FLM path (may contain $$ device pack references)
    m = re.search(r"-FP0\((.+?)\)\)?", dll_str)
    if m:
        result["flm_path"] = m.group(1)

    return result


def _parse_memory_layout(cpu_str: str, target: ET.Element) -> dict:
    """从 Cpu 字符串和 OnChipMemories 提取内存布局。"""
    result = {}

    # 从 Cpu 字符串解析: IRAM(0x20000000,0x8000) IROM(0x08000000,0x20000)
    m = re.search(r"IRAM\(0x([0-9a-fA-F]+),0x([0-9a-fA-F]+)\)", cpu_str)
    if m:
        result["ram_base"] = _fmt_hex(m.group(1))
        result["ram_size"] = int(m.group(2), 16)

    m = re.search(r"IROM\(0x([0-9a-fA-F]+),0x([0-9a-fA-F]+)\)", cpu_str)
    if m:
        result["flash_base"] = _fmt_hex(m.group(1))
        result["flash_size"] = int(m.group(2), 16)

    # 尝试从 OnChipMemories 获取更精确的值
    ocm = target.find(".//OnChipMemories")
    if ocm is not None:
        iram = ocm.find("IRAM")
        if iram is not None:
            addr = _text(iram, "StartAddress", "")
            size = _text(iram, "Size", "")
            if addr and size and int(size, 16) > 0:
                result["ram_base"] = _fmt_hex(addr)
                result["ram_size"] = int(size, 16)

        irom = ocm.find("IROM")
        if irom is not None:
            addr = _text(irom, "StartAddress", "")
            size = _text(irom, "Size", "")
            if addr and size and int(size, 16) > 0:
                result["flash_base"] = _fmt_hex(addr)
                result["flash_size"] = int(size, 16)

    return result


def extract_flash_address_from_sct(sct_path: str) -> str | None:
    """从 Keil scatter file (.sct) 提取 Flash 起始地址。

    解析 LR_IROM1 0x08000000 ... 格式的加载区域定义。
    """
    mem = parse_scatter_file(sct_path)
    if mem and mem.get("flash_base"):
        return mem["flash_base"]
    return None


def parse_scatter_file(sct_path: str) -> dict | None:
    """从 Keil scatter file (.sct) 提取完整内存布局。

    Returns:
        包含 flash_base, flash_size, ram_base, ram_size 的字典，解析失败返回 None。
    """
    try:
        with open(sct_path, "r", encoding="utf-8") as f:
            content = f.read()
    except (OSError, UnicodeDecodeError):
        try:
            with open(sct_path, "r", encoding="gbk") as f:
                content = f.read()
        except (OSError, UnicodeDecodeError):
            return None

    result: dict = {}

    # 移除注释行（分号开头）
    lines = content.splitlines()
    clean_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(";"):
            continue
        # 移除行内注释
        semi_idx = stripped.find(";")
        if semi_idx >= 0:
            stripped = stripped[:semi_idx].strip()
        clean_lines.append(stripped)
    clean = "\n".join(clean_lines)

    # 解析 Flash 加载区域: LR_IROM<n> <base> <size> {
    m = re.search(r"LR_IROM\d+\s+(0x[0-9a-fA-F]+)\s+(0x[0-9a-fA-F]+)", clean)
    if m:
        result["flash_base"] = _fmt_hex(m.group(1)[2:])
        result["flash_size"] = int(m.group(2), 16)

    # 解析 RAM 区域: RW_IRAM<n> <base> <size> {
    m = re.search(r"RW_IRAM\d+\s+(0x[0-9a-fA-F]+)\s+(0x[0-9a-fA-F]+)", clean)
    if m:
        result["ram_base"] = _fmt_hex(m.group(1)[2:])
        result["ram_size"] = int(m.group(2), 16)

    return result if result else None


def uses_scatter_file(uvprojx_path: str) -> tuple[bool, str | None]:
    """检查 Keil 工程是否配置使用 scatter file。

    umfTarg="0" 表示使用 scatter file，umfTarg="1" 表示使用 Target Dialog 内存布局。

    Returns:
        (uses_sct, sct_path): 是否使用 scatter file，以及 scatter file 的绝对路径
    """
    try:
        tree = ET.parse(uvprojx_path)
    except (ET.ParseError, OSError):
        return False, None

    root = tree.getroot()
    for ldads in root.iter("LDads"):
        umf = ldads.find("umfTarg")
        # umfTarg="0" → 使用 scatter file；"1" 或缺失 → 使用 Target Dialog
        if umf is not None and umf.text and umf.text.strip() == "0":
            scatter = ldads.find("ScatterFile")
            if scatter is not None and scatter.text and scatter.text.strip():
                sct_rel = scatter.text.strip()
                uvp_dir = Path(uvprojx_path).parent
                sct_abs = (uvp_dir / sct_rel).resolve()
                if sct_abs.exists():
                    return True, str(sct_abs)
                return True, sct_rel
    return False, None


def get_flash_start_address(project_root: str) -> str | None:
    """从 Keil 工程提取 Flash 起始地址（考虑 scatter file 配置）。

    优先级：
    1. 如果使用 scatter file → 从 .sct 文件解析
    2. 否则 → 从 .uvprojx 的 OnChipMemories/IROM 提取
    3. 回退 → 从 project_info.json 的 flash_base 字段
    """
    from mklink.project_config import load_project_info

    project = load_project_info(project_root)
    if not project:
        return None

    uvp_path = project.get("uvprojx_path", "")
    if uvp_path and Path(uvp_path).exists():
        use_sct, sct_path = uses_scatter_file(uvp_path)
        if use_sct and sct_path and Path(sct_path).exists():
            addr = extract_flash_address_from_sct(sct_path)
            if addr:
                return addr

        info = parse_uvprojx(uvp_path)
        if info and info.get("flash_base"):
            return info["flash_base"]

    return project.get("flash_base")


def _parse_groups(target: ET.Element) -> list[dict]:
    """解析源文件组信息。"""
    groups = []
    for group in target.findall(".//Groups/Group"):
        name = _text(group, "GroupName", "")
        files = []
        for f in group.findall("Files/File"):
            files.append({
                "name": _text(f, "FileName", ""),
                "path": _text(f, "FilePath", ""),
                "type": int(_text(f, "FileType", "1")),
            })
        groups.append({"name": name, "files": files})
    return groups
