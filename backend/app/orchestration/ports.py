from __future__ import annotations

import ipaddress
import re
from pathlib import Path
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.database.models import ApplicationInstanceRecord, ApplicationRecord
from app.orchestration.port_discovery import (
    PortDiscoveryError,
    RuntimePortDiscovery,
    scan_host_listeners,
)
from app.schemas.applications import ApplicationManifest, PortDefinition, validate_manifest_paths
from app.schemas.lifecycle import ApplicationStatus
from app.schemas.ports import (
    EndpointsResponse,
    ObservedPort,
    PortConflict,
    PortsResponse,
    PortStatus,
    PortView,
    SystemPortsResponse,
)


class PortServiceError(ValueError):
    def __init__(self, code: str, message: str, details: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = details or {}


def transport_protocol(protocol: str) -> str:
    return "tcp" if protocol in {"http", "https", "tcp"} else "udp"


def addresses_conflict(first: str, second: str) -> bool:
    try:
        first_ip = ipaddress.ip_address(first)
        second_ip = ipaddress.ip_address(second)
    except ValueError:
        return first == second
    if first_ip == second_ip:
        return True
    if first == "::" or second == "::":
        return True  # Conservative: IPv6 wildcard may also accept IPv4.
    if first == "0.0.0.0" and second_ip.version == 4:
        return True
    if second == "0.0.0.0" and first_ip.version == 4:
        return True
    return False


def _public_host(value: str | None) -> str | None:
    if value is None:
        return None
    if any(character in value for character in ("/", "\\", "@", "#", "?", "\r", "\n")):
        return None
    try:
        address = ipaddress.ip_address(value)
        return f"[{address}]" if address.version == 6 else str(address)
    except ValueError:
        if not re.fullmatch(
            r"(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*"
            r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?",
            value,
        ):
            return None
        return value.lower()


def endpoint_url(port: PortDefinition, scope: str = "local") -> str | None:
    if port.protocol not in {"http", "https"} or not port.open_in_browser:
        return None
    address = ipaddress.ip_address(port.bind_address)
    if address.is_unspecified or address.is_loopback:
        configured = settings.public_host_local if scope == "local" else settings.public_host_lan
        host = _public_host(configured)
    else:
        host = _public_host(str(address))
    if host is None:
        return None
    parsed = urlsplit(port.path or "/")
    safe_path = quote(parsed.path or "/", safe="/%:@-._~!$&'()*+,;=")
    safe_query = urlencode(parse_qsl(parsed.query, keep_blank_values=True), doseq=True)
    netloc = f"{host}:{port.host_port}"
    return urlunsplit((port.protocol, netloc, safe_path, safe_query, ""))


class PortService:
    def __init__(
        self, session: Session, discovery: RuntimePortDiscovery | None = None
    ) -> None:
        self.session = session
        self.discovery = discovery or RuntimePortDiscovery()

    def _manifest(self, application_id: str) -> ApplicationManifest:
        record = self.session.get(ApplicationRecord, application_id)
        if record is None:
            raise PortServiceError("APP_NOT_FOUND", f"Application not found: {application_id}")
        manifest = ApplicationManifest.model_validate(yaml.safe_load(record.config_yaml))
        validation = validate_manifest_paths(manifest)
        if not validation.valid:
            raise PortServiceError(
                "CONFIG_INVALID", "; ".join(issue.message for issue in validation.errors)
            )
        return manifest

    def _status(self, application_id: str) -> ApplicationStatus:
        state = self.session.scalar(
            select(ApplicationInstanceRecord)
            .where(ApplicationInstanceRecord.application_id == application_id)
            .order_by(ApplicationInstanceRecord.created_at.desc())
            .limit(1)
        )
        return ApplicationStatus(state.status) if state else ApplicationStatus.UNKNOWN

    async def _observed(self, manifest: ApplicationManifest) -> tuple[list[ObservedPort], bool]:
        try:
            return await self.discovery.discover(manifest), True
        except PortDiscoveryError:
            return [], False

    async def ports(self, application_id: str, scope: str = "local") -> PortsResponse:
        manifest = self._manifest(application_id)
        observed, discovery_succeeded = await self._observed(manifest)
        state = self._status(application_id)
        conflict_scan_succeeded = True
        try:
            conflicts = await self.conflicts(manifest)
        except PortDiscoveryError:
            conflicts = []
            conflict_scan_succeeded = False
        remaining = list(observed)
        views = []
        for declared in manifest.ports:
            protocol = transport_protocol(declared.protocol)
            match = next(
                (
                    item
                    for item in remaining
                    if item.protocol == protocol
                    and item.host_port == declared.host_port
                    and item.bind_address == declared.bind_address
                ),
                None,
            )
            if match is not None:
                remaining.remove(match)
                port_status = PortStatus.LISTENING
            elif any(
                conflict.protocol == protocol and conflict.port == declared.host_port
                for conflict in conflicts
            ):
                port_status = PortStatus.CONFLICTED
            elif not discovery_succeeded or not conflict_scan_succeeded:
                port_status = PortStatus.UNKNOWN
            elif state in {ApplicationStatus.RUNNING, ApplicationStatus.UNHEALTHY}:
                port_status = PortStatus.NOT_LISTENING
            else:
                port_status = PortStatus.DECLARED
            views.append(
                PortView(
                    id=declared.id,
                    name=declared.name,
                    protocol=declared.protocol,
                    declared=declared,
                    observed=match,
                    status=port_status,
                    url=endpoint_url(declared, scope),
                )
            )
        for item in remaining:
            views.append(
                PortView(
                    id=f"observed-{item.protocol}-{item.host_port}",
                    name=f"Observed {item.protocol.upper()} {item.host_port}",
                    protocol=item.protocol,
                    declared=None,
                    observed=item,
                    status=PortStatus.UNDECLARED,
                )
            )
        return PortsResponse(application_id=application_id, ports=views)

    async def endpoints(self, application_id: str, scope: str = "local") -> EndpointsResponse:
        if scope not in {"local", "lan"}:
            raise PortServiceError("SCOPE_INVALID", "Endpoint scope must be local or lan")
        response = await self.ports(application_id, scope)
        endpoints = [
            item
            for item in response.ports
            if item.declared is not None
            and item.declared.protocol in {"http", "https"}
            and item.declared.open_in_browser
        ]
        primary = next((item for item in endpoints if item.declared and item.declared.primary), None)
        return EndpointsResponse(primary=primary, endpoints=endpoints)

    async def _managed_observations(self) -> list[ObservedPort]:
        records = self.session.scalars(select(ApplicationRecord)).all()
        observations = []
        for record in records:
            try:
                manifest = ApplicationManifest.model_validate(yaml.safe_load(record.config_yaml))
                items = await self.discovery.discover(manifest)
                for item in items:
                    item.application_id = record.id
                observations.extend(items)
            except (PortDiscoveryError, ValueError, OSError):
                continue
        return observations

    async def system_ports(self) -> SystemPortsResponse:
        listeners = await scan_host_listeners()
        managed = await self._managed_observations()
        for listener in listeners:
            owner = next(
                (
                    item
                    for item in managed
                    if (
                        listener.pid is not None
                        and item.pid is not None
                        and listener.pid == item.pid
                    )
                    or (
                        listener.protocol == item.protocol
                        and listener.host_port == item.host_port
                        and addresses_conflict(listener.bind_address, item.bind_address)
                    )
                ),
                None,
            )
            if owner:
                listener.application_id = owner.application_id
        return SystemPortsResponse(ports=listeners)

    async def conflicts(self, manifest: ApplicationManifest) -> list[PortConflict]:
        if not manifest.ports:
            return []
        listeners = (await self.system_ports()).ports
        conflicts = []
        for declared in manifest.ports:
            protocol = transport_protocol(declared.protocol)
            for listener in listeners:
                if listener.protocol != protocol or listener.host_port != declared.host_port:
                    continue
                if not addresses_conflict(declared.bind_address, listener.bind_address):
                    continue
                if listener.application_id == manifest.id:
                    continue
                conflicts.append(
                    PortConflict(
                        protocol=protocol,
                        bind_address=listener.bind_address,
                        port=listener.host_port,
                        pid=listener.pid,
                        process_name=listener.process_name,
                        application_id=listener.application_id,
                        managed_by_machinedeck=listener.application_id is not None,
                    )
                )
        return conflicts
