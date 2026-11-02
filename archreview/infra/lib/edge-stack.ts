import * as cdk from 'aws-cdk-lib';
import * as acm from 'aws-cdk-lib/aws-certificatemanager';
import * as route53 from 'aws-cdk-lib/aws-route53';
import * as wafv2 from 'aws-cdk-lib/aws-wafv2';
import { Construct } from 'constructs';

export interface EdgeStackProps extends cdk.StackProps {
  siteDomain: string;
  zoneName: string;
  zoneId: string;
}

/** Resources CloudFront requires in us-east-1: the ACM certificate and a CLOUDFRONT-scope WAF web ACL. */
export class EdgeStack extends cdk.Stack {
  readonly certificate: acm.ICertificate;
  readonly webAclArn: string;

  constructor(scope: Construct, id: string, props: EdgeStackProps) {
    super(scope, id, props);
    const zone = route53.HostedZone.fromHostedZoneAttributes(this, 'Zone', {
      hostedZoneId: props.zoneId, zoneName: props.zoneName,
    });

    this.certificate = new acm.Certificate(this, 'SiteCert', {
      domainName: props.siteDomain,
      validation: acm.CertificateValidation.fromDns(zone),
    });

    const acl = new wafv2.CfnWebACL(this, 'CloudFrontAcl', {
      name: 'archrev-cloudfront-acl',
      scope: 'CLOUDFRONT',
      defaultAction: { allow: {} },
      visibilityConfig: { cloudWatchMetricsEnabled: true, metricName: 'archrev-cf', sampledRequestsEnabled: true },
      rules: [
        {
          name: 'RateLimitPerIp', priority: 0, action: { block: {} },
          statement: { rateBasedStatement: { limit: 1000, aggregateKeyType: 'IP', evaluationWindowSec: 300 } },
          visibilityConfig: { cloudWatchMetricsEnabled: true, metricName: 'archrev-cf-rate', sampledRequestsEnabled: true },
        },
        {
          name: 'AWSCommon', priority: 1, overrideAction: { none: {} },
          statement: { managedRuleGroupStatement: { vendorName: 'AWS', name: 'AWSManagedRulesCommonRuleSet' } },
          visibilityConfig: { cloudWatchMetricsEnabled: true, metricName: 'archrev-cf-common', sampledRequestsEnabled: true },
        },
      ],
    });
    this.webAclArn = acl.attrArn;
    new cdk.CfnOutput(this, 'CertificateArn', { value: this.certificate.certificateArn });
    new cdk.CfnOutput(this, 'CloudFrontWebAclArn', { value: acl.attrArn });
  }
}
