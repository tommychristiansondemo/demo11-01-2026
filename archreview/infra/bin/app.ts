#!/usr/bin/env node
import * as cdk from 'aws-cdk-lib';
import { EdgeStack } from '../lib/edge-stack';
import { AppStack } from '../lib/app-stack';

const app = new cdk.App();
const account = process.env.CDK_DEFAULT_ACCOUNT;
const ctx = (k: string, d: string) => (app.node.tryGetContext(k) as string) ?? d;

const zoneName = ctx('zoneName', 'awsteach.com');
const zoneId = ctx('hostedZoneId', 'Z02882605B7GUS0L3VHX');
const recordName = ctx('recordName', 'archreview');
const siteDomain = `${recordName}.${zoneName}`;
const region = ctx('region', 'us-west-2');
const modelIds = ctx('modelIds', '["us.anthropic.claude-sonnet-4-6","us.anthropic.claude-haiku-4-5-20251001-v1:0"]');
const alarmEmail = ctx('alarmEmail', '');

const edge = new EdgeStack(app, 'ArchRevEdge', {
  env: { account, region: 'us-east-1' },
  crossRegionReferences: true,
  siteDomain, zoneName, zoneId,
  description: 'Enterprise Solution Review Assistant (demo): ACM certificate and CloudFront WAF (us-east-1)',
});

const main = new AppStack(app, 'ArchRevApp', {
  env: { account, region },
  crossRegionReferences: true,
  siteDomain, zoneName, zoneId, modelIds, alarmEmail,
  certificate: edge.certificate,
  cloudFrontWebAclArn: edge.webAclArn,
  description: 'Enterprise Solution Review Assistant (demo): web, API, agent, and two AgentCore MCP runtimes',
});
main.addDependency(edge);

cdk.Tags.of(app).add('Application', 'EnterpriseSolutionReviewAssistant');
cdk.Tags.of(app).add('Environment', 'demo');
