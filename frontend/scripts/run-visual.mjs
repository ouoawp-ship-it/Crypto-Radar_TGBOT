import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";

const playwrightCli = fileURLToPath(new URL("../node_modules/@playwright/test/cli.js", import.meta.url));
const env = {
  ...process.env,
  PAOXX_CAPTURE_DSF: process.env.PAOXX_CAPTURE_DSF || "1.25",
  PAOXX_ACTUAL_DIR: process.env.PAOXX_ACTUAL_DIR || "e2e/paoxx-current-local",
};

const exitCode = await new Promise((resolve, reject) => {
  const child = spawn(process.execPath, [playwrightCli, "test", "--grep", "paoxx workstation visual fixtures remain stable"], {
    env,
    stdio: "inherit",
  });
  child.once("error", reject);
  child.once("exit", (code) => resolve(code ?? 1));
});

process.exitCode = exitCode;
