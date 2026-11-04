/**
 * Minimal, dependency-free Chrome DevTools Protocol driver for the e2e smoke test.
 * It launches a locally installed Chromium browser (Edge or Chrome) headless and drives
 * one page through CDP (Node >= 22 has a global WebSocket). No package is installed.
 *
 * Browser lookup: LAE_E2E_BROWSER, then the usual Edge / Chrome install paths on Windows,
 * macOS and Linux.
 */
import { spawn } from "node:child_process";
import { existsSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const CANDIDATES = [
  process.env.LAE_E2E_BROWSER,
  "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
  "C:/Program Files/Microsoft/Edge/Application/msedge.exe",
  "C:/Program Files/Google/Chrome/Application/chrome.exe",
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
  "/usr/bin/google-chrome",
  "/usr/bin/chromium",
  "/usr/bin/microsoft-edge",
].filter(Boolean);

export function findBrowser() {
  const found = CANDIDATES.find((p) => existsSync(p));
  if (!found) {
    throw new Error("no Chromium browser found; set LAE_E2E_BROWSER to a Chrome/Edge binary");
  }
  return found;
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function waitForJson(url, timeoutMs) {
  const end = Date.now() + timeoutMs;
  let last;
  while (Date.now() < end) {
    try {
      const res = await fetch(url);
      if (res.ok) return await res.json();
    } catch (e) {
      last = e;
    }
    await sleep(200);
  }
  throw new Error(`browser debug endpoint not reachable: ${String(last ?? "timeout")}`);
}

export async function launch({ width = 1440, height = 1000, port = 9333 } = {}) {
  const exe = findBrowser();
  const profile = mkdtempSync(join(tmpdir(), "lae-e2e-"));
  const proc = spawn(
    exe,
    [
      "--headless=new",
      `--remote-debugging-port=${port}`,
      `--user-data-dir=${profile}`,
      `--window-size=${width},${height}`,
      "--no-first-run",
      "--no-default-browser-check",
      "--disable-extensions",
      "--disable-gpu",
      "about:blank",
    ],
    { stdio: "ignore" },
  );
  const targets = await waitForJson(`http://127.0.0.1:${port}/json/list`, 20000);
  const page = targets.find((t) => t.type === "page");
  if (!page) throw new Error("no page target");
  const ws = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((res, rej) => {
    ws.addEventListener("open", res, { once: true });
    ws.addEventListener("error", rej, { once: true });
  });
  return new Page(ws, proc, profile, { width, height });
}

export class Page {
  constructor(ws, proc, profile, size) {
    this.ws = ws;
    this.proc = proc;
    this.profile = profile;
    this.size = size;
    this.nextId = 1;
    this.pending = new Map();
    this.listeners = new Map();
    this.consoleErrors = [];
    ws.addEventListener("message", (ev) => {
      const msg = JSON.parse(String(ev.data));
      if (msg.id && this.pending.has(msg.id)) {
        const { resolve, reject } = this.pending.get(msg.id);
        this.pending.delete(msg.id);
        if (msg.error) reject(new Error(`${msg.error.message}`));
        else resolve(msg.result);
      } else if (msg.method) {
        for (const fn of this.listeners.get(msg.method) ?? []) fn(msg.params);
      }
    });
  }

  send(method, params = {}) {
    const id = this.nextId++;
    this.ws.send(JSON.stringify({ id, method, params }));
    return new Promise((resolve, reject) => this.pending.set(id, { resolve, reject }));
  }

  on(method, fn) {
    if (!this.listeners.has(method)) this.listeners.set(method, []);
    this.listeners.get(method).push(fn);
  }

  async init() {
    await this.send("Page.enable");
    await this.send("Runtime.enable");
    this.on("Runtime.exceptionThrown", (p) =>
      this.consoleErrors.push(p.exceptionDetails?.exception?.description ?? "exception"),
    );
    this.on("Runtime.consoleAPICalled", (p) => {
      if (p.type === "error") {
        this.consoleErrors.push(p.args.map((a) => a.value ?? a.description ?? "").join(" "));
      }
    });
    await this.send("Emulation.setDeviceMetricsOverride", {
      width: this.size.width,
      height: this.size.height,
      deviceScaleFactor: 1,
      mobile: false,
    });
  }

  async goto(url) {
    const nav = await this.send("Page.navigate", { url });
    if (nav.errorText) throw new Error(`navigation to ${url} failed: ${nav.errorText}`);
    // Wait until the new document is the one we asked for and has finished loading.
    const end = Date.now() + 20000;
    while (Date.now() < end) {
      try {
        const ok = await this.eval(
          `location.href === ${JSON.stringify(url)} && document.readyState === "complete"`,
        );
        if (ok) return;
      } catch {
        /* the document is being replaced */
      }
      await sleep(150);
    }
    throw new Error(`navigation to ${url} did not complete`);
  }

  /**
   * In-app (SPA) navigation through the History API, which React Router listens to. Used after
   * the first load: headless Edge sometimes denies sessionStorage to a reloaded document.
   */
  async nav(path) {
    await this.call((p) => {
      window.history.pushState({}, "", p);
      window.dispatchEvent(new PopStateEvent("popstate"));
    }, path);
    await this.waitFor((p) => location.pathname === p, [path], { what: `route ${path}` });
    await sleep(300);
  }

  /** Evaluates a JS expression in the page and returns its JSON value. */
  async eval(expression) {
    const r = await this.send("Runtime.evaluate", {
      expression,
      awaitPromise: true,
      returnByValue: true,
    });
    if (r.exceptionDetails) {
      throw new Error(r.exceptionDetails.exception?.description ?? r.exceptionDetails.text);
    }
    return r.result.value;
  }

  /** Calls an in-page function with JSON arguments (keeps values out of the source string). */
  async call(fn, ...args) {
    return this.eval(`(${fn.toString()})(...${JSON.stringify(args)})`);
  }

  async waitFor(fn, args = [], { timeout = 30000, what = "condition" } = {}) {
    const end = Date.now() + timeout;
    while (Date.now() < end) {
      // DOM nodes cannot be returned by value: objects become `true`.
      const v = await this.eval(
        `(async () => { const v = await (${fn.toString()})(...${JSON.stringify(args)});` +
          ` return v && typeof v === "object" ? true : v; })()`,
      );
      if (v) return v;
      await sleep(250);
    }
    throw new Error(`timed out waiting for ${what}`);
  }

  async waitForText(text, opts = {}) {
    return this.waitFor((t) => document.body?.innerText.includes(t), [text], {
      what: `text "${text}"`,
      ...opts,
    });
  }

  /** Clicks the first enabled button/link whose aria-label or text equals `name`. */
  async click(name, { timeout = 30000 } = {}) {
    await this.waitFor(
      (n) => {
        const els = [...document.querySelectorAll("button, a, summary, [role=button], input[type=radio], input[type=checkbox]")];
        const el = els.find(
          (e) =>
            (e.getAttribute("aria-label") === n || e.textContent.trim().replace(/^● /, "") === n) && !e.disabled,
        );
        if (!el) return false;
        el.scrollIntoView({ block: "center" });
        el.click();
        return true;
      },
      [name],
      { timeout, what: `clickable "${name}"` },
    );
  }

  /** Sets a React-controlled input/textarea value by element id. */
  async fill(id, value) {
    await this.waitFor(
      (i, v) => {
        const el = document.getElementById(i);
        if (!el) return false;
        const proto = el.tagName === "TEXTAREA" ? HTMLTextAreaElement : HTMLInputElement;
        Object.getOwnPropertyDescriptor(proto.prototype, "value").set.call(el, v);
        el.dispatchEvent(new Event("input", { bubbles: true }));
        return true;
      },
      [id, value],
      { what: `input #${id}` },
    );
  }

  async text() {
    return this.eval("document.body.innerText");
  }

  /** Full-page PNG screenshot. */
  async screenshot(path) {
    await sleep(400);
    const m = await this.send("Page.getLayoutMetrics");
    const w = Math.ceil(m.cssContentSize?.width ?? this.size.width);
    const h = Math.min(Math.ceil(m.cssContentSize?.height ?? this.size.height), 8000);
    const r = await this.send("Page.captureScreenshot", {
      format: "png",
      captureBeyondViewport: true,
      clip: { x: 0, y: 0, width: Math.max(w, this.size.width), height: h, scale: 1 },
    });
    writeFileSync(path, Buffer.from(r.data, "base64"));
  }

  async close() {
    try {
      await this.send("Browser.close");
    } catch {
      /* already closed */
    }
    this.ws.close();
    await sleep(500);
    if (this.proc.exitCode === null) this.proc.kill();
    try {
      rmSync(this.profile, { recursive: true, force: true });
    } catch {
      /* profile may still be locked on Windows */
    }
  }
}
