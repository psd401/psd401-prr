// ABOUTME: Contract for the Terraform PostToolUse hook: fmt and validate run
// ABOUTME: after an edit to a Terraform root, and failures go back to the agent.

import { afterEach, describe, expect, test } from "bun:test";
import {
  chmodSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  symlinkSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";

// The PostToolUse hook in .claude/settings.json. Its exit codes are the
// contract with the agent loop: 0 = nothing to report, 2 = hand the failure
// back to the agent.
const HOOK = resolve(__dirname, "../../../.claude/hooks/terraform-check.sh");

const projects: string[] = [];

afterEach(() => {
  for (const dir of projects.splice(0)) rmSync(dir, { recursive: true, force: true });
});

// A throwaway project with one Terraform root and a stand-in `terraform` on
// PATH that logs each call, so the hook is tested without real providers.
function fakeProject(opts: { fmt?: string; init?: string; validate?: string; initialized?: boolean } = {}) {
  const root = mkdtempSync(join(tmpdir(), "terraform-check-hook-"));
  projects.push(root);
  const tfRoot = join(root, "infra/terraform/google-identity");
  mkdirSync(tfRoot, { recursive: true });
  writeFileSync(join(tfRoot, "main.tf"), "");
  if (opts.initialized) mkdirSync(join(tfRoot, ".terraform"));
  const bin = join(root, "bin");
  mkdirSync(bin);
  const log = join(root, "calls.log");
  writeFileSync(
    join(bin, "terraform"),
    [
      "#!/bin/bash",
      `echo "$PWD $*" >> "${log}"`,
      'case "$1" in',
      `  fmt) ${opts.fmt ?? "exit 0"} ;;`,
      `  init) ${opts.init ?? "exit 0"} ;;`,
      `  validate) ${opts.validate ?? "exit 0"} ;;`,
      "esac",
      "",
    ].join("\n"),
  );
  chmodSync(join(bin, "terraform"), 0o755);
  // jq on its own, so a PATH can offer jq without the developer's terraform.
  const jqBin = join(root, "jq-bin");
  mkdirSync(jqBin);
  const jq = Bun.which("jq");
  if (jq) symlinkSync(jq, join(jqBin, "jq"));
  return { root, tfRoot, bin, jqBin, log };
}

function runHook(
  project: { root: string; bin: string; jqBin: string },
  filePath: string,
  path = `${project.bin}:${project.jqBin}:/usr/bin:/bin`,
) {
  const proc = Bun.spawnSync(["/bin/bash", HOOK], {
    stdin: Buffer.from(JSON.stringify({ tool_input: { file_path: filePath } })),
    env: { PATH: path, CLAUDE_PROJECT_DIR: project.root },
  });
  return { code: proc.exitCode, stderr: proc.stderr.toString() };
}

function calls(log: string): string[] {
  try {
    return readFileSync(log, "utf8").trim().split("\n").filter(Boolean);
  } catch {
    return [];
  }
}

describe("terraform-check PostToolUse hook", () => {
  test("ignores files outside a Terraform root", () => {
    const project = fakeProject({ fmt: "exit 1" });
    const result = runHook(project, join(project.root, "infra/cdk/lib/app.ts"));
    expect(result).toEqual({ code: 0, stderr: "" });
    expect(calls(project.log)).toEqual([]);
  });

  test("checks formatting, then validates, in the edited root", () => {
    const project = fakeProject({ initialized: true });
    const result = runHook(project, join(project.tfRoot, "main.tf"));
    expect(result).toEqual({ code: 0, stderr: "" });
    expect(calls(project.log)).toEqual([
      `${project.tfRoot} fmt -check -diff`,
      `${project.tfRoot} validate -no-color`,
    ]);
  });

  test("reports a formatting failure back to the agent", () => {
    const project = fakeProject({ fmt: "echo '-  a = 1'; exit 3", initialized: true });
    const result = runHook(project, join(project.tfRoot, "variables.tf"));
    expect(result.code).toBe(2);
    expect(result.stderr).toContain("terraform fmt");
    expect(result.stderr).toContain("-  a = 1");
  });

  test("reports a validation failure back to the agent", () => {
    const project = fakeProject({ validate: "echo 'Unsupported argument'; exit 1", initialized: true });
    const result = runHook(project, join(project.tfRoot, "main.tf"));
    expect(result.code).toBe(2);
    expect(result.stderr).toContain("terraform validate failed");
    expect(result.stderr).toContain("Unsupported argument");
  });

  // The same init CI runs: no backend, so the S3 state is never touched, and
  // a read-only lockfile, so the hook cannot rewrite pinned provider hashes.
  test("initializes an uninitialized root without its backend", () => {
    const project = fakeProject();
    const result = runHook(project, join(project.tfRoot, "main.tf"));
    expect(result).toEqual({ code: 0, stderr: "" });
    expect(calls(project.log)).toContain(
      `${project.tfRoot} init -backend=false -input=false -lockfile=readonly -no-color`,
    );
  });

  test("reports an init failure back to the agent", () => {
    const project = fakeProject({ init: "echo 'registry unreachable'; exit 1" });
    const result = runHook(project, join(project.tfRoot, "main.tf"));
    expect(result.code).toBe(2);
    expect(result.stderr).toContain("terraform init failed");
    expect(result.stderr).toContain("registry unreachable");
  });

  test("says it skipped when terraform is not installed", () => {
    const project = fakeProject();
    const result = runHook(
      project,
      join(project.tfRoot, "main.tf"),
      `${project.jqBin}:/usr/bin:/bin`,
    );
    expect(result.code).toBe(0);
    expect(result.stderr).toContain("terraform-check hook skipped: terraform is not installed");
  });

  test("says it skipped when jq is not installed", () => {
    const project = fakeProject({ fmt: "exit 1" });
    const result = runHook(project, join(project.tfRoot, "main.tf"), "/nonexistent");
    expect(result.code).toBe(0);
    expect(result.stderr).toContain("terraform-check hook skipped: jq is not installed");
  });
});
