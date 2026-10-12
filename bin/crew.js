#!/usr/bin/env node
// crew's npm entry point: make sure crew's Python runtime exists, then hand over to crew.py.
// The runtime is a venv in ~/.crew/runtime (kept across upgrades, rebuilt when requirements change).
"use strict";
const { spawnSync } = require("node:child_process");
const crypto = require("node:crypto");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const root = path.resolve(__dirname, "..");
const crewPy = path.join(root, "python", "crew.py");
const reqs = path.join(root, "python", "requirements.txt");
const home = process.env.CREW_HOME || path.join(os.homedir(), ".crew");
const venv = path.join(home, "runtime", "venv");
const py = path.join(venv, "bin", "python");
const stamp = path.join(home, "runtime", "requirements.sha1");

function fail(msg) {
  process.stderr.write(`crew: ${msg}\n`);
  process.exit(1);
}

function findPython() {
  const candidates = [process.env.CREW_PYTHON, "python3.14", "python3.13", "python3.12", "python3.11", "python3.10", "python3"];
  for (const c of candidates.filter(Boolean)) {
    const r = spawnSync(c, ["-c", "import sys; print(sys.version_info >= (3, 10))"], { encoding: "utf8" });
    if (r.status === 0 && r.stdout.trim() === "True") return c;
  }
  fail("needs Python 3.10 or newer (brew install python), or set CREW_PYTHON to one.");
}

function ensureRuntime() {
  const want = crypto.createHash("sha1").update(fs.readFileSync(reqs)).digest("hex");
  const have = fs.existsSync(stamp) ? fs.readFileSync(stamp, "utf8").trim() : "";
  if (fs.existsSync(py) && have === want) return;
  process.stderr.write("crew: setting up its Python runtime (once)…\n");
  if (!fs.existsSync(py)) {
    fs.mkdirSync(path.dirname(venv), { recursive: true });
    const r = spawnSync(findPython(), ["-m", "venv", venv], { stdio: "inherit" });
    if (r.status !== 0) fail("couldn't create its Python venv.");
  }
  const r = spawnSync(py, ["-m", "pip", "install", "--quiet", "--disable-pip-version-check", "-r", reqs], { stdio: "inherit" });
  if (r.status !== 0) fail("couldn't install its Python packages (rich, textual).");
  fs.writeFileSync(stamp, want + "\n");
}

if (process.platform !== "darwin") fail("runs on macOS (it uses launchd, tmux and macOS notifications).");
ensureRuntime();
if (process.argv[2] === "--setup-only") process.exit(0);
const r = spawnSync(py, [crewPy, ...process.argv.slice(2)], { stdio: "inherit" });
if (r.signal) process.kill(process.pid, r.signal);
process.exit(r.status ?? 1);
