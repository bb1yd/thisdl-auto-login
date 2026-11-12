"""User-scoped installation and launchd management."""

import argparse
import logging
import logging.handlers
import os
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

LABEL = "cn.thisdl.login"
LEGACY_LABEL = "local.user.campus-auto-connect"
HOME = Path.home()
APP = HOME / "Library" / "Application Support" / "ThisDLLogin"
LAUNCHER = APP / "thisdl-login"
AGENT = HOME / "Library" / "LaunchAgents" / (LABEL + ".plist")
BIN = HOME / ".local" / "bin" / "thisdl-login"
LOG = APP / "activity.log"
MARKER = APP / ".thisdl-login"
READY = APP / ".ready"
SOURCE = Path(__file__).resolve().parent.parent
FILES = ("__init__.py", "auth.py", "cli.py", "monitor.py")


def domain():
    return "gui/%s" % os.getuid()


def launchctl(*arguments):
    return subprocess.run(["/bin/launchctl", *arguments], capture_output=True, text=True)


def loaded():
    return launchctl("print", domain() + "/" + LABEL).returncode == 0


def owned_agent(path=None, label=LABEL, launcher=None):
    path = AGENT if path is None else path
    launcher = LAUNCHER if launcher is None else launcher
    if not path.exists():
        return False
    with path.open("rb") as stream:
        config = plistlib.load(stream)
    return config.get("Label") == label and str(launcher) in config.get("ProgramArguments", [])


def atomic_write(path, data, mode):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".thisdl-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        os.chmod(temp, mode)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def check_link():
    if BIN.is_symlink():
        if BIN.resolve(strict=False) != LAUNCHER.resolve(strict=False):
            raise RuntimeError("%s 已被其他程序使用。" % BIN)
    elif BIN.exists():
        raise RuntimeError("%s 已存在，未覆盖。" % BIN)


def migrate_legacy():
    old = HOME / "Library" / "LaunchAgents" / (LEGACY_LABEL + ".plist")
    old_launcher = HOME / "Library" / "Application Support" / "CampusAutoLogin" / "campus-auto-connect.py"
    if not old.exists():
        return
    if not owned_agent(old, LEGACY_LABEL, old_launcher):
        raise RuntimeError("发现名称相同但不属于旧版的后台任务，未修改。")
    old_target = domain() + "/" + LEGACY_LABEL
    if launchctl("print", old_target).returncode == 0:
        result = launchctl("bootout", old_target)
        if result.returncode:
            raise RuntimeError("无法停用旧版后台任务：" + result.stderr.strip())
    old.unlink()
    print("已停用旧版测试任务；旧版文件和日志予以保留。")


def install():
    check_link()
    if APP.exists() and not MARKER.exists():
        raise RuntimeError("安装目录已存在且不属于本工具：%s" % APP)
    source_launcher = SOURCE / "thisdl-login"
    source_package = SOURCE / "thisdl_login"
    if not source_launcher.is_file() or not all((source_package / name).is_file() for name in FILES):
        raise RuntimeError("安装文件不完整，请保留 thisdl-login 与 thisdl_login 目录。")
    if AGENT.exists() and not owned_agent():
        raise RuntimeError("发现同名但不属于本工具的后台任务，未覆盖。")
    # Read first so reinstalling from the installed copy is safe.
    launcher_data = source_launcher.read_bytes()
    sources = {name: (source_package / name).read_bytes() for name in FILES}
    if APP.exists():
        write_installation(APP, launcher_data, sources)
    else:
        # Publish a complete, marked directory in one rename. Even a killed
        # installer cannot leave an unmarked APP that blocks the next attempt.
        APP.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".thisdl-install-", dir=APP.parent) as temp:
            staged = Path(temp) / APP.name
            write_installation(staged, launcher_data, sources)
            staged.rename(APP)
    BIN.parent.mkdir(parents=True, exist_ok=True)
    if not BIN.is_symlink():
        BIN.symlink_to(LAUNCHER)
    was_loaded = loaded()
    if was_loaded:
        result = launchctl("bootout", domain() + "/" + LABEL)
        if result.returncode:
            raise RuntimeError("无法重启已有后台任务：" + result.stderr.strip())
    enable()
    try:
        migrate_legacy()
    except (OSError, RuntimeError, ValueError, plistlib.InvalidFileException):
        disable()
        raise
    print("安装完成。")
    path_entries = os.environ.get("PATH", "").split(os.pathsep)
    if str(BIN.parent) not in path_entries:
        print("提示：%s 不在 PATH 中，可先使用 %s。" % (BIN.parent, BIN))


def write_installation(root, launcher_data, sources):
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    package = root / "thisdl_login"
    package.mkdir(mode=0o700, exist_ok=True)
    atomic_write(root / LAUNCHER.name, launcher_data, 0o700)
    for name, data in sources.items():
        atomic_write(package / name, data, 0o600)
    atomic_write(root / MARKER.name, (LABEL + "\n").encode(), 0o600)


def enable():
    if not MARKER.is_file() or not LAUNCHER.is_file():
        raise RuntimeError("尚未安装，请先运行 thisdl-login install。")
    if AGENT.exists() and not owned_agent():
        raise RuntimeError("发现同名但不属于本工具的后台任务，未修改。")
    if loaded():
        if not AGENT.exists():
            raise RuntimeError("发现没有本工具配置文件的同名运行任务，未修改。")
        print("自动登录已启用。")
        return
    config = {
        "Label": LABEL,
        "ProgramArguments": [os.path.abspath(sys.executable), str(LAUNCHER), "_run"],
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 30,
        "ProcessType": "Background",
        "StandardErrorPath": str(APP / "errors.log"),
    }
    READY.unlink(missing_ok=True)
    atomic_write(AGENT, plistlib.dumps(config), 0o600)
    result = launchctl("bootstrap", domain(), str(AGENT))
    if result.returncode:
        AGENT.unlink(missing_ok=True)
        raise RuntimeError("无法启动后台任务：" + result.stderr.strip())
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        info = launchctl("print", domain() + "/" + LABEL)
        match = re.search(r"^\s*pid\s*=\s*(\d+)\s*$", info.stdout, re.MULTILINE)
        try:
            ready_pid = READY.read_text(encoding="ascii").strip()
        except (OSError, UnicodeError):
            ready_pid = ""
        if info.returncode == 0 and match and match.group(1) == ready_pid:
            break
        time.sleep(0.1)
    else:
        launchctl("bootout", domain() + "/" + LABEL)
        AGENT.unlink(missing_ok=True)
        READY.unlink(missing_ok=True)
        raise RuntimeError("后台监听器未能启动；请查看 %s 和 %s。" % (LOG, APP / "errors.log"))
    print("自动登录已启用。")


def disable():
    if AGENT.exists() and not owned_agent():
        raise RuntimeError("发现同名但不属于本工具的后台任务，未修改。")
    if loaded():
        result = launchctl("bootout", domain() + "/" + LABEL)
        if result.returncode:
            raise RuntimeError("无法停止后台任务：" + result.stderr.strip())
    if AGENT.exists():
        AGENT.unlink()
    READY.unlink(missing_ok=True)
    print("自动登录已停用。")


def uninstall():
    if not MARKER.is_file():
        if AGENT.exists():
            raise RuntimeError("找不到安装标记，未修改现有后台任务。")
        print("本工具未安装。")
        return
    check_link()
    disable()
    if BIN.is_symlink():
        BIN.unlink()
    package = APP / "thisdl_login"
    if package.is_symlink():
        raise RuntimeError("安装目录异常，未删除符号链接。")
    if package.is_dir():
        shutil.rmtree(package)
    for file in (LAUNCHER, MARKER, READY, LOG, APP / "activity.log.1", APP / "errors.log"):
        file.unlink(missing_ok=True)
    try:
        APP.rmdir()
    except OSError:
        print("安装目录含其他文件，已保留：%s" % APP)
    print("卸载完成。")


def status():
    if not MARKER.is_file():
        print("状态：未安装")
    elif not AGENT.exists():
        print("状态：已安装，已停用")
    elif not owned_agent():
        print("状态：后台任务配置冲突")
        return 1
    else:
        result = launchctl("print", domain() + "/" + LABEL)
        if result.returncode:
            print("状态：已启用，但后台任务未运行")
            return 1
        if "state = running" in result.stdout:
            print("状态：已启用，后台任务运行中")
        else:
            print("状态：已启用，但后台任务未运行")
            return 1
    return 0


def daemon():
    os.umask(0o077)
    APP.mkdir(parents=True, exist_ok=True, mode=0o700)
    handler = logging.handlers.RotatingFileHandler(LOG, maxBytes=200000, backupCount=1, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    from . import monitor
    try:
        monitor.run(on_ready=lambda: atomic_write(READY, str(os.getpid()).encode("ascii"), 0o600))
    except Exception:
        logging.exception("后台监听器无法运行。")
        return 1
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv == ["_run"]:
        return daemon()
    parser = argparse.ArgumentParser(prog="thisdl-login", description="校园网自动登录；不带命令时显示当前状态。")
    parser.add_argument("command", nargs="?", choices=("install", "enable", "disable", "uninstall"),
                        help="install 安装并启用；enable 启用；disable 停用；uninstall 卸载")
    args = parser.parse_args(argv)
    if sys.platform != "darwin":
        print("本工具仅支持 macOS。", file=sys.stderr)
        return 1
    if sys.version_info < (3, 9):
        print("本工具需要 Python 3.9 或更高版本。", file=sys.stderr)
        return 1
    try:
        if args.command is None:
            return status()
        {"install": install, "enable": enable, "disable": disable, "uninstall": uninstall}[args.command]()
        return 0
    except (OSError, RuntimeError, ValueError, plistlib.InvalidFileException) as exc:
        print("操作失败：%s" % exc, file=sys.stderr)
        return 1
