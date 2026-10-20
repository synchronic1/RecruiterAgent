# RecruiterAgent public demo

This folder packages the hackathon design preview as Cloudflare static assets for `recruiteragent.airanger.dev`. It includes only the generated synthetic preview, its synthetic PDF fixtures, and the linked agent-instruction files. The real resume corpus and real-data screenshots are intentionally excluded.

Build the public package from the repository root, using the optional dependencies
in `tools/requirements-demo.txt`:

```powershell
python tools/build_design_preview.py
python tools/build_public_preview.py
python -m pytest tests/unit/test_public_preview_package.py tests/unit/test_preview_agent_declarations.py
```

The second command adds the canonical for the public extensionless preview route,
an explicit synthetic-preview title/description, complete main/skip navigation,
readable saved-state text and phone-width decision controls. Its no-JavaScript
guidance explains the preview boundary. It rejects the real-corpus variant. The
offline report templates retain their local rendering contract; generated public
assets remain ignored. No publication or modification dates are manufactured.

Deploy from this directory with the existing Wrangler login:

```powershell
& 'C:\Users\NM2\airanger.dev\node_modules\.bin\wrangler.cmd' deploy --config .\wrangler.jsonc
```

The root URL redirects to `/recruiteragent-design-preview.html`. The design preview keeps all review edits in the current browser tab; no backend or dataset API is configured here.
