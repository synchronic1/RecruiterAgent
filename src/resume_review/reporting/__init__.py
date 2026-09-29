"""Report generation: the self-contained snapshot and its atomic publication.

Authority: PRD sections 8.5 ("Connected page versus direct-file snapshot") and
9.3, plus the frozen client/server seam in ``docs/contracts/report-payload.md``.

Two delivery modes share one payload shape:

* ``connected`` — served by the helper over an authenticated address; state comes
  from the API and edits are enabled per role.
* ``snapshot`` — a single self-contained ``review.html`` opened from ``file:``;
  the payload is embedded, and edits, chat, scan, and file actions are disabled.

Nothing here performs inference, and nothing here moves applicant files.
"""

from __future__ import annotations

from .payload import (
    REPORT_SCHEMA_VERSION,
    build_snapshot_payload,
)
from .publish import PublishResult, publish_report
from .snapshot import (
    ASSET_FILENAMES,
    load_assets,
    render_snapshot,
    write_snapshot,
)

__all__ = [
    "ASSET_FILENAMES",
    "REPORT_SCHEMA_VERSION",
    "PublishResult",
    "build_snapshot_payload",
    "load_assets",
    "publish_report",
    "render_snapshot",
    "write_snapshot",
]
