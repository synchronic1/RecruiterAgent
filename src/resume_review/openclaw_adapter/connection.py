"""Operator-owned connection profiles for the desktop companion (ADR 0003)."""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, StrictBool

from ..errors import Code, InvalidInput
from ..models import ModelRoute
from .client import AdapterConfig
from .policy import RouteAttestation, RoutePolicy, split_endpoint


class HostedAttestation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    attested_by: str = Field(min_length=1, max_length=200)
    attested_at: str = Field(min_length=1, max_length=100)
    no_shell: StrictBool
    no_write_or_edit: StrictBool
    no_browser_control: StrictBool
    no_messaging: StrictBool
    no_credential_read: StrictBool
    no_unrestricted_file_read: StrictBool
    no_cross_session: StrictBool
    no_agent_spawning: StrictBool
    trusted_instruction_workspace: StrictBool


class ConnectionProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(ge=1, le=1)
    instance_id: str = Field(min_length=1, max_length=128)
    base_url: str = Field(min_length=1, max_length=2048)
    agent_id: str = Field(min_length=1, max_length=128)
    secret_file: str = Field(min_length=1, max_length=4096)
    provider_label: str = Field(min_length=1, max_length=200)
    privacy_approved: StrictBool
    timeout_seconds: float = Field(default=60, gt=0, le=600)
    attestation: HostedAttestation


def _outside_job(path: Path, root: Path) -> Path:
    resolved = path.resolve()
    if resolved.is_relative_to(root.resolve()):
        raise InvalidInput(
            "Connection configuration and credentials must be outside the applicant folder.",
            code=Code.ROUTE_POLICY_VIOLATION,
        )
    return resolved


def load_connection(path: Path, *, root: Path, instance_id: str) -> AdapterConfig:
    """Load a strict, instance-bound profile; never accept browser route settings."""
    profile_path = _outside_job(Path(path), root)
    try:
        if profile_path.stat().st_size > 32_768:
            raise ValueError("oversize")
        profile = ConnectionProfile.model_validate_json(profile_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise InvalidInput("The connection profile is unreadable or invalid.") from None
    if profile.instance_id != instance_id or profile.privacy_approved is not True:
        raise InvalidInput(
            "The connection must be approved for this instance and its applicant-data privacy policy.",
            code=Code.ROUTE_POLICY_VIOLATION,
        )
    try:
        parts = urlsplit(profile.base_url)
        scheme, host, port = split_endpoint(profile.base_url)
        attested_at = datetime.fromisoformat(profile.attestation.attested_at)
        if attested_at.tzinfo is None:
            raise ValueError("attestation_timezone_missing")
    except ValueError:
        raise InvalidInput("The connection origin or attestation timestamp is invalid.") from None
    if scheme != "https" or parts.path not in ("", "/") or parts.query or parts.fragment:
        raise InvalidInput(
            "Use an HTTPS API origin, without a dashboard path, query, or fragment.",
            code=Code.ROUTE_POLICY_VIOLATION,
        )
    secret_path = Path(profile.secret_file)
    if not secret_path.is_absolute():
        raise InvalidInput("The credential file must be an absolute protected host path.")
    secret_path = _outside_job(secret_path, root)
    try:
        info = secret_path.stat()
        if not secret_path.is_file() or info.st_size > 16_384:
            raise ValueError("credential_file_invalid")
        if os.name != "nt" and info.st_mode & 0o077:
            raise ValueError("credential_permissions")
    except (OSError, ValueError):
        raise InvalidInput("The protected credential file is unavailable or has unsafe permissions.") from None
    policy = RoutePolicy(
        route=ModelRoute.APPROVED_PROVIDER,
        restricted=True,
        attestation=RouteAttestation(**profile.attestation.model_dump()),
        provider_label=profile.provider_label,
    )
    policy.assert_inference_allowed()
    config = AdapterConfig(
        base_url=profile.base_url,
        agent_id=profile.agent_id,
        timeout_seconds=profile.timeout_seconds,
        route_policy=policy,
        secret_path=secret_path,
        approved_https_origin=f"{scheme}://{host}:{port}",
    )
    config.read_secret()
    return config
