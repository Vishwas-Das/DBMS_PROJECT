/**
 * Live end-to-end smoke test of the LAE UI (npm run test:e2e).
 *
 * Needs: the API on :8000 (uvicorn lae.api.main:app), the UI dev server on :5173 (npm run dev)
 * and LAE_API_TOKEN in the environment. The token is put into the page's sessionStorage only;
 * it is never printed or written to disk.
 *
 * Flow (all through the UI): dashboard -> customer search -> create request -> discovery ->
 * plan (retries with another customer if the server blocks the plan) -> dry run -> approve ->
 * LIVE execute -> lineage (table / column / record) -> verification & audit -> benchmarks.
 * A LIVE run really erases one synthetic customer of the dev DB.
 *
 * Screenshots go to ../docs/screenshots (LAE_E2E_SCREENSHOTS overrides).
 */
import { mkdirSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { launch } from "./cdp.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const BASE = process.env.LAE_E2E_BASE_URL ?? "http://localhost:5173";
const SHOTS = resolve(process.env.LAE_E2E_SCREENSHOTS ?? join(here, "..", "..", "docs", "screenshots"));
const MAX_ATTEMPTS = Number(process.env.LAE_E2E_MAX_ATTEMPTS ?? "4");
const SEARCH = process.env.LAE_E2E_SEARCH ?? "";
const TOKEN = process.env.LAE_API_TOKEN;
// Resume: skip request creation and execution, only check / capture the pages of this request.
const RESUME_ID = process.env.LAE_E2E_REQUEST_ID ?? null;
// Confirm every SUGGESTED edge in the Lineage Explorer before planning (human decision, test DB).
const CONFIRM_SUGGESTED = process.env.LAE_E2E_CONFIRM_SUGGESTED === "1";

const log = (...a) => process.stdout.write(`${a.join(" ")}\n`);

async function api(path) {
  // All endpoints except /api/health need the token, GETs included. Never printed.
  const res = await fetch(`${BASE}${path}`, {
    headers: { "X-LAE-Token": TOKEN ?? "", "X-LAE-Actor": "e2e-reader" },
  });
  return { status: res.status, body: await res.json().catch(() => null) };
}

async function main() {
  if (!TOKEN) {
    log("LAE_API_TOKEN is not set; mutating steps need it. Aborting.");
    process.exit(2);
  }
  const health = await api("/api/health").catch(() => null);
  if (!health || health.status !== 200) {
    log(`API not reachable through ${BASE}/api (is uvicorn and npm run dev running?)`);
    process.exit(2);
  }
  mkdirSync(SHOTS, { recursive: true });
  const page = await launch();
  const summary = { attempts: [] };
  try {
    await page.init();

    await page.goto(`${BASE}/`);
    // credentials: sessionStorage only (same keys as src/api/credentials.ts)
    await page.call(
      (t) => {
        sessionStorage.setItem("lae.apiToken", t);
        // Four-eyes: approver and executor must be different actors.
        sessionStorage.setItem("lae.actor", "e2e-approver");
        sessionStorage.setItem("lae.executor", "e2e-executor");
      },
      TOKEN,
    );

    // ---------------------------------------------------------------- dashboard
    await page.goto(`${BASE}/`); // reload once so the app reads the credentials
    await page.waitForText("Synthetic customers", { timeout: 20000 }).catch(() => {});
    await page.waitFor(() => !document.body.innerText.includes("Loading"), [], {
      what: "dashboard loaded",
    });
    await page.screenshot(join(SHOTS, "01-dashboard.png"));
    const tokenStatus = (await page.text()).match(/Token set[^\n]*|No token[^\n]*/)?.[0];
    log(`dashboard ok (${tokenStatus ?? "no token status"})`);

    if (CONFIRM_SUGGESTED) {
      await page.nav(`/lineage`);
      await page.waitForText("Suggested edges awaiting review", { timeout: 30000 });
      const confirmed = [];
      const openCount = () =>
        page.call(
          () =>
            [...document.querySelectorAll('table[aria-label="Inferred edges"] tbody tr')].filter((r) =>
              r.textContent.includes("SUGGESTED"),
            ).length,
        );
      for (const level of ["Table view", "Column view"]) {
        await page.click(level);
        await page.waitForText("Suggested edges awaiting review", { timeout: 30000 });
        for (let i = 0; i < 20; i++) {
          const before = await openCount();
          const label = await page.call(() => {
            const rows = [...document.querySelectorAll('table[aria-label="Inferred edges"] tbody tr')];
            const b = rows.find((r) => r.textContent.includes("SUGGESTED"))?.querySelector("button");
            if (!b) return null;
            b.click();
            return b.getAttribute("aria-label");
          });
          if (!label) break;
          await page.click(label.replace("Review edge", "Confirm edge"));
          await page.waitForText("now reports this edge as CONFIRMED");
          confirmed.push(`${level}: ${label.replace("Review edge ", "")}`);
          await page.waitFor(
            (n) =>
              [...document.querySelectorAll('table[aria-label="Inferred edges"] tbody tr')].filter((r) =>
                r.textContent.includes("SUGGESTED"),
              ).length < n,
            [before],
            { what: "refetched review queue" },
          );
        }
      }
      summary.confirmed_edges = confirmed;
      log(`confirmed ${confirmed.length} suggested edge(s): ${confirmed.join("; ")}`);
      await page.screenshot(join(SHOTS, "09b-lineage-confirmed.png"));
    }

    const tried = new Set();
    let requestId = RESUME_ID;
    let plan = null;
    for (let attempt = 1; attempt <= MAX_ATTEMPTS && !requestId; attempt++) {
      // ------------------------------------------------------------ create request
      await page.nav(`/requests`);
      await page.fill("customer-search", SEARCH);
      await page.waitFor(() => document.querySelector('table[aria-label="Customer search results"]'), [], {
        what: "customer search results",
      });
      const name = await page.call((skip) => {
        const rows = [...document.querySelectorAll('table[aria-label="Customer search results"] tbody tr')];
        for (const r of rows) {
          const cells = r.querySelectorAll("td");
          const n = cells[1]?.textContent.trim();
          const open = cells[3]?.textContent.trim();
          if (n && open === "-" && !skip.includes(n)) return n;
        }
        return null;
      }, [...tried]);
      if (!name) throw new Error("no customer without an open request in the search results");
      tried.add(name);
      await page.click(`Select ${name}`);
      await page.fill("request-reason", "e2e smoke test (synthetic data)");
      if (attempt === 1) await page.screenshot(join(SHOTS, "02-requests-create.png"));
      await page.click("Create erasure request");
      await page.waitForText("Created request");
      const rid = await page.call(() => {
        const s = [...document.querySelectorAll('[role="status"]')].find((e) =>
          e.textContent.includes("Created request"),
        );
        return s?.querySelector("code")?.textContent ?? null;
      });
      log(`attempt ${attempt}: created request ${rid}`);

      // ------------------------------------------------------------ discovery + plan
      await page.click("Continue to discovery and planning");
      await page.click("1. Run discovery");
      await page.waitFor(() => !document.body.innerText.includes("Discovering..."), [], {
        timeout: 60000,
        what: "discovery finished",
      });
      await page.waitForText("Discovery result", { timeout: 60000 });
      await page.click("Generate plan");
      await page.waitFor(() => /Plan v\d+/.test(document.body.innerText), [], {
        timeout: 60000,
        what: "generated plan",
      });
      const p = (await api(`/api/erasure-requests/${rid}`)).body;
      const planOut = (await api(`/api/erasure-plans/${p.current_plan_id}`)).body;
      const codes = planOut.conflicts.map((c) => `${c.code}/${c.severity}`);
      summary.attempts.push({ request_id: rid, plan_id: planOut.id, is_blocked: planOut.is_blocked, conflicts: codes });
      log(`  plan ${planOut.id} status=${planOut.status} blocked=${planOut.is_blocked} conflicts=${codes.join(",") || "none"}`);
      if (planOut.is_blocked) {
        await page.screenshot(join(SHOTS, "03b-plan-blocked.png"));
        continue;
      }
      requestId = rid;
      plan = planOut;
    }
    if (!requestId) throw new Error(`every plan was blocked after ${MAX_ATTEMPTS} attempts`);
    summary.request_id = requestId;
    if (!RESUME_ID) {

    // ---------------------------------------------------------------- dry run
    await page.nav(`/execution/${requestId}`);
    await page.click("Dry run");
    await page.waitForText("ROLLED_BACK", { timeout: 60000 });
    log("dry run ok (ROLLED_BACK)");

    // ---------------------------------------------------------------- approve
    await page.nav(`/plan/${requestId}`);
    await page.waitForText("Discovery result", { timeout: 60000 });
    await page.screenshot(join(SHOTS, "03-plan.png"));
    await page.click("Approve this plan hash");
    await page.waitForText("Plan approved", { timeout: 30000 });
    const approved = (await api(`/api/erasure-plans/${plan.id}`)).body;
    summary.plan_status_after_approve = approved.status;
    summary.plan_hash_unchanged = approved.plan_hash === plan.plan_hash;
    log(`approved: status=${approved.status} hash unchanged=${summary.plan_hash_unchanged}`);

    // ---------------------------------------------------------------- LIVE
    await page.nav(`/execution/${requestId}`);
    await page.click("Execute LIVE...");
    await page.waitFor(() => document.querySelector('[role="dialog"] input[type="checkbox"]'), [], {
      what: "LIVE confirmation dialog",
    });
    await page.call(() => document.querySelector('[role="dialog"] input[type="checkbox"]').click());
    await page.click("Execute LIVE now");
    await page.waitFor(
      () => /LIVE execution\s*(COMMITTED|FAILED|ROLLED_BACK)/.test(document.body.innerText),
      [],
      { timeout: 120000, what: "LIVE execution result" },
    );
    const runs = (await api(`/api/executions?request_id=${requestId}`)).body;
    const live = runs.find((r) => r.mode === "LIVE");
    summary.execution = {
      id: live?.id,
      status: live?.status,
      error: live?.error?.code ?? null,
      runs: runs.map((r) => `${r.mode}:${r.status}`),
      steps: live?.steps.map((s) => `${s.step_order}:${s.action}:${s.status}:${s.rows_affected ?? "-"}/${s.expected_rows ?? "-"}`),
    };
    log(`LIVE: ${live?.status} ${live?.error?.code ?? ""}`);
    }
    if (RESUME_ID) {
      await page.nav(`/plan/${requestId}`);
      await page.waitFor(() => /Plan v\d+/.test(document.body.innerText), [], { what: "plan" });
      await page.screenshot(join(SHOTS, "03-plan.png"));
      await page.nav(`/execution/${requestId}`);
      await page.waitFor(() => /LIVE execution|Dry run/.test(document.body.innerText), [], { what: "runs" });
      const runs = (await api(`/api/executions?request_id=${requestId}`)).body;
      const live = runs.find((r) => r.mode === "LIVE");
      summary.execution = { id: live?.id, status: live?.status, error: live?.error?.code ?? null, runs: runs.map((r) => `${r.mode}:${r.status}`) };
    }
    await page.screenshot(join(SHOTS, "04-execution.png"));

    // ---------------------------------------------------------------- verification
    await page.nav(`/verification/${requestId}`);
    await page.waitFor(() => /Coverage|coverage/.test(document.body.innerText), [], {
      timeout: 60000,
      what: "verification summary",
    });
    await page.click("Verify full audit chain");
    await page.waitFor(() => /Hash chain:[\s\S]{0,40}(VALID|INVALID)/.test(document.body.innerText), [], {
      what: "chain status",
    });
    await page.screenshot(join(SHOTS, "05-verification-audit.png"));
    const ver = await api(`/api/verification/${requestId}`);
    const req = (await api(`/api/erasure-requests/${requestId}`)).body;
    summary.verification = {
      http: ver.status,
      status: ver.body?.status,
      outcome: ver.body?.outcome,
      coverage: ver.body ? `${ver.body.verified_artifacts}/${ver.body.required_artifacts}` : null,
      residual_hits: ver.body?.residual_hits ?? null,
      failed_checks: (ver.body?.checks ?? []).filter((c) => !c.passed).map((c) => `${c.check_type}:${c.schema_name}.${c.table_name}`),
    };
    summary.request_status = req.status;
    summary.latest_outcome = req.latest_outcome;
    summary.chain_valid = (await api("/api/health")).body.audit_chain_valid;
    log(`verification: ${summary.verification.status} outcome=${summary.verification.outcome}`);

    // ---------------------------------------------------------------- lineage
    await page.nav(`/lineage/${requestId}`);
    await page.waitFor(() => document.querySelector(".react-flow__node") || /Could not load/.test(document.body.innerText), [], {
      timeout: 30000,
      what: "lineage table graph",
    });
    await page.screenshot(join(SHOTS, "06-lineage-table.png"));
    await page.click("Column view");
    await page.waitFor(() => /level COLUMN/.test(document.body.innerText) || /Could not load/.test(document.body.innerText), [], {
      what: "column graph",
    });
    await page.screenshot(join(SHOTS, "07-lineage-column.png"));
    await page.click("Record graph");
    await page.waitFor(() => /Subject root: \d|No record-level lineage|Could not load/.test(document.body.innerText), [], {
      what: "record graph",
    });
    await page.screenshot(join(SHOTS, "08-lineage-records.png"));
    summary.lineage_request_text = (await page.text()).match(/Could not load[^\n]*\n[^\n]*/)?.[0] ?? "ok";

    // latest snapshot schema graph with the review queue for SUGGESTED edges
    await page.nav(`/lineage`);
    await page.waitForText("Suggested edges awaiting review", { timeout: 30000 });
    const first = await page.call(() => {
      const b = document.querySelector('table[aria-label="Inferred edges"] button');
      if (!b) return null;
      b.click();
      return b.getAttribute("aria-label");
    });
    if (first) await page.waitFor(() => document.querySelector('[aria-label="Edge review"]'), [], { what: "edge review panel" });
    await page.screenshot(join(SHOTS, "09-lineage-suggestions.png"));
    summary.suggestions = (await page.text()).match(/Suggested edges awaiting review \([^)]*\)/)?.[0];

    // ---------------------------------------------------------------- remaining pages
    await page.nav(`/requests`);
    await page.waitFor(() => !document.body.innerText.includes("Loading requests"), [], { what: "requests list" });
    await page.screenshot(join(SHOTS, "10-requests.png"));
    await page.nav(`/benchmarks`);
    await page.waitFor(() => !/Loading/.test(document.body.innerText), [], { what: "benchmarks" });
    await page.screenshot(join(SHOTS, "11-benchmarks.png"));
    await page.nav(`/`);
    await page.waitFor(() => !document.body.innerText.includes("Loading"), [], { what: "dashboard" });
    await page.screenshot(join(SHOTS, "12-dashboard-after.png"));

    summary.console_errors = page.consoleErrors.length;
    log(JSON.stringify(summary, null, 2));
    const ok = summary.execution.status === "COMMITTED" && summary.verification.http === 200;
    if (!ok) process.exitCode = 1;
  } catch (e) {
    summary.error = String(e?.message ?? e);
    summary.console_errors = page.consoleErrors;
    await page.screenshot(join(SHOTS, "zz-failure.png")).catch(() => {});
    log(JSON.stringify(summary, null, 2));
    process.exitCode = 1;
  } finally {
    await page.close();
  }
}

await main();
