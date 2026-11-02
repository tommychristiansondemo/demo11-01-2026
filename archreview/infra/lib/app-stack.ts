import * as path from 'path';
import * as cdk from 'aws-cdk-lib';
import * as acm from 'aws-cdk-lib/aws-certificatemanager';
import * as agentcore from 'aws-cdk-lib/aws-bedrockagentcore';
import * as apigw from 'aws-cdk-lib/aws-apigateway';
import * as cloudfront from 'aws-cdk-lib/aws-cloudfront';
import * as origins from 'aws-cdk-lib/aws-cloudfront-origins';
import * as cw from 'aws-cdk-lib/aws-cloudwatch';
import * as cwActions from 'aws-cdk-lib/aws-cloudwatch-actions';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as route53 from 'aws-cdk-lib/aws-route53';
import * as targets from 'aws-cdk-lib/aws-route53-targets';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as s3assets from 'aws-cdk-lib/aws-s3-assets';
import * as s3deploy from 'aws-cdk-lib/aws-s3-deployment';
import * as sns from 'aws-cdk-lib/aws-sns';
import * as subs from 'aws-cdk-lib/aws-sns-subscriptions';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import * as wafv2 from 'aws-cdk-lib/aws-wafv2';
import { Construct } from 'constructs';

export interface AppStackProps extends cdk.StackProps {
  siteDomain: string;
  zoneName: string;
  zoneId: string;
  modelIds: string;
  alarmEmail: string;
  certificate: acm.ICertificate;
  cloudFrontWebAclArn: string;
}

const ROOT = path.join(__dirname, '..', '..');
const PARAM_PREFIX = '/archrev';
const CORRELATION_HEADER = 'X-Amzn-Bedrock-AgentCore-Runtime-Custom-Correlation-Id';
const NS = 'ArchReview';

export class AppStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: AppStackProps) {
    super(scope, id, props);
    const siteOrigin = `https://${props.siteDomain}`;

    // ---------------- MCP servers on AgentCore Runtime (two independent runtimes) ----------------
    const runtimes: Record<string, agentcore.CfnRuntime> = {};
    for (const [key, name, desc] of [
      ['standards', 'archrev_standards_mcp', 'Enterprise Standards MCP server (synthetic data)'],
      ['catalog', 'archrev_catalog_mcp', 'Approved Service Catalog MCP server (synthetic data)'],
    ] as const) {
      const code = new s3assets.Asset(this, `${key}McpCode`, { path: path.join(ROOT, 'build', `mcp-${key}`) });
      const role = new iam.Role(this, `${key}McpRole`, {
        description: `AgentCore Runtime execution role for ${name}`,
        assumedBy: new iam.ServicePrincipal('bedrock-agentcore.amazonaws.com', {
          conditions: {
            StringEquals: { 'aws:SourceAccount': this.account },
            ArnLike: { 'aws:SourceArn': `arn:aws:bedrock-agentcore:${this.region}:${this.account}:*` },
          },
        }),
      });
      const policy = new iam.Policy(this, `${key}McpPolicy`, {
        statements: [
          new iam.PolicyStatement({
            actions: ['logs:CreateLogGroup', 'logs:CreateLogStream', 'logs:PutLogEvents', 'logs:DescribeLogStreams'],
            resources: [`arn:aws:logs:${this.region}:${this.account}:log-group:/aws/bedrock-agentcore/runtimes/*`],
          }),
          new iam.PolicyStatement({ actions: ['logs:DescribeLogGroups'], resources: [`arn:aws:logs:${this.region}:${this.account}:log-group:*`] }),
          new iam.PolicyStatement({
            actions: ['xray:PutTraceSegments', 'xray:PutTelemetryRecords', 'xray:GetSamplingRules', 'xray:GetSamplingTargets'],
            resources: ['*'],
          }),
          new iam.PolicyStatement({
            actions: ['cloudwatch:PutMetricData'], resources: ['*'],
            conditions: { StringEquals: { 'cloudwatch:namespace': 'bedrock-agentcore' } },
          }),
          new iam.PolicyStatement({
            actions: ['s3:GetObject'],
            resources: [code.bucket.arnForObjects(code.s3ObjectKey)],
          }),
        ],
      });
      policy.attachToRole(role);
      code.grantRead(role);
      const rt = new agentcore.CfnRuntime(this, `${key}Runtime`, {
        agentRuntimeName: name,
        description: desc,
        roleArn: role.roleArn,
        protocolConfiguration: 'MCP',
        networkConfiguration: { networkMode: 'PUBLIC' },
        agentRuntimeArtifact: {
          codeConfiguration: {
            code: { s3: { bucket: code.s3BucketName, prefix: code.s3ObjectKey } },
            runtime: 'PYTHON_3_13',
            entryPoint: ['main.py'],
          },
        },
        requestHeaderConfiguration: { requestHeaderAllowlist: [CORRELATION_HEADER] },
        lifecycleConfiguration: { idleRuntimeSessionTimeout: 300, maxLifetime: 1800 },
        environmentVariables: { PYTHONUNBUFFERED: '1', DATASET: 'synthetic-demo' },
        tags: { Application: 'EnterpriseSolutionReviewAssistant', Environment: 'demo' },
      });
      rt.node.addDependency(policy);
      rt.node.addDependency(role);
      runtimes[key] = rt;
      new ssm.StringParameter(this, `${key}RuntimeArnParam`, {
        parameterName: `${PARAM_PREFIX}/runtimes/${key}`, stringValue: rt.attrAgentRuntimeArn,
      });
      new cdk.CfnOutput(this, `${key}RuntimeArn`, { value: rt.attrAgentRuntimeArn });
    }

    // ---------------- Configuration ----------------
    new ssm.StringParameter(this, 'ModelIdsParam', {
      parameterName: `${PARAM_PREFIX}/model-ids`,
      description: 'Ordered list of Bedrock model/inference-profile IDs verified invocable at deploy time',
      stringValue: props.modelIds,
    });

    // ---------------- Data ----------------
    const table = new dynamodb.Table(this, 'Reviews', {
      partitionKey: { name: 'review_id', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      timeToLiveAttribute: 'ttl',
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });
    table.addGlobalSecondaryIndex({
      indexName: 'owner-index',
      partitionKey: { name: 'owner', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'created_at', type: dynamodb.AttributeType.NUMBER },
      projectionType: dynamodb.ProjectionType.INCLUDE,
      nonKeyAttributes: ['status', 'title', 'correlation_id', 'completed_at', 'error'],
    });
    table.addGlobalSecondaryIndex({
      indexName: 'all-index',
      partitionKey: { name: 'entity', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'created_at', type: dynamodb.AttributeType.NUMBER },
      projectionType: dynamodb.ProjectionType.INCLUDE,
      nonKeyAttributes: ['status', 'title', 'correlation_id', 'completed_at', 'owner', 'error'],
    });

    // ---------------- Identity (self-registration via API; 6-char simple passwords; no email) ----------------
    const pool = new cognito.UserPool(this, 'Users', {
      userPoolName: 'archrev-users',
      selfSignUpEnabled: false, // registration goes through the API so the role group is assigned atomically
      signInAliases: { username: true },
      signInCaseSensitive: false,
      passwordPolicy: {
        minLength: 6, requireLowercase: false, requireUppercase: false, requireDigits: false, requireSymbols: false,
        tempPasswordValidity: cdk.Duration.days(1),
      },
      accountRecovery: cognito.AccountRecovery.NONE,
      mfa: cognito.Mfa.OFF,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });
    for (const [g, d] of [['architect', 'Solution architects: submit reviews and see their own'],
      ['governance', 'Governance reviewers: submit reviews and see all reviews']]) {
      new cognito.CfnUserPoolGroup(this, `Group-${g}`, { userPoolId: pool.userPoolId, groupName: g, description: d });
    }
    const client = pool.addClient('WebClient', {
      authFlows: { userPassword: true },
      generateSecret: false,
      preventUserExistenceErrors: true,
      idTokenValidity: cdk.Duration.hours(4),
      accessTokenValidity: cdk.Duration.hours(4),
      refreshTokenValidity: cdk.Duration.days(1),
    });

    // ---------------- Lambdas ----------------
    const code = lambda.Code.fromAsset(path.join(ROOT, 'build', 'lambda'));
    const common = {
      runtime: lambda.Runtime.PYTHON_3_13,
      architecture: lambda.Architecture.ARM_64,
      code,
      tracing: lambda.Tracing.ACTIVE,
      environment: {
        TABLE_NAME: table.tableName, PARAM_PREFIX, METRICS_NAMESPACE: NS, LOG_PROPOSAL_TEXT: 'false',
      },
    };
    const workerLogs = new logs.LogGroup(this, 'WorkerLogs', {
      retention: logs.RetentionDays.ONE_MONTH, removalPolicy: cdk.RemovalPolicy.DESTROY,
    });
    const worker = new lambda.Function(this, 'Worker', {
      ...common,
      description: 'Review agent: orchestrates Bedrock + both AgentCore MCP servers',
      handler: 'archrev.worker.handler',
      timeout: cdk.Duration.seconds(300),
      memorySize: 1024,
      reservedConcurrentExecutions: 10,
      retryAttempts: 0,
      logGroup: workerLogs,
      environment: {
        ...common.environment, SERVICE_NAME: 'archrev-worker',
        STANDARDS_RUNTIME_ARN: runtimes.standards.attrAgentRuntimeArn,
        CATALOG_RUNTIME_ARN: runtimes.catalog.attrAgentRuntimeArn,
      },
    });
    table.grantReadWriteData(worker);
    worker.addToRolePolicy(new iam.PolicyStatement({
      actions: ['bedrock-agentcore:InvokeAgentRuntime'],
      resources: Object.values(runtimes).flatMap(r => [r.attrAgentRuntimeArn, `${r.attrAgentRuntimeArn}/*`]),
    }));
    worker.addToRolePolicy(new iam.PolicyStatement({
      actions: ['bedrock:InvokeModel', 'bedrock:InvokeModelWithResponseStream'],
      resources: [
        'arn:aws:bedrock:*::foundation-model/anthropic.*',
        'arn:aws:bedrock:*::foundation-model/amazon.nova-*',
        `arn:aws:bedrock:${this.region}:${this.account}:inference-profile/us.*`,
        `arn:aws:bedrock:${this.region}:${this.account}:inference-profile/global.*`,
      ],
    }));
    worker.addToRolePolicy(new iam.PolicyStatement({
      actions: ['ssm:GetParametersByPath', 'ssm:GetParameter'],
      resources: [`arn:aws:ssm:${this.region}:${this.account}:parameter${PARAM_PREFIX}`,
        `arn:aws:ssm:${this.region}:${this.account}:parameter${PARAM_PREFIX}/*`],
    }));

    const apiLogs = new logs.LogGroup(this, 'ApiFnLogs', {
      retention: logs.RetentionDays.ONE_MONTH, removalPolicy: cdk.RemovalPolicy.DESTROY,
    });
    const apiFn = new lambda.Function(this, 'ApiFn', {
      ...common,
      description: 'Public REST API for the review assistant',
      handler: 'archrev.api.handler',
      timeout: cdk.Duration.seconds(15),
      memorySize: 512,
      logGroup: apiLogs,
      environment: {
        ...common.environment, SERVICE_NAME: 'archrev-api', ALLOWED_ORIGIN: siteOrigin,
        USER_POOL_ID: pool.userPoolId, USER_POOL_CLIENT_ID: client.userPoolClientId,
        WORKER_FUNCTION_NAME: worker.functionName, REVIEWS_PER_USER_PER_HOUR: '10', REVIEW_RETENTION_DAYS: '7',
      },
    });
    table.grantReadWriteData(apiFn);
    worker.grantInvoke(apiFn);
    apiFn.addToRolePolicy(new iam.PolicyStatement({
      actions: ['cognito-idp:AdminCreateUser', 'cognito-idp:AdminSetUserPassword', 'cognito-idp:AdminAddUserToGroup',
        'cognito-idp:AdminDeleteUser', 'cognito-idp:AdminListGroupsForUser'],
      resources: [pool.userPoolArn],
    }));

    // ---------------- API Gateway (REST) ----------------
    const accessLogs = new logs.LogGroup(this, 'ApiAccessLogs', {
      retention: logs.RetentionDays.ONE_MONTH, removalPolicy: cdk.RemovalPolicy.DESTROY,
    });
    const api = new apigw.RestApi(this, 'Api', {
      restApiName: 'archrev-api',
      description: 'Enterprise Solution Review Assistant API (demo)',
      endpointTypes: [apigw.EndpointType.REGIONAL],
      cloudWatchRole: true,
      cloudWatchRoleRemovalPolicy: cdk.RemovalPolicy.DESTROY,
      deployOptions: {
        stageName: 'v1',
        tracingEnabled: true,
        metricsEnabled: true,
        loggingLevel: apigw.MethodLoggingLevel.ERROR,
        dataTraceEnabled: false,
        throttlingRateLimit: 20,
        throttlingBurstLimit: 40,
        accessLogDestination: new apigw.LogGroupLogDestination(accessLogs),
        accessLogFormat: apigw.AccessLogFormat.jsonWithStandardFields({
          caller: false, httpMethod: true, ip: true, protocol: true, requestTime: true, resourcePath: true,
          responseLength: true, status: true, user: false,
        }),
      },
      defaultCorsPreflightOptions: {
        allowOrigins: [siteOrigin],
        allowMethods: ['GET', 'POST', 'OPTIONS'],
        allowHeaders: ['Content-Type', 'Authorization', 'X-Correlation-Id'],
        maxAge: cdk.Duration.hours(1),
      },
    });
    for (const [rid, type] of [['Default4xx', apigw.ResponseType.DEFAULT_4XX], ['Default5xx', apigw.ResponseType.DEFAULT_5XX]] as const) {
      api.addGatewayResponse(rid, {
        type,
        responseHeaders: { 'Access-Control-Allow-Origin': `'${siteOrigin}'`, 'Vary': "'Origin'" },
        templates: { 'application/json': '{"error":{"code":"$context.error.responseType","message":$context.error.messageString}}' },
      });
    }
    const authorizer = new apigw.CognitoUserPoolsAuthorizer(this, 'Authorizer', { cognitoUserPools: [pool] });
    const integ = new apigw.LambdaIntegration(apiFn, { proxy: true });
    const open = { authorizationType: apigw.AuthorizationType.NONE };
    const authd = { authorizer, authorizationType: apigw.AuthorizationType.COGNITO };
    api.root.addResource('health').addMethod('GET', integ, open);
    const auth = api.root.addResource('auth');
    auth.addResource('register').addMethod('POST', integ, open);
    auth.addResource('login').addMethod('POST', integ, open);
    api.root.addResource('me').addMethod('GET', integ, authd);
    const reviews = api.root.addResource('reviews');
    reviews.addMethod('POST', integ, authd);
    reviews.addMethod('GET', integ, authd);
    reviews.addResource('{id}').addMethod('GET', integ, authd);

    // Regional WAF on the API stage (rate limiting + managed rules + tighter auth limits)
    const apiAcl = new wafv2.CfnWebACL(this, 'ApiAcl', {
      name: 'archrev-api-acl',
      scope: 'REGIONAL',
      defaultAction: { allow: {} },
      visibilityConfig: { cloudWatchMetricsEnabled: true, metricName: 'archrev-api', sampledRequestsEnabled: true },
      rules: [
        {
          name: 'RateLimitPerIp', priority: 0, action: { block: {} },
          statement: { rateBasedStatement: { limit: 300, aggregateKeyType: 'IP', evaluationWindowSec: 300 } },
          visibilityConfig: { cloudWatchMetricsEnabled: true, metricName: 'archrev-api-rate', sampledRequestsEnabled: true },
        },
        {
          name: 'AuthRateLimitPerIp', priority: 1, action: { block: {} },
          statement: {
            rateBasedStatement: {
              limit: 100, aggregateKeyType: 'IP', evaluationWindowSec: 300,
              scopeDownStatement: {
                byteMatchStatement: {
                  fieldToMatch: { uriPath: {} }, positionalConstraint: 'CONTAINS', searchString: '/auth/',
                  textTransformations: [{ priority: 0, type: 'LOWERCASE' }],
                },
              },
            },
          },
          visibilityConfig: { cloudWatchMetricsEnabled: true, metricName: 'archrev-api-auth-rate', sampledRequestsEnabled: true },
        },
        {
          name: 'AWSCommon', priority: 2, overrideAction: { none: {} },
          statement: { managedRuleGroupStatement: { vendorName: 'AWS', name: 'AWSManagedRulesCommonRuleSet' } },
          visibilityConfig: { cloudWatchMetricsEnabled: true, metricName: 'archrev-api-common', sampledRequestsEnabled: true },
        },
      ],
    });
    new wafv2.CfnWebACLAssociation(this, 'ApiAclAssoc', {
      resourceArn: api.deploymentStage.stageArn, webAclArn: apiAcl.attrArn,
    });

    // ---------------- Front end: S3 + CloudFront + Route 53 ----------------
    const siteBucket = new s3.Bucket(this, 'Site', {
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
    });
    const apiOrigin = `https://${api.restApiId}.execute-api.${this.region}.amazonaws.com`;
    const headers = new cloudfront.ResponseHeadersPolicy(this, 'SecurityHeaders', {
      responseHeadersPolicyName: 'archrev-security-headers',
      securityHeadersBehavior: {
        contentSecurityPolicy: {
          override: true,
          contentSecurityPolicy: [
            "default-src 'self'", `connect-src 'self' ${apiOrigin}`, "script-src 'self'", "style-src 'self'",
            "img-src 'self' data:", "font-src 'self'", "object-src 'none'", "base-uri 'none'", "frame-ancestors 'none'",
            "form-action 'self'",
          ].join('; '),
        },
        strictTransportSecurity: { accessControlMaxAge: cdk.Duration.days(365), includeSubdomains: false, override: true },
        contentTypeOptions: { override: true },
        frameOptions: { frameOption: cloudfront.HeadersFrameOption.DENY, override: true },
        referrerPolicy: { referrerPolicy: cloudfront.HeadersReferrerPolicy.STRICT_ORIGIN_WHEN_CROSS_ORIGIN, override: true },
      },
    });
    const dist = new cloudfront.Distribution(this, 'Cdn', {
      comment: 'Enterprise Solution Review Assistant (demo)',
      defaultRootObject: 'index.html',
      domainNames: [props.siteDomain],
      certificate: props.certificate,
      minimumProtocolVersion: cloudfront.SecurityPolicyProtocol.TLS_V1_2_2021,
      priceClass: cloudfront.PriceClass.PRICE_CLASS_100,
      webAclId: props.cloudFrontWebAclArn,
      httpVersion: cloudfront.HttpVersion.HTTP2_AND_3,
      defaultBehavior: {
        origin: origins.S3BucketOrigin.withOriginAccessControl(siteBucket),
        viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        responseHeadersPolicy: headers,
        cachePolicy: cloudfront.CachePolicy.CACHING_OPTIMIZED,
      },
    });
    new s3deploy.BucketDeployment(this, 'DeploySite', {
      destinationBucket: siteBucket,
      sources: [
        s3deploy.Source.asset(path.join(ROOT, 'frontend')),
        s3deploy.Source.asset(path.join(ROOT, 'docs', 'download')),
        s3deploy.Source.jsonData('config.json', {
          apiUrl: api.url.replace(/\/$/, ''),
          region: this.region,
          siteDomain: props.siteDomain,
        }),
      ],
      distribution: dist,
      distributionPaths: ['/*'],
      memoryLimit: 512,
    });
    const zone = route53.HostedZone.fromHostedZoneAttributes(this, 'Zone', {
      hostedZoneId: props.zoneId, zoneName: props.zoneName,
    });
    const recordName = props.siteDomain.replace(`.${props.zoneName}`, '');
    new route53.ARecord(this, 'SiteA', { zone, recordName, target: route53.RecordTarget.fromAlias(new targets.CloudFrontTarget(dist)) });
    new route53.AaaaRecord(this, 'SiteAAAA', { zone, recordName, target: route53.RecordTarget.fromAlias(new targets.CloudFrontTarget(dist)) });

    // ---------------- Observability: dashboard + alarms ----------------
    const topic = new sns.Topic(this, 'Alarms', { displayName: 'archrev alarms' });
    if (props.alarmEmail) topic.addSubscription(new subs.EmailSubscription(props.alarmEmail));
    const alarmAction = new cwActions.SnsAction(topic);
    const m = (name: string, dims?: Record<string, string>, stat = 'Sum') =>
      new cw.Metric({ namespace: NS, metricName: name, dimensionsMap: dims, statistic: stat, period: cdk.Duration.minutes(5) });

    const alarms = [
      new cw.Alarm(this, 'WorkerErrors', {
        alarmDescription: 'Review worker Lambda errors', metric: worker.metricErrors({ period: cdk.Duration.minutes(5) }),
        threshold: 1, evaluationPeriods: 1, treatMissingData: cw.TreatMissingData.NOT_BREACHING,
      }),
      new cw.Alarm(this, 'ReviewsFailed', {
        alarmDescription: 'Reviews ending in failed status', metric: m('ReviewsFailed'),
        threshold: 1, evaluationPeriods: 1, treatMissingData: cw.TreatMissingData.NOT_BREACHING,
      }),
      new cw.Alarm(this, 'Api5xx', {
        alarmDescription: 'API 5xx responses', metric: api.metricServerError({ period: cdk.Duration.minutes(5) }),
        threshold: 5, evaluationPeriods: 1, treatMissingData: cw.TreatMissingData.NOT_BREACHING,
      }),
      new cw.Alarm(this, 'ApiLatencyP95', {
        alarmDescription: 'API p95 latency above 5 s', metric: api.metricLatency({ period: cdk.Duration.minutes(5), statistic: 'p95' }),
        threshold: 5000, evaluationPeriods: 2, treatMissingData: cw.TreatMissingData.NOT_BREACHING,
      }),
      ...['enterprise-standards', 'approved-service-catalog'].map((srv, i) => new cw.Alarm(this, `McpErrors${i}`, {
        alarmDescription: `MCP call failures to ${srv}`, metric: m('McpCallErrors', { Server: srv }),
        threshold: 3, evaluationPeriods: 1, treatMissingData: cw.TreatMissingData.NOT_BREACHING,
      })),
    ];
    alarms.forEach(a => a.addAlarmAction(alarmAction));

    const dash = new cw.Dashboard(this, 'Dashboard', { dashboardName: 'archrev-demo' });
    dash.addWidgets(
      new cw.TextWidget({ markdown: '# Enterprise Solution Review Assistant (synthetic demo)\nAPI, agent, and MCP health. Correlation IDs link API, worker, and MCP runtime logs.', width: 24, height: 2 }),
      new cw.GraphWidget({ title: 'Reviews', left: [m('ReviewsSubmitted'), m('ReviewsCompleted', { Status: 'complete' }), m('ReviewsCompleted', { Status: 'partial' }), m('ReviewsFailed'), m('ReviewsUsingBothServers')], width: 12 }),
      new cw.GraphWidget({ title: 'Review duration (ms)', left: [m('ReviewDuration', undefined, 'Average'), m('ReviewDuration', undefined, 'p90')], width: 12 }),
      new cw.GraphWidget({ title: 'MCP calls by server', left: ['enterprise-standards', 'approved-service-catalog'].map(s => m('McpCalls', { Server: s })), right: ['enterprise-standards', 'approved-service-catalog'].map(s => m('McpCallErrors', { Server: s })), width: 12 }),
      new cw.GraphWidget({ title: 'MCP latency p90 (ms)', left: ['enterprise-standards', 'approved-service-catalog'].map(s => m('McpCallLatency', { Server: s }, 'p90')), width: 12 }),
      new cw.GraphWidget({ title: 'API requests / errors', left: [api.metricCount(), api.metricClientError(), api.metricServerError()], width: 12 }),
      new cw.GraphWidget({ title: 'Lambda errors & throttles', left: [worker.metricErrors(), apiFn.metricErrors(), worker.metricThrottles()], width: 12 }),
      new cw.AlarmStatusWidget({ title: 'Alarms', alarms, width: 24 }),
      new cw.LogQueryWidget({
        title: 'Recent errors (worker + API)', width: 24,
        logGroupNames: [workerLogs.logGroupName, apiLogs.logGroupName],
        queryLines: ['fields @timestamp, event, correlation_id, error, detail', 'filter level = "ERROR" or level = "WARN"', 'sort @timestamp desc', 'limit 50'],
      }),
    );

    // ---------------- Outputs ----------------
    new cdk.CfnOutput(this, 'SiteUrl', { value: siteOrigin });
    new cdk.CfnOutput(this, 'CloudFrontDomain', { value: dist.distributionDomainName });
    new cdk.CfnOutput(this, 'CloudFrontDistributionId', { value: dist.distributionId });
    new cdk.CfnOutput(this, 'ApiUrl', { value: api.url });
    new cdk.CfnOutput(this, 'UserPoolId', { value: pool.userPoolId });
    new cdk.CfnOutput(this, 'UserPoolClientId', { value: client.userPoolClientId });
    new cdk.CfnOutput(this, 'TableName', { value: table.tableName });
    new cdk.CfnOutput(this, 'WorkerFunctionName', { value: worker.functionName });
    new cdk.CfnOutput(this, 'ApiFunctionName', { value: apiFn.functionName });
    new cdk.CfnOutput(this, 'DashboardName', { value: 'archrev-demo' });
    new cdk.CfnOutput(this, 'AlarmTopicArn', { value: topic.topicArn });
  }
}
