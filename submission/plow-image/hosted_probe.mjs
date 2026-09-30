// Offline integration against the pinned Gateway and its real plugin service.
import assert from "node:assert/strict";
import { randomBytes } from "node:crypto";
import { mkdir, mkdtemp, writeFile } from "node:fs/promises";
import { spawnSync } from "node:child_process";
import { probeIdentity } from "/opt/plow/boot/probe-fixture.js";
import { renderConfig, syncConfig } from "/opt/plow/boot/config.js";
import { startGateway } from "/opt/plow/boot/process.js";

process.env.PLOW_AGENT_TOKEN = "synthetic-probe-" + randomBytes(16).toString("hex");
process.env.RECRUITERAGENT_PUBLIC_ORIGIN = "http://127.0.0.1:3000";
const temporary = await mkdtemp("/tmp/recruiteragent-hosted-probe-");
process.env.RESUME_REVIEW_REGISTRY_DIR = temporary + "/registry";
await writeFile(temporary + "/requisition.txt", "Software engineer: Python services");
const setup = spawnSync("/opt/plow/skills/recruiteragent/.venv/bin/python", ["-m", "resume_review.cli", "setup",
  "--folder", temporary + "/job", "--job", temporary + "/requisition.txt", "--json"], { env: process.env, encoding: "utf8" });
assert.equal(setup.status, 0, setup.stderr);
const instance = JSON.parse(setup.stdout).instance_id;
await mkdir("/var/lib/plow/workspace", { recursive: true });
await syncConfig(renderConfig(probeIdentity, "http://127.0.0.1:1"), "/var/lib/plow/openclaw.json", "/etc/plow/openclaw");
const child = await startGateway(true);
let log = "";
child.stdout.on("data", chunk => { log += chunk; });
child.stderr.on("data", chunk => { log += chunk; });
const origin = process.env.RECRUITERAGENT_PUBLIC_ORIGIN;
const owner = { "x-plow-user": "synthetic-owner", "x-forwarded-for": "192.0.2.1" };
const request = (path, options = {}) => fetch(origin + path, { ...options, headers: { ...owner, ...options.headers } });
const timeout = setTimeout(() => { console.error("Hosted probe timed out"); process.exitCode = 1; process.kill(process.pid, "SIGTERM"); }, 90_000);
try {
  let response;
  for (let attempt = 0; attempt < 100; attempt++) {
    try {
      response = await request("/recruiteragent");
      if (response.status === 200 && (await response.clone().text()).includes("Your job workspaces")) break;
    } catch {}
    await new Promise(resolve => setTimeout(resolve, 500));
  }
  assert.equal(response?.status, 200, log.slice(-6000));
  assert.match(await response.text(), /Your job workspaces/, log.slice(-6000));
  assert.equal((await fetch(origin + "/recruiteragent")).status, 401);
  assert.equal((await request("/recruiteragent/demo")).status, 200);
  const base = `/api/v1/instances/${instance}`;
  assert.equal((await request(base + "/documents")).status, 401);
  response = await request(base + "/review");
  assert.equal(response.status, 200);
  const page = await response.text();
  assert.match(page, /"mode":"connected"/);
  const csrf = page.match(/name="csrf-token" content="([^"]+)"/)[1];
  const cookie = response.headers.getSetCookie()[0].split(";", 1)[0];
  assert.equal((await request(base + "/assets/report.js", { headers: { cookie } })).status, 200);
  assert.equal((await request(base + "/scan", { method: "POST", headers: { cookie, origin, "content-type": "application/json" }, body: "{}" })).status, 403);
  response = await request(base + "/scan", { method: "POST", headers: { cookie, origin, "content-type": "application/json", "x-csrf-token": csrf, "idempotency-key": "offline-hosted-scan" }, body: "{}" });
  assert.equal(response.status, 202, await response.text());
  console.log(JSON.stringify({ ok: true, fixture_only: true, gateway_plugin_service: "passed", authenticated_landing_demo_review_assets: "passed", origin_csrf_scan: "passed", inference_performed: false }));
} catch (error) {
  console.error(error.message);
  console.error(log.slice(-6000));
  process.exitCode = 1;
} finally {
  clearTimeout(timeout);
  process.kill(process.pid, "SIGTERM");
}
