"""
MKLink Serial Bridge — 项目配置管理（.mklink/ 目录）。

零外部依赖（仅 stdlib json/pathlib），零内部依赖。
管理 .mklink/ 目录下的 JSON 配置文件，提供统一的配置读写接口。
"""

from __future__ import annotations

import json
import os
import re as _re
from datetime import datetime
from pathlib import Path


_CONFIG_FILE = "config.json"
_PROJECT_INFO_FILE = "project_info.json"  # IDE-agnostic project config (formerly keel_project.json)
_RTT_CONFIG_FILE = "rtt_config.json"
_TOOLCHAIN_CONFIG_FILE = "toolchain.json"  # optional GNU Arm host-tool overrides (read by toolchain.py)
_JSCOPE_PROJECT_FILE = "jscope_project.json"

_HEX_ADDR_RE = _re.compile(r"^0x[0-9a-fA-F]{1,8}$")
_COM_PORT_RE = _re.compile(r"^COM\d+$", _re.IGNORECASE)
_KNOWN_MCU_KEYS: set[str] | None = None


def get_mklink_dir(project_root: str) -> Path:
    """返回 .mklink/ 目录路径。"""
    return Path(project_root) / ".mklink"


def ensure_mklink_dir(project_root: str) -> Path:
    """创建 .mklink/ 目录（如不存在），返回路径。"""
    d = get_mklink_dir(project_root)
    d.mkdir(parents=True, exist_ok=True)
    return d


def is_configured(project_root: str) -> bool:
    """检查项目是否已配置（.mklink/config.json 存在）。"""
    return (get_mklink_dir(project_root) / _CONFIG_FILE).exists()


# --- config.json: 基本配置（COM 口、波特率、MCU 类型） ---

def load_config(project_root: str) -> dict | None:
    """读取 .mklink/config.json。"""
    return _load_json(project_root, _CONFIG_FILE)


def save_config(project_root: str, config: dict) -> None:
    """写入 .mklink/config.json。"""
    _save_json(project_root, _CONFIG_FILE, config)


# --- project_info.json: IDE-agnostic 工程解析结果（支持 Keil/IAR） ---

def load_project_info(project_root: str) -> dict | None:
    """读取 .mklink/project_info.json。"""
    return _load_json(project_root, _PROJECT_INFO_FILE)


def save_project_info(project_root: str, project_data: dict) -> None:
    """写入 .mklink/project_info.json。"""
    _save_json(project_root, _PROJECT_INFO_FILE, project_data)


# --- 向后兼容别名 ---
def load_keil_project(project_root: str) -> dict | None:
    """读取 .mklink/project_info.json（向后兼容别名）。"""
    return load_project_info(project_root)


def save_keil_project(project_root: str, keil_info: dict) -> None:
    """写入 .mklink/project_info.json（向后兼容别名）。"""
    save_project_info(project_root, keil_info)


# --- jscope_project.json: J-SCOPE channel state persistence ---

def load_jscope_project(project_root: str) -> dict | None:
    """读取 .mklink/jscope_project.json。

    Returns the parsed dict on success, or None if the file does not exist
    or contains invalid JSON (graceful degradation).
    """
    return _load_json(project_root, _JSCOPE_PROJECT_FILE)


def save_jscope_project(project_root: str, data: dict) -> None:
    """写入 .mklink/jscope_project.json。

    Persists J-SCOPE channel state (visible channels, colors, trigger settings, etc.)
    so they survive across sessions.
    """
    _save_json(project_root, _JSCOPE_PROJECT_FILE, data)


# --- rtt_config.json: RTT 配置 ---

def load_rtt_config(project_root: str) -> dict | None:
    """读取 .mklink/rtt_config.json。"""
    return _load_json(project_root, _RTT_CONFIG_FILE)


# --- toolchain.json: optional GNU Arm host-tool path overrides ---

def load_toolchain_config(project_root: str) -> dict | None:
    """读取 .mklink/toolchain.json（可选；留空键表示自动解析）。"""
    return _load_json(project_root, _TOOLCHAIN_CONFIG_FILE)


def save_toolchain_config(project_root: str, toolchain_config: dict) -> None:
    """写入 .mklink/toolchain.json。"""
    _save_json(project_root, _TOOLCHAIN_CONFIG_FILE, toolchain_config)


def lint_json_file(project_root: str, filename: str) -> str | None:
    """检查 .mklink/ 下 JSON 文件格式，返回错误详情或 None。"""
    p = get_mklink_dir(project_root) / filename
    if not p.exists():
        return f"{filename} 不存在"
    try:
        with open(p, "r", encoding="utf-8-sig") as f:
            json.load(f)
    except json.JSONDecodeError as e:
        return f"{filename} JSON 格式错误: 第 {e.lineno} 行第 {e.colno} 列: {e.msg}"
    except OSError as e:
        return f"{filename} 无法读取: {e}"
    return None


def lint_project_json(project_root: str, filenames: list[str] | None = None) -> list[str]:
    """lint .mklink/ 配置 JSON 文件，返回所有错误。"""
    if filenames is None:
        filenames = [_CONFIG_FILE, _PROJECT_INFO_FILE, _RTT_CONFIG_FILE]
    errors: list[str] = []
    for filename in filenames:
        error = lint_json_file(project_root, filename)
        if error:
            errors.append(error)
    return errors


def _get_known_mcu_keys() -> set[str]:
    """Lazy-load known MCU keys from mcu_profiles.json."""
    global _KNOWN_MCU_KEYS
    if _KNOWN_MCU_KEYS is None:
        from mklink.profiles import load_mcu_profiles
        _KNOWN_MCU_KEYS = set(load_mcu_profiles().keys())
    return _KNOWN_MCU_KEYS


def lint_config_semantic(project_root: str) -> list[str]:
    """语义校验 .mklink/ 配置文件字段值，返回警告列表。"""
    warnings: list[str] = []

    # --- config.json ---
    config = load_config(project_root)
    if config:
        com = config.get("com_port", "")
        if com and not _COM_PORT_RE.match(com):
            warnings.append(f"config.json: com_port '{com}' 格式异常（应为 COM + 数字，如 COM6）")

        mcu = config.get("mcu_key", "")
        if mcu:
            known = _get_known_mcu_keys()
            if mcu not in known:
                warnings.append(
                    f"config.json: mcu_key '{mcu}' 不在已知配置中"
                    f"（已知: {', '.join(sorted(known))}）"
                )

    # --- project_info.json ---
    project = load_project_info(project_root)
    if project:
        bin_path = project.get("bin_path", "")
        if bin_path and not bin_path.lower().endswith(".bin"):
            warnings.append(
                f"project_info.json: bin_path '{bin_path}' 后缀异常（应为 .bin）"
            )

        hex_path = project.get("hex_path", "")
        if hex_path:
            if hex_path.lower().endswith(".bin"):
                warnings.append(
                    f"project_info.json: hex_path 指向 .bin 文件 '{hex_path}'，"
                    f"应为 .hex 文件（BIN 不含地址信息，烧录有风险）"
                )
            elif not hex_path.lower().endswith(".hex"):
                warnings.append(
                    f"project_info.json: hex_path '{hex_path}' 后缀异常（应为 .hex）"
                )

        map_path = project.get("map_path", "")
        if map_path and not map_path.lower().endswith(".map"):
            warnings.append(
                f"project_info.json: map_path '{map_path}' 后缀异常（应为 .map）"
            )

        flash_base = project.get("flash_base", "")
        if flash_base and not _HEX_ADDR_RE.match(flash_base):
            warnings.append(
                f"project_info.json: flash_base '{flash_base}' 格式异常（应为 0x 开头的十六进制地址）"
            )

        for field in ("bin_base", "download_base"):
            value = project.get(field, "")
            if value and not _HEX_ADDR_RE.match(value):
                warnings.append(
                    f"project_info.json: {field} '{value}' 格式异常（应为 0x 开头的十六进制地址）"
                )

    # --- rtt_config.json ---
    rtt = load_rtt_config(project_root)
    if rtt:
        rtt_addr = rtt.get("rtt_addr", "")
        if rtt_addr:
            if not _HEX_ADDR_RE.match(rtt_addr):
                warnings.append(
                    f"rtt_config.json: rtt_addr '{rtt_addr}' 格式异常（应为 0x 开头的十六进制地址）"
                )
            else:
                addr_int = int(rtt_addr, 16)
                if not (0x20000000 <= addr_int <= 0x3FFFFFFF):
                    warnings.append(
                        f"rtt_config.json: rtt_addr '{rtt_addr}' 不在 RAM 地址范围内"
                        f"（期望 0x20000000–0x3FFFFFFF）"
                    )

        # rtt_storage_mode：0=动态搜寻, 1=静态编译
        if "rtt_storage_mode" in rtt:
            mode = rtt["rtt_storage_mode"]
            if mode not in (0, 1):
                warnings.append(
                    f"rtt_config.json: rtt_storage_mode '{mode}' 非法（应为 0 或 1）"
                )

    return warnings


def resolve_rtt_storage_mode(rtt_config: dict | None) -> int:
    """从 rtt_config 解析 RTT 控制块存储方式，缺省时默认 0（动态搜寻）。

    Args:
        rtt_config: rtt_config.json 加载后的 dict，可为 None。

    Returns:
        0 (动态搜寻) 或 1 (静态编译)。任何异常或非法值都回退到 0。
    """
    if not rtt_config:
        return 0
    try:
        mode = int(rtt_config.get("rtt_storage_mode", 0))
    except (TypeError, ValueError):
        return 0
    return mode if mode in (0, 1) else 0


def save_rtt_config(project_root: str, rtt_config: dict) -> None:
    """写入 .mklink/rtt_config.json。"""
    _save_json(project_root, _RTT_CONFIG_FILE, rtt_config)


def ensure_rtt_config_updated(project_root: str) -> dict | None:
    """每次启动 RTT 前调用。以 MAP 文件中的地址为准，若与 config 不同则更新。

    Returns:
        更新后的 rtt_config dict，或 None（无法更新）
    """
    from mklink.rtt_addr import find_rtt_addr_from_map

    rtt = load_rtt_config(project_root)
    if rtt is None:
        return None

    project = load_project_info(project_root)
    if project is None:
        return rtt

    map_path = project.get("map_path", "")
    if not map_path or not Path(map_path).exists():
        return rtt

    # 每次都从 map 文件解析最新地址（正则匹配文本文件，速度很快）
    new_addr = find_rtt_addr_from_map(map_path)
    if not new_addr:
        return rtt

    old_addr = rtt.get("rtt_addr", "")
    if new_addr != old_addr:
        print(f"[AUTO] RTT 地址已更新: {old_addr or '(空)'} → {new_addr}（来源: MAP 文件）")
        rtt["rtt_addr"] = new_addr
        save_rtt_config(project_root, rtt)

    return rtt


# --- 全局项目历史 (~/.mklink/project_history.json) ---

_HISTORY_FILE = "project_history.json"
_MAX_HISTORY = 10


def _global_mklink_dir() -> Path:
    return Path.home() / ".mklink"


def _ensure_global_dir() -> Path:
    d = _global_mklink_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_project_history() -> dict:
    p = _global_mklink_dir() / _HISTORY_FILE
    if not p.exists():
        return {"last_project": None, "history": []}
    try:
        return json.loads(p.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError):
        return {"last_project": None, "history": []}


def save_project_history(data: dict) -> None:
    d = _ensure_global_dir()
    p = d / _HISTORY_FILE
    with open(p, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def add_to_project_history(path: str, max_entries: int = _MAX_HISTORY) -> dict:
    normalized = os.path.normcase(os.path.abspath(path))
    name = os.path.basename(path.rstrip("/\\"))

    # 尝试从 project_info 获取项目名
    try:
        info = load_project_info(path)
        if info:
            flm = info.get("flm_name", "")
            if flm:
                name = flm.replace(".FLM", "").replace(".flm", "")
    except Exception:
        pass

    data = load_project_history()
    history = data.get("history", [])

    # 移除已有条目（按 normalized path 去重）
    history = [e for e in history if os.path.normcase(os.path.abspath(e["path"])) != normalized]

    history.insert(0, {
        "path": os.path.abspath(path),
        "name": name,
        "last_used": datetime.now().isoformat(),
    })
    history = history[:max_entries]

    data["last_project"] = os.path.abspath(path)
    data["history"] = history
    save_project_history(data)
    return data


def remove_from_project_history(path: str) -> dict:
    normalized = os.path.normcase(os.path.abspath(path))
    data = load_project_history()
    history = data.get("history", [])
    history = [e for e in history if os.path.normcase(os.path.abspath(e["path"])) != normalized]

    if os.path.normcase(os.path.abspath(data.get("last_project") or "")) == normalized:
        data["last_project"] = history[0]["path"] if history else None

    data["history"] = history
    save_project_history(data)
    return data


# --- 内部工具 ---

def _load_json(project_root: str, filename: str) -> dict | None:
    """从 .mklink/ 读取 JSON 文件。"""
    p = get_mklink_dir(project_root) / filename
    if not p.exists():
        return None
    try:
        with open(p, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return None


def _save_json(project_root: str, filename: str, data: dict) -> None:
    """写入 JSON 文件到 .mklink/。"""
    d = ensure_mklink_dir(project_root)
    p = d / filename
    with open(p, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


# --- 配置检查 ---

class ProjectConfigStatus:
    """项目配置检查结果。"""

    def __init__(
        self,
        is_valid: bool,
        has_config: bool = False,
        has_keil_project: bool = False,
        has_rtt_config: bool = False,
        errors: list[str] | None = None,
        warnings: list[str] | None = None,
        flm_on_microkeen: bool = False,
        microkeen_flm_path: str | None = None,
    ):
        self.is_valid = is_valid
        self.has_config = has_config
        self.has_keil_project = has_keil_project
        self.has_rtt_config = has_rtt_config
        self.errors = errors or []
        self.warnings = warnings or []
        self.flm_on_microkeen = flm_on_microkeen
        self.microkeen_flm_path = microkeen_flm_path

    def needs_init(self) -> bool:
        """是否需要运行 project-init。"""
        return not self.has_config or not self.has_keil_project


def check_project_config(project_root: str) -> ProjectConfigStatus:
    """检查项目配置是否完整有效。

    检查项：
    - .mklink/config.json 是否存在且有效
    - .mklink/keil_project.json 是否存在且有效
    - .mklink/rtt_config.json 是否存在
    - keil_project.json 中的文件路径是否存在
    - MICROKEEN 磁盘上是否有对应的 FLM 文件

    返回 ProjectConfigStatus，调用方根据 needs_init() 判断是否需要初始化。
    """
    errors: list[str] = []
    warnings: list[str] = []

    has_config = False
    has_keil_project = False
    has_rtt_config = False
    flm_on_microkeen = False
    microkeen_flm_path = None

    # 延迟导入避免循环依赖
    from mklink.discovery import check_flm_on_microkeen

    # 先 lint JSON，避免把语法错误吞成“无效配置”。
    json_errors = lint_project_json(project_root)
    for error in json_errors:
        if "不存在" in error and "rtt_config.json" in error:
            warnings.append(error + "（RTT 功能不可用）")
        elif "不存在" in error:
            errors.append(error)
        else:
            errors.append(error)

    # 检查 config.json
    config = load_config(project_root)
    if config is None:
        if not any("config.json" in e for e in errors):
            errors.append("config.json 不存在或无效")
    else:
        has_config = True
        # 检查必要字段
        if not config.get("mcu_key"):
            warnings.append("config.json 缺少 mcu_key")
        if not config.get("com_port"):
            warnings.append("config.json 缺少 com_port（将无法直接烧录）")

    # 检查 project_info.json（IDE-agnostic）
    project = load_project_info(project_root)
    if project is None:
        if not any("project_info.json" in e for e in errors):
            errors.append("project_info.json 不存在或无效（项目未初始化）")
    else:
        has_keil_project = True
        # 检查关键路径是否存在
        hex_path = project.get("hex_path", "")
        if hex_path and not Path(hex_path).exists():
            warnings.append(f"HEX 文件不存在: {hex_path}")
        map_path = project.get("map_path", "")
        if map_path and not Path(map_path).exists():
            warnings.append(f"MAP 文件不存在: {map_path}")
        flm_path = project.get("flm_path", "")
        if flm_path and "$$" in flm_path and not Path(flm_path.replace("$$", "")).exists():
            warnings.append(f"FLM 路径包含未解析的变量: {flm_path}")

        # 检查 MICROKEEN 磁盘上的 FLM 文件
        flm_name = project.get("flm_name", "")
        if flm_name:
            flm_on_microkeen, microkeen_flm_path = check_flm_on_microkeen(flm_name)
            if not flm_on_microkeen:
                warnings.append(
                    f"FLM 文件 '{flm_name}' 不存在于 MICROKEEN 磁盘的 FLM 目录中。"
                    f" 请将 FLM 文件拷贝到 [MICROKEEN] 磁盘的 FLM 文件夹。"
                )

    # 检查 rtt_config.json
    rtt = load_rtt_config(project_root)
    if rtt is None:
        if not any("rtt_config.json" in e for e in errors + warnings):
            warnings.append("rtt_config.json 不存在或无效（RTT 功能不可用）")
    else:
        has_rtt_config = True

    # 语义校验
    semantic_warnings = lint_config_semantic(project_root)
    warnings.extend(semantic_warnings)

    is_valid = len(errors) == 0

    return ProjectConfigStatus(
        is_valid=is_valid,
        has_config=has_config,
        has_keil_project=has_keil_project,
        has_rtt_config=has_rtt_config,
        errors=errors,
        warnings=warnings,
        flm_on_microkeen=flm_on_microkeen if has_keil_project else False,
        microkeen_flm_path=microkeen_flm_path,
    )


def format_config_status(status: ProjectConfigStatus) -> str:
    """格式化配置检查结果为可读字符串。"""
    lines = []

    if status.is_valid and not status.warnings:
        lines.append("[OK] 项目配置正常")
    else:
        if status.errors:
            lines.append("[FAIL] 项目配置存在问题:")
            for e in status.errors:
                lines.append(f"  - {e}")
        if status.warnings:
            if not status.errors:
                lines.append("[WARN] 项目配置有警告:")
            for w in status.warnings:
                lines.append(f"  - {w}")

    # MICROKEEN FLM 状态
    if status.flm_on_microkeen:
        lines.append(f"[OK] FLM 文件已在 MICROKEEN: {status.microkeen_flm_path}")

    if status.needs_init():
        lines.append("")
        lines.append("提示: 运行 `/mklink-project-init` 初始化项目配置")

    return "\n".join(lines)
