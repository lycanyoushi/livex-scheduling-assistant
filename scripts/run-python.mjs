import { spawnSync } from "node:child_process";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

const repository = fileURLToPath(new URL("../", import.meta.url));
const python = join(
  repository,
  ".venv",
  process.platform === "win32" ? "Scripts/python.exe" : "bin/python",
);

const result = spawnSync(python, process.argv.slice(2), {
  cwd: repository,
  stdio: "inherit",
});

if (result.error) {
  console.error("Create .venv with python -m venv .venv and install requirements.txt into it.");
}

process.exitCode = result.status ?? 1;
