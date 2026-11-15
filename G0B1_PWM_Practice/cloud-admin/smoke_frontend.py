"""Read-only HTTPS hosting checks. No end-user tokens, upload or deployment requests."""
import hashlib
import json
from pathlib import Path
import re
import urllib.error
import urllib.request

from publish_frontend import ROOT, runtime_config, stack_outputs
from smoke_test import aws


def response(url):
    try:
        return urllib.request.urlopen(url, timeout=30)
    except urllib.error.HTTPError as error:
        return error


def check_headers(headers, config):
    assert headers['X-Content-Type-Options'] == 'nosniff'
    assert headers['X-Frame-Options'] == 'DENY'
    assert headers['Referrer-Policy'] == 'no-referrer'
    assert 'max-age=31536000' in headers['Strict-Transport-Security']
    csp = headers['Content-Security-Policy']
    assert "script-src 'self';" in csp and 'unsafe-eval' not in csp
    assert "frame-ancestors 'none'" in csp and "base-uri 'none'" in csp
    assert config['apiBaseUrl'] in csp and config['cognitoDomain'] in csp


def main():
    assert aws('sts', 'get-caller-identity')['Account'] == '844274146091'
    frontend, backend = stack_outputs('stm32-admin-frontend'), stack_outputs('stm32-admin-backend')
    origin, bucket = frontend['WebsiteUrl'], frontend['AssetsBucketName']
    config = runtime_config(frontend, backend)
    evidence = json.loads((ROOT / 'build/cloud-admin/frontend-publish.json').read_text())
    assert evidence['runtime'] == config
    for key, digest in evidence['sha256'].items():
        with response(origin + '/' + key) as result:
            assert result.status == 200, (key, result.status)
            body = result.read()
            assert hashlib.sha256(body).hexdigest() == digest, key
            check_headers(result.headers, config)
            assert ('immutable' if key.startswith('assets/') else 'no-store') in result.headers['Cache-Control']
            if key == 'config.json':
                assert json.loads(body) == config
    with response(origin + '/') as result:
        assert result.status == 200 and 'no-store' in result.headers['Cache-Control']
        assert hashlib.sha256(result.read()).hexdigest() == evidence['sha256']['index.html']
    for key in ('config.example.json', 'does-not-exist.js'):
        with response(origin + '/' + key) as result:
            assert result.status in (403, 404), (key, result.status)
    print('PASS HTTPS assets/hashes, runtime config, security headers, caching and missing-file failures')

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    try:
        urllib.request.build_opener(NoRedirect).open(origin.replace('https:', 'http:') + '/', timeout=30)
    except urllib.error.HTTPError as error:
        assert error.code in (301, 302, 307, 308)
        assert error.headers['Location'] == origin + '/'
    else:
        raise AssertionError('HTTP did not redirect to HTTPS')
    for key in ('index.html', 'config.json'):
        with response('https://' + bucket + '.s3.ap-southeast-2.amazonaws.com/' + key) as result:
            assert result.status == 403, 'S3 origin allowed anonymous read'
    block = aws('s3api', 'get-public-access-block', '--bucket', bucket)['PublicAccessBlockConfiguration']
    assert all(block.values())
    assert not aws('s3api', 'get-bucket-policy-status', '--bucket', bucket)['PolicyStatus']['IsPublic']
    distribution = aws('cloudfront', 'get-distribution', '--id', frontend['DistributionId'])['Distribution']
    assert distribution['Status'] == 'Deployed'
    assert distribution['DistributionConfig']['Origins']['Items'][0]['OriginAccessControlId']
    print('PASS HTTPS redirect, private S3 origin and deployed Origin Access Control')
    print('Hosted URL:', origin + '/')
    print('End-user login/logout and UI flow must still be checked in the browser.')


if __name__ == '__main__':
    main()