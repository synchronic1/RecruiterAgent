import { randomBytes } from "node:crypto";
import { spawn } from "node:child_process";
import { mkdir, writeFile } from "node:fs/promises";
import { hostname } from "node:os";

const PORT = 8775;
const PYTHON = "/opt/plow/skills/recruiteragent/.venv/bin/python";
const PREVIEW = "/opt/plow/skills/recruiteragent/application/docs/review-artifacts/recruiteragent-design-preview.html";
const SECRET_FILE = "/var/lib/plow/recruiteragent-service/bridge-secret";
const INSTANCE = /^\/api\/v1\/instances\/inst_[a-f0-9]+\/(?:[A-Za-z0-9_.\/-]+)?$/;

export function allowedPath(raw) {
  const path = raw.split("?", 1)[0];
  if (path.includes("%") || path.includes("..") || path.includes("\\") || path.includes("//")) return false;
  return ["/recruiteragent", "/recruiteragent/", "/recruiteragent/demo"].includes(path) || INSTANCE.test(path);
}

export function registerCompanion(api) {
  let child;
  let timer;
  let stopping = false;
  let attempts = 0;
  let bridgeSecret;
  const match = hostname().match(/^plow-agent-([a-f0-9]{32})$/);
  const publicOrigin = process.env.RECRUITERAGENT_PUBLIC_ORIGIN || (match ? `https://${match[1]}.plow.run` : "http://127.0.0.1:3000");
  const launch = () => {
    if (stopping) return;
    child = spawn(PYTHON, ["-m", "resume_review.hosted", "--bridge-secret-file", SECRET_FILE,
      "--public-origin", publicOrigin, "--port", String(PORT), "--preview", PREVIEW], {
      stdio: ["ignore", "ignore", "ignore"], env: { ...process.env, HOME: "/var/lib/plow" },
    });
    child.once("error", () => api.logger.error("recruiteragent: companion launch failed"));
    child.once("exit", code => {
      child = undefined;
      if (!stopping) {
        api.logger.error(`recruiteragent: companion exited (${code}); bounded restart pending`);
        if (++attempts <= 5) timer = setTimeout(launch, Math.min(30_000, 1000 * 2 ** attempts));
      }
    });
  };
  api.registerService({
    id: "recruiteragent-companion",
    async start() {
      bridgeSecret = randomBytes(32).toString("hex");
      await mkdir("/var/lib/plow/recruiteragent-service", { recursive: true, mode: 0o700 });
      await writeFile(SECRET_FILE, bridgeSecret, { mode: 0o600 });
      launch();
    },
    async stop() {
      stopping = true;
      clearTimeout(timer);
      const running = child;
      if (running) {
        const exited = new Promise(resolve => running.once("exit", resolve));
        running.kill("SIGTERM");
        await Promise.race([exited, new Promise(resolve => setTimeout(resolve, 5000))]);
        if (running.exitCode === null) running.kill("SIGKILL");
      }
    },
  });
  const handler = async (req, res) => {
    res.setHeader("Cache-Control", "no-store");
    const fail = (status, message) => {
      res.statusCode = status;
      res.setHeader("Content-Type", "application/json");
      res.end(JSON.stringify({ error: message }));
      return true;
    };
    if (!allowedPath(req.url || "")) return fail(404, "Unknown companion route");
    if (!["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"].includes(req.method || "")) return fail(405, "Method refused");
    // Gateway auth has already validated the identity-bearing Plow proxy request.
    const owner = req.headers["x-plow-user"];
    if (typeof owner !== "string" || !/^[A-Za-z0-9_-]{1,100}$/.test(owner)) return fail(401, "Plow owner authentication required");
    if (!["GET", "HEAD"].includes(req.method) && req.headers.origin !== publicOrigin) return fail(403, "Origin rejected");
    if (!child || !bridgeSecret) return fail(503, "RecruiterAgent companion is starting. Retry shortly.");
    const chunks = [];
    let size = 0;
    for await (const chunk of req) {
      size += chunk.length;
      if (size > 2 * 1024 * 1024) return fail(413, "Request too large");
      chunks.push(chunk);
    }
    const headers = { "x-recruiteragent-bridge": bridgeSecret, "x-recruiteragent-owner": owner };
    for (const name of ["cookie", "origin", "content-type", "x-csrf-token", "x-request-id", "idempotency-key", "accept"]) {
      if (typeof req.headers[name] === "string") headers[name] = req.headers[name];
    }
    try {
      const response = await fetch(`http://127.0.0.1:${PORT}${req.url}`, {
        method: req.method, headers, redirect: "manual", signal: AbortSignal.timeout(90_000),
        ...(["GET", "HEAD"].includes(req.method) ? {} : { body: Buffer.concat(chunks) }),
      });
      res.statusCode = response.status;
      for (const name of ["content-type", "content-security-policy", "x-frame-options", "x-content-type-options", "referrer-policy", "location", "content-disposition", "x-request-id"]) {
        const value = response.headers.get(name);
        if (value) res.setHeader(name, value);
      }
      const cookies = response.headers.getSetCookie();
      if (cookies.length) res.setHeader("Set-Cookie", cookies);
      if (req.method !== "HEAD") {
        for await (const chunk of response.body || []) {
          if (!res.write(chunk)) await new Promise(resolve => res.once("drain", resolve));
        }
      }
      res.end();
      return true;
    } catch {
      if (!res.headersSent) return fail(503, "RecruiterAgent companion is unavailable. Retry shortly.");
      res.destroy();
      return true;
    }
  };
  for (const path of ["/recruiteragent", "/api/v1/instances"]) {
    api.registerHttpRoute({ path, match: "prefix", auth: "gateway", handler });
  }
}
