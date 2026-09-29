"""Workspace provisioning, ownership, and integrity.

Authority: PRD sections 4, 5.2, 13 and acceptance tests AT-01, AT-02, AT-04, AT-37.

Responsibilities, one module each:

``workspace``
    The PRD section 4 folder layout, its path constants, and the collision check
    that stops setup from adopting a folder it did not create.
``registry``
    The protected host registry, stored outside the job folder, holding instance
    records and the trusted release manifests.
``ownership``
    The OS-backed single-writer lock. Real kernel locking, not a PID file.
``manifest``
    Release integrity: build, verify, deploy, and re-verify the application bundle.
``setup``
    The PRD section 5.2 sequence, minus starting the helper process.

Only ``setup`` and the dataclasses below are expected to be used by other layers;
the storage-level modules are exposed because the CLI and the API surface report
their diagnostics directly.
"""

from __future__ import annotations

from .manifest import (
    MANIFEST_VERSION,
    DeployResult,
    ManifestError,
    ManifestVerdict,
    build_manifest,
    compute_bundle_hash,
    deploy_bundle,
    verify_deployed,
    verify_manifest,
)
from .ownership import HolderInfo, InstanceLock, InstanceLockedError, lock_backend_name
from .registry import HostRegistry, RegistryEntry, default_registry_dir, default_registry_path
from .setup import SetupResult, setup_instance, snapshot_counts
from .workspace import (
    APP_MARKER,
    CollisionError,
    CollisionReport,
    app_dir,
    assert_no_collision,
    backups_dir,
    db_path,
    ensure_layout,
    exports_dir,
    extracted_dir,
    instance_manifest_path,
    is_recognisably_ours,
    job_manifest_path,
    journal_path,
    owner_lock_path,
    read_instance_manifest,
    recognised_instance_id,
    report_path,
    review_dir,
)

__all__ = [
    "APP_MARKER",
    "CollisionError",
    "CollisionReport",
    "DeployResult",
    "HolderInfo",
    "HostRegistry",
    "InstanceLock",
    "InstanceLockedError",
    "MANIFEST_VERSION",
    "ManifestError",
    "ManifestVerdict",
    "RegistryEntry",
    "SetupResult",
    "app_dir",
    "assert_no_collision",
    "backups_dir",
    "build_manifest",
    "compute_bundle_hash",
    "db_path",
    "default_registry_dir",
    "default_registry_path",
    "deploy_bundle",
    "ensure_layout",
    "exports_dir",
    "extracted_dir",
    "instance_manifest_path",
    "is_recognisably_ours",
    "job_manifest_path",
    "journal_path",
    "lock_backend_name",
    "owner_lock_path",
    "read_instance_manifest",
    "recognised_instance_id",
    "report_path",
    "review_dir",
    "setup_instance",
    "snapshot_counts",
    "verify_deployed",
    "verify_manifest",
]
