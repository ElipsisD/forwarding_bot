from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import logging
import re
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen


logger = logging.getLogger(__name__)
SUGGEST_URL = "https://suggest-maps.yandex.ru/v1/suggest"
GEOCODER_URL = "https://geocode-maps.yandex.ru/v1/"


@dataclass(frozen=True)
class GeocodeResult:
    latitude: str | None
    longitude: str | None
    quality: str


@dataclass(frozen=True)
class Suggestion:
    title: str
    uri: str


@dataclass(frozen=True)
class AddressLookup:
    geocode: GeocodeResult | None = None
    suggestions: list[Suggestion] | None = None
    card_uri: str | None = None


class YandexAPIError(Exception):
    def __init__(self, api_name: str, status: int, response_body: str) -> None:
        self.api_name = api_name
        self.status = status
        self.response_body = response_body
        super().__init__(f"{api_name} returned HTTP {status}")


async def geocode_addresses(
    addresses: list[str],
    suggest_api_key: str | None,
    geocoder_api_key: str | None,
) -> dict[str, AddressLookup]:
    if not suggest_api_key or not geocoder_api_key:
        return {}

    results: dict[str, AddressLookup] = {}
    for address in addresses:
        try:
            results[_address_key(address)] = await asyncio.to_thread(
                _lookup_address,
                _expand_city_abbreviation(_without_parenthetical_text(address)),
                suggest_api_key,
                geocoder_api_key,
            )
        except YandexAPIError as error:
            logger.warning("%s returned HTTP %s: %s", error.api_name, error.status, error.response_body)
        except (URLError, OSError, ValueError):
            logger.warning("Yandex geocoding failed for address: %s", address, exc_info=True)
    return results


async def geocode_suggestion(uri: str, geocoder_api_key: str | None) -> GeocodeResult | None:
    if not geocoder_api_key:
        return None
    try:
        return await asyncio.to_thread(_geocode_uri, uri, geocoder_api_key)
    except YandexAPIError as error:
        logger.warning("%s returned HTTP %s: %s", error.api_name, error.status, error.response_body)
    except (URLError, OSError, ValueError):
        logger.warning("Yandex geocoding failed", exc_info=True)
    return None


async def coordinates_from_navigator_link(link: str, geocoder_api_key: str | None) -> GeocodeResult | None:
    try:
        coordinates, uri = await asyncio.to_thread(_extract_link_data, link)
    except (OSError, URLError, ValueError):
        return None
    if coordinates:
        latitude, longitude = coordinates
        return GeocodeResult(latitude, longitude, "координаты из ссылки Яндекс Навигатора")
    return await geocode_suggestion(uri, geocoder_api_key) if uri else None


def is_navigator_link(value: str) -> bool:
    parsed = urlparse(value.strip())
    return parsed.scheme in {"https", "http", "yandexnavi"} and _is_yandex_host(parsed)


def _lookup_address(address: str, suggest_api_key: str, geocoder_api_key: str) -> AddressLookup:
    suggest = _get_json(
        SUGGEST_URL,
        {
            "apikey": suggest_api_key,
            "text": address,
            "lang": "ru",
            "ll": "92.90,56.02",
            "spn": "0.55,0.35",
            "print_address": "1",
            "attrs": "uri",
        },
    )
    suggestions = suggest.get("results", [])
    options = _suggestions(suggestions)
    if not options:
        return AddressLookup(geocode=GeocodeResult(None, None, "недостаточно данных: подходящий адрес не найден"))
    if len(options) > 1 and _has_transport_company_marker(address):
        return AddressLookup(suggestions=options)
    return AddressLookup(geocode=_geocode_uri(options[0].uri, geocoder_api_key), card_uri=options[0].uri)


def _geocode_uri(uri: str, geocoder_api_key: str) -> GeocodeResult:
    geocode = _get_json(
        GEOCODER_URL,
        {
            "apikey": geocoder_api_key,
            "uri": uri,
            "lang": "ru_RU",
            "format": "json",
            "results": "1",
        },
    )
    response = geocode.get("response")
    if not isinstance(response, dict):
        return GeocodeResult(None, None, "недостаточно данных: координаты не найдены")
    collection = response.get("GeoObjectCollection")
    if not isinstance(collection, dict):
        return GeocodeResult(None, None, "недостаточно данных: координаты не найдены")
    members = collection.get("featureMember", [])
    if not members or not isinstance(members[0], dict):
        return GeocodeResult(None, None, "недостаточно данных: координаты не найдены")

    geo_object = members[0].get("GeoObject", {})
    if not isinstance(geo_object, dict):
        return GeocodeResult(None, None, "недостаточно данных: координаты не найдены")
    metadata_property = geo_object.get("metaDataProperty")
    metadata = metadata_property.get("GeocoderMetaData", {}) if isinstance(metadata_property, dict) else {}
    point = geo_object.get("Point")
    position = point.get("pos", "") if isinstance(point, dict) else ""
    if not isinstance(position, str):
        return GeocodeResult(None, None, "недостаточно данных: координаты не найдены")
    longitude, separator, latitude = position.partition(" ")
    if not separator or not longitude or not latitude:
        return GeocodeResult(None, None, "недостаточно данных: координаты не найдены")
    return GeocodeResult(latitude, longitude, _quality(metadata.get("precision")))


def _suggestions(values: Any) -> list[Suggestion]:
    if not isinstance(values, list):
        return []
    result: list[Suggestion] = []
    for value in values:
        if not isinstance(value, dict):
            continue
        uri = value.get("uri")
        title = value.get("address", {}).get("formatted_address") or value.get("title", {}).get("text")
        if isinstance(uri, str) and uri and isinstance(title, str) and title:
            result.append(Suggestion(title=title, uri=uri))
    return result


def _get_json(url: str, params: dict[str, str]) -> dict[str, Any]:
    request_url = f"{url}?{urlencode(params)}"
    try:
        with urlopen(request_url, timeout=15) as response:  # noqa: S310 - API URLs are constants.
            payload = json.load(response)
    except HTTPError as error:
        api_name = "Yandex Suggest API" if url == SUGGEST_URL else "Yandex Geocoder API"
        response_body = error.read().decode("utf-8", errors="replace")[:500]
        raise YandexAPIError(api_name, error.code, response_body) from error
    if not isinstance(payload, dict):
        raise ValueError("Unexpected Yandex API response")
    return payload


def _extract_link_data(link: str) -> tuple[tuple[str, str] | None, str | None]:
    parsed = urlparse(link)
    if parsed.scheme not in {"https", "http", "yandexnavi"} or not _is_yandex_host(parsed):
        raise ValueError("Not a Yandex link")
    coordinates, uri = _link_data(link)
    if coordinates or uri or parsed.scheme == "yandexnavi":
        return coordinates, uri
    request = Request(link, headers={"User-Agent": "forwarding-bot/1.0"})
    with urlopen(request, timeout=15) as response:  # noqa: S310 - host is validated above.
        final_url = response.geturl()
        page = response.read(2_000_000).decode("utf-8", errors="replace")
    final_parsed = urlparse(final_url)
    if not _is_yandex_host(final_parsed):
        raise ValueError("Redirected outside Yandex")
    coordinates, uri = _link_data(final_url)
    return coordinates or _organization_card_coordinates(page, final_url), uri or _organization_card_uri(page)


def _link_data(link: str) -> tuple[tuple[str, str] | None, str | None]:
    query = parse_qs(urlparse(link).query)
    uri = query.get("uri", [None])[0]
    longitude = query.get("lon", [None])[0]
    latitude = query.get("lat", [None])[0]
    if longitude and latitude:
        return _validated_coordinates(latitude, longitude), uri
    for parameter in ("ll", "pt", "whatshere[point]"):
        value = query.get(parameter, [None])[0]
        if value:
            longitude, separator, latitude = value.partition(",")
            if separator:
                return _validated_coordinates(latitude, longitude), uri
    return None, uri if isinstance(uri, str) else None


def _validated_coordinates(latitude: str, longitude: str) -> tuple[str, str] | None:
    try:
        latitude_value = float(latitude)
        longitude_value = float(longitude)
    except ValueError:
        return None
    if -90 <= latitude_value <= 90 and -180 <= longitude_value <= 180:
        return latitude, longitude
    return None


def _is_yandex_host(parsed) -> bool:
    return parsed.scheme == "yandexnavi" or parsed.hostname in {"ya.cc", "yandex.ru", "www.yandex.ru"} or bool(
        parsed.hostname and parsed.hostname.endswith(".yandex.ru")
    )


def _organization_card_coordinates(page: str, url: str) -> tuple[str, str] | None:
    match = re.search(r"/org/[^/]+/(\d+)", url)
    if not match:
        return None
    object_id = re.escape(match.group(1))
    coordinates = re.search(
        rf'data-id="{object_id}"[^>]*data-coordinates="(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)"', page
    )
    if not coordinates:
        return None
    longitude, latitude = coordinates.groups()
    return _validated_coordinates(latitude, longitude)


def _organization_card_uri(page: str) -> str | None:
    match = re.search(r'"uri":"(ymapsbm1://org\?oid=\d+)"', page)
    return match.group(1) if match else None


def _quality(precision: Any) -> str:
    return {
        "exact": "точные координаты дома",
        "number": "приблизительно: номер дома найден, корпус отличается",
        "near": "приблизительно: найден ближайший номер дома",
        "range": "приблизительные координаты дома",
        "street": "недостаточно данных: определена только улица",
        "other": "недостаточно данных: найден объект без улицы",
    }.get(str(precision), "точность координат неизвестна")


def _address_key(address: str) -> str:
    return " ".join(address.casefold().split())


def _expand_city_abbreviation(address: str) -> str:
    return re.sub(r"\bкрск\b", "Красноярск", address, flags=re.IGNORECASE)


def _without_parenthetical_text(address: str) -> str:
    return " ".join(re.sub(r"\([^()]*\)", " ", address).split())


def _has_transport_company_marker(address: str) -> bool:
    return bool(re.search(r"\bтк\b|транспортн\w*\s+компан\w*", address, flags=re.IGNORECASE))
