import unittest
from datetime import datetime, timezone
from typing import ClassVar
from unittest.mock import Mock, patch

import requests

from main import (
    ConfigurationError,
    EmailDeliveryError,
    TDNetError,
    build_disclosure_url,
    build_email,
    collect_disclosures,
    deduplicate_disclosures,
    fetch_disclosures,
    get_report_date,
    get_smtp_config,
    get_yesterday_jst,
    is_requested_stock_code,
    normalize_stock_code,
    parse_disclosures,
    send_email,
    validate_report_date,
)

RESULT_PAGE = b"""
<!doctype html>
<html><body>
<table id="maintable">
  <tr>
    <td class="time">2026/08/14 14:00</td>
    <td class="code">91450</td>
    <td class="companyname">Being HD</td>
    <td class="title"><a href="/inbs/being.pdf">Being</a></td>
  </tr>
  <tr>
    <td class="time">2026/07/31 15:30</td>
    <td class="code">14500</td>
    <td class="companyname">TANAKEN</td>
    <td class="title"><a href="/inbs/tanaken.pdf">Q1 results</a></td>
  </tr>
</table>
</body></html>
"""

NO_RESULTS_PAGE = b"""
<html><body>
  <div id="contentwrapper" class="clearfix nothing">No results</div>
</body></html>
"""


class StockCodeTests(unittest.TestCase):
    def test_exact_and_tdnet_trailing_zero_formats_match(self):
        self.assertTrue(is_requested_stock_code("1450", "1450"))
        self.assertTrue(is_requested_stock_code("14500", "1450"))
        self.assertTrue(is_requested_stock_code("441A0", "441a"))
        self.assertTrue(is_requested_stock_code("66580", "6658"))

    def test_substring_collision_does_not_match(self):
        self.assertFalse(is_requested_stock_code("91450", "1450"))
        self.assertFalse(is_requested_stock_code("14501", "1450"))

    def test_stock_code_normalization_and_validation(self):
        self.assertEqual(normalize_stock_code(" 441a "), "441A")
        with self.assertRaises(ValueError):
            normalize_stock_code("14500")


class DateTests(unittest.TestCase):
    def test_yesterday_uses_japan_time(self):
        now = datetime(2026, 8, 14, 15, 5, tzinfo=timezone.utc)
        self.assertEqual(get_yesterday_jst(now), "20260814")

    def test_invalid_date_is_rejected(self):
        with self.assertRaises(ValueError):
            validate_report_date("20260230")

    def test_manual_report_date_override_is_validated(self):
        self.assertEqual(
            get_report_date({"TDNET_REPORT_DATE": "20260731"}),
            "20260731",
        )
        with self.assertRaises(ValueError):
            get_report_date({"TDNET_REPORT_DATE": "not-a-date"})


class ParsingTests(unittest.TestCase):
    def test_parser_rejects_substring_collision_and_keeps_tanaken(self):
        results = parse_disclosures(RESULT_PAGE, "1450")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["code"], "14500")
        self.assertEqual(results[0]["company"], "TANAKEN")
        self.assertEqual(results[0]["url"], "https://www.release.tdnet.info/inbs/tanaken.pdf")

    def test_legitimate_no_results_page_is_empty(self):
        self.assertEqual(parse_disclosures(NO_RESULTS_PAGE, "1450"), [])

    def test_unexpected_page_fails_closed(self):
        with self.assertRaises(TDNetError):
            parse_disclosures(b"<html><body>Service Unavailable</body></html>", "1450")

    def test_empty_result_table_fails_closed(self):
        with self.assertRaises(TDNetError):
            parse_disclosures(b'<table id="maintable"></table>', "1450")

    def test_external_disclosure_link_is_rejected(self):
        with self.assertRaises(TDNetError):
            build_disclosure_url("https://attacker.example/phishing.pdf")

    def test_deduplication_preserves_order(self):
        first = {"url": "https://www.release.tdnet.info/inbs/a.pdf"}
        second = {"url": "https://www.release.tdnet.info/inbs/b.pdf"}
        self.assertEqual(deduplicate_disclosures([first, first, second]), [first, second])


class RequestTests(unittest.TestCase):
    def test_fetch_uses_timeout_and_parses_response(self):
        response = Mock()
        response.headers = {"Content-Type": "text/html; charset=UTF-8"}
        response.iter_content.return_value = [RESULT_PAGE]
        session = Mock()
        session.post.return_value = response

        results = fetch_disclosures("1450", "20260731", session=session)

        self.assertEqual([result["code"] for result in results], ["14500"])
        request_kwargs = session.post.call_args.kwargs
        self.assertEqual(request_kwargs["timeout"], (10, 30))
        self.assertTrue(request_kwargs["stream"])
        self.assertFalse(request_kwargs["allow_redirects"])
        self.assertTrue(request_kwargs["verify"])
        response.close.assert_called_once_with()

    def test_partial_query_failure_fails_closed(self):
        class FailingSession:
            def post(self, *args, **kwargs):
                raise requests.Timeout("simulated timeout")

        with self.assertRaises(TDNetError):
            collect_disclosures("20260731", stock_codes=("1450",), session=FailingSession())


class SMTPTests(unittest.TestCase):
    VALID_ENV: ClassVar[dict[str, str]] = {
        "SMTP_SERVER": "smtp.example.com",
        "SMTP_PORT": "587",
        "SMTP_USER": "sender@example.com",
        "SMTP_PASSWORD": "not-logged",
        "RECIPIENT_EMAIL": "recipient@example.com",
    }

    def test_smtp_configuration_is_validated(self):
        config = get_smtp_config(self.VALID_ENV)
        self.assertEqual(config.port, 587)
        self.assertEqual(config.sender, "sender@example.com")
        self.assertNotIn("not-logged", repr(config))
        self.assertEqual(get_smtp_config(dict(self.VALID_ENV, SMTP_PORT="")).port, 587)

        invalid = dict(self.VALID_ENV, SMTP_PORT="not-a-port")
        with self.assertRaises(ConfigurationError):
            get_smtp_config(invalid)

    def test_email_header_injection_is_rejected(self):
        invalid = dict(self.VALID_ENV, RECIPIENT_EMAIL="a@example.com\r\nBcc: victim@example.com")
        with self.assertRaises(ConfigurationError):
            get_smtp_config(invalid)

    def test_email_is_plain_text_and_uses_report_date(self):
        config = get_smtp_config(self.VALID_ENV)
        message = build_email(
            [{
                "time": "2026/07/31 15:30",
                "code": "14500",
                "company": "TANAKEN",
                "title": "Q1 results",
                "url": "https://www.release.tdnet.info/inbs/tanaken.pdf",
            }],
            "20260731",
            config,
        )
        self.assertEqual(message["Subject"], "TDnet Disclosure Alert - 2026-07-31")
        self.assertIn("TANAKEN", message.get_content())

    def test_smtp_recipient_refusal_is_not_reported_as_success(self):
        config = get_smtp_config(self.VALID_ENV)
        smtp_server = Mock()
        smtp_server.send_message.return_value = {
            "recipient@example.com": (550, b"mailbox unavailable")
        }

        with patch("main.smtplib.SMTP") as smtp:
            smtp.return_value.__enter__.return_value = smtp_server
            with self.assertRaises(EmailDeliveryError):
                send_email(
                    [{
                        "time": "2026/07/31 15:30",
                        "code": "14500",
                        "company": "TANAKEN",
                        "title": "Q1 results",
                        "url": "https://www.release.tdnet.info/inbs/tanaken.pdf",
                    }],
                    "20260731",
                    config,
                )


if __name__ == "__main__":
    unittest.main()
