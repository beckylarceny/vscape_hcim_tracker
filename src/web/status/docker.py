"""Docker API access for the status page."""

from __future__ import annotations

import http.client
import json
import os
import socket
from typing import NamedTuple

DOCKER_SOCKET = os.environ.get(
    "VSCAPE_TRACKER_DOCKER_SOCKET", "/var/run/docker.sock"
)
COMPOSE_PROJECT = os.environ.get(
    "VSCAPE_TRACKER_COMPOSE_PROJECT", "vscape_tracker"
)


class ContainerInfo(NamedTuple):
    service: str
    name: str
    state: str
    status: str
    image: str


class DockerStatus(NamedTuple):
    containers: tuple[ContainerInfo, ...]
    error: str | None = None


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str, timeout: float = 3.0) -> None:
        super().__init__("localhost", timeout=timeout)
        self.socket_path = socket_path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.settimeout(self.timeout)
            sock.connect(self.socket_path)
        except Exception:
            sock.close()
            raise
        self.sock = sock


def _docker_get(socket_path: str, path: str):
    connection = _UnixHTTPConnection(socket_path)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        body = response.read()
        if response.status != 200:
            raise OSError(f"docker api returned {response.status}")
        return json.loads(body)
    finally:
        connection.close()


def _container_list(payload) -> list[dict]:
    if not isinstance(payload, list):
        return []
    return [item for item in payload if isinstance(item, dict)]


def self_project(payload) -> str | None:
    """Compose project of the container whose id starts with the hostname."""
    hostname = socket.gethostname()
    for container in _container_list(payload):
        if str(container.get("Id", "")).startswith(hostname):
            labels = container.get("Labels") or {}
            project = labels.get("com.docker.compose.project")
            if project:
                return str(project)
    return None


def container_rows(payload, project: str) -> tuple[ContainerInfo, ...]:
    rows: list[ContainerInfo] = []
    for container in _container_list(payload):
        labels = container.get("Labels") or {}
        if labels.get("com.docker.compose.project") != project:
            continue
        names = container.get("Names") or []
        if names:
            name = str(names[0])
            if name.startswith("/"):
                name = name[1:]
        else:
            name = str(container.get("Id", ""))[:12]
        rows.append(
            ContainerInfo(
                str(labels.get("com.docker.compose.service", "")),
                name,
                str(container.get("State", "")),
                str(container.get("Status", "")),
                str(container.get("Image", "")),
            )
        )
    rows.sort(key=lambda row: (row.service, row.name))
    return tuple(rows)


def container_status(socket_path: str = DOCKER_SOCKET) -> DockerStatus:
    try:
        payload = _docker_get(socket_path, "/containers/json?all=1")
        project = self_project(payload) or COMPOSE_PROJECT
        return DockerStatus(container_rows(payload, project))
    except Exception as error:  # noqa: BLE001 - docker must not break the page
        return DockerStatus((), str(error))
