import { afterEach, describe, expect, test } from "bun:test";
import { chmodSync, mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";

// The PostToolUse hook in .claude/settings.json. Its exit codes are the
// contract with the agent loop: 0 = nothing to report, 2 = hand the failure
// back to the agent.
const HOOK = resolve(__dirname, "../../../.claude/hooks/cdk-synth.sh");

const projects: string[] = [];

afterEach(() => {
  for (const dir of projects.splice(0)) rmSync(dir, { recursive: true, force: true });
});

// A throwaway project whose infra/cdk has stand-in typecheck and cdk commands,
// so the hook's behavior is tested without synthesizing the real app.
function fakeProject(opts: { typecheck?: string; cdk?: string; nodeModules?: boolean } = {}): string {
  const root = mkdtempSync(join(tmpdir(), "cdk-synth-hook-"));
  projects.push(root);
  const cdk = join(root, "infra/cdk");
  mkdirSync(cdk, { recursive: true });
  writeFileSync(
    join(cdk, "package.json"),
    JSON.stringify({ scripts: { typecheck: opts.typecheck ?? "true" } }),
  );
  if (opts.nodeModules ?? true) {
    const bin = join(cdk, "node_modules/.bin");
    mkdirSync(bin, { recursive: true });
    writeFileSync(join(bin, "cdk"), `#!/bin/bash\n${opts.cdk ?? "exit 0"}\n`);
    chmodSync(join(bin, "cdk"), 0o755);
  }
  return root;
}

function runHook(root: string, filePath: string, env: Record<string, string> = {}) {
  const proc = Bun.spawnSync(["/bin/bash", HOOK], {
    stdin: Buffer.from(JSON.stringify({ tool_input: { file_path: filePath } })),
    env: {
      PATH: `${dirname(process.execPath)}:${process.env.PATH}`,
      CLAUDE_PROJECT_DIR: root,
      ...env,
    },
  });
  return { code: proc.exitCode, stderr: proc.stderr.toString() };
}

describe("cdk-synth PostToolUse hook", () => {
  test("ignores files outside the CDK app", () => {
    const root = fakeProject({ typecheck: "exit 1" });
    const result = runHook(root, join(root, "apps/records/models.py"));
    expect(result).toEqual({ code: 0, stderr: "" });
  });

  test("passes when typecheck and synth succeed", () => {
    const root = fakeProject();
    expect(runHook(root, join(root, "infra/cdk/lib/config.ts"))).toEqual({ code: 0, stderr: "" });
  });

  test("reports a typecheck failure back to the agent", () => {
    const root = fakeProject({ typecheck: "echo 'TS2322: broken'; exit 1" });
    const result = runHook(root, join(root, "infra/cdk/bin/prr.ts"));
    expect(result.code).toBe(2);
    expect(result.stderr).toContain("CDK typecheck failed");
    expect(result.stderr).toContain("TS2322: broken");
  });

  test("reports a synth failure back to the agent", () => {
    const root = fakeProject({ cdk: "echo 'synth exploded'; exit 1" });
    const result = runHook(root, join(root, "infra/cdk/cdk.context.json"));
    expect(result.code).toBe(2);
    expect(result.stderr).toContain("cdk synth failed");
    expect(result.stderr).toContain("synth exploded");
  });

  test("synthesizes without AWS_PROFILE, as CI does", () => {
    const root = fakeProject({ cdk: '[ -z "${AWS_PROFILE:-}" ] || { echo "profile=$AWS_PROFILE"; exit 1; }' });
    const result = runHook(root, join(root, "infra/cdk/lib/config.ts"), { AWS_PROFILE: "psd401-prr-prod" });
    expect(result).toEqual({ code: 0, stderr: "" });
  });

  test("says it skipped when infra/cdk dependencies are not installed", () => {
    const root = fakeProject({ nodeModules: false });
    const result = runHook(root, join(root, "infra/cdk/lib/config.ts"));
    expect(result.code).toBe(0);
    expect(result.stderr).toContain("cdk-synth hook skipped: run 'bun install'");
  });

  test("says it skipped when jq is not installed", () => {
    const root = fakeProject({ typecheck: "exit 1" });
    const result = runHook(root, join(root, "infra/cdk/lib/config.ts"), { PATH: "/nonexistent" });
    expect(result.code).toBe(0);
    expect(result.stderr).toContain("cdk-synth hook skipped: jq is not installed");
  });
});
