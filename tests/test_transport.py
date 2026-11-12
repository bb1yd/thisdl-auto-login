import ctypes
import socket
import unittest
from unittest.mock import Mock, patch

from thisdl_login import transport


class TransportTests(unittest.TestCase):
    def fake_dns(self, error=0):
        lib = Mock()

        def start(reference, flags, index, protocol, hostname, callback, context):
            reference._obj.value = 123
            lib.callback = callback
            return 0

        def reply(reference):
            # Darwin sockaddr_in: byte length, byte family, port, IPv4 address.
            raw = bytes([16, socket.AF_INET, 0, 0]) + socket.inet_aton("192.0.2.8") + bytes(8)
            address = ctypes.create_string_buffer(raw)
            lib.callback(reference, 2, 7, error, b"example.test", ctypes.addressof(address), 60, None)
            return 0

        lib.DNSServiceGetAddrInfo.side_effect = start
        lib.DNSServiceRefSockFD.return_value = 9
        lib.DNSServiceProcessResult.side_effect = reply
        return lib

    def test_numeric_gateway_does_not_need_dns(self):
        with patch.object(transport, "_dns_library", side_effect=AssertionError("unexpected DNS")):
            self.assertEqual(transport.resolve_ipv4("10.0.11.251", 7, 5), "10.0.11.251")

    def test_dns_uses_wifi_interface_and_releases_query(self):
        lib = self.fake_dns()
        with patch.object(transport, "_dns_library", return_value=lib), \
             patch.object(transport.select, "select", return_value=([9], [], [])):
            self.assertEqual(transport.resolve_ipv4("example.test", 7, 5), "192.0.2.8")
        args = lib.DNSServiceGetAddrInfo.call_args.args
        self.assertEqual(args[2:5], (7, 1, b"example.test"))
        lib.DNSServiceRefDeallocate.assert_called_once()
        self.assertEqual(lib.DNSServiceRefDeallocate.call_args.args[0].value, 123)

    def test_dns_timeout_releases_query(self):
        lib = self.fake_dns()
        with patch.object(transport, "_dns_library", return_value=lib), \
             patch.object(transport.select, "select", return_value=([], [], [])):
            with self.assertRaises(TimeoutError):
                transport.resolve_ipv4("example.test", 7, 5)
        lib.DNSServiceRefDeallocate.assert_called_once()
        lib.DNSServiceProcessResult.assert_not_called()

    def test_dns_error_preserves_manual_login(self):
        lib = self.fake_dns(error=-65554)
        with patch.object(transport, "_dns_library", return_value=lib), \
             patch.object(transport.select, "select", return_value=([9], [], [])):
            with self.assertRaisesRegex(OSError, "-65554"):
                transport.resolve_ipv4("example.test", 7, 5)
        lib.DNSServiceRefDeallocate.assert_called_once()

    def test_http_connects_without_default_dns_or_route(self):
        sock = Mock()
        with patch.object(transport.socket, "if_nametoindex", return_value=7), \
             patch.object(transport, "resolve_ipv4", return_value="10.0.11.251") as resolve, \
             patch.object(transport.socket, "socket", return_value=sock), \
             patch.object(transport.socket, "getaddrinfo", side_effect=OSError("default DNS unavailable")), \
             patch.object(transport.socket, "create_connection", side_effect=OSError("default route unavailable")):
            connection = transport.WiFiHTTPConnection("10.0.11.251:8080", "en0", timeout=5)
            connection.connect()
        resolve.assert_called_once_with("10.0.11.251", 7, 5)
        sock.setsockopt.assert_called_once_with(socket.IPPROTO_IP, transport.IP_BOUND_IF, 7)
        sock.connect.assert_called_once_with(("10.0.11.251", 8080))
        self.assertIs(connection.sock, sock)
        sock.close.assert_not_called()

    def test_failed_connection_closes_socket(self):
        sock = Mock()
        sock.connect.side_effect = OSError("network changed")
        with patch.object(transport.socket, "if_nametoindex", return_value=7), \
             patch.object(transport, "resolve_ipv4", return_value="10.0.11.251"), \
             patch.object(transport.socket, "socket", return_value=sock):
            connection = transport.WiFiHTTPConnection("10.0.11.251:8080", "en0", timeout=5)
            with self.assertRaisesRegex(OSError, "network changed"):
                connection.connect()
        sock.close.assert_called_once()
        self.assertIsNone(connection.sock)

    def test_discovers_wifi_instead_of_assuming_en0(self):
        output = ("Hardware Port: Ethernet\nDevice: en0\nEthernet Address: ...\n\n"
                  "Hardware Port: Wi-Fi\nDevice: en2\nEthernet Address: ...\n")
        with patch.object(transport.subprocess, "run", return_value=Mock(stdout=output)):
            self.assertEqual(transport.wifi_interface(), "en2")


if __name__ == "__main__":
    unittest.main()
