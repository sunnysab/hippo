"""Who a request is allowed to claim to be.

The forwarded header is caller-controlled over TCP, so believing it blindly lets
a direct connection borrow someone else's address and slip past a per-address
rate limit.
"""

import unittest
from types import SimpleNamespace

from hippo.api.deps import client_ip


def _request(*, client=('203.0.113.9', 1234), forwarded: str | None = None) -> SimpleNamespace:
    headers = {'x-forwarded-for': forwarded} if forwarded is not None else {}
    return SimpleNamespace(
        headers=headers,
        client=SimpleNamespace(host=client[0]) if client else None,
    )


class ClientIpTest(unittest.TestCase):
    def test_a_direct_connection_cannot_claim_a_forwarded_address(self) -> None:
        """Over TCP the peer address is authoritative, whatever the header says."""
        request = _request(client=('198.51.100.7', 5000), forwarded='1.2.3.4')

        self.assertEqual('198.51.100.7', client_ip(request))

    def test_a_socket_connection_believes_the_proxy(self) -> None:
        """A Unix-socket request has no peer address, which is the proxy's signature."""
        request = _request(client=None, forwarded='1.2.3.4, 10.0.0.1')

        self.assertEqual('1.2.3.4', client_ip(request))

    def test_no_address_at_all_is_none_not_empty(self) -> None:
        self.assertIsNone(client_ip(_request(client=None)))
        self.assertIsNone(client_ip(_request(client=('', 0))))
