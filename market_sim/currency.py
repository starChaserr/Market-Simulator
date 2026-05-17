from __future__ import annotations

import re
from typing import Any


DEFAULT_CURRENCY = "USD"


COUNTRY_TO_CURRENCY = {
    "AE": "AED",
    "AR": "ARS",
    "AT": "EUR",
    "AU": "AUD",
    "BE": "EUR",
    "BG": "BGN",
    "BR": "BRL",
    "CA": "CAD",
    "CH": "CHF",
    "CL": "CLP",
    "CN": "CNY",
    "CO": "COP",
    "CZ": "CZK",
    "DE": "EUR",
    "DK": "DKK",
    "EE": "EUR",
    "ES": "EUR",
    "FI": "EUR",
    "FR": "EUR",
    "GB": "GBP",
    "GR": "EUR",
    "HK": "HKD",
    "HR": "EUR",
    "HU": "HUF",
    "ID": "IDR",
    "IE": "EUR",
    "IL": "ILS",
    "IN": "INR",
    "IT": "EUR",
    "JP": "JPY",
    "KR": "KRW",
    "LT": "EUR",
    "LU": "EUR",
    "LV": "EUR",
    "MX": "MXN",
    "MY": "MYR",
    "NL": "EUR",
    "NO": "NOK",
    "NZ": "NZD",
    "PH": "PHP",
    "PL": "PLN",
    "PT": "EUR",
    "RO": "RON",
    "SA": "SAR",
    "SE": "SEK",
    "SG": "SGD",
    "TH": "THB",
    "TR": "TRY",
    "TW": "TWD",
    "US": "USD",
    "VN": "VND",
    "ZA": "ZAR",
}


TIMEZONE_TO_COUNTRY = {
    "America/Argentina/Buenos_Aires": "AR",
    "America/Bogota": "CO",
    "America/Chicago": "US",
    "America/Denver": "US",
    "America/Los_Angeles": "US",
    "America/Mexico_City": "MX",
    "America/New_York": "US",
    "America/Phoenix": "US",
    "America/Sao_Paulo": "BR",
    "America/Toronto": "CA",
    "Asia/Dubai": "AE",
    "Asia/Calcutta": "IN",
    "Asia/Hong_Kong": "HK",
    "Asia/Jakarta": "ID",
    "Asia/Jerusalem": "IL",
    "Asia/Kolkata": "IN",
    "Asia/Kuala_Lumpur": "MY",
    "Asia/Manila": "PH",
    "Asia/Riyadh": "SA",
    "Asia/Seoul": "KR",
    "Asia/Shanghai": "CN",
    "Asia/Singapore": "SG",
    "Asia/Taipei": "TW",
    "Asia/Tokyo": "JP",
    "Asia/Bangkok": "TH",
    "Asia/Ho_Chi_Minh": "VN",
    "Australia/Melbourne": "AU",
    "Australia/Sydney": "AU",
    "Europe/Amsterdam": "NL",
    "Europe/Berlin": "DE",
    "Europe/Brussels": "BE",
    "Europe/Bucharest": "RO",
    "Europe/Budapest": "HU",
    "Europe/Copenhagen": "DK",
    "Europe/Dublin": "IE",
    "Europe/Helsinki": "FI",
    "Europe/Lisbon": "PT",
    "Europe/London": "GB",
    "Europe/Madrid": "ES",
    "Europe/Oslo": "NO",
    "Europe/Paris": "FR",
    "Europe/Prague": "CZ",
    "Europe/Rome": "IT",
    "Europe/Sofia": "BG",
    "Europe/Stockholm": "SE",
    "Europe/Vienna": "AT",
    "Europe/Warsaw": "PL",
    "Europe/Zurich": "CH",
    "Pacific/Auckland": "NZ",
}


def currency_preferences(
    *,
    locale: str | None = None,
    timezone: str | None = None,
    explicit_currency: str | None = None,
    accept_language: str | None = None,
) -> dict[str, Any]:
    currency = _normalize_currency(explicit_currency)
    if currency:
        return _response(currency, "explicit", locale, timezone, None)

    region = region_from_timezone(timezone)
    if region:
        return _response(COUNTRY_TO_CURRENCY.get(region, DEFAULT_CURRENCY), "timezone", locale, timezone, region)

    region = region_from_locale(locale) or region_from_accept_language(accept_language)
    if region:
        return _response(COUNTRY_TO_CURRENCY.get(region, DEFAULT_CURRENCY), "locale", locale, timezone, region)

    return _response(DEFAULT_CURRENCY, "default", locale, timezone, None)


def region_from_accept_language(accept_language: str | None) -> str | None:
    if not accept_language:
        return None
    for part in accept_language.split(","):
        region = region_from_locale(part.split(";", 1)[0].strip())
        if region:
            return region
    return None


def region_from_locale(locale: str | None) -> str | None:
    if not locale:
        return None
    tokens = re.split(r"[-_]", locale.strip())
    for token in reversed(tokens[1:]):
        if len(token) == 2 and token.isalpha():
            return token.upper()
    return None


def region_from_timezone(timezone: str | None) -> str | None:
    if not timezone:
        return None
    clean_timezone = timezone.strip()
    if clean_timezone in TIMEZONE_TO_COUNTRY:
        return TIMEZONE_TO_COUNTRY[clean_timezone]
    if clean_timezone.startswith("Europe/"):
        return "DE"
    if clean_timezone.startswith("America/"):
        return "US"
    if clean_timezone.startswith("Australia/"):
        return "AU"
    return None


def _normalize_currency(value: str | None) -> str | None:
    if not value:
        return None
    currency = value.strip().upper()
    if len(currency) == 3 and currency.isalpha():
        return currency
    return None


def _response(currency: str, source: str, locale: str | None, timezone: str | None, region: str | None) -> dict[str, Any]:
    return {
        "currency": currency,
        "source": source,
        "locale": locale,
        "timezone": timezone,
        "region": region,
    }
