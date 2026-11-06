import unittest
from unittest.mock import Mock, patch

from projects.provider_store.pinned_https_transport import (
    PinnedHTTPSConnection,
    PinnedHTTPSHandler,
)


class PinnedHTTPSTransportTests(unittest.TestCase):
    def test_tcp_uses_pinned_ip_and_tls_uses_original_hostname(self):
        connection = PinnedHTTPSConnection("api.example.com", "93.184.216.34", timeout=4)
        connection._context = Mock()
        wrapped = Mock(name="tls_socket")
        connection._context.wrap_socket.return_value = wrapped
        raw = Mock(name="raw_socket")
        with patch(
            "projects.provider_store.pinned_https_transport.socket.create_connection",
            return_value=raw,
        ) as create:
            connection.connect()
        create.assert_called_once_with(("93.184.216.34", 443), 4, None)
        connection._context.wrap_socket.assert_called_once_with(
            raw, server_hostname="api.example.com"
        )
        self.assertIs(connection.sock, wrapped)

    def test_tls_handshake_failure_closes_raw_socket(self):
        connection = PinnedHTTPSConnection("api.example.com", "93.184.216.34")
        connection._context = Mock()
        connection._context.wrap_socket.side_effect = OSError("handshake failed")
        raw = Mock(name="raw_socket")
        with patch(
            "projects.provider_store.pinned_https_transport.socket.create_connection",
            return_value=raw,
        ):
            with self.assertRaises(OSError):
                connection.connect()
        raw.close.assert_called_once_with()

    def test_proxy_tunneling_is_rejected(self):
        connection = PinnedHTTPSConnection("api.example.com", "93.184.216.34")
        connection.set_tunnel("proxy.example.com")
        with patch(
            "projects.provider_store.pinned_https_transport.socket.create_connection"
        ) as create:
            with self.assertRaisesRegex(OSError, "proxy tunneling is disabled"):
                connection.connect()
        create.assert_not_called()

    def test_handler_requires_a_pinned_ip(self):
        with self.assertRaisesRegex(ValueError, "validated pinned IP"):
            PinnedHTTPSHandler("")


if __name__ == "__main__":
    unittest.main()
