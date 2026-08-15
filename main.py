from __future__ import annotations

import os
import re
import smtplib
import ssl
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from email.utils import getaddresses, parseaddr
from pathlib import Path
from time import strptime
from urllib.parse import urljoin, urlparse, urlunparse
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - requirements.txt installs this package
    load_dotenv = None


# Configuration
STOCK_CODES = ("441A", "1450", "6658")
TDNET_ORIGIN = "https://www.release.tdnet.info"
TDNET_HOST = "www.release.tdnet.info"
TDNET_URL = f"{TDNET_ORIGIN}/onsf/TDJFSearch/TDJFSearch"
TDNET_REFERER = f"{TDNET_ORIGIN}/onsf/TDJFSearch/I_head"
JST = ZoneInfo("Asia/Tokyo")

REQUEST_TIMEOUT = (10, 30)  # connect timeout, read timeout in seconds
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_RESULTS_PER_QUERY = 5_000
MAX_SOURCE_FIELD_LENGTH = 2_000
MAX_EMAIL_BYTES = 5 * 1024 * 1024
SMTP_TIMEOUT = 30

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; TDnetDisclosureAlert/1.0)",
    "Referer": TDNET_REFERER,
    "Content-Type": "application/x-www-form-urlencoded",
}

# TSE uses four numeric characters for traditional codes and three numeric
# characters plus a letter for newer codes such as 441A.
STOCK_CODE_PATTERN = re.compile(r"^(?:[0-9]{4}|[0-9]{3}[A-Z])$")
DATE_PATTERN = re.compile(r"^[0-9]{8}$")
SOURCE_TIME_FORMAT = "%Y/%m/%d %H:%M"


class TDNetError(RuntimeError):
    """Raised when TDnet cannot be queried or its response is unsafe to use."""


class ConfigurationError(RuntimeError):
    """Raised when required runtime configuration is missing or invalid."""


class EmailDeliveryError(RuntimeError):
    """Raised when an alert cannot be delivered."""


@dataclass(frozen=True)
class SMTPConfig:
    server: str
    port: int
    username: str
    password: str = field(repr=False)
    sender: str
    recipient: str


def _load_local_environment() -> None:
    """Load only this repository's .env file, never a parent-directory file."""
    if load_dotenv is None:
        return

    env_path = Path(__file__).resolve().with_name(".env")
    if env_path.is_file():
        load_dotenv(dotenv_path=env_path, override=False)


def normalize_stock_code(stock_code: str) -> str:
    """Normalize and validate a configured four-character TSE stock code."""
    if not isinstance(stock_code, str):
        raise TypeError("stock code must be a string")

    normalized = stock_code.strip().upper()
    if not STOCK_CODE_PATTERN.fullmatch(normalized):
        raise ValueError(f"invalid stock code: {stock_code!r}")
    return normalized


def validate_stock_codes(stock_codes: Iterable[str]) -> tuple[str, ...]:
    """Return normalized, unique stock codes or fail before querying TDnet."""
    normalized = tuple(normalize_stock_code(code) for code in stock_codes)
    if not normalized:
        raise ConfigurationError("at least one stock code must be configured")
    if len(set(normalized)) != len(normalized):
        raise ConfigurationError("duplicate stock codes are configured")
    return normalized


def is_requested_stock_code(returned_code: str, requested_code: str) -> bool:
    """Match TDnet's displayed code to the code we asked it to search for.

    TDnet's search is a substring search, so the returned code can belong to a
    different company (for example, searching for 1450 also returns 91450).
    Some codes are displayed with a trailing zero (14500, 441A0, and 66580),
    so allow that one format without allowing arbitrary prefix matches.
    """
    returned = str(returned_code).strip().upper()
    requested = str(requested_code).strip().upper()

    return returned == requested or (
        len(returned) == len(requested) + 1
        and returned.endswith("0")
        and returned[:-1] == requested
    )


def validate_report_date(date_str: str) -> str:
    """Validate the YYYYMMDD date format sent to TDnet."""
    if not isinstance(date_str, str) or not DATE_PATTERN.fullmatch(date_str):
        raise ValueError(f"invalid report date: {date_str!r}")
    try:
        date.fromisoformat(f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}")
    except ValueError as exc:
        raise ValueError(f"invalid report date: {date_str!r}") from exc
    return date_str


def get_yesterday_jst(now: datetime | None = None) -> str:
    """Return the previous calendar date in Japan Standard Time."""
    if now is None:
        current_jst = datetime.now(JST)
    else:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        current_jst = now.astimezone(JST)

    yesterday = current_jst - timedelta(days=1)
    return yesterday.strftime("%Y%m%d")


def get_report_date(
    environment: Mapping[str, str] | None = None,
    now: datetime | None = None,
) -> str:
    """Use a validated manual date when supplied, otherwise use yesterday in JST."""
    if environment is None:
        _load_local_environment()
    env = os.environ if environment is None else environment
    override = env.get("TDNET_REPORT_DATE", "").strip()
    if override:
        return validate_report_date(override)
    return get_yesterday_jst(now)


def create_tdnet_session() -> requests.Session:
    """Create a bounded, retrying HTTPS client for the read-only TDnet query."""
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        status=3,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"POST"}),
        # Keep server-controlled retry delays bounded by the request timeout
        # and workflow timeout; the exponential backoff is sufficient here.
        respect_retry_after_header=False,
        backoff_max=5,
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session = requests.Session()
    session.headers.update(HEADERS)
    session.mount("https://", adapter)
    # This job has a fixed destination. Do not silently honor arbitrary proxy
    # variables inherited from the runner environment.
    session.trust_env = False
    return session


def _read_limited_response(response: requests.Response) -> bytes:
    """Read a response without allowing an unexpectedly large body."""
    content_length = response.headers.get("Content-Length")
    if content_length:
        try:
            declared_length = int(content_length)
            if declared_length < 0:
                raise TDNetError("TDnet returned an invalid Content-Length")
            if declared_length > MAX_RESPONSE_BYTES:
                raise TDNetError("TDnet response is too large")
        except ValueError as exc:
            raise TDNetError("TDnet returned an invalid Content-Length") from exc

    chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_content(chunk_size=64 * 1024):
        if not chunk:
            continue
        total += len(chunk)
        if total > MAX_RESPONSE_BYTES:
            raise TDNetError("TDnet response is too large")
        chunks.append(chunk)
    return b"".join(chunks)


def _clean_source_text(value: str, field_name: str) -> str:
    """Collapse source whitespace and enforce a safe field size."""
    if any(
        (ord(character) < 32 and character not in "\t\n\r") or ord(character) == 127
        for character in value
    ):
        raise TDNetError(f"TDnet returned controls in {field_name}")
    cleaned = " ".join(value.split())
    if not cleaned:
        raise TDNetError(f"TDnet returned an empty {field_name}")
    if len(cleaned) > MAX_SOURCE_FIELD_LENGTH:
        raise TDNetError(f"TDnet returned an oversized {field_name}")
    return cleaned


def build_disclosure_url(href: str) -> str:
    """Resolve a TDnet link and reject redirects to another host or scheme."""
    if not isinstance(href, str) or not href or len(href) > 2_048:
        raise TDNetError("TDnet returned an invalid disclosure link")
    if any(ord(character) < 32 or ord(character) == 127 for character in href):
        raise TDNetError("TDnet returned a disclosure link containing controls")

    try:
        resolved = urljoin(f"{TDNET_ORIGIN}/", href)
        parsed = urlparse(resolved)
    except ValueError as exc:
        raise TDNetError("TDnet returned an invalid disclosure link") from exc
    if parsed.scheme.lower() != "https" or parsed.netloc.lower() != TDNET_HOST:
        raise TDNetError("TDnet returned a link outside the trusted TDnet host")
    if not parsed.path.startswith("/inbs/"):
        raise TDNetError("TDnet returned a link outside its disclosure path")

    # Fragments are not needed for a PDF and can contain untrusted display data.
    return urlunparse(("https", TDNET_HOST, parsed.path, parsed.params, parsed.query, ""))


def parse_disclosures(content: bytes, requested_code: str) -> list[dict[str, str]]:
    """Parse and validate the TDnet result page for one requested code."""
    requested = normalize_stock_code(requested_code)
    if not isinstance(content, (bytes, bytearray)):
        raise TDNetError("TDnet response is not bytes")
    if len(content) > MAX_RESPONSE_BYTES:
        raise TDNetError("TDnet response is too large")

    soup = BeautifulSoup(bytes(content), "html.parser")
    table = soup.find("table", id="maintable")
    if table is None:
        # TDnet's legitimate no-results page has this marker. Treat every
        # other page as an outage or format change instead of a quiet no-op.
        if soup.select_one("#contentwrapper.nothing") is not None:
            return []
        raise TDNetError("TDnet returned an unexpected result page")

    results: list[dict[str, str]] = []
    saw_data_row = False
    for row in table.find_all("tr"):
        if not row.find_all("td"):
            continue
        saw_data_row = True

        time_cell = row.select_one("td.time")
        code_cell = row.select_one("td.code")
        company_cell = row.select_one("td.companyname")
        title_cell = row.select_one("td.title")
        if any(cell is None for cell in (time_cell, code_cell, company_cell, title_cell)):
            raise TDNetError("TDnet result row is missing an expected field")

        code = _clean_source_text(code_cell.get_text(" ", strip=True), "stock code")
        if not is_requested_stock_code(code, requested):
            continue

        time = _clean_source_text(time_cell.get_text(" ", strip=True), "disclosure time")
        try:
            strptime(time, SOURCE_TIME_FORMAT)
        except ValueError as exc:
            raise TDNetError(f"TDnet returned an invalid disclosure time: {time!r}") from exc

        company = _clean_source_text(company_cell.get_text(" ", strip=True), "company")
        title = _clean_source_text(title_cell.get_text(" ", strip=True), "title")
        link_tag = title_cell.find("a", href=True)
        if link_tag is None:
            raise TDNetError("TDnet result row has no disclosure link")
        url = build_disclosure_url(link_tag["href"])

        results.append(
            {
                "time": time,
                "code": code,
                "company": company,
                "title": title,
                "url": url,
            }
        )
        if len(results) > MAX_RESULTS_PER_QUERY:
            raise TDNetError("TDnet returned too many disclosures")

    if not saw_data_row:
        raise TDNetError("TDnet returned an empty result table")
    return results


def fetch_disclosures(
    stock_code: str,
    date_str: str,
    session: requests.Session | None = None,
) -> list[dict[str, str]]:
    """Fetch one day's disclosures and retain only the requested stock code."""
    requested_code = normalize_stock_code(stock_code)
    report_date = validate_report_date(date_str)
    own_session = session is None
    client = session if session is not None else create_tdnet_session()
    response: requests.Response | None = None

    try:
        response = client.post(
            TDNET_URL,
            headers=HEADERS,
            data={"t0": report_date, "t1": report_date, "q": requested_code, "m": "0"},
            timeout=REQUEST_TIMEOUT,
            stream=True,
            allow_redirects=False,
            verify=True,
        )
        response.raise_for_status()
        content_type = response.headers.get("Content-Type", "").lower()
        if "text/html" not in content_type:
            raise TDNetError("TDnet returned a non-HTML response")
        content = _read_limited_response(response)
    finally:
        if response is not None:
            response.close()
        if own_session:
            client.close()

    return parse_disclosures(content, requested_code)


def deduplicate_disclosures(results: Iterable[dict[str, str]]) -> list[dict[str, str]]:
    """Remove duplicate disclosures while preserving TDnet's result order."""
    unique: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    for result in results:
        url = result["url"]
        if url in seen_urls:
            continue
        seen_urls.add(url)
        unique.append(result)
    return unique


def collect_disclosures(
    date_str: str,
    stock_codes: Iterable[str] = STOCK_CODES,
    session: requests.Session | None = None,
) -> list[dict[str, str]]:
    """Query all configured codes and fail closed if any query is unreliable."""
    report_date = validate_report_date(date_str)
    codes = validate_stock_codes(stock_codes)
    failures: list[str] = []
    all_results: list[dict[str, str]] = []

    own_session = session is None
    client = session if session is not None else create_tdnet_session()
    try:
        for code in codes:
            print(f"Fetching for {code}...")
            try:
                all_results.extend(fetch_disclosures(code, report_date, session=client))
            except (TDNetError, requests.RequestException) as exc:
                # Do not send a partial digest that falsely implies every
                # tracked code was checked successfully.
                failures.append(f"{code}: {type(exc).__name__}: {exc}")
    finally:
        if own_session:
            client.close()

    if failures:
        raise TDNetError("one or more TDnet queries failed: " + "; ".join(failures))
    return deduplicate_disclosures(all_results)


def _validate_header_value(value: str, field_name: str) -> str:
    if not value or len(value) > 998 or any(character in value for character in "\r\n"):
        raise ConfigurationError(f"invalid {field_name}")
    return value


def _validate_email_address(value: str, field_name: str) -> str:
    value = _validate_header_value(value, field_name)
    addresses = getaddresses([value])
    if len(addresses) != 1 or not addresses[0][1] or "@" not in addresses[0][1]:
        raise ConfigurationError(f"invalid {field_name}")
    _, parsed_address = parseaddr(value)
    if parsed_address != addresses[0][1]:
        raise ConfigurationError(f"invalid {field_name}")
    return value


def get_smtp_config(environment: Mapping[str, str] | None = None) -> SMTPConfig:
    """Read and validate SMTP settings without ever logging the password."""
    if environment is None:
        _load_local_environment()
    env = os.environ if environment is None else environment
    required = ("SMTP_SERVER", "SMTP_USER", "SMTP_PASSWORD", "RECIPIENT_EMAIL")
    missing = [name for name in required if not env.get(name)]
    if missing:
        raise ConfigurationError("missing SMTP configuration: " + ", ".join(missing))

    server = _validate_header_value(env["SMTP_SERVER"].strip(), "SMTP_SERVER")
    username = _validate_header_value(env["SMTP_USER"].strip(), "SMTP_USER")
    password = env["SMTP_PASSWORD"]
    if not password:
        raise ConfigurationError("invalid SMTP_PASSWORD")
    recipient = _validate_email_address(env["RECIPIENT_EMAIL"].strip(), "RECIPIENT_EMAIL")
    sender = _validate_email_address(
        (env.get("SMTP_FROM") or username).strip(),
        "SMTP_FROM/SMTP_USER",
    )

    raw_port = (env.get("SMTP_PORT") or "587").strip()
    try:
        port = int(raw_port)
    except (TypeError, ValueError) as exc:
        raise ConfigurationError("SMTP_PORT must be an integer") from exc
    if not 1 <= port <= 65_535:
        raise ConfigurationError("SMTP_PORT must be between 1 and 65535")

    return SMTPConfig(
        server=server,
        port=port,
        username=username,
        password=password,
        sender=sender,
        recipient=recipient,
    )


def build_email(
    all_results: Iterable[dict[str, str]],
    report_date: str,
    config: SMTPConfig,
) -> EmailMessage:
    """Build a plain-text email with a bounded, deterministic subject."""
    report_date = validate_report_date(report_date)
    results = list(all_results)
    if not results:
        raise ValueError("cannot build an empty disclosure email")
    sender = _validate_email_address(config.sender, "SMTP sender")
    recipient = _validate_email_address(config.recipient, "SMTP recipient")

    lines = ["New disclosures found:", ""]
    for item in results:
        lines.extend(
            (
                f"Time: {item['time']}",
                f"Company: {item['company']} ({item['code']})",
                f"Title: {item['title']}",
                f"Link: {item['url']}",
                "-" * 30,
            )
        )
    body = "\n".join(lines) + "\n"
    if len(body.encode("utf-8")) > MAX_EMAIL_BYTES:
        raise ValueError("disclosure email is too large")

    message = EmailMessage()
    message["From"] = sender
    message["To"] = recipient
    subject_date = f"{report_date[:4]}-{report_date[4:6]}-{report_date[6:]}"
    message["Subject"] = f"TDnet Disclosure Alert - {subject_date}"
    message.set_content(body)
    return message


def send_email(
    all_results: Iterable[dict[str, str]],
    report_date: str,
    config: SMTPConfig | None = None,
) -> None:
    """Deliver an alert over an explicitly verified STARTTLS connection."""
    config = config or get_smtp_config()
    message = build_email(all_results, report_date, config)
    tls_context = ssl.create_default_context()

    try:
        with smtplib.SMTP(config.server, config.port, timeout=SMTP_TIMEOUT) as server:
            server.ehlo()
            server.starttls(context=tls_context)
            server.ehlo()
            server.login(config.username, config.password)
            refused = server.send_message(message)
            if refused:
                raise EmailDeliveryError("SMTP server refused one or more recipients")
    except (OSError, smtplib.SMTPException) as exc:
        raise EmailDeliveryError("SMTP delivery failed") from exc

    print("Email sent successfully.")


def main() -> int:
    try:
        report_date = get_report_date()
        print(f"Checking disclosures for date: {report_date}")
        smtp_config = get_smtp_config()
        all_results = collect_disclosures(report_date)
        if all_results:
            print(f"Found {len(all_results)} disclosures. Sending email...")
            send_email(all_results, report_date, smtp_config)
        else:
            print("No new disclosures found.")
        return 0
    except (ConfigurationError, EmailDeliveryError, TDNetError, requests.RequestException, ValueError) as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
