"""HTTP and DNS scoped to the Wi-Fi interface during macOS captive login."""

import ctypes
import functools
import http.client
import select
import socket
import subprocess
import time
from urllib.request import HTTPHandler

PTR = ctypes.c_void_p
DNS_CALLBACK = ctypes.CFUNCTYPE(None, PTR, ctypes.c_uint32, ctypes.c_uint32,
                               ctypes.c_int32, ctypes.c_char_p, PTR, ctypes.c_uint32, PTR)
IP_BOUND_IF = 25  # macOS netinet/in.h; affects only this socket, not system routes.


def wifi_interface():
    result = subprocess.run(["/usr/sbin/networksetup", "-listallhardwareports"],
                            capture_output=True, text=True, timeout=3, check=True)
    for block in result.stdout.split("\n\n"):
        if "Hardware Port: Wi-Fi" in block or "Hardware Port: AirPort" in block:
            for line in block.splitlines():
                if line.startswith("Device: "):
                    return line.split(": ", 1)[1].strip()
    raise RuntimeError("无法找到 Wi-Fi 网络接口。")


@functools.lru_cache(maxsize=1)
def _dns_library():
    lib = ctypes.CDLL("/usr/lib/libSystem.dylib")
    lib.DNSServiceGetAddrInfo.argtypes = [ctypes.POINTER(PTR), ctypes.c_uint32,
                                        ctypes.c_uint32, ctypes.c_uint32,
                                        ctypes.c_char_p, DNS_CALLBACK, PTR]
    lib.DNSServiceGetAddrInfo.restype = ctypes.c_int32
    lib.DNSServiceRefSockFD.argtypes = [PTR]
    lib.DNSServiceRefSockFD.restype = ctypes.c_int
    lib.DNSServiceProcessResult.argtypes = [PTR]
    lib.DNSServiceProcessResult.restype = ctypes.c_int32
    lib.DNSServiceRefDeallocate.argtypes = [PTR]
    lib.DNSServiceRefDeallocate.restype = None
    return lib


def resolve_ipv4(hostname, interface_index, timeout):
    try:
        socket.inet_pton(socket.AF_INET, hostname)
        return hostname
    except OSError:
        pass
    lib = _dns_library()
    addresses, errors = [], []

    def answer(ref, flags, index, error, name, address, ttl, context):
        if error:
            errors.append(error)
        elif address and flags & 2:  # kDNSServiceFlagsAdd; ignore removed records.
            raw = ctypes.string_at(address, 8)
            if raw[1] == socket.AF_INET:
                addresses.append(socket.inet_ntoa(raw[4:8]))

    callback = DNS_CALLBACK(answer)
    ref = PTR()
    error = lib.DNSServiceGetAddrInfo(ctypes.byref(ref), 0, interface_index, 1,
                                     hostname.encode("idna"), callback, None)
    if error:
        raise OSError("Wi-Fi DNS 查询无法启动（%s）。" % error)
    try:
        fd = lib.DNSServiceRefSockFD(ref)
        if fd < 0:
            raise OSError("Wi-Fi DNS 查询没有可用连接。")
        deadline = time.monotonic() + timeout
        while not addresses and not errors:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([fd], [], [], remaining)[0]:
                raise TimeoutError("Wi-Fi DNS 查询超时。")
            error = lib.DNSServiceProcessResult(ref)
            if error:
                raise OSError("Wi-Fi DNS 查询失败（%s）。" % error)
        if errors:
            raise OSError("Wi-Fi DNS 没有返回地址（%s）。" % errors[0])
        return addresses[0]
    finally:
        lib.DNSServiceRefDeallocate(ref)


class WiFiHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host, interface, **kwargs):
        super().__init__(host, **kwargs)
        self.interface_index = socket.if_nametoindex(interface)

    def connect(self):
        # Both lookup and TCP need an interface: binding TCP alone still leaves
        # hostname lookup on the unavailable default network during captive login.
        if self._tunnel_host:
            raise OSError("认证连接不使用代理隧道。")
        deadline = time.monotonic() + self.timeout
        address = resolve_ipv4(self.host, self.interface_index, self.timeout)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Wi-Fi 认证连接超时。")
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.setsockopt(socket.IPPROTO_IP, IP_BOUND_IF, self.interface_index)
            sock.settimeout(remaining)
            sock.connect((address, self.port))
        except BaseException:
            sock.close()
            raise
        self.sock = sock


class WiFiHTTPHandler(HTTPHandler):
    def __init__(self, interface):
        super().__init__()
        self.interface = interface

    def http_open(self, request):
        def connection(host, **kwargs):
            return WiFiHTTPConnection(host, self.interface, **kwargs)
        return self.do_open(connection, request)
