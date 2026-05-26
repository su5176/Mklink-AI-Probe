"""支持 python -m mklink 执行。自动修复模块导入路径。"""
import os
import sys
import site
import subprocess

_skill_dir = os.path.dirname(os.path.abspath(__file__))
_base_dir = os.path.dirname(_skill_dir)  # mklink-ai-probe/


def _ensure_package_installed():
    """确保 mklink 包已正确安装。"""
    try:
        import mklink  # noqa: F401
        return True
    except ImportError:
        pass

    # 尝试通过 pip install -e 安装
    try:
        result = subprocess.run(
            [sys.executable, '-m', 'pip', 'install', '-e', _base_dir],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            # 重新加载 site 模块以识别新安装的包
            site.main()
            try:
                import mklink  # noqa: F401
                return True
            except ImportError:
                pass
    except Exception:
        pass
    return False


# 检查 skill 是否在 sys.path 中（.pth 文件方式）
# 如果不在，尝试添加（便携模式）或创建 .pth 文件（持久化模式）
if _base_dir not in sys.path:
    # 先尝试直接添加（便携模式，适合 USB 拷贝到任意电脑）
    sys.path.insert(0, _base_dir)

    # 检查 import 是否成功
    try:
        import mklink  # noqa: F401
    except ImportError:
        # 尝试自动安装
        if not _ensure_package_installed():
            # 安装失败，尝试创建 .pth 文件（持久化模式）
            _pth_path = os.path.join(site.getsitepackages()[0], 'mklink.pth')
            _pth_content = _base_dir.replace('\\', '/')
            if not os.path.exists(_pth_path):
                try:
                    with open(_pth_path, 'w') as f:
                        f.write(_pth_content)
                    # 重新添加并刷新 site 模块
                    if _base_dir not in sys.path:
                        sys.path.insert(0, _base_dir)
                    site.addsitedir(_base_dir)
                except (OSError, PermissionError):
                    pass  # 无法创建 .pth，降级到便携模式

# 保存原工作目录，cli main() 内部会自行切换到项目目录
from mklink.cli import main

raise SystemExit(main())
