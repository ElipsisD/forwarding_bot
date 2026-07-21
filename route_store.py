from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
import json
from pathlib import Path
import secrets
from typing import Any

from excel_parser import Order
from yandex_geocoder import AddressLookup, GeocodeResult, Suggestion


@dataclass(frozen=True)
class RoutePoint:
    address: str
    orders: list[Order]
    delivered: bool = False
    latitude: str | None = None
    longitude: str | None = None
    geocode_quality: str | None = None
    suggestions: list[Suggestion] | None = None
    address_confirmed: bool = False
    card_uri: str | None = None
    completed_at: str | None = None
    payment_received_at: str | None = None


@dataclass(frozen=True)
class Route:
    id: str
    chat_id: int
    created_at: str
    points: list[RoutePoint]
    selected_point: int | None = None
    start_mileage: str | None = None
    final_mileage: str | None = None
    awaiting_mileage: str | None = None
    awaiting_suggestion: int | None = None
    awaiting_coordinate_point: int | None = None
    route_start_latitude: str | None = None
    route_start_longitude: str | None = None
    route_end_latitude: str | None = None
    route_end_longitude: str | None = None
    awaiting_route_endpoint: str | None = None
    editing_route_endpoint: bool = False
    awaiting_payment_confirmation: int | None = None
    route_endpoint_prompt_message_id: int | None = None


class RouteStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = asyncio.Lock()

    async def create_route(
        self,
        chat_id: int,
        orders: list[Order],
        lookups: dict[str, AddressLookup] | None = None,
    ) -> Route:
        points_by_address: dict[str, RoutePoint] = {}
        points: list[RoutePoint] = []
        for order in orders:
            address = order.address or "Адрес не указан"
            key = " ".join(address.casefold().split())
            point = points_by_address.get(key)
            if point is None:
                lookup = (lookups or {}).get(key)
                geocode = lookup.geocode if lookup else None
                point = RoutePoint(
                    address=address,
                    orders=[],
                    latitude=geocode.latitude if geocode else None,
                    longitude=geocode.longitude if geocode else None,
                    geocode_quality=geocode.quality if geocode else None,
                    suggestions=lookup.suggestions if lookup else None,
                    card_uri=lookup.card_uri if lookup else None,
                )
                points_by_address[key] = point
                points.append(point)
            point.orders.append(order)

        awaiting_suggestion = next((index for index, point in enumerate(points) if point.suggestions), None)
        route = Route(
            id=secrets.token_urlsafe(8),
            chat_id=chat_id,
            created_at=datetime.now(UTC).isoformat(),
            points=points,
            awaiting_route_endpoint="start" if awaiting_suggestion is None else None,
            awaiting_suggestion=awaiting_suggestion,
        )
        async with self._lock:
            data = self._read()
            data[str(chat_id)] = self._serialize(route)
            self._write(data)
        return route

    async def get_route(self, chat_id: int, route_id: str) -> Route | None:
        async with self._lock:
            raw_route = self._read().get(str(chat_id))
        if not raw_route or raw_route.get("id") != route_id:
            return None
        return self._deserialize(raw_route)

    async def get_current_route(self, chat_id: int) -> Route | None:
        async with self._lock:
            raw_route = self._read().get(str(chat_id))
        return self._deserialize(raw_route) if raw_route else None

    async def save_mileage(self, chat_id: int, mileage: str) -> Route | None:
        async with self._lock:
            data = self._read()
            raw_route = data.get(str(chat_id))
            if not raw_route:
                return None
            route = self._deserialize(raw_route)
            if route.awaiting_mileage == "start":
                updated = replace(route, start_mileage=mileage, awaiting_mileage=None)
            elif route.awaiting_mileage == "final":
                updated = replace(route, final_mileage=mileage, awaiting_mileage=None)
            else:
                return None
            data[str(chat_id)] = self._serialize(updated)
            self._write(data)
            return updated

    async def save_route_endpoint(
        self, chat_id: int, endpoint: str, geocode: GeocodeResult
    ) -> Route | None:
        if endpoint not in {"start", "end"} or not geocode.latitude or not geocode.longitude:
            return None
        async with self._lock:
            data = self._read()
            raw_route = data.get(str(chat_id))
            if not raw_route:
                return None
            route = self._deserialize(raw_route)
            if route.awaiting_route_endpoint != endpoint:
                return None
            if endpoint == "start":
                updated = replace(
                    route,
                    route_start_latitude=geocode.latitude,
                    route_start_longitude=geocode.longitude,
                    awaiting_route_endpoint=None if route.editing_route_endpoint else "end",
                    editing_route_endpoint=False,
                    route_endpoint_prompt_message_id=None,
                )
            else:
                updated = replace(
                    route,
                    route_end_latitude=geocode.latitude,
                    route_end_longitude=geocode.longitude,
                    awaiting_route_endpoint=None,
                    awaiting_mileage="start"
                    if not route.editing_route_endpoint and route.start_mileage is None
                    else route.awaiting_mileage,
                    editing_route_endpoint=False,
                    route_endpoint_prompt_message_id=None,
                )
            data[str(chat_id)] = self._serialize(updated)
            self._write(data)
            return updated

    async def set_route_endpoint_prompt_message(
        self, chat_id: int, route_id: str, message_id: int
    ) -> Route | None:
        return await self._update_route_prompt_message(chat_id, route_id, message_id)

    async def _update_route_prompt_message(self, chat_id: int, route_id: str, message_id: int) -> Route | None:
        async with self._lock:
            data = self._read()
            raw_route = data.get(str(chat_id))
            if not raw_route or raw_route.get("id") != route_id:
                return None
            route = self._deserialize(raw_route)
            updated = replace(route, route_endpoint_prompt_message_id=message_id)
            data[str(chat_id)] = self._serialize(updated)
            self._write(data)
            return updated

    async def request_route_endpoint_update(self, chat_id: int, route_id: str, endpoint: str) -> Route | None:
        if endpoint not in {"start", "end"}:
            return None
        async with self._lock:
            data = self._read()
            raw_route = data.get(str(chat_id))
            if not raw_route or raw_route.get("id") != route_id:
                return None
            route = self._deserialize(raw_route)
            updated = replace(route, awaiting_route_endpoint=endpoint, editing_route_endpoint=True)
            data[str(chat_id)] = self._serialize(updated)
            self._write(data)
            return updated

    async def save_suggestion_geocode(
        self, chat_id: int, route_id: str, point_index: int, suggestion_index: int, geocode: GeocodeResult
    ) -> Route | None:
        async with self._lock:
            data = self._read()
            raw_route = data.get(str(chat_id))
            if not raw_route or raw_route.get("id") != route_id:
                return None
            route = self._deserialize(raw_route)
            if route.awaiting_suggestion != point_index or point_index < 0 or point_index >= len(route.points):
                return None
            point = route.points[point_index]
            if not point.suggestions or suggestion_index < 0 or suggestion_index >= len(point.suggestions):
                return None
            points = list(route.points)
            points[point_index] = RoutePoint(
                point.address,
                point.orders,
                point.delivered,
                geocode.latitude,
                geocode.longitude,
                geocode.quality,
                address_confirmed=True,
                card_uri=point.suggestions[suggestion_index].uri,
            )
            next_suggestion = next((index for index, item in enumerate(points) if item.suggestions), None)
            updated = replace(
                route,
                points=points,
                awaiting_mileage=None,
                awaiting_suggestion=next_suggestion,
                awaiting_route_endpoint="start" if next_suggestion is None else None,
            )
            data[str(chat_id)] = self._serialize(updated)
            self._write(data)
            return updated

    async def request_coordinate_refinement(self, chat_id: int, route_id: str, point_index: int) -> Route | None:
        async with self._lock:
            data = self._read()
            raw_route = data.get(str(chat_id))
            if not raw_route or raw_route.get("id") != route_id:
                return None
            route = self._deserialize(raw_route)
            if route.selected_point != point_index and route.awaiting_mileage != "start" and route.awaiting_route_endpoint is None:
                return None
            updated = replace(route, awaiting_coordinate_point=point_index)
            data[str(chat_id)] = self._serialize(updated)
            self._write(data)
            return updated

    async def cancel_coordinate_refinement(self, chat_id: int, route_id: str) -> Route | None:
        async with self._lock:
            data = self._read()
            raw_route = data.get(str(chat_id))
            if not raw_route or raw_route.get("id") != route_id:
                return None
            route = self._deserialize(raw_route)
            if route.awaiting_coordinate_point is None:
                return None
            updated = replace(route, awaiting_coordinate_point=None)
            data[str(chat_id)] = self._serialize(updated)
            self._write(data)
            return updated

    async def save_manual_coordinates(
        self, chat_id: int, point_index: int, geocode: GeocodeResult
    ) -> Route | None:
        return await self._save_coordinate_refinement(chat_id, point_index, geocode)

    async def save_manual_address_geocode(
        self, chat_id: int, point_index: int, address: str, geocode: GeocodeResult, card_uri: str | None
    ) -> Route | None:
        return await self._save_coordinate_refinement(chat_id, point_index, geocode, address, card_uri)

    async def _save_coordinate_refinement(
        self,
        chat_id: int,
        point_index: int,
        geocode: GeocodeResult,
        address: str | None = None,
        card_uri: str | None = None,
    ) -> Route | None:
        async with self._lock:
            data = self._read()
            raw_route = data.get(str(chat_id))
            if not raw_route:
                return None
            route = self._deserialize(raw_route)
            if route.awaiting_coordinate_point != point_index or point_index >= len(route.points):
                return None
            points = list(route.points)
            point = points[point_index]
            points[point_index] = RoutePoint(
                address or point.address,
                point.orders,
                point.delivered,
                geocode.latitude,
                geocode.longitude,
                geocode.quality,
                point.suggestions,
                address_confirmed=address is not None or point.address_confirmed,
                card_uri=card_uri or point.card_uri,
                completed_at=point.completed_at,
                payment_received_at=point.payment_received_at,
            )
            updated = replace(route, points=points, awaiting_coordinate_point=None)
            data[str(chat_id)] = self._serialize(updated)
            self._write(data)
            return updated

    async def select_point(self, chat_id: int, route_id: str, point_index: int) -> Route | None:
        return await self._update_route(chat_id, route_id, selected_point=point_index)

    async def clear_selection(self, chat_id: int, route_id: str) -> Route | None:
        return await self._update_route(chat_id, route_id, selected_point=None)

    async def complete_point(self, chat_id: int, route_id: str, point_index: int) -> Route | None:
        return await self._complete_point(chat_id, route_id, point_index, payment_confirmed=False)

    async def request_payment_confirmation(self, chat_id: int, route_id: str, point_index: int) -> Route | None:
        async with self._lock:
            data = self._read()
            raw_route = data.get(str(chat_id))
            if not raw_route or raw_route.get("id") != route_id:
                return None
            route = self._deserialize(raw_route)
            if point_index < 0 or point_index >= len(route.points):
                return None
            point = route.points[point_index]
            if route.selected_point != point_index or point.delivered or not any(
                order.payment.strip() and order.payment.strip().casefold() != "без оплаты" for order in point.orders
            ):
                return None
            updated = replace(route, awaiting_payment_confirmation=point_index)
            data[str(chat_id)] = self._serialize(updated)
            self._write(data)
            return updated

    async def confirm_payment_and_complete(self, chat_id: int, route_id: str, point_index: int) -> Route | None:
        return await self._complete_point(chat_id, route_id, point_index, payment_confirmed=True)

    async def _complete_point(
        self, chat_id: int, route_id: str, point_index: int, *, payment_confirmed: bool
    ) -> Route | None:
        async with self._lock:
            data = self._read()
            raw_route = data.get(str(chat_id))
            if not raw_route or raw_route.get("id") != route_id:
                return None
            route = self._deserialize(raw_route)
            if point_index < 0 or point_index >= len(route.points):
                return None
            points = list(route.points)
            point = points[point_index]
            if point.delivered or route.selected_point != point_index:
                return None
            requires_payment_confirmation = any(
                order.payment.strip() and order.payment.strip().casefold() != "без оплаты" for order in point.orders
            )
            if requires_payment_confirmation:
                if route.awaiting_payment_confirmation != point_index or not payment_confirmed:
                    return None
            elif payment_confirmed:
                return None
            completed_at = datetime.now(UTC).isoformat()
            points[point_index] = RoutePoint(
                point.address,
                point.orders,
                delivered=True,
                latitude=point.latitude,
                longitude=point.longitude,
                geocode_quality=point.geocode_quality,
                suggestions=point.suggestions,
                address_confirmed=point.address_confirmed,
                card_uri=point.card_uri,
                completed_at=completed_at,
                payment_received_at=completed_at if payment_confirmed else None,
            )
            updated = replace(
                route,
                points=points,
                selected_point=None,
                awaiting_mileage="final" if all(item.delivered for item in points) else None,
                awaiting_coordinate_point=None,
                awaiting_payment_confirmation=None,
            )
            data[str(chat_id)] = self._serialize(updated)
            self._write(data)
            return updated

    async def restore_point(self, chat_id: int, route_id: str, point_index: int) -> Route | None:
        async with self._lock:
            data = self._read()
            raw_route = data.get(str(chat_id))
            if not raw_route or raw_route.get("id") != route_id:
                return None
            route = self._deserialize(raw_route)
            if point_index < 0 or point_index >= len(route.points):
                return None
            point = route.points[point_index]
            if not point.delivered:
                return None
            points = list(route.points)
            points[point_index] = replace(point, delivered=False, completed_at=None, payment_received_at=None)
            updated = replace(route, points=points, awaiting_mileage=None, awaiting_payment_confirmation=None)
            data[str(chat_id)] = self._serialize(updated)
            self._write(data)
            return updated

    async def _update_route(self, chat_id: int, route_id: str, *, selected_point: int | None) -> Route | None:
        async with self._lock:
            data = self._read()
            raw_route = data.get(str(chat_id))
            if not raw_route or raw_route.get("id") != route_id:
                return None
            route = self._deserialize(raw_route)
            if selected_point is not None and (
                selected_point < 0
                or selected_point >= len(route.points)
                or route.points[selected_point].delivered
            ):
                return None
            updated = replace(
                route,
                selected_point=selected_point,
                awaiting_coordinate_point=route.awaiting_coordinate_point if selected_point is not None else None,
                awaiting_payment_confirmation=None,
            )
            data[str(chat_id)] = self._serialize(updated)
            self._write(data)
            return updated

    def _read(self) -> dict[str, Any]:
        if not self._path.exists():
            return {}
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def _write(self, data: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self._path.with_suffix(".tmp")
        temp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temp_path.replace(self._path)

    @staticmethod
    def _serialize(route: Route) -> dict[str, Any]:
        return asdict(route)

    @staticmethod
    def _deserialize(value: dict[str, Any]) -> Route:
        return Route(
            id=value["id"],
            chat_id=value["chat_id"],
            created_at=value["created_at"],
            points=[
                RoutePoint(
                    address=point["address"],
                    orders=[Order(**order) for order in point["orders"]],
                    delivered=point.get("delivered", False),
                    latitude=point.get("latitude"),
                    longitude=point.get("longitude"),
                    geocode_quality=point.get("geocode_quality"),
                    suggestions=[Suggestion(**suggestion) for suggestion in point.get("suggestions") or []] or None,
                    address_confirmed=point.get("address_confirmed", False),
                    card_uri=point.get("card_uri"),
                    completed_at=point.get("completed_at"),
                    payment_received_at=point.get("payment_received_at"),
                )
                for point in value["points"]
            ],
            selected_point=value.get("selected_point"),
            start_mileage=value.get("start_mileage"),
            final_mileage=value.get("final_mileage"),
            awaiting_mileage=value.get("awaiting_mileage"),
            awaiting_suggestion=value.get("awaiting_suggestion"),
            awaiting_coordinate_point=value.get("awaiting_coordinate_point"),
            route_start_latitude=value.get("route_start_latitude"),
            route_start_longitude=value.get("route_start_longitude"),
            route_end_latitude=value.get("route_end_latitude"),
            route_end_longitude=value.get("route_end_longitude"),
            awaiting_route_endpoint=value.get("awaiting_route_endpoint"),
            editing_route_endpoint=value.get("editing_route_endpoint", False),
            awaiting_payment_confirmation=value.get("awaiting_payment_confirmation"),
            route_endpoint_prompt_message_id=value.get("route_endpoint_prompt_message_id"),
        )
