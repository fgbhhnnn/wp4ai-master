#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
一键打包脚本（Python 版）

用法：
1) 全量打包（默认）:
   python build.py

2) 仅打包 GUI:
   python build.py --only gui

3) 仅打包 CLI:
   python build.py --only cli

4) 跳过依赖安装（更快）:
   python build.py --skip-deps
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")


ROOT = Path(__file__).resolve().parent
DIST_DIR = ROOT / "dist"
BUILD_DIR = ROOT / "build"

WORKER_SCRIPT = ROOT / "wp4ai_generate.py"
GUI_SCRIPT = ROOT / "wp4ai_gui.py"
YOAST_PLUGIN_DIR = ROOT / "wordpress-plugin" / "wp4ai-yoast-rest-meta"
RELEASE_ZIP = ROOT / "dist.zip"

COMMON_EXCLUDES = [
    "--exclude-module", "torch",
    "--exclude-module", "tensorflow",
    "--exclude-module", "pandas",
    "--exclude-module", "scipy",
    "--exclude-module", "numpy",
    "--exclude-module", "pyarrow",
    "--exclude-module", "matplotlib",
    "--exclude-module", "pytest",
    "--exclude-module", "sqlalchemy",
    "--exclude-module", "PIL",
]


def log(msg: str) -> None:
    print(msg, flush=True)


def run_step(
    name: str,
    cmd: list[str],
    cwd: Path = ROOT,
    timeout: int | None = None,
) -> None:
    log(f"\n==> {name}")
    log("$ " + " ".join(cmd))
    try:
        result = subprocess.run(cmd, cwd=str(cwd), timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"步骤超时: {name} (>{timeout}s)") from exc
    if result.returncode != 0:
        raise RuntimeError(f"步骤失败: {name} (exit={result.returncode})")


def module_exists(python_exe: str, module_name: str) -> bool:
    code = (
        "import importlib.util,sys;"
        f"sys.exit(0 if importlib.util.find_spec('{module_name}') else 1)"
    )
    ret = subprocess.run([python_exe, "-c", code], cwd=str(ROOT))
    return ret.returncode == 0


def pip_install_cmd(
    python_exe: str,
    packages: list[str],
    pip_timeout: int,
    pip_retries: int,
    pip_index_url: str,
    upgrade: bool = False,
) -> list[str]:
    cmd = [
        python_exe,
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--no-input",
        "--retries",
        str(pip_retries),
        "--timeout",
        str(pip_timeout),
    ]
    if pip_index_url:
        cmd += ["-i", pip_index_url]
    if upgrade:
        cmd.append("--upgrade")
    cmd += packages
    return cmd


def ensure_minimum_runtime(
    python_exe: str,
    skip_deps: bool,
    pip_timeout: int,
    pip_retries: int,
    pip_index_url: str,
) -> None:
    """
    确保打包最低依赖存在。
    - 无论是否 --skip-deps，都必须保证 PyInstaller 存在；
    - --skip-deps 模式下仅补装缺失的最小包，而不全量安装 requirements。
    """
    must_have = [
        ("PyInstaller", "pyinstaller"),
    ]
    minimal_runtime = [
        ("openai", "openai"),
        ("httpx", "httpx"),
        ("requests", "requests"),
    ]

    missing: list[tuple[str, str]] = []
    for module_name, pip_name in must_have:
        if not module_exists(python_exe, module_name):
            missing.append((module_name, pip_name))

    if skip_deps:
        for module_name, pip_name in minimal_runtime:
            if not module_exists(python_exe, module_name):
                missing.append((module_name, pip_name))

    if not missing:
        return

    uniq_pips: list[str] = []
    for _, pip_name in missing:
        if pip_name not in uniq_pips:
            uniq_pips.append(pip_name)

    log("==> 检测到当前 Python 缺少必要包，自动补装: " + ", ".join(uniq_pips))
    run_step(
        "补装最低依赖",
        pip_install_cmd(
            python_exe=python_exe,
            packages=uniq_pips,
            pip_timeout=pip_timeout,
            pip_retries=pip_retries,
            pip_index_url=pip_index_url,
        ),
    )


def kill_process_locks() -> None:
    """结束可能占用 dist/build 的旧进程。"""
    if os.name != "nt":
        return

    targets = ["wp4ai_gui.exe", "wp4ai_generate_cli.exe"]
    killed_any = False
    for image in targets:
        # /T: 同时结束子进程；/F: 强制结束
        ret = subprocess.run(
            ["taskkill", "/F", "/T", "/IM", image],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if ret.returncode == 0:
            killed_any = True
    if killed_any:
        log("==> 已结束旧的 wp4ai 进程占用。")
        time.sleep(0.8)


def remove_dir_with_retry(path: Path, retries: int = 5) -> None:
    if not path.exists():
        return

    log(f"==> 清理目录: {path}")

    # Windows 上优先使用 rmdir /s /q，通常比 shutil.rmtree 更稳定
    if os.name == "nt":
        try:
            ret = subprocess.run(
                ["cmd", "/c", "rmdir", "/s", "/q", str(path)],
                timeout=120,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if ret.returncode == 0 and not path.exists():
                return
        except subprocess.TimeoutExpired:
            log(f"   - rmdir 超时，改用 Python 删除: {path}")
        except Exception:
            pass

    last_error: Exception | None = None
    for i in range(1, retries + 1):
        try:
            shutil.rmtree(path)
            return
        except Exception as e:  # noqa: BLE001
            last_error = e
            log(f"   - 删除失败 ({i}/{retries}): {path} | {e}")
            time.sleep(0.4 * i)

    # 兜底：尝试改名，避免完全阻塞打包流程
    stamp = time.strftime("%Y%m%d_%H%M%S")
    backup = path.with_name(f"{path.name}.locked_{stamp}")
    try:
        path.rename(backup)
        log(f"   - 目录被占用，已改名为: {backup}")
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"清理目录失败: {path} | {last_error} | rename失败: {e}") from e


def ensure_inputs_exist() -> None:
    missing = []
    for fp in [WORKER_SCRIPT, GUI_SCRIPT, ROOT / "requirements.txt"]:
        if not fp.exists():
            missing.append(str(fp))
    if missing:
        raise FileNotFoundError("缺少必要文件:\n- " + "\n- ".join(missing))


def build_release_archives(
    dist_dir: Path,
    plugin_dir: Path,
    release_zip: Path,
    readme_path: Path | None = None,
) -> None:
    """Create allowlisted release archives without local site credentials."""
    if not plugin_dir.is_dir():
        raise FileNotFoundError(f"缺少 Yoast WordPress 插件目录: {plugin_dir}")

    plugin_zip = dist_dir / "wp4ai-yoast-rest-meta.zip"
    with zipfile.ZipFile(plugin_zip, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for file_path in sorted(plugin_dir.rglob("*")):
            if file_path.is_file():
                relative_path = file_path.relative_to(plugin_dir)
                archive.write(
                    file_path,
                    arcname=(Path(plugin_dir.name) / relative_path).as_posix(),
                )

    executable_paths = [
        dist_dir / "wp4ai_gui.exe",
        dist_dir / "wp4ai_generate_cli.exe",
    ]
    available_executables = [path for path in executable_paths if path.is_file()]
    if not available_executables:
        raise FileNotFoundError(f"发布目录中没有可打包的 EXE: {dist_dir}")

    release_files = [*available_executables, plugin_zip]
    if readme_path is not None and readme_path.is_file():
        release_files.append(readme_path)

    with zipfile.ZipFile(release_zip, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for file_path in release_files:
            archive.write(file_path, arcname=file_path.name)

    log(f"==> WordPress 插件包: {plugin_zip}")
    log(f"==> 安全发布包（不含 wp4ai_sites.db/config.ini）: {release_zip}")


def build_cli_cmd(python_exe: str) -> list[str]:
    return [
        python_exe, "-m", "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--console",
        "--name", "wp4ai_generate_cli",
        "--collect-all", "openai",
        "--collect-all", "httpx",
        *COMMON_EXCLUDES,
        str(WORKER_SCRIPT),
    ]


def build_gui_cmd(python_exe: str) -> list[str]:
    return [
        python_exe, "-m", "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--windowed",
        "--name", "wp4ai_gui",
        "--collect-all", "openai",
        "--collect-all", "httpx",
        *COMMON_EXCLUDES,
        str(GUI_SCRIPT),
    ]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="WP4AI 一键打包脚本（Python 版）")
    parser.add_argument(
        "--only",
        choices=["all", "gui", "cli"],
        default="all",
        help="仅打包指定目标（默认 all）",
    )
    parser.add_argument(
        "--skip-deps",
        action="store_true",
        help="跳过 pip 依赖安装步骤",
    )
    parser.add_argument(
        "--no-clean",
        action="store_true",
        help="不清理 build/dist（用于目录清理很慢或占用时）",
    )
    parser.add_argument(
        "--python",
        default=sys.executable,
        help="指定 Python 解释器路径（默认当前解释器）",
    )
    parser.add_argument(
        "--upgrade-pip",
        action="store_true",
        help="安装依赖前先升级 pip（默认关闭，避免网络较慢时长时间等待）",
    )
    parser.add_argument(
        "--pip-timeout",
        type=int,
        default=120,
        help="pip 单次网络超时时间（秒），默认 120",
    )
    parser.add_argument(
        "--pip-retries",
        type=int,
        default=2,
        help="pip 下载重试次数，默认 2",
    )
    parser.add_argument(
        "--pip-index-url",
        default=os.getenv("PIP_INDEX_URL", ""),
        help="可选：指定 pip 镜像源，例如 https://pypi.tuna.tsinghua.edu.cn/simple",
    )
    parser.add_argument(
        "--step-timeout",
        type=int,
        default=0,
        help="可选：为每个构建步骤设置超时（秒），0 表示不限制",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    os.chdir(ROOT)
    log(f"==> 工作目录: {ROOT}")

    ensure_inputs_exist()
    kill_process_locks()
    if args.no_clean:
        log("==> 已跳过旧产物清理（--no-clean）")
    else:
        remove_dir_with_retry(BUILD_DIR)
        remove_dir_with_retry(DIST_DIR)

    py = args.python
    log(f"==> 使用 Python: {py}")
    step_timeout = args.step_timeout if args.step_timeout > 0 else None

    if not args.skip_deps:
        if args.upgrade_pip:
            run_step(
                "升级 pip",
                pip_install_cmd(
                    python_exe=py,
                    packages=["pip"],
                    pip_timeout=args.pip_timeout,
                    pip_retries=args.pip_retries,
                    pip_index_url=args.pip_index_url,
                    upgrade=True,
                ),
                timeout=step_timeout,
            )
        else:
            log("==> 已跳过 pip 升级（如需升级可加 --upgrade-pip）")

        run_step(
            "安装依赖",
            pip_install_cmd(
                python_exe=py,
                packages=["-r", "requirements.txt"],
                pip_timeout=args.pip_timeout,
                pip_retries=args.pip_retries,
                pip_index_url=args.pip_index_url,
            ),
            timeout=step_timeout,
        )
    else:
        log("==> 已跳过依赖安装（--skip-deps）")
        ensure_minimum_runtime(
            py,
            skip_deps=True,
            pip_timeout=args.pip_timeout,
            pip_retries=args.pip_retries,
            pip_index_url=args.pip_index_url,
        )

    if not module_exists(py, "PyInstaller"):
        run_step(
            "安装 PyInstaller",
            pip_install_cmd(
                python_exe=py,
                packages=["pyinstaller"],
                pip_timeout=args.pip_timeout,
                pip_retries=args.pip_retries,
                pip_index_url=args.pip_index_url,
            ),
            timeout=step_timeout,
        )

    if args.only in ("all", "cli"):
        run_step("构建 wp4ai_generate_cli.exe", build_cli_cmd(py), timeout=step_timeout)
    if args.only in ("all", "gui"):
        run_step("构建 wp4ai_gui.exe", build_gui_cmd(py), timeout=step_timeout)

    build_release_archives(
        dist_dir=DIST_DIR,
        plugin_dir=YOAST_PLUGIN_DIR,
        release_zip=RELEASE_ZIP,
        readme_path=ROOT / "README.md",
    )

    log(f"\n✅ 打包完成！输出目录: {DIST_DIR}")
    if (DIST_DIR / "wp4ai_gui.exe").exists():
        log("   - dist\\wp4ai_gui.exe")
    if (DIST_DIR / "wp4ai_generate_cli.exe").exists():
        log("   - dist\\wp4ai_generate_cli.exe")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001
        log(f"\n❌ 打包失败: {exc}")
        raise SystemExit(1)
