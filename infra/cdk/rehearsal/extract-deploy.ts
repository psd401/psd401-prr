// ABOUTME: Prints the synthesized application-deployment shell script so the
// ABOUTME: rehearsal harness exercises exactly what CDK would send to SSM.
import { App } from "aws-cdk-lib";
import { Template } from "aws-cdk-lib/assertions";

import { prodConfig } from "../lib/config";
import { PrrHostingStack } from "../lib/prr-hosting-stack";

const app = new App();
const stack = new PrrHostingStack(
  app,
  `psd401-prr-${prodConfig.envName}`,
  prodConfig,
);
const template = Template.fromStack(stack);
const association = Object.values(
  template.findResources("AWS::SSM::Association"),
).find(
  (candidate) =>
    (candidate as { Properties: { AssociationName?: string } }).Properties
      .AssociationName === `psd401-prr-${prodConfig.envName}-application`,
) as { Properties: { Parameters: { commands: string[] } } } | undefined;
if (association === undefined) {
  throw new Error("application deployment association not found");
}
process.stdout.write(association.Properties.Parameters.commands.join("\n"));
