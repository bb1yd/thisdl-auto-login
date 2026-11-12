"""macOS network and wake notifications using only the Python standard library."""

import ctypes
import ctypes.util
import logging
import os
import plistlib
import pwd
import subprocess
import threading
import time

from . import auth

LOG = logging.getLogger(__name__)
NETWORK_PATTERNS = (
    "State:/Network/Global/IPv4", "State:/Network/Global/IPv6",
    "State:/Network/Interface/.*/IPv4", "State:/Network/Interface/.*/IPv6",
    "State:/Network/Interface/.*/AirPort",
)
SYSTEM_CAN_SLEEP = 0xE0000270
SYSTEM_WILL_SLEEP = 0xE0000280
SYSTEM_HAS_POWERED_ON = 0xE0000300
CF_UTF8 = 0x08000100
PTR = ctypes.c_void_p
NETWORK_CALLBACK = ctypes.CFUNCTYPE(None, PTR, PTR, PTR)
POWER_CALLBACK = ctypes.CFUNCTYPE(None, PTR, ctypes.c_uint, ctypes.c_uint32, PTR)


def _function(library, name, result, arguments):
    fn = getattr(library, name)
    fn.restype = result
    fn.argtypes = arguments
    return fn


def close_login_window():
    """Send TERM only to this user's macOS captive portal assistant."""
    try:
        user = pwd.getpwuid(os.getuid()).pw_name
        result = subprocess.run(
            ["/usr/bin/killall", "-TERM", "-u", user, "Captive Network Assistant"],
            capture_output=True, timeout=3,
        )
        if result.returncode == 0:
            LOG.info("已请求关闭系统认证窗口。")
            return True
        LOG.info("尚未发现或未能关闭系统认证窗口（退出码 %s）。", result.returncode)
    except (OSError, KeyError, subprocess.TimeoutExpired) as exc:
        LOG.warning("无法关闭系统认证窗口（%s）。", type(exc).__name__)
    return False


class RetryWorker:
    """Coalesce events while keeping network requests off the system run loop."""

    def __init__(self, check=auth.check_and_login, retry_delays=(0, 8, 15), on_success=None,
                 dismiss_delays=(0, 1, 2)):
        self._check = check
        self._retry_delays = retry_delays
        self._on_success = on_success
        self._dismiss_delays = dismiss_delays
        self._condition = threading.Condition()
        self._generation = 0

    def notify(self):
        with self._condition:
            self._generation += 1
            self._condition.notify()

    def _dismiss_after_login(self):
        # The gateway's success redirect is the authority. Waiting for external
        # connectivity here can deadlock while the system login window is open.
        # Keep pending network events for the next check, without discarding a
        # successful login or requiring another portal page to close the window.
        for delay in self._dismiss_delays:
            if delay:
                time.sleep(delay)
            try:
                if self._on_success() is not False:
                    return
            except Exception as exc:
                LOG.warning("处理认证窗口失败（%s）。", type(exc).__name__)
                return
        LOG.info("本轮关闭认证窗口的有限重试已结束。")

    def run(self):
        handled = 0
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._generation != handled)
                handled = self._generation
            for delay in self._retry_delays:
                if delay:
                    with self._condition:
                        changed = self._condition.wait_for(lambda: self._generation != handled, timeout=delay)
                    if changed:
                        break
                try:
                    success = self._check()
                except Exception as exc:
                    LOG.error("校园网检测异常（%s）。", type(exc).__name__)
                    success = False
                if success:
                    if self._on_success is not None:
                        self._dismiss_after_login()
                    break
                with self._condition:
                    if self._generation != handled:
                        break


class MacNotifications:
    def __init__(self, worker):
        self.worker = worker
        self.cf = ctypes.CDLL(ctypes.util.find_library("CoreFoundation"))
        self.sc = ctypes.CDLL(ctypes.util.find_library("SystemConfiguration"))
        self.io = ctypes.CDLL(ctypes.util.find_library("IOKit"))
        self._bind()
        self.store = None
        self.network_source = None
        self.power_port = PTR()
        self.power_notifier = ctypes.c_uint()
        self.power_root = 0
        self.previous = None
        self.network_callback = NETWORK_CALLBACK(self._network_changed)
        self.power_callback = POWER_CALLBACK(self._power_changed)

    def _bind(self):
        c, s, i = self.cf, self.sc, self.io
        _function(c, "CFStringCreateWithCString", PTR, [PTR, ctypes.c_char_p, ctypes.c_uint32])
        _function(c, "CFArrayCreate", PTR, [PTR, ctypes.POINTER(PTR), ctypes.c_long, PTR])
        _function(c, "CFRelease", None, [PTR])
        _function(c, "CFRunLoopGetCurrent", PTR, [])
        _function(c, "CFRunLoopAddSource", None, [PTR, PTR, PTR])
        _function(c, "CFRunLoopRun", None, [])
        _function(c, "CFPropertyListCreateData", PTR, [PTR, PTR, ctypes.c_long, ctypes.c_ulong, PTR])
        _function(c, "CFDataGetLength", ctypes.c_long, [PTR])
        _function(c, "CFDataGetBytePtr", PTR, [PTR])
        _function(s, "SCDynamicStoreCreate", PTR, [PTR, PTR, NETWORK_CALLBACK, PTR])
        _function(s, "SCDynamicStoreSetNotificationKeys", ctypes.c_bool, [PTR, PTR, PTR])
        _function(s, "SCDynamicStoreCreateRunLoopSource", PTR, [PTR, PTR, ctypes.c_long])
        _function(s, "SCDynamicStoreCopyValue", PTR, [PTR, PTR])
        _function(i, "IORegisterForSystemPower", ctypes.c_uint, [PTR, ctypes.POINTER(PTR), POWER_CALLBACK, ctypes.POINTER(ctypes.c_uint)])
        _function(i, "IONotificationPortGetRunLoopSource", PTR, [PTR])
        _function(i, "IOAllowPowerChange", ctypes.c_int, [ctypes.c_uint, ctypes.c_long])
        _function(i, "IODeregisterForSystemPower", ctypes.c_int, [ctypes.POINTER(ctypes.c_uint)])
        _function(i, "IONotificationPortDestroy", None, [PTR])
        _function(i, "IOServiceClose", ctypes.c_int, [ctypes.c_uint])

    def _string(self, value):
        result = self.cf.CFStringCreateWithCString(None, value.encode("utf-8"), CF_UTF8)
        if not result:
            raise RuntimeError("无法创建系统字符串。")
        return result

    def _copy_value(self, key):
        name = self._string(key)
        try:
            value = self.sc.SCDynamicStoreCopyValue(self.store, name)
        finally:
            self.cf.CFRelease(name)
        if not value:
            return None
        try:
            data = self.cf.CFPropertyListCreateData(None, value, 200, 0, None)
            if not data:
                return None
            try:
                raw = ctypes.string_at(self.cf.CFDataGetBytePtr(data), self.cf.CFDataGetLength(data))
                return plistlib.loads(raw)
            finally:
                self.cf.CFRelease(data)
        finally:
            self.cf.CFRelease(value)

    def _snapshot(self):
        ipv4 = self._copy_value("State:/Network/Global/IPv4")
        ipv6 = self._copy_value("State:/Network/Global/IPv6")
        values = [ipv4, ipv6]
        for global_state in (ipv4, ipv6):
            interface = global_state.get("PrimaryInterface") if isinstance(global_state, dict) else None
            if interface:
                for kind in ("IPv4", "IPv6", "AirPort"):
                    values.append(self._copy_value("State:/Network/Interface/%s/%s" % (interface, kind)))
        return values

    def _network_changed(self, store, changed_keys, info):
        try:
            current = self._snapshot()
            if current != self.previous:
                self.previous = current
                LOG.info("网络状态发生变化。")
                self.worker.notify()
        except Exception:
            LOG.exception("处理网络变化失败。")

    def _power_changed(self, refcon, service, message, argument):
        try:
            if message in (SYSTEM_CAN_SLEEP, SYSTEM_WILL_SLEEP):
                self.io.IOAllowPowerChange(self.power_root, int(argument or 0))
            elif message == SYSTEM_HAS_POWERED_ON:
                LOG.info("系统已唤醒。")
                self.worker.notify()
        except Exception:
            LOG.exception("处理电源通知失败。")

    def start(self, on_ready=None):
        name = self._string("ThisDLLogin")
        try:
            self.store = self.sc.SCDynamicStoreCreate(None, name, self.network_callback, None)
        finally:
            self.cf.CFRelease(name)
        if not self.store:
            raise RuntimeError("无法创建网络状态监听器。")
        strings = [self._string(value) for value in NETWORK_PATTERNS]
        try:
            values = (PTR * len(strings))(*strings)
            callbacks = ctypes.addressof((ctypes.c_byte * 40).in_dll(self.cf, "kCFTypeArrayCallBacks"))
            patterns = self.cf.CFArrayCreate(None, values, len(strings), callbacks)
            if not patterns:
                raise RuntimeError("无法创建网络监听条件。")
            try:
                if not self.sc.SCDynamicStoreSetNotificationKeys(self.store, None, patterns):
                    raise RuntimeError("无法订阅网络状态。")
            finally:
                self.cf.CFRelease(patterns)
        finally:
            for string in strings:
                self.cf.CFRelease(string)
        self.network_source = self.sc.SCDynamicStoreCreateRunLoopSource(None, self.store, 0)
        if not self.network_source:
            raise RuntimeError("无法启动网络状态监听。")
        runloop = self.cf.CFRunLoopGetCurrent()
        mode = PTR.in_dll(self.cf, "kCFRunLoopDefaultMode")
        self.cf.CFRunLoopAddSource(runloop, self.network_source, mode)
        self.power_root = self.io.IORegisterForSystemPower(None, ctypes.byref(self.power_port), self.power_callback, ctypes.byref(self.power_notifier))
        if not self.power_root or not self.power_port:
            raise RuntimeError("无法注册系统唤醒通知。")
        power_source = self.io.IONotificationPortGetRunLoopSource(self.power_port)
        if not power_source:
            raise RuntimeError("无法启动系统唤醒监听。")
        self.cf.CFRunLoopAddSource(runloop, power_source, mode)
        self.previous = self._snapshot()
        LOG.info("网络变化和系统唤醒监听已启动。")
        if on_ready is not None:
            on_ready()
        self.worker.notify()
        self.cf.CFRunLoopRun()

    def close(self):
        if self.power_notifier.value:
            self.io.IODeregisterForSystemPower(ctypes.byref(self.power_notifier))
        if self.power_port:
            self.io.IONotificationPortDestroy(self.power_port)
        if self.power_root:
            self.io.IOServiceClose(self.power_root)
        if self.network_source:
            self.cf.CFRelease(self.network_source)
        if self.store:
            self.cf.CFRelease(self.store)


def run(on_ready=None):
    worker = RetryWorker(on_success=close_login_window)
    threading.Thread(target=worker.run, name="campus-login", daemon=True).start()
    monitor = MacNotifications(worker)
    try:
        monitor.start(on_ready=on_ready)
    finally:
        monitor.close()
