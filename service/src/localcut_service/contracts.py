"""Typed versioned UI/service protocol. Keep in sync with protocol.schema.json."""

from dataclasses import dataclass
from typing import Any, Literal, TypedDict

PROTOCOL_VERSION = 1


class Request(TypedDict):
    protocolVersion: int
    requestId: int
    method: str
    params: dict[str, Any]


class ServiceError(TypedDict):
    code: str
    message: str


class Response(TypedDict, total=False):
    protocolVersion: int
    requestId: int
    ok: bool
    result: dict[str, Any]
    error: ServiceError


@dataclass(frozen=True)
class HealthCheck:
    available: bool
    detail: str
    version: str | None = None


HealthComponent = Literal["ui", "service", "ffmpeg", "storage"]
