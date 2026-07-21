import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";

const playwrightCli = fileURLToPath(new URL("../node_modules/@playwright/test/cli.js", import.meta.url));
const env = {
  ...process.env,
  MERCU_CAPTURE_DSF: process.env.MERCU_CAPTURE_DSF || "1.25",
  MERCU_ACTUAL_DIR: process.env.MERCU_ACTUAL_DIR || "e2e/mercu-current-local",
};

const exitCode = await new Promise((resolve, reject) => {
  const child = spawn(process.execPath, [playwrightCli, "test", "--grep", "workstation visual fixtures remain stable"], {
    env,
    stdio: "inherit",
  });
  child.once("error", reject);
  child.once("exit", (code) => resolve(code ?? 1));
});

process.exitCode = exitCode;
