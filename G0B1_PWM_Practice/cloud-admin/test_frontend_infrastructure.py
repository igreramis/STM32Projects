"""Offline security invariants for static hosting, separate from the backend."""
from pathlib import Path
import unittest

from test_infrastructure import references, yaml


@unittest.skipIf(yaml is None, 'PyYAML is required to validate infrastructure')
class FrontendInfrastructureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.template = yaml.safe_load(Path(__file__).with_name('frontend.yaml').read_text())
        cls.resources = cls.template['Resources']

    def props(self, name):
        return self.resources[name]['Properties']

    def test_bucket_private_retained_versioned_and_encrypted(self):
        bucket = self.props('AssetsBucket')
        self.assertEqual(bucket['PublicAccessBlockConfiguration'], dict(
            BlockPublicAcls=True, IgnorePublicAcls=True, BlockPublicPolicy=True, RestrictPublicBuckets=True))
        self.assertEqual(bucket['OwnershipControls']['Rules'], [{'ObjectOwnership': 'BucketOwnerEnforced'}])
        self.assertEqual(bucket['VersioningConfiguration']['Status'], 'Enabled')
        self.assertEqual(bucket['BucketEncryption']['ServerSideEncryptionConfiguration'][0]
                         ['ServerSideEncryptionByDefault']['SSEAlgorithm'], 'AES256')
        self.assertNotIn('WebsiteConfiguration', bucket)
        self.assertEqual(self.resources['AssetsBucket']['DeletionPolicy'], 'Retain')
        self.assertEqual(self.resources['AssetsBucket']['UpdateReplacePolicy'], 'Retain')

    def test_only_this_distribution_can_read_origin(self):
        control = self.props('OriginAccessControl')['OriginAccessControlConfig']
        self.assertEqual(control['SigningBehavior'], 'always')
        self.assertEqual(control['SigningProtocol'], 'sigv4')
        self.assertEqual(control['OriginAccessControlOriginType'], 's3')
        statements = self.props('AssetsBucketPolicy')['PolicyDocument']['Statement']
        allows = [s for s in statements if s['Effect'] == 'Allow']
        self.assertEqual(len(allows), 1)
        self.assertEqual(allows[0]['Principal'], {'Service': 'cloudfront.amazonaws.com'})
        self.assertEqual(allows[0]['Action'], 's3:GetObject')
        self.assertEqual(allows[0]['Condition']['StringEquals']['AWS:SourceArn'], {
            'Fn::Sub': 'arn:${AWS::Partition}:cloudfront::${AWS::AccountId}:distribution/${Distribution}'})
        self.assertTrue(any(s['Effect'] == 'Deny' and s['Condition'] == {
            'Bool': {'aws:SecureTransport': 'false'}} for s in statements))

    def test_static_https_only_no_credentials_or_callback_forwarding(self):
        config = self.props('Distribution')['DistributionConfig']
        self.assertEqual(config['DefaultRootObject'], 'index.html')
        self.assertNotIn('Logging', config)
        self.assertEqual(config['Origins'][0]['OriginAccessControlId'], {'Ref': 'OriginAccessControl'})
        self.assertEqual(config['Origins'][0]['DomainName'], {'Fn::GetAtt': ['AssetsBucket', 'RegionalDomainName']})
        self.assertEqual(config['CacheBehaviors'][0]['PathPattern'], '/assets/*')
        for behavior in [config['DefaultCacheBehavior']] + config['CacheBehaviors']:
            self.assertEqual(behavior['AllowedMethods'], ['GET', 'HEAD'])
            self.assertEqual(behavior['ViewerProtocolPolicy'], 'redirect-to-https')
            self.assertEqual(behavior['ResponseHeadersPolicyId'], {'Ref': 'SecurityHeaders'})
            self.assertNotIn('OriginRequestPolicyId', behavior)
        for name in ('NoCachePolicy', 'HashedAssetsPolicy'):
            policy = self.props(name)['CachePolicyConfig']['ParametersInCacheKeyAndForwardedToOrigin']
            self.assertEqual(policy['CookiesConfig']['CookieBehavior'], 'none')
            self.assertEqual(policy['HeadersConfig']['HeaderBehavior'], 'none')
            self.assertEqual(policy['QueryStringsConfig']['QueryStringBehavior'], 'none')
        for error in config['CustomErrorResponses']:
            self.assertNotIn('ResponsePagePath', error)
            self.assertEqual(error['ErrorCachingMinTTL'], 0)

    def test_runtime_and_html_never_edge_cached(self):
        policy = self.props('NoCachePolicy')['CachePolicyConfig']
        for ttl in ('MinTTL', 'MaxTTL', 'DefaultTTL'):
            self.assertEqual(policy[ttl], 0)
        self.assertEqual(self.props('Distribution')['DistributionConfig']['DefaultCacheBehavior']
                         ['CachePolicyId'], {'Ref': 'NoCachePolicy'})

    def test_security_headers_and_exact_connect_destinations(self):
        headers = self.props('SecurityHeaders')['ResponseHeadersPolicyConfig']['SecurityHeadersConfig']
        csp = headers['ContentSecurityPolicy']['ContentSecurityPolicy']['Fn::Sub']
        self.assertIn("connect-src 'self' ${ApiOrigin} ${CognitoOrigin};", csp)
        self.assertIn("script-src 'self';", csp)
        self.assertNotIn('unsafe-eval', csp)
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertIn("base-uri 'none'", csp)
        self.assertIn("form-action 'none'", csp)
        self.assertNotIn('*', csp)
        self.assertEqual(headers['FrameOptions']['FrameOption'], 'DENY')
        self.assertEqual(headers['ReferrerPolicy']['ReferrerPolicy'], 'no-referrer')
        self.assertEqual(headers['StrictTransportSecurity']['AccessControlMaxAgeSec'], 31536000)
        self.assertTrue(all(value['Override'] for value in headers.values()))

    def test_no_new_backend_permissions_and_references_resolve(self):
        allowed = {'AWS::S3::Bucket', 'AWS::S3::BucketPolicy', 'AWS::CloudFront::Distribution',
                   'AWS::CloudFront::OriginAccessControl', 'AWS::CloudFront::CachePolicy',
                   'AWS::CloudFront::ResponseHeadersPolicy'}
        self.assertTrue({r['Type'] for r in self.resources.values()} <= allowed)
        known = set(self.resources) | set(self.template['Parameters'])
        self.assertTrue(all(ref in known or ref.startswith('AWS::') for ref in references(self.template)))


if __name__ == '__main__':
    unittest.main()