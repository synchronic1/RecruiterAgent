# RecruiterAgent public demo

This folder packages the hackathon design preview as Cloudflare static assets for `recruiteragent.airanger.dev`. It includes only the generated synthetic preview, its synthetic PDF fixtures, and the linked agent-instruction files. The real resume corpus and real-data screenshots are intentionally excluded.

Deploy from this directory with the existing Wrangler login:

```powershell
& 'C:\Users\NM2\airanger.dev\node_modules\.bin\wrangler.cmd' deploy --config .\wrangler.jsonc
```

The root URL redirects to `/recruiteragent-design-preview.html`. The design preview keeps all review edits in the current browser tab; no backend or dataset API is configured here.
