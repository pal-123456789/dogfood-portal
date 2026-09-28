# src/webhooks/test_ssrf.py
"""DB-free, NETWORK-FREE unit tests for the SSRF gate (run: python src/manage.py test webhooks).

Every case uses IP-LITERAL URLs or an INJECTED fake resolver, so no real DNS or socket call is ever
made. A resolver that raises AssertionError proves the literal/scheme/credential paths never touch
the network at all."""
from django.test import SimpleTestCase

from webhooks.ssrf import SsrfError, is_blocked_ip, validate_url


def _no_dns(host):
    raise AssertionError("no DNS should be attempted for %r" % host)


class IsBlockedIpTests(SimpleTestCase):
    def test_blocked_ip_literals(self):
        for ip in ["127.0.0.1", "10.0.0.5", "169.254.169.254", "::1", "fc00::1", "0.0.0.0",
                   "192.168.1.1", "172.16.0.1", "::ffff:10.0.0.5", "fd00:ec2::254", "224.0.0.1"]:
            self.assertTrue(is_blocked_ip(ip), "%s should be blocked" % ip)

    def test_allowed_public_ip_literals(self):
        for ip in ["93.184.216.34", "8.8.8.8", "2606:2800:220:1:248:1893:25c8:1946"]:
            self.assertFalse(is_blocked_ip(ip), "%s should be allowed" % ip)

    def test_non_ip_string_is_blocked(self):
        self.assertTrue(is_blocked_ip("not-an-ip"))
        self.assertTrue(is_blocked_ip(""))


class ValidateUrlTests(SimpleTestCase):
    def test_blocked_ip_literal_urls_raise_without_dns(self):
        for url in ["http://127.0.0.1/", "http://10.0.0.5/",
                    "http://169.254.169.254/latest/meta-data", "http://[::1]/",
                    "http://[fc00::1]/", "http://0.0.0.0/", "http://192.168.1.1/",
                    "http://172.16.0.1/"]:
            with self.assertRaises(SsrfError, msg=url):
                validate_url(url, resolver=_no_dns)

    def test_scheme_credential_and_schemeless_rejected(self):
        for url in ["ftp://example.com/x", "http://user:pass@example.com/x",
                    "https://user@example.com/x", "example.com/x", "file:///etc/passwd"]:
            with self.assertRaises(SsrfError, msg=url):
                validate_url(url, resolver=_no_dns)

    def test_public_ip_literal_allowed_without_resolver(self):
        ips = validate_url("http://93.184.216.34/", resolver=_no_dns)
        self.assertEqual(ips, ["93.184.216.34"])

    def test_name_resolving_to_internal_is_blocked(self):
        with self.assertRaises(SsrfError):
            validate_url("http://hook.example.com/x", resolver=lambda h: ["10.0.0.9"])

    def test_name_resolving_to_public_is_allowed(self):
        ips = validate_url("https://hook.example.com/x", resolver=lambda h: ["93.184.216.34"])
        self.assertEqual(ips, ["93.184.216.34"])

    def test_name_with_any_internal_address_is_blocked(self):
        # mixed answer: one public, one internal -> ANY blocked address rejects the whole URL
        with self.assertRaises(SsrfError):
            validate_url("http://hook.example.com/x",
                         resolver=lambda h: ["93.184.216.34", "127.0.0.1"])
