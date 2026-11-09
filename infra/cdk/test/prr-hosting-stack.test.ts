// ABOUTME: Synthesis assertions for the psd401-prr hosting stack, covering the
// ABOUTME: network isolation, host hardening, and IAM limits the review demanded.

import { describe, expect, test } from "bun:test";
import { App, ContextProvider, Stack } from "aws-cdk-lib";
import { Match, Template } from "aws-cdk-lib/assertions";
import { spawnSync } from "node:child_process";
import {
  chmodSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { prodConfig, type PrrEnvConfig } from "../lib/config";
import { PrrHostingStack } from "../lib/prr-hosting-stack";
import {
  SHARED_SUBNET_LOOKUP_PROVIDER,
  sharedSubnetQuery,
  type SharedSubnetCandidate,
} from "../lib/shared-subnet";

// Route tables, NACLs and subnets belonging to the shared DMZ that serves the
// district's legacy automation production box. A single reference to one of
// these from this stack would put an internet-facing FERPA host back on the
// VPN and peering paths the security review told us to leave.
const SHARED_DMZ_IDS = [
  "rtb-08cc195c57a89c865",
  "acl-085da8a56ac930419",
  "subnet-0f692ddb4076078e9",
];

// A stand-in for the subnet psd-aws-infra tags for this host, so the suite
// exercises the tag lookup rather than its synth-time dummy.
const TEST_SUBNET_ID = "subnet-0123456789abcdef0";

function taggedSubnet(
  config: PrrEnvConfig,
  subnetId: string,
  tags: Record<string, string> = {
    Name: `psd401-prr-${config.envName}`,
    "psd:shared": "network",
  },
): SharedSubnetCandidate {
  return {
    Identifier: subnetId,
    AvailabilityZone: config.availabilityZone,
    Tags: Object.entries(tags).map(([Key, Value]) => ({ Key, Value })),
  };
}

// What `cdk synth` would have cached in cdk.context.json for the subnet
// lookup, keyed exactly as the stack asks for it.
function subnetContext(
  config: PrrEnvConfig,
  subnets: SharedSubnetCandidate[],
): Record<string, unknown> {
  const probe = new Stack(new App(), "ContextKeyProbe", {
    env: { account: config.account, region: config.region },
  });
  const { key } = ContextProvider.getKey(probe, {
    provider: SHARED_SUBNET_LOOKUP_PROVIDER,
    props: sharedSubnetQuery(config.vpcId),
  });
  return { [key]: subnets };
}

function stackFor(
  config: PrrEnvConfig,
  subnets: SharedSubnetCandidate[] = [taggedSubnet(config, TEST_SUBNET_ID)],
  extraContext: Record<string, unknown> = {},
): PrrHostingStack {
  const app = new App({
    context: { ...subnetContext(config, subnets), ...extraContext },
  });
  return new PrrHostingStack(app, `psd401-prr-${config.envName}`, config);
}

function templateFor(config: PrrEnvConfig): Template {
  return Template.fromStack(stackFor(config));
}

function jsonFor(config: PrrEnvConfig): string {
  return JSON.stringify(templateFor(config).toJSON());
}

function assertShellAndEmbeddedPythonSyntax(command: string): void {
  const shellCheck = spawnSync("bash", ["-n"], {
    encoding: "utf8",
    input: command,
  });
  if (shellCheck.status !== 0) {
    throw new Error(`Generated SSM shell is invalid:\n${shellCheck.stderr}`);
  }

  const heredocBlocks = Array.from(
    command.matchAll(/<<'PY'\n([\s\S]*?)\nPY(?=\n|$)/g),
    (match) => match[1],
  );
  const inlineBlocks = Array.from(
    command.matchAll(
      /(?:python3|python manage\.py shell(?: --no-imports)?) -c '([^']*)'/g,
    ),
    (match) => match[1],
  );
  const heredocStarts = command.match(/<<'PY'/g)?.length ?? 0;
  const inlineStarts =
    command.match(
      /(?:python3|python manage\.py shell(?: --no-imports)?) -c '/g,
    )?.length ?? 0;
  expect(heredocBlocks).toHaveLength(heredocStarts);
  expect(inlineBlocks).toHaveLength(inlineStarts);
  expect(heredocBlocks.length + inlineBlocks.length).toBeGreaterThan(0);

  for (const [index, block] of [...heredocBlocks, ...inlineBlocks].entries()) {
    const pythonCheck = spawnSync(
      "python3",
      [
        "-c",
        "import sys; compile(sys.stdin.read(), '<embedded-python>', 'exec')",
      ],
      { encoding: "utf8", input: block },
    );
    if (pythonCheck.status !== 0) {
      throw new Error(
        `Generated SSM Python block ${index + 1} is invalid:\n${pythonCheck.stderr}`,
      );
    }
  }
}

function embeddedPythonBlock(command: string, marker: string): string {
  const matches = Array.from(
    command.matchAll(/<<'PY'\n([\s\S]*?)\nPY(?=\n|$)/g),
    (match) => match[1],
  ).filter((block) => block.includes(marker));
  expect(matches).toHaveLength(1);
  return matches[0];
}

function runEmbeddedPython(
  source: string,
  args: string[] = [],
  environment: Record<string, string> = {},
) {
  return spawnSync("python3", ["-", ...args], {
    encoding: "utf8",
    env: { ...process.env, ...environment },
    input: source,
  });
}

function ssmDocumentCommand(template: Template, documentName: string): string {
  const document = Object.values(
    template.findResources("AWS::SSM::Document"),
  ).find((candidate) => candidate.Properties.Name === documentName);
  expect(document).toBeDefined();
  return document!.Properties.Content.mainSteps[0].inputs.runCommand.join("\n");
}

function applicationDeploymentCommand(template: Template): string {
  const association = Object.values(
    template.findResources("AWS::SSM::Association"),
  ).find(
    (candidate) =>
      candidate.Properties.AssociationName ===
      "psd401-prr-prod-application",
  );
  expect(association).toBeDefined();
  return association!.Properties.Parameters.commands.join("\n");
}

type IamStatement = {
  Action?: string | string[];
  Condition?: Record<string, unknown>;
  Effect?: string;
  Resource?: unknown | unknown[];
};

function actionsFor(statement: IamStatement): string[] {
  if (!statement.Action) {
    return [];
  }
  return Array.isArray(statement.Action)
    ? statement.Action
    : [statement.Action];
}

function resourcesFor(statement: IamStatement): unknown[] {
  if (!statement.Resource) {
    return [];
  }
  return Array.isArray(statement.Resource)
    ? statement.Resource
    : [statement.Resource];
}

function runtimePolicyStatements(template: Template): IamStatement[] {
  const policies = template.findResources("AWS::IAM::Policy");
  return Object.values(policies)
    .filter((policy) =>
      JSON.stringify(policy.Properties?.Roles ?? "").includes("RuntimeRole"),
    )
    .flatMap(
      (policy) =>
        (policy.Properties?.PolicyDocument?.Statement ?? []) as IamStatement[],
    );
}

function rolePolicyStatements(
  template: Template,
  logicalRoleName: string,
): IamStatement[] {
  const policies = template.findResources("AWS::IAM::Policy");
  return Object.values(policies)
    .filter((policy) =>
      JSON.stringify(policy.Properties?.Roles ?? "").includes(logicalRoleName),
    )
    .flatMap(
      (policy) =>
        (policy.Properties?.PolicyDocument?.Statement ?? []) as IamStatement[],
    );
}

function recordsPolicyStatements(template: Template): IamStatement[] {
  return runtimePolicyStatements(template)
    .filter((statement) =>
      JSON.stringify(statement.Resource ?? "").includes("RecordsBucket"),
    );
}

function recordsResourceSuffix(resource: unknown): string {
  const rendered = resource as {
    "Fn::GetAtt"?: unknown;
    "Fn::Join"?: [string, Array<string | Record<string, unknown>>];
  };
  if (rendered["Fn::GetAtt"]) {
    return "";
  }
  const parts = rendered["Fn::Join"]?.[1];
  if (!parts || !JSON.stringify(parts).includes("RecordsBucket")) {
    throw new Error(`Not a records-bucket resource: ${JSON.stringify(resource)}`);
  }
  return parts.filter((part): part is string => typeof part === "string").join("");
}

// AWS hosts production only. The stack stays environment-parameterized, so a
// future separately approved AWS environment needs a config entry rather than
// a code change.
const environments: Array<[string, PrrEnvConfig]> = [["prod", prodConfig]];

// A second environment would not have production's pinned Elastic IP, so the
// stack has to be able to allocate its own. Exercised here rather than left
// to be discovered later.
const unpinnedConfig: PrrEnvConfig = {
  ...prodConfig,
  envName: "dev",
  eipAllocationId: undefined,
  hostname: "unpinned.example.invalid",
};

describe.each(environments)("psd401-prr-%s network", (envName, config) => {
  const template = templateFor(config);
  const json = jsonFor(config);

  // psd-aws-infra owns every subnet in a shared VPC (psd-dev-standards 08
  // "Shared network"; psd401/psd-aws-infra#42). The subnet, its route table,
  // association and default route left this stack with Retain deployed, so
  // CloudFormation kept them for Terraform to import.
  test("owns no network resources", () => {
    for (const type of [
      "AWS::EC2::VPC",
      "AWS::EC2::Subnet",
      "AWS::EC2::RouteTable",
      "AWS::EC2::SubnetRouteTableAssociation",
      "AWS::EC2::Route",
    ]) {
      template.resourceCountIs(type, 0);
    }
    expect(json).not.toContain("10.0.0.0/8");
    expect(json).not.toContain("VpcPeeringConnectionId");
    expect(json).not.toContain("vgw-");
    expect(json).not.toContain("pcx-");
  });

  test("points the deployment and the InstanceId output at the instance", () => {
    const instanceRef = {
      Ref: Object.keys(template.findResources("AWS::EC2::Instance"))[0],
    };
    template.hasResourceProperties("AWS::SSM::Association", {
      AssociationName: `psd401-prr-${envName}-application`,
      Targets: [{ Key: "InstanceIds", Values: [instanceRef] }],
    });
    template.hasOutput("InstanceId", { Value: instanceRef });
  });

  test("places the host in the handed-over subnet", () => {
    template.hasResourceProperties("AWS::EC2::Instance", {
      SubnetId: TEST_SUBNET_ID,
      AvailabilityZone: config.availabilityZone,
    });
    template.hasOutput("SubnetId", { Value: TEST_SUBNET_ID });
  });

  // psd-dev-standards 08 "Shared network": an app finds a shared network
  // resource by its psd:shared tag and stable Name, never by an ID in code.
  test("queries every subnet in the configured VPC", () => {
    expect(sharedSubnetQuery(config.vpcId)).toEqual({
      typeName: "AWS::EC2::Subnet",
      propertyMatch: { VpcId: config.vpcId },
      propertiesToReturn: ["AvailabilityZone", "Tags"],
      expectedMatchCount: "at-least-one",
    });
  });

  test("picks the one subnet carrying both tags", () => {
    const picked = Template.fromStack(
      stackFor(config, [
        taggedSubnet(config, "subnet-0aaaaaaaaaaaaaaa1", {
          Name: `psd401-prr-${envName}`,
        }),
        taggedSubnet(config, "subnet-0aaaaaaaaaaaaaaa2", {
          Name: "some-other-app",
          "psd:shared": "network",
        }),
        taggedSubnet(config, TEST_SUBNET_ID),
      ]),
    );
    picked.hasResourceProperties("AWS::EC2::Instance", {
      SubnetId: TEST_SUBNET_ID,
    });
  });

  // An untagged VPC or a second subnet with the same tags must stop synth,
  // never quietly move a FERPA host somewhere else.
  test("fails synth unless exactly one subnet matches", () => {
    expect(() =>
      stackFor(config, [
        taggedSubnet(config, "subnet-0aaaaaaaaaaaaaaa1", { Name: "other" }),
      ]),
    ).toThrow(/found 0/);
    expect(() =>
      stackFor(config, [
        taggedSubnet(config, TEST_SUBNET_ID),
        taggedSubnet(config, "subnet-0aaaaaaaaaaaaaaa2"),
      ]),
    ).toThrow(/found 2/);
  });

  // The VPC import names the configured AZ; a matching subnet anywhere else
  // would move the host, so it stops synth too.
  test("fails synth if the matching subnet is in another AZ", () => {
    expect(() =>
      stackFor(config, [
        {
          ...taggedSubnet(config, TEST_SUBNET_ID),
          AvailabilityZone: "us-west-2c",
        },
      ]),
    ).toThrow(/us-west-2c/);
  });

  // The first `cdk synth` pass, before the CLI has done the lookup, sees
  // CDK's dummy value. It must still build so the CLI can ask AWS, and must
  // never emit anything that looks like a real subnet.
  test("synthesizes with the dummy value before the lookup is cached", () => {
    const app = new App();
    const stack = new PrrHostingStack(app, `psd401-prr-${envName}`, config);
    Template.fromStack(stack).hasResourceProperties("AWS::EC2::Instance", {
      SubnetId: "subnet-00000000000000000",
      AvailabilityZone: config.availabilityZone,
    });
  });

  // Both left the stack and were imported back during the subnet handover.
  // Retain keeps the host, and the Elastic IP on it, if either ever leaves
  // the stack again.
  test("retains the instance and its Elastic IP association", () => {
    for (const type of ["AWS::EC2::Instance", "AWS::EC2::EIPAssociation"]) {
      const resources = template.findResources(type);
      expect(Object.keys(resources)).toHaveLength(1);
      for (const [logicalId, resource] of Object.entries(resources)) {
        expect({
          logicalId,
          DeletionPolicy: resource.DeletionPolicy,
          UpdateReplacePolicy: resource.UpdateReplacePolicy,
        }).toEqual({
          logicalId,
          DeletionPolicy: "Retain",
          UpdateReplacePolicy: "Retain",
        });
      }
    }
  });

  test("never touches shared DMZ resources", () => {
    for (const id of SHARED_DMZ_IDS) {
      expect(json).not.toContain(id);
    }
  });

  test("leaves DNS to the hand-managed zone", () => {
    expect(json).not.toContain("AWS::Route53");
  });

  test("opens only the public web ports; no SSH ingress", () => {
    template.hasResourceProperties("AWS::EC2::SecurityGroup", {
      SecurityGroupIngress: [
        {
          CidrIp: "0.0.0.0/0",
          IpProtocol: "tcp",
          FromPort: 80,
          ToPort: 80,
        },
        {
          CidrIp: "0.0.0.0/0",
          IpProtocol: "tcp",
          FromPort: 443,
          ToPort: 443,
        },
      ],
    });
    const groups = template.findResources("AWS::EC2::SecurityGroup");
    for (const group of Object.values(groups)) {
      expect(group.Properties?.SecurityGroupIngress).toHaveLength(2);
    }
    expect(Object.keys(template.findResources("AWS::EC2::SecurityGroupIngress"))).toHaveLength(0);
    expect(json).not.toContain("CidrIpv6");
  });
});

describe.each(environments)("psd401-prr-%s host", (envName, config) => {
  const template = templateFor(config);

  test("rollback restores from a local tag, never a bare image digest", () => {
    const command = applicationDeploymentCommand(template);

    assertShellAndEmbeddedPythonSyntax(command);
    // Compose treats a bare sha256 as something to pull: it asks a registry
    // for a repository literally named "sha256" and fails with "pull access
    // denied". Passing $previous_image directly meant the rollback died on
    // its first `up` and could never restore the previous release.
    expect(command).not.toContain('PRR_RELEASE_IMAGE="$previous_image"');
    expect(command).toContain('docker tag "$previous_image"');
    expect(command).toContain('export PRR_RELEASE_IMAGE="$rollback_tag"');
  });

  test("installs the compose wrapper that cannot drop the Gmail overlay", () => {
    const command = applicationDeploymentCommand(template);

    assertShellAndEmbeddedPythonSyntax(command);
    // Gmail's enabled state is the presence of an activation overlay that
    // plain `docker compose` applies only when the caller passes -f. Forgetting
    // it silently disables inbound requester mail, and "not passed" is
    // indistinguishable from "deliberately off". The wrapper resolves the
    // overlay every time so the safe invocation is the convenient one.
    expect(command).toContain(
      '"$release/infra/aws/prr-compose.sh" /usr/local/sbin/prr-compose',
    );
  });

  test("reclaims superseded images so the root volume cannot fill", () => {
    const command = applicationDeploymentCommand(template);

    assertShellAndEmbeddedPythonSyntax(command);
    // Every deploy builds an image. With nothing reclaiming the old ones, 34
    // accumulated and filled the 48G root volume to 97%, at which point the
    // SSM document worker could not write its own stdout and every further
    // deployment failed to stabilize.
    // Deliberately NOT `docker image prune -a`: it judges an image only by
    // whether a container references it, and the rollback target has none.
    // Using it deleted the image deployed-release.json pins, leaving a
    // rollback record aimed at nothing.
    expect(command).not.toContain("docker image prune -a");
    expect(command).toContain("/srv/prr/deployed-release.json");
    expect(command).toContain("docker rmi");
    expect(command).toContain("docker builder prune -f");

    // Plain rmi, never forced: Docker refuses to remove an image any
    // container references, so the running services protect themselves.
    expect(command).not.toContain("docker rmi -f");
    expect(command).not.toContain("docker rmi --force");

    // Non-fatal, and only after the ERR trap is disarmed: housekeeping must
    // never be able to roll back a deployment that already succeeded.
    const pruneAt = command.indexOf("docker rmi");
    const trapDisarmedAt = command.indexOf("trap - ERR TERM INT HUP");
    expect(trapDisarmedAt).toBeGreaterThan(-1);
    expect(pruneAt).toBeGreaterThan(trapDisarmedAt);
    for (const line of command.split("\n")) {
      const statement = line.trimStart();
      if (
        statement.startsWith("docker")
        && (statement.includes("prune") || statement.includes("rmi"))
      ) {
        expect(line).toContain("|| true");
      }
    }
  });

  test("prunes only unpinned release directories", () => {
    const command = applicationDeploymentCommand(template);

    assertShellAndEmbeddedPythonSyntax(command);
    // Every deploy unpacks a full copy of the source asset into
    // /srv/prr/cdk-releases/<hash>/ and nothing ever removed the old ones: 78
    // directories had accumulated on the 48G root volume, each carrying its
    // own .env.app, which is how the signing key ended up in 77 places.
    expect(command).toContain("prr_prunable_releases()");
    expect(command).toContain("/srv/prr/cdk-releases");

    // What must survive. The live release is the symlink prr-compose
    // resolves; the record pins the rollback target; the compose working_dir
    // label is where a running project's own compose files and .env.app live,
    // which the rollback path reads to restore the previous runtime.
    expect(command).toContain("/srv/prr/cdk-current");
    expect(command).toContain("/srv/prr/deployed-release.json");
    expect(command).toContain("com.docker.compose.project.working_dir");
    // Stopped containers count too, and so does a bind mount, which is how a
    // container started by hand against an old release is seen at all.
    expect(command).toContain("'ps', '--all', '--quiet', '--no-trunc'");

    // The format docker is handed is Python source first and a Go template
    // second, so the template has to survive Python's own escape handling: a
    // `\n` written into that literal becomes a real line feed inside a quoted
    // string, Go's template lexer refuses it ("unterminated quoted string"),
    // docker exits non-zero, and the prune skips every deploy while reporting
    // nothing but a skip. Evaluate the literal and insist the template Go
    // receives is a single line.
    const formatSource = embeddedPythonBlock(command, "keep_recent = 3")
      .split("\n")
      .filter((line) => line.startsWith("container_format = "));
    expect(formatSource).toHaveLength(1);
    const renderedFormat = spawnSync(
      "python3",
      ["-c", `${formatSource[0]}\nimport sys; sys.stdout.write(container_format)`],
      { encoding: "utf8" },
    );
    expect(renderedFormat.status).toBe(0);
    expect(renderedFormat.stdout).not.toMatch(/[\u0000-\u001f]/);
    expect(renderedFormat.stdout).toContain("{{ range .Mounts }}");
    // A margin of recent directories, so a release nothing happens to pin is
    // still there to look at after a bad deploy.
    expect(command).toContain("keep_recent = 3");

    // Deletion runs on a path this script builds from the fixed releases root
    // plus a name it has already validated as a release hash -- never on a
    // path read from the host, and never on a bare variable.
    const deletions = command
      .split("\n")
      .filter((line) => line.includes("rm -rf"))
      .map((line) => line.trim());
    expect(deletions).toContain(
      'rm -rf -- "/srv/prr/cdk-releases/$prr_release_name" || true',
    );
    for (const deletion of deletions) {
      expect(deletion).toContain("rm -rf -- ");
    }
    // The shell re-checks the name even though Python already did: a name
    // holding anything but release-hash characters is skipped, so no `/`,
    // `..`, glob or whitespace can ever reach rm.
    expect(command).toContain('case "$prr_release_name" in *[!0-9a-f]*|"")');

    // Non-fatal, and only after the ERR trap is disarmed: housekeeping must
    // never be able to roll back a deployment that already succeeded.
    const pruneAt = command.indexOf("prr_prunable_releases()");
    const trapDisarmedAt = command.indexOf("trap - ERR TERM INT HUP");
    expect(trapDisarmedAt).toBeGreaterThan(-1);
    expect(pruneAt).toBeGreaterThan(trapDisarmedAt);
    expect(command).toContain(
      'if prr_prunable="$(prr_prunable_releases 2>/dev/null)"; then',
    );

    // Reported like the disk line, so the trend is visible before the volume
    // is full again rather than after -- and a prune that refused to decide
    // says so, because a silently skipped prune is how the disk filled.
    expect(command).toContain("release directories after prune:");
    expect(command).toContain(
      'echo "release prune skipped: the release inventory could not be read"',
    );

    // On any host without the production layout the selector refuses rather
    // than naming anything, so the loop deletes nothing and the skip is
    // reported. Which directories it keeps on a host that does have the layout
    // is exercised by `bun run rehearse`, against a real /srv/prr.
    const selector = embeddedPythonBlock(command, "keep_recent = 3");
    if (!existsSync("/srv/prr/cdk-releases")) {
      const absent = runEmbeddedPython(selector, ["/srv/prr/cdk-releases/0"]);
      expect(absent.status).not.toBe(0);
      expect(absent.stdout.trim()).toBe("");
    }
  });

  test("imports the deployed switch values into the settings store once", () => {
    const command = applicationDeploymentCommand(template);

    // The entrypoint's own import step cannot run in production: this
    // deployment replaces the entrypoint to migrate. If the import is not
    // here, the store stays empty, every switch falls back to whatever the
    // base runtime file says, and Gmail -- enabled through the activation
    // overlay rather than that file -- would silently read as off.
    //
    // It must run in the worker service, not through release_manage: the
    // activation overlay sets the Gmail switches for web, worker, and beat
    // only, and Compose merges by service name, so the migrate service never
    // sees them. An import there would store Gmail off -- permanently, since
    // the import never overwrites a stored row -- on a host where inbound
    // polling is on.
    const importStep =
      "compose_runtime run --rm --no-deps --entrypoint /usr/bin/env -e RUN_MIGRATIONS=false -e AUTO_SEED=false worker python manage.py import_environment_settings";
    expect(command).toContain(importStep);
    expect(command).not.toContain("release_manage import_environment_settings");
    // The service the import reads is one the overlay actually covers.
    expect(command).toContain(
      "\\n  worker:\\n    environment: *gmail-inbound-activation\\n",
    );
    // After the schema exists and before the replacement services start.
    expect(command.indexOf("release_manage migrate --noinput")).toBeLessThan(
      command.indexOf(importStep),
    );
    expect(command.indexOf(importStep)).toBeLessThan(
      command.indexOf("compose_runtime up -d --no-deps --force-recreate web"),
    );
  });

  test("gates the inbound-mail continuity proof on the stored Gmail switches", () => {
    const command = applicationDeploymentCommand(template);

    // Gmail is switched on and off on the Settings page now, not by the
    // activation overlay. Gating the proof on the overlay file would skip it
    // for a store-enabled mailbox, and would run it -- and fail the deploy --
    // for a mailbox an administrator turned off while the file remains.
    const probe =
      'gmail_store_mode="$(docker exec "$web_id" python manage.py shell --no-imports -c \'from apps.configuration import runtime; print("enabled" if runtime.get("PRR_GMAIL_ENABLED", raise_on_store_error=True) and runtime.get("PRR_GMAIL_INBOUND_POLL_ENABLED", raise_on_store_error=True) else "disabled")\')"';
    expect(command).toContain(probe);
    expect(command).toContain(
      'test "$gmail_store_mode" = enabled -o "$gmail_store_mode" = disabled',
    );
    const gate = 'if test "$gmail_store_mode" = enabled; then';
    expect(command).toContain(gate);
    expect(command).not.toContain('if test "$gmail_inbound_mode" = enabled; then');
    // Read from the replacement web, after the import, before the proof.
    expect(command.indexOf("worker python manage.py import_environment_settings")).toBeLessThan(
      command.indexOf(probe),
    );
    expect(command.indexOf('if test "$health" = healthy -o "$health" = running; then break; fi')).toBeLessThan(
      command.indexOf(probe),
    );
    expect(command.indexOf(probe)).toBeLessThan(command.indexOf(gate));
    expect(command.indexOf(gate)).toBeLessThan(
      command.indexOf('docker exec "$web_id" python manage.py verify_gmail'),
    );
  });

  test("preserves and proves inbound Gmail activation during deployment", () => {
    const command = applicationDeploymentCommand(template);

    assertShellAndEmbeddedPythonSyntax(command);
    expect(command).toContain(
      "/srv/prr/.gmail-inbound-activation.compose.yml",
    );
    expect(command).toContain("/srv/prr/deployment.lock");
    expect(command).toContain("PRR_CDK_DEPLOYMENT_LOCKED");
    expect(command).toContain("fcntl.LOCK_EX | fcntl.LOCK_NB");
    expect(command).toContain("com.docker.compose.project.config_files");
    expect(command).toContain("PRR_GMAIL_ENABLED");
    expect(command).toContain("PRR_GMAIL_INBOUND_POLL_ENABLED");
    expect(command).toContain("gmail_inbound_mode");
    expect(command).toContain("previous_gmail_inbound_mode");
    expect(command).toContain("compose_runtime()");
    expect(command).toContain("compose_previous_runtime()");
    expect(command).toContain("verify_runtime_switches");
    expect(command).toContain('replacement_image_id="$(docker image inspect');
    expect(command).toContain("poll_inbound_mailbox");
    expect(command).toContain("reconcile_stored_unmatched_email_intake");
    expect(command).toContain("gmail_checkpoint_outside_resync_window");
    expect(command.indexOf("before = reconcile_stored_unmatched_email_intake")).toBeLessThan(
      command.indexOf("if not fresh:"),
    );
    expect(command).toContain("dt.timedelta(seconds=7200)");
    expect(command).toContain('docker stop --signal=TERM -t 60 "$previous_beat_id"');
    expect(command).toContain("PRR_GMAIL_SCHEDULE_QUEUE_BASELINE");
    expect(command).toContain("PRR_GMAIL_SCHEDULE_TASK_ID");
    expect(command).toContain("app.AsyncResult(task_id)");
    expect(command).toContain("gmail_post_deploy_poll_unavailable");
    expect(command).toContain(`https://${config.hostname}/request`);
    expect(command).toContain("csrftoken");
    expect(command).toContain("prr_public_intake");
    expect(command).toContain("line.startswith('#HttpOnly_')");
    expect(command).toContain("public_intake_bootstrap_unavailable");
    expect(command).toContain("public_intake_asset_unavailable");
    expect(command).toContain("Frontend build not found");
    expect(command).toContain("/static/app/assets/");
    expect(command).toContain("--connect-timeout 10");
    expect(command).toContain("--max-time 30");

    const cookieValidator = command.match(
      /python3 - "\$public_intake_cookie_jar" "[^"]+" <<'PY'\n([\s\S]*?)\nPY\nthen echo/,
    )?.[1];
    expect(cookieValidator).toBeDefined();
    const fixtureDirectory = mkdtempSync(join(tmpdir(), "prr-cookie-"));
    const cookieJar = join(fixtureDirectory, "cookies.txt");
    try {
      const validCookieText = [
        "# Netscape HTTP Cookie File",
        `${config.hostname}\tFALSE\t/\tTRUE\t0\tcsrftoken\ttest-csrf`,
        `#HttpOnly_${config.hostname}\tFALSE\t/api/public/requests/\tTRUE\t0\tprr_public_intake\ttest-intake`,
        "",
      ].join("\n");
      writeFileSync(
        cookieJar,
        validCookieText,
      );
      const validCookieJar = spawnSync("python3", ["-", cookieJar, config.hostname], {
        encoding: "utf8",
        input: cookieValidator,
      });
      expect(validCookieJar.status).toBe(0);
      writeFileSync(
        cookieJar,
        validCookieText.replace("/api/public/requests/", "/"),
      );
      const unsafeCookieJar = spawnSync("python3", ["-", cookieJar, config.hostname], {
        encoding: "utf8",
        input: cookieValidator,
      });
      expect(unsafeCookieJar.status).not.toBe(0);

      const pageValidator = command.match(
        /python3 - "\$public_intake_page" <<'PY'\n([\s\S]*?)\nPY\n\)"/,
      )?.[1];
      expect(pageValidator).toBeDefined();
      const builtPage = join(__dirname, "../../../frontend/dist/index.html");
      expect(readFileSync(builtPage, "utf8")).toContain("/static/app/assets/");
      const validPage = spawnSync("python3", ["-", builtPage], {
        encoding: "utf8",
        input: pageValidator,
      });
      expect(validPage.status).toBe(0);
      expect(validPage.stdout.trim()).toMatch(
        /^\/static\/app\/assets\/[A-Za-z0-9._-]+[.]js$/,
      );
      writeFileSync(
        join(fixtureDirectory, "fallback.html"),
        "<html><body>Frontend build not found</body></html>",
      );
      const fallbackPage = spawnSync(
        "python3",
        ["-", join(fixtureDirectory, "fallback.html")],
        { encoding: "utf8", input: pageValidator },
      );
      expect(fallbackPage.status).not.toBe(0);
    } finally {
      rmSync(fixtureDirectory, { recursive: true, force: true });
    }
    expect(command).not.toContain(
      'compose() { docker compose --project-directory "$release" --project-name app -f "$release/docker-compose.deploy.yml" -f "$release/docker-compose.aws.yml" "$@"; }',
    );
    const rollback = command.slice(
      command.indexOf("rollback()"),
      command.indexOf("trap rollback ERR"),
    );
    expect(command).toContain('compose_previous_base() { docker compose --project-directory "$previous_working_dir"');
    expect(rollback).toContain("compose_previous_runtime up");
    expect(rollback).toContain("verify_previous_runtime");
    expect(rollback).not.toContain('compose_base -f "$activation_overlay"');
    expect(command).toContain('replay_output="$(docker exec');
    expect(command).toContain('2>/dev/null)" || replay_status=$?');
  });

  test("deploys onto a stopped stack without inventing a rollback target", () => {
    const command = applicationDeploymentCommand(template);

    assertShellAndEmbeddedPythonSyntax(command);

    // A hotfix is needed most when production is already down. All three
    // services live is a rolling replacement, none live is a cold start, and a
    // partial runtime stays a loud failure rather than a guess.
    expect(command).toContain("deployment_start_mode=warm");
    expect(command).toContain("deployment_start_mode=cold");
    expect(command).toContain("application_partial_runtime_detected");
    expect(command).not.toContain(
      'test -n "$previous_web_id"\ntest -n "$previous_worker_id"\ntest -n "$previous_beat_id"',
    );

    // Everything derived from a live container is warm-only.
    expect(command).toContain(
      'if test "$deployment_start_mode" = warm; then previous_image=',
    );
    for (const warmOnly of [
      'previous_working_dir="$(docker inspect',
      'docker stop --signal=TERM -t 60 "$previous_beat_id"',
    ]) {
      const line = command.slice(0, command.indexOf(warmOnly)).lastIndexOf("\n");
      const guard = command.slice(
        command.lastIndexOf('if test "$deployment_start_mode" = warm; then', line),
        line,
      );
      expect(guard).toContain('deployment_start_mode" = warm');
    }

    // A cold start reads .env.app from durable evidence, never a live label.
    expect(command).toContain("/srv/prr/cdk-current/.env.app");
    expect(command).toContain("/srv/prr/runtime-config/.env.app");
    // Staging the copy first keeps a redeploy of the live release from
    // deleting the only copy of its own runtime environment.
    expect(command.indexOf('cat -- "$runtime_app_env" > "$runtime_app_env_stage"')).toBeLessThan(
      command.indexOf('rm -f -- "$release/.env" "$release/.env.app"'),
    );

    // There is no previous image to restore to on a cold start, so the trap
    // reports that and leaves the staged release alone.
    const rollback = command.slice(
      command.indexOf("rollback() {"),
      command.indexOf("trap rollback ERR"),
    );
    const coldBranch = rollback.slice(rollback.indexOf("    else"));
    expect(coldBranch).toContain("application_cold_start_rollback_unavailable");
    expect(coldBranch).not.toContain("compose_previous_runtime");
    expect(coldBranch).not.toContain("PRR_RELEASE_IMAGE");

    // The backup gate is not optional on either path: it needs a running
    // runtime to dump, so a cold start brings one up first and still backs up
    // before `manage.py migrate`, the only state mutation in the script.
    const backup = command.indexOf(
      'if test "$deployment_start_mode" = warm; then run_application_backup; fi',
    );
    const coldBringUp = command.indexOf(
      'if test "$deployment_start_mode" = cold; then',
    );
    const migrate = command.indexOf("compose_runtime run --rm --no-deps");
    expect(backup).toBeGreaterThan(-1);
    expect(backup).toBeLessThan(coldBringUp);
    expect(coldBringUp).toBeLessThan(migrate);
    expect(command).toContain(
      "  compose_mode disabled up -d --no-deps --no-recreate db redis clamav",
    );
    expect(command).toContain("  run_application_backup");
    expect(
      command.indexOf("  run_application_backup"),
    ).toBeLessThan(migrate);
    // Cold bring-up stays in the disabled Gmail mode so no inbound polling or
    // scheduled work runs against the pre-migration schema.
    expect(command).toContain(
      "  compose_mode disabled up -d --no-deps --force-recreate web worker beat",
    );
  });

  test("applies a changed scanner configuration before web is replaced", () => {
    const command = applicationDeploymentCommand(template);

    // Without this, a raised clamd limit never reaches production: a warm
    // deploy leaves clamav alone and a cold start uses --no-recreate. Compose
    // recreates it only when its configuration changed, and web waits for a
    // healthy scanner, so a new release never starts against old limits.
    const scanner = command.indexOf(
      "compose_runtime up -d --no-deps clamav\n",
    );
    const scannerHealthy = command.indexOf(
      'await_container_health "$gmail_inbound_mode" clamav healthy replacement_scanner_unavailable',
    );
    const web = command.indexOf(
      "compose_runtime up -d --no-deps --force-recreate web\n",
    );
    expect(scanner).toBeGreaterThan(command.indexOf("release_manage verify_settings_secrets"));
    expect(scannerHealthy).toBeGreaterThan(scanner);
    expect(web).toBeGreaterThan(scannerHealthy);
  });

  test("makes the rollback trap reachable, and lets it fire only once", () => {
    const command = applicationDeploymentCommand(template);

    // Without errtrace the ERR trap is not inherited by shell functions, and
    // every mutating command runs through compose_runtime/compose_mode/
    // compose_base -- a failed replacement would exit without rolling back.
    expect(command.split("\n")[1]).not.toBe("set -euo pipefail");
    expect(command).toContain("set -Eeuo pipefail");

    // errtrace also propagates the trap into command substitutions and into
    // each nested function level inside them, so an unguarded rollback would
    // re-create the previous runtime two or three times for one failure.
    const rollback = command.slice(
      command.indexOf("rollback() {"),
      command.indexOf("trap rollback ERR"),
    );
    expect(rollback).toContain('if test "$BASHPID" != "$$"; then exit "$code"; fi');
    expect(
      rollback.indexOf('if test "$BASHPID" != "$$"'),
    ).toBeLessThan(rollback.indexOf('if test "$deployment_mutated" -eq 1'));
  });

  test("reads the Gmail mode from the overlay alone when nothing is running", () => {
    const command = applicationDeploymentCommand(template);
    const gmailMode = embeddedPythonBlock(
      command,
      "live Gmail deployment evidence is incomplete",
    );
    const fixtureDirectory = mkdtempSync(join(tmpdir(), "prr-gmail-mode-"));
    try {
      const absentOverlay = join(fixtureDirectory, "no-activation.compose.yml");

      const cold = runEmbeddedPython(gmailMode, [absentOverlay]);
      expect(cold.status).toBe(0);
      expect(cold.stdout.trim()).toBe("absent disabled");

      // Partial or duplicated live evidence is still rejected outright.
      for (const evidence of [
        ["web-id"],
        ["web-id", "worker-id"],
        ["web-id", "worker-id", "worker-id"],
        ["a", "b", "c", "d"],
      ]) {
        const rejected = runEmbeddedPython(gmailMode, [absentOverlay, ...evidence]);
        expect(rejected.status).not.toBe(0);
      }
    } finally {
      rmSync(fixtureDirectory, { recursive: true, force: true });
    }
  });

  test("fails closed when a cold start has no durable runtime configuration", () => {
    const command = applicationDeploymentCommand(template);
    const resolver = embeddedPythonBlock(
      command,
      "cold start runtime configuration is unavailable",
    );

    // Only these two canonical locations are accepted, so a replaced symlink
    // cannot substitute a different runtime environment.
    expect(resolver).toContain("/srv/prr/cdk-releases/[0-9a-f]{16,128}/[.]env[.]app");
    expect(resolver).toContain("/srv/prr/runtime-config/[.]env[.]app");

    // On any host without the production layout the resolver must refuse by
    // name rather than fall through to an unvalidated file.
    if (!existsSync("/srv/prr")) {
      const missing = runEmbeddedPython(resolver);
      expect(missing.status).not.toBe(0);
      expect(missing.stderr).toContain(
        "cold start runtime configuration is unavailable",
      );
    }
  });

  test("bounds the application command inside the association waiter", () => {
    template.hasResourceProperties("AWS::SSM::Association", {
      AssociationName: `psd401-prr-${envName}-application`,
      Name: "AWS-RunShellScript",
      Parameters: {
        executionTimeout: ["3300"],
      },
      WaitForSuccessTimeoutSeconds: 3600,
    });
  });

  test("runs the agreed instance shape", () => {
    template.resourceCountIs("AWS::EC2::Instance", 1);
    template.hasResourceProperties("AWS::EC2::Instance", {
      InstanceType: "t3a.large",
      CreditSpecification: { CPUCredits: "unlimited" },
      DisableApiTermination: true,
      KeyName: { Ref: Match.stringLikeRegexp("^KeyPair") },
    });
  });

  test("lets containers reach the instance role over IMDSv2", () => {
    // A hop limit of 1 stops any container on a Docker bridge network from
    // reaching the metadata service, which is how the application gets its
    // S3 and Secrets Manager credentials. Two hops, not more.
    template.hasResourceProperties("AWS::EC2::Instance", {
      MetadataOptions: {
        HttpTokens: "required",
        HttpPutResponseHopLimit: 2,
      },
    });
  });

  test("keeps the records volume encrypted and survivable", () => {
    template.hasResourceProperties("AWS::EC2::Instance", {
      BlockDeviceMappings: [
        {
          DeviceName: "/dev/sda1",
          Ebs: {
            Encrypted: true,
            VolumeSize: 50,
            VolumeType: "gp3",
            DeleteOnTermination: false,
          },
        },
      ],
    });
  });

  test("issues a dedicated ed25519 key pair", () => {
    template.hasResourceProperties("AWS::EC2::KeyPair", {
      KeyName: `psd401-prr-${envName}`,
      KeyType: "ed25519",
    });
  });

  test("installs Docker through user data", () => {
    const instances = template.findResources("AWS::EC2::Instance");
    const userData = JSON.stringify(
      Object.values(instances)[0]?.Properties?.UserData,
    );
    expect(userData).toContain("get.docker.com");
  });

  test("gives up on a download rather than retrying forever", () => {
    // An unbounded `until` loop turns an unreachable mirror into a host that
    // boots and hangs silently. A bounded one leaves the reason in the
    // console log, which is the only place an operator can see it.
    const instances = template.findResources("AWS::EC2::Instance");
    const userData = JSON.stringify(
      Object.values(instances)[0]?.Properties?.UserData,
    );
    expect(userData).not.toContain("until curl");
    expect(userData).not.toContain("until snap");
    expect(userData).toContain("attempts");
  });

  test("keeps app backup installation out of immutable instance user data", () => {
    const instances = template.findResources("AWS::EC2::Instance");
    const userData = JSON.stringify(
      Object.values(instances)[0]?.Properties?.UserData,
    );
    expect(userData).not.toContain("/srv/prr/backups");
  });
});

describe("elastic IP ownership", () => {
  test("prod associates the pinned allocation and never creates one", () => {
    const template = templateFor(prodConfig);
    template.resourceCountIs("AWS::EC2::EIP", 0);
    template.hasResourceProperties("AWS::EC2::EIPAssociation", {
      AllocationId: prodConfig.eipAllocationId,
    });
  });

  test("an environment with no pinned allocation gets its own", () => {
    const template = templateFor(unpinnedConfig);
    template.resourceCountIs("AWS::EC2::EIP", 1);
    template.resourceCountIs("AWS::EC2::EIPAssociation", 1);
  });
});

describe.each(environments)("psd401-prr-%s storage", (envName, config) => {
  const template = templateFor(config);

  test("keeps every bucket private, versioned and encrypted", () => {
    template.resourceCountIs("AWS::S3::Bucket", 3);
    for (const bucketName of [
      `psd401-prr-records-${envName}`,
      `psd401-prr-backups-${envName}`,
      `psd401-prr-access-logs-${envName}`,
    ]) {
      template.hasResource("AWS::S3::Bucket", {
        DeletionPolicy: "Retain",
        Properties: {
          BucketName: bucketName,
          VersioningConfiguration: { Status: "Enabled" },
          PublicAccessBlockConfiguration: {
            BlockPublicAcls: true,
            BlockPublicPolicy: true,
            IgnorePublicAcls: true,
            RestrictPublicBuckets: true,
          },
          BucketEncryption: {
            ServerSideEncryptionConfiguration: [
              {
                ServerSideEncryptionByDefault: {
                  SSEAlgorithm: "AES256",
                },
              },
            ],
          },
        },
      });
    }
  });

  test("sends records and backups server access logs to the log bucket", () => {
    const resources = template.toJSON().Resources as Record<
      string,
      { Type: string; Properties?: Record<string, unknown> }
    >;
    const logBucketId = Object.entries(resources).find(
      ([, resource]) =>
        resource.Type === "AWS::S3::Bucket" &&
        resource.Properties?.BucketName === `psd401-prr-access-logs-${envName}`,
    )?.[0];
    expect(logBucketId).toBeDefined();
    for (const [bucketName, prefix] of [
      [`psd401-prr-records-${envName}`, "records/"],
      [`psd401-prr-backups-${envName}`, "backups/"],
    ] as const) {
      template.hasResourceProperties("AWS::S3::Bucket", {
        BucketName: bucketName,
        LoggingConfiguration: {
          DestinationBucketName: { Ref: logBucketId },
          LogFilePrefix: prefix,
        },
      });
    }
  });

  test("lets only S3 log delivery from this account write access logs", () => {
    // ACL-based log delivery is the legacy path; the bucket policy grant is
    // the only one that works with object ownership enforced.
    template.hasResourceProperties("AWS::S3::Bucket", {
      BucketName: `psd401-prr-access-logs-${envName}`,
      OwnershipControls: {
        Rules: [{ ObjectOwnership: "BucketOwnerEnforced" }],
      },
    });
    template.hasResourceProperties("AWS::S3::BucketPolicy", {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Effect: "Allow",
            Principal: { Service: "logging.s3.amazonaws.com" },
            Action: "s3:PutObject",
            Condition: Match.objectLike({
              StringEquals: { "aws:SourceAccount": config.account },
            }),
          }),
        ]),
      },
    });
  });

  test("declares no lifecycle expiry", () => {
    // The WA public-records retention schedule is unconfirmed; automatic
    // deletion of a record is the legal risk, not storage cost.
    expect(jsonFor(config)).not.toContain("LifecycleConfiguration");
  });
});

describe.each(environments)("psd401-prr-%s identity", (envName, config) => {
  const template = templateFor(config);
  const json = jsonFor(config);

  test("names the runtime role exactly as Terraform expects", () => {
    template.hasResourceProperties("AWS::IAM::Role", {
      RoleName: `psd401-prr-${envName}-runtime`,
      AssumeRolePolicyDocument: {
        Statement: [
          {
            Action: "sts:AssumeRole",
            Effect: "Allow",
            Principal: { Service: "ec2.amazonaws.com" },
          },
        ],
      },
    });
  });


  test("permits only the AWS identity proof used by Google federation", () => {
    const stsStatements = runtimePolicyStatements(template).filter((statement) =>
      actionsFor(statement).some((action) => action.startsWith("sts:")),
    );

    expect(stsStatements).toHaveLength(1);
    expect(actionsFor(stsStatements[0])).toEqual(["sts:GetCallerIdentity"]);
    expect(resourcesFor(stsStatements[0])).toEqual(["*"]);
    expect(stsStatements[0].Condition).toBeUndefined();
    expect(json).not.toContain("sts:*");
    expect(stsStatements.flatMap(actionsFor)).not.toContain("sts:AssumeRole");
  });

  test("invokes only the allow-listed US advisory inference profiles", () => {
    const statements = runtimePolicyStatements(template).filter((statement) =>
      actionsFor(statement).some((action) => action.startsWith("bedrock:")),
    );
    const allowed: Array<[string, string]> = [
      ["us.amazon.nova-2-lite-v1:0", "amazon.nova-2-lite-v1:0"],
      ["us.anthropic.claude-opus-5-5", "anthropic.claude-opus-5-5"],
      ["us.anthropic.claude-fable-5-1", "anthropic.claude-fable-5-1"],
      ["us.anthropic.claude-sonnet-5-5", "anthropic.claude-sonnet-5-5"],
      ["us.anthropic.claude-haiku-5-5", "anthropic.claude-haiku-5-5"],
      ["us.openai.gpt-6.1-sol", "openai.gpt-6.1-sol"],
      ["us.openai.gpt-6.1-terra", "openai.gpt-6.1-terra"],
    ];

    // One read-only statement lets verification confirm zero retention.
    const retention = statements.filter((statement) =>
      actionsFor(statement).includes("bedrock:GetAccountDataRetention"),
    );
    expect(retention).toHaveLength(1);
    expect(actionsFor(retention[0])).toEqual(["bedrock:GetAccountDataRetention"]);
    expect(JSON.stringify(statements)).not.toContain("PutAccountDataRetention");
    const invoke = statements.filter((statement) => statement !== retention[0]);
    statements.splice(0, statements.length, ...invoke);
    expect(statements).toHaveLength(allowed.length * 2);
    expect(new Set(statements.flatMap(actionsFor))).toEqual(
      new Set(["bedrock:InvokeModel"]),
    );
    allowed.forEach(([profileId, foundationModelId], index) => {
      const profileArn =
        `arn:aws:bedrock:us-west-2:${config.account}:inference-profile/` +
        profileId;
      const profile = statements[index * 2];
      const foundation = statements[index * 2 + 1];
      expect(resourcesFor(profile)).toEqual([profileArn]);
      expect(profile.Condition).toBeUndefined();
      expect(resourcesFor(foundation)).toEqual(
        ["us-east-1", "us-east-2", "us-west-2"].map(
          (region) =>
            `arn:aws:bedrock:${region}::foundation-model/${foundationModelId}`,
        ),
      );
      expect(foundation.Condition).toEqual({
        StringLike: { "bedrock:InferenceProfileArn": profileArn },
      });
    });
    expect(json).not.toContain("bedrock:*");
    expect(json).not.toContain("foundation-model/*");
  });

  test("grants exactly the private object operations the application uses", () => {
    const statements = recordsPolicyStatements(template);
    const s3Statements = runtimePolicyStatements(template).filter((statement) =>
      actionsFor(statement).some((action) => action.startsWith("s3:")),
    );
    expect(s3Statements).toHaveLength(6);
    expect(s3Statements.every((statement) => statement.Effect === "Allow")).toBe(
      true,
    );
    expect(s3Statements.flatMap(actionsFor).sort()).toEqual(
      [
        "s3:DeleteObject",
        "s3:DeleteObjectVersion",
        "s3:GetBucket*",
        "s3:GetBucketVersioning",
        "s3:GetObject",
        "s3:GetObject*",
        "s3:List*",
        "s3:ListBucketVersions",
        "s3:PutObject",
        "s3:PutObject",
      ].sort(),
    );
    expect(statements).toHaveLength(4);
    expect(statements.flatMap(actionsFor).sort()).toEqual(
      [
        "s3:DeleteObject",
        "s3:DeleteObjectVersion",
        "s3:GetBucketVersioning",
        "s3:GetObject",
        "s3:ListBucketVersions",
        "s3:PutObject",
      ].sort(),
    );

    const versioningStatement = statements.find((statement) =>
      actionsFor(statement).includes("s3:GetBucketVersioning"),
    );
    expect(versioningStatement).toBeDefined();
    expect(actionsFor(versioningStatement!)).toEqual([
      "s3:GetBucketVersioning",
    ]);
    expect(resourcesFor(versioningStatement!).map(recordsResourceSuffix)).toEqual([
      "",
    ]);
    expect(versioningStatement!.Condition).toBeUndefined();

    const listVersionsStatement = statements.find((statement) =>
      actionsFor(statement).includes("s3:ListBucketVersions"),
    );
    expect(listVersionsStatement).toBeDefined();
    expect(actionsFor(listVersionsStatement!)).toEqual([
      "s3:ListBucketVersions",
    ]);
    expect(resourcesFor(listVersionsStatement!).map(recordsResourceSuffix)).toEqual([
      "",
    ]);
    expect(listVersionsStatement!.Condition).toEqual({
      StringLike: {
        "s3:prefix": [
          "quarantine/intake/*",
          "quarantine/correspondence/raw/*",
          "quarantine/correspondence/attachments/*",
          "quarantine/vault-messages/*",
          "quarantine/legacy-files/*",
          "quarantine/record-uploads/*",
        ],
      },
    });

    const objectReadWriteStatement = statements.find((statement) =>
      actionsFor(statement).includes("s3:GetObject"),
    );
    expect(objectReadWriteStatement).toBeDefined();
    expect(actionsFor(objectReadWriteStatement!).sort()).toEqual(
      ["s3:GetObject", "s3:PutObject"].sort(),
    );
    expect(
      resourcesFor(objectReadWriteStatement!).map(recordsResourceSuffix),
    ).toEqual([
      "/quarantine/intake/*",
      "/quarantine/correspondence/raw/*",
      "/quarantine/correspondence/attachments/*",
      "/quarantine/vault-messages/*",
      "/quarantine/legacy-files/*",
      "/quarantine/record-uploads/*",
      "/intake/*",
      "/correspondence/attachments/*",
      "/records/*",
      "/derivatives/*",
      "/productions/*",
      "/exemption-logs/*",
      "/vault-exports/*",
      "/vault-messages/*",
    ]);
    expect(objectReadWriteStatement!.Condition).toBeUndefined();

    for (const obsoleteAction of [
      "s3:AbortMultipartUpload",
      "s3:ListBucket",
      "s3:ListBucketMultipartUploads",
      "s3:ListMultipartUploadParts",
    ]) {
      expect(statements.flatMap(actionsFor)).not.toContain(obsoleteAction);
    }
  });

  test("restricts each destructive grant to the generated quarantine prefixes", () => {
    const destructiveStatements = runtimePolicyStatements(template).filter(
      (statement) =>
        actionsFor(statement).some(
          (action) => action === "s3:*" || action.startsWith("s3:Delete"),
        ),
    );
    expect(destructiveStatements).toHaveLength(1);
    expect(actionsFor(destructiveStatements[0]).sort()).toEqual(
      ["s3:DeleteObject", "s3:DeleteObjectVersion"].sort(),
    );
    expect(
      resourcesFor(destructiveStatements[0]).map(recordsResourceSuffix).sort(),
    ).toEqual(
      [
        "/quarantine/intake/*",
        "/quarantine/correspondence/raw/*",
        "/quarantine/correspondence/attachments/*",
        "/quarantine/vault-messages/*",
        "/quarantine/legacy-files/*",
        "/quarantine/record-uploads/*",
      ].sort(),
    );
    expect(destructiveStatements[0].Condition).toBeUndefined();
  });

  test("can only append to the backups bucket", () => {
    const backupStatements = runtimePolicyStatements(template).filter((statement: any) =>
      JSON.stringify(statement.Resource ?? "").includes("BackupsBucket"),
    );
    expect(backupStatements).toHaveLength(1);
    const appBackup = backupStatements.find((statement: any) =>
      JSON.stringify(statement.Resource).includes("/app-backups/*"),
    );
    expect(appBackup).toBeDefined();
    expect(appBackup!.Action).toBe("s3:PutObject");
    expect(JSON.stringify(appBackup!.Resource)).toContain(
      "/app-backups/*",
    );
    expect(appBackup!.Condition).toEqual({
      Null: { "s3:if-none-match": "false" },
      StringEquals: { "s3:x-amz-server-side-encryption": "AES256" },
    });
  });

  test("publishes backup health only to the PRR metric namespace", () => {
    const statements = runtimePolicyStatements(template).filter((statement) =>
      actionsFor(statement).includes("cloudwatch:PutMetricData"),
    );
    expect(statements).toHaveLength(1);
    expect(actionsFor(statements[0])).toEqual(["cloudwatch:PutMetricData"]);
    expect(resourcesFor(statements[0])).toEqual(["*"]);
    expect(statements[0].Condition).toEqual({
      StringEquals: { "cloudwatch:namespace": "PSD401/PRR" },
    });
  });

  test("reads only its own runtime secret, and cannot change it", () => {
    // DJANGO_SECRET_KEY and PRR_SETTINGS_ENCRYPTION_KEY are read at process
    // start through this role (standards/04 rule 15a). Read-only: the value is
    // seeded by an operator, never written or generated by the host.
    const policies = template.findResources("AWS::IAM::Policy");
    const statements = Object.values(policies).flatMap(
      (policy) => policy.Properties?.PolicyDocument?.Statement ?? [],
    );
    const secretStatements = statements.filter((statement: any) =>
      JSON.stringify(statement.Action ?? "").includes("secretsmanager:"),
    );
    expect(secretStatements).toHaveLength(1);
    expect(actionsFor(secretStatements[0])).toEqual([
      "secretsmanager:GetSecretValue",
    ]);
    const secretId = Object.keys(
      template.findResources("AWS::SecretsManager::Secret"),
    )[0];
    expect(secretStatements[0].Resource).toEqual({ Ref: secretId });
  });

  test("registers the host with least-privilege Systems Manager permissions", () => {
    expect(json).not.toContain("AmazonSSMManagedInstanceCore");
    expect(json).not.toContain('"ssm:GetParameter"');
    expect(json).not.toContain('"ssm:GetParameters"');

    const statements = runtimePolicyStatements(template);
    const messageStatement = statements.find((statement) =>
      actionsFor(statement).includes("ssmmessages:OpenControlChannel"),
    );
    expect(messageStatement).toBeDefined();
    expect(actionsFor(messageStatement!).sort()).toEqual(
      [
        "ssmmessages:CreateControlChannel",
        "ssmmessages:CreateDataChannel",
        "ssmmessages:OpenControlChannel",
        "ssmmessages:OpenDataChannel",
      ].sort(),
    );
    expect(resourcesFor(messageStatement!)).toEqual(["*"]);

    const documentStatement = statements.find((statement) =>
      actionsFor(statement).includes("ssm:GetDocument"),
    );
    expect(documentStatement).toBeDefined();
    expect(actionsFor(documentStatement!).sort()).toEqual(
      ["ssm:DescribeDocument", "ssm:GetDocument"].sort(),
    );
    expect(JSON.stringify(resourcesFor(documentStatement!))).toContain(
      `document/psd401-prr-${envName}-email-diagnostics`,
    );
    // The Gmail switches are district configuration now, so the activation
    // document that used to flip them is retired and the runtime role has no
    // reason to read it.
    expect(JSON.stringify(resourcesFor(documentStatement!))).not.toContain(
      `document/psd401-prr-${envName}-enable-gmail-inbound`,
    );
    expect(JSON.stringify(resourcesFor(documentStatement!))).not.toContain(
      `document/psd401-prr-${envName}-deploy`,
    );
    expect(JSON.stringify(resourcesFor(documentStatement!))).not.toContain(
      `document/psd401-prr-${envName}-recover-public-failure`,
    );
  });


  test("provides a fixed, parameter-free email diagnostic command", () => {
    template.hasResourceProperties("AWS::SSM::Document", {
      DocumentType: "Command",
      Name: `psd401-prr-${envName}-email-diagnostics`,
      TargetType: "/AWS::EC2::Instance",
      UpdateMethod: "NewVersion",
      Content: {
        schemaVersion: "2.2",
        description:
          "Read-only PRR email worker, poller, and Gmail credential diagnostics without requester data.",
        mainSteps: [
          {
            action: "aws:runShellScript",
            name: "emailDiagnostics",
            inputs: {
              runCommand: Match.arrayWith([
                Match.stringLikeRegexp("docker compose ps.*web worker beat"),
                // The Gmail switches live in the settings store now; an
                // image that has it must report the store, not the
                // environment value the store stopped consulting.
                Match.stringLikeRegexp(
                  'manage.py shell -c.*GmailSyncState.*runtime.get\\("PRR_GMAIL_INBOUND_POLL_ENABLED"\\)',
                ),
                Match.stringLikeRegexp("manage.py verify_gmail"),
                'exit "$diagnostic_status"',
              ]),
            },
          },
        ],
      },
    });
    const documents = Object.values(
      template.findResources("AWS::SSM::Document"),
    );
    const document = documents.find(
      (candidate) =>
        candidate.Properties.Name ===
        `psd401-prr-${envName}-email-diagnostics`,
    );
    expect(document).toBeDefined();
    expect(document!.Properties.Content.Parameters).toBeUndefined();
    const runCommand =
      document!.Properties.Content.mainSteps[0].inputs.runCommand;
    assertShellAndEmbeddedPythonSyntax(runCommand.join("\n"));
    expect(runCommand[0]).toBe("set -eu");
    expect(runCommand[2]).toBe(
      "export PRR_RELEASE_IMAGE=prr-diagnostics-placeholder",
    );
    expect(JSON.stringify(runCommand)).not.toContain("pipefail");
    expect(JSON.stringify(runCommand)).not.toContain("gmail_inbound_status");
    // The stale threshold is an in-app setting since Phase B, so the
    // diagnostic reads the store when the running image has one and falls
    // back to Django settings only on an image that predates it.
    expect(runCommand.join("\n")).toContain(
      'runtime.get("PRR_GMAIL_INBOUND_STALE_SECONDS")',
    );
    expect(runCommand.join("\n")).toContain(
      'getattr(settings, "PRR_GMAIL_INBOUND_STALE_SECONDS", 300)',
    );
    expect(runCommand.join("\n")).toContain(
      'runtime.get("PRR_GMAIL_MAILBOX")',
    );
    // One document, not two. The Gmail activation command existed only to
    // flip PRR_GMAIL_ENABLED and PRR_GMAIL_INBOUND_POLL_ENABLED safely in an
    // operator-held runtime file; both are in-app settings now, changed by a
    // named Application Admin with a recorded reason and no redeploy. Read-only
    // diagnostics are the only fixed command the stack still publishes.
    expect(documents).toHaveLength(1);
    expect(
      documents.map((candidate) => candidate.Properties.Name),
    ).toEqual([`psd401-prr-${envName}-email-diagnostics`]);
    expect(JSON.stringify(documents)).not.toContain("enable-gmail-inbound");
    expect(JSON.stringify(documents)).not.toContain("run-reviewed-operation");
    expect(JSON.stringify(documents)).not.toContain("adopt-baseline");
  });

  test("holds one empty runtime secret that outlives the stack", () => {
    // The stack creates the container only. A generated value would replace
    // the signing key the settings store is encrypted under and silently
    // strand every stored secret, so there is no SecretString and no
    // GenerateSecretString: an operator copies the live value in.
    template.resourceCountIs("AWS::SecretsManager::Secret", 1);
    const secrets = Object.values(
      template.findResources("AWS::SecretsManager::Secret"),
    );
    expect(secrets[0].DeletionPolicy).toBe("Retain");
    expect(secrets[0].UpdateReplacePolicy).toBe("Retain");
    expect(secrets[0].Properties.Name).toBe(`psd401-prr/${envName}/runtime`);
    expect(secrets[0].Properties.SecretString).toBeUndefined();
    expect(secrets[0].Properties.GenerateSecretString).toBeUndefined();
    expect(Object.keys(template.toJSON().Outputs ?? {})).toContain(
      "RuntimeSecretArn",
    );

    // The retired Google sign-in copy stays gone.
    expect(json).not.toContain("google-oidc-client-secret");
    expect(Object.keys(template.toJSON().Outputs ?? {})).not.toContain(
      "OidcClientSecretArn",
    );
  });

  test("the host itself never touches Secrets Manager", () => {
    // The secret is read by each Django process through the instance role,
    // not rendered into a file by the deployment script.
    expect(applicationDeploymentCommand(template)).not.toContain("secretsmanager");
  });

  test("rolls back unless every stored settings secret decrypts", () => {
    // A wrong key does not crash: stored secrets read as unset and sign-in
    // quietly breaks. The gate runs after the settings import and before the
    // replacement web container, so a failure leaves the old release serving.
    const command = applicationDeploymentCommand(template);
    const gate = command.indexOf("release_manage verify_settings_secrets");
    expect(gate).toBeGreaterThan(command.indexOf("import_environment_settings"));
    expect(gate).toBeLessThan(
      command.indexOf("compose_runtime up -d --no-deps --force-recreate web"),
    );
  });
});

describe.each(environments)("psd401-prr-%s backups", (envName, config) => {
  const template = templateFor(config);

  test("snapshots this instance only, daily, for fourteen days", () => {
    template.hasResourceProperties("AWS::DLM::LifecyclePolicy", {
      State: "ENABLED",
      PolicyDetails: {
        ResourceTypes: ["INSTANCE"],
        TargetTags: [{ Key: "psd401-prr-backup", Value: envName }],
        Schedules: [
          {
            CopyTags: true,
            CreateRule: {
              Interval: 24,
              IntervalUnit: "HOURS",
              Times: ["09:00"],
            },
            RetainRule: { Count: 14 },
          },
        ],
      },
    });
  });

  test("alarms when the daily app backup fails or stops reporting", () => {
    const template = templateFor(config);
    template.hasResourceProperties("AWS::CloudWatch::Alarm", {
      ComparisonOperator: "LessThanThreshold",
      EvaluationPeriods: 1,
      MetricName: "AppBackupSuccess",
      Namespace: "PSD401/PRR",
      Period: 86400,
      Statistic: "Minimum",
      Threshold: 1,
      TreatMissingData: "breaching",
      Dimensions: [
        {
          Name: "Environment",
          Value: envName,
        },
      ],
    });
  });

  test("alarms before the root volume fills, and if reporting stops", () => {
    template.hasResourceProperties("AWS::CloudWatch::Alarm", {
      AlarmName: `psd401-prr-${envName}-root-disk-filling`,
      ComparisonOperator: "GreaterThanOrEqualToThreshold",
      MetricName: "RootDiskUsedPercent",
      Namespace: "PSD401/PRR",
      Threshold: 80,
      // A monitor that goes silent must not read as healthy.
      TreatMissingData: "breaching",
    });

    // The reporter and its timer are installed by every deployment, so the
    // units on the host cannot drift away from the alarm watching them.
    const command = applicationDeploymentCommand(template);
    expect(command).toContain("/usr/local/sbin/prr-report-disk-usage");
    expect(command).toContain(`systemctl enable --now prr-disk-usage@${envName}.timer`);
  });

  test("alarms when inbound Gmail polling fails or stops reporting", () => {
    template.resourceCountIs("AWS::CloudWatch::Alarm", 3);
    template.hasResourceProperties("AWS::CloudWatch::Alarm", {
      AlarmName: `psd401-prr-${envName}-gmail-inbound-missing-or-failed`,
      ComparisonOperator: "LessThanThreshold",
      EvaluationPeriods: 2,
      DatapointsToAlarm: 2,
      MetricName: "GmailInboundPollHealth",
      Namespace: "PSD401/PRR",
      Period: 60,
      Statistic: "Minimum",
      Threshold: 1,
      TreatMissingData: "breaching",
      Dimensions: [
        {
          Name: "Environment",
          Value: "production",
        },
      ],
    });
  });

  test("every alarm notifies techalerts@ when it fires and when it clears", () => {
    template.resourceCountIs("AWS::SNS::Topic", 1);
    const topicId = Object.keys(template.findResources("AWS::SNS::Topic"))[0];
    template.hasResourceProperties("AWS::SNS::Topic", {
      TopicName: `psd401-prr-${envName}-alarms`,
    });

    template.resourceCountIs("AWS::SNS::Subscription", 1);
    template.hasResourceProperties("AWS::SNS::Subscription", {
      Protocol: "email",
      Endpoint: "techalerts@psd401.net",
      TopicArn: { Ref: topicId },
    });

    // Publishing to the topic over plain HTTP is denied.
    template.hasResourceProperties("AWS::SNS::TopicPolicy", {
      PolicyDocument: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({
            Effect: "Deny",
            Condition: { Bool: { "aws:SecureTransport": "false" } },
          }),
        ]),
      }),
    });

    const alarms = Object.values(template.findResources("AWS::CloudWatch::Alarm"));
    expect(alarms).toHaveLength(3);
    for (const alarm of alarms) {
      expect(alarm.Properties.AlarmActions).toEqual([{ Ref: topicId }]);
      expect(alarm.Properties.OKActions).toEqual([{ Ref: topicId }]);
    }
  });

  test("uses a DLM-compatible lifecycle description", () => {
    const policies = template.findResources("AWS::DLM::LifecyclePolicy");
    const descriptions = Object.values(policies).map(
      (policy) => policy.Properties.Description,
    );

    expect(descriptions).toHaveLength(1);
    expect(descriptions[0]).toMatch(/^[0-9A-Za-z _-]+$/);
  });

  test("tags the instance for that policy and nothing else's", () => {
    template.hasResourceProperties("AWS::EC2::Instance", {
      // The district tag set comes from the app (lib/app.ts, covered in
      // test/app.test.ts); this stack adds only the classification. What
      // matters here is that exactly one DLM target tag exists, so the
      // snapshot policy cannot pick up another team's instances.
      Tags: [
        { Key: "Name", Value: `psd401-prr-${envName}` },
        { Key: "psd:data-classification", Value: "student-pii" },
        { Key: "psd401-prr-backup", Value: envName },
      ],
    });
  });
});

// The instance's SubnetId is replace-on-change, and its root volume holds
// PostgreSQL. The committed lookup cache is now the only source of that ID,
// so pin what it resolves to: an edited or reset cache that picks any other
// subnet fails here, not at deploy.
test("the committed lookup cache resolves production to its subnet", () => {
  const cached = JSON.parse(
    readFileSync(join(__dirname, "../cdk.context.json"), "utf8"),
  );
  const app = new App({ context: cached });
  const stack = new PrrHostingStack(app, "psd401-prr-prod", prodConfig);
  Template.fromStack(stack).hasResourceProperties("AWS::EC2::Instance", {
    SubnetId: "subnet-02ccab0368061c253",
    AvailabilityZone: "us-west-2a",
  });
});

// The subnet handover finished on 2026-11-03 (README, "Moving to
// psd-aws-infra"). Its transitional "detached" template dropped the instance
// from the stack; a deploy of it now, with no import to follow, would leave
// the host unmanaged. The context that selected it must do nothing.
test("the retired subnetHandover context changes nothing", () => {
  const stack = stackFor(prodConfig, undefined, {
    subnetHandover: "detached",
  });
  expect(Template.fromStack(stack).toJSON()).toEqual(
    templateFor(prodConfig).toJSON(),
  );
});
