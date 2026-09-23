"""Tests for the transfer XML and SIP header rules.

These encode behaviour that was established by runtime testing against Vobiz,
not by reading the docs — which is exactly why they are worth pinning: the
rules are easy to "tidy" into something that silently stops working.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("VOBIZ_AUTH_ID", "test")
os.environ.setdefault("VOBIZ_AUTH_TOKEN", "test")

import server  # noqa: E402


class TransferXML(unittest.TestCase):
    def test_pstn_uses_number_element(self):
        xml = server.build_transfer_xml("pstn", "+10000000000", "h.dev", "https")
        self.assertIn("<Number>+10000000000</Number>", xml)
        self.assertNotIn("<User", xml)

    def test_sip_uses_user_element_and_adds_scheme(self):
        xml = server.build_transfer_xml("sip", "agent@registrar.vobiz.ai", "h.dev", "https")
        self.assertIn("sip:agent@registrar.vobiz.ai", xml)
        self.assertIn("<User", xml)
        self.assertNotIn("<Number>", xml)

    def test_sip_scheme_not_doubled(self):
        xml = server.build_transfer_xml("sip", "sip:a@b.c", "h.dev", "https")
        self.assertNotIn("sip:sip:", xml)

    def test_callback_url_is_present(self):
        # The Dial callbackUrl is the ONLY webhook reporting the B-leg's identity
        # and outcome; hangup_url covers the A-leg only. Losing this makes a
        # transferred leg invisible.
        xml = server.build_transfer_xml("pstn", "+10000000000", "h.dev", "https")
        self.assertIn('callbackUrl="https://h.dev/dial-events"', xml)

    def test_fallback_speak_follows_dial(self):
        # Elements after </Dial> run only if the bridge never happens.
        xml = server.build_transfer_xml("pstn", "+10000000000", "h.dev", "https")
        self.assertLess(xml.index("</Dial>"), xml.index("could not be completed"))

    def test_sip_headers_land_on_both_dial_and_user(self):
        xml = server.build_transfer_xml("sip", "a@b.c", "h.dev", "https", "X-VH-Ref=abc123")
        self.assertEqual(xml.count('sipHeaders="X-VH-Ref=abc123"'), 2)


class SipHeaderRules(unittest.TestCase):
    def test_prefixed_key_is_accepted_without_warning(self):
        cleaned, warnings = server.validate_sip_headers("X-VH-Ref=abc123")
        self.assertEqual(cleaned, "X-VH-Ref=abc123")
        self.assertEqual(warnings, [])

    def test_missing_prefix_warns(self):
        _, warnings = server.validate_sip_headers("Ref=abc123")
        self.assertTrue(any("X-VH-" in w for w in warnings))

    def test_non_alphanumeric_value_warns(self):
        # Free text cannot ride in a SIP header; callers must send an opaque id.
        _, warnings = server.validate_sip_headers("X-VH-Note=needs help")
        self.assertTrue(any("alphanumeric" in w for w in warnings))

    def test_multiple_pairs_round_trip(self):
        cleaned, warnings = server.validate_sip_headers("X-VH-Ref=a1, X-VH-Clinic=b2")
        self.assertEqual(cleaned, "X-VH-Ref=a1,X-VH-Clinic=b2")
        self.assertEqual(warnings, [])

    def test_malformed_pair_is_dropped_not_crashed(self):
        cleaned, warnings = server.validate_sip_headers("nonsense")
        self.assertEqual(cleaned, "")
        self.assertTrue(warnings)


if __name__ == "__main__":
    unittest.main()
