"""Publish built static files only. Does not change backend, firmware, or IoT state."""
import hashlib
import json
from pathlib import Path
import re
import tempfile
from urllib.parse import urlsplit

from smoke_test import aws

ROOT = Path(__file__).resolve().parents[1]


def stack_outputs(name):
    stack = aws('cloudformation', 'describe-stacks', '--stack-name', name)['Stacks'][0]
    assert stack['StackStatus'] in ('CREATE_COMPLETE', 'UPDATE_COMPLETE')
    return {item['OutputKey']: item['OutputValue'] for item in stack['Outputs']}


def runtime_config(frontend, backend):
    origin = frontend['WebsiteUrl']
    assert re.fullmatch(r'https://[a-z0-9]+\.cloudfront\.net', origin)
    assert re.fullmatch(r'https://[a-z0-9]+\.execute-api\.ap-southeast-2\.amazonaws\.com', backend['ApiUrl'])
    assert re.fullmatch(r'https://[a-z0-9-]+\.auth\.ap-southeast-2\.amazoncognito\.com', backend['CognitoDomain'])
    assert re.fullmatch(r'[a-z0-9]+', backend['ClientId'])
    return dict(apiBaseUrl=backend['ApiUrl'], cognitoDomain=backend['CognitoDomain'],
                userPoolClientId=backend['ClientId'], redirectUri=origin + '/', region='ap-southeast-2')


def build_files(dist):
    """Never recursively publish public/, source maps, examples, or local config."""
    assets = sorted((dist / 'assets').iterdir())
    assert assets and (dist / 'index.html').is_file()
    files = []
    for path in assets:
        assert path.is_file() and not path.is_symlink()
        assert re.fullmatch(r'[A-Za-z0-9_-]+-[A-Za-z0-9_-]{8,}\.(js|css)', path.name), path.name
        files.append((path, 'assets/' + path.name,
                      'text/javascript' if path.suffix == '.js' else 'text/css',
                      'public,max-age=31536000,immutable'))
    for name, content_type in (('favicon.svg', 'image/svg+xml'), ('index.html', 'text/html')):
        path = dist / name
        assert path.is_file() and not path.is_symlink()
        files.append((path, name, content_type, 'no-store'))
    html = (dist / 'index.html').read_text()
    assert '<script' in html and 'localhost' not in html
    references = re.findall(r'(?:src|href)="(/assets/[^\"]+)"', html)
    assert references and all(any(key == ref.lstrip('/') for _, key, _, _ in files) for ref in references)
    return files


def main():
    assert aws('sts', 'get-caller-identity')['Account'] == '844274146091'
    frontend, backend = stack_outputs('stm32-admin-frontend'), stack_outputs('stm32-admin-backend')
    config = runtime_config(frontend, backend)
    bucket = frontend['AssetsBucketName']
    assert bucket.startswith('stm32-admin-frontend-')
    function = aws('lambda', 'get-function-configuration', '--function-name', backend['HttpFunctionName'])
    assert function['Environment']['Variables']['FIRMWARE_DEPLOYMENT_ENABLED'] == 'false'
    distribution = aws('cloudfront', 'get-distribution', '--id', frontend['DistributionId'])['Distribution']
    assert distribution['DomainName'] == urlsplit(frontend['WebsiteUrl']).hostname
    files = build_files(ROOT / 'admin-ui/dist')
    manifest = {}
    # Upload hashed assets first and entry HTML last. Keep older assets for open tabs.
    with tempfile.TemporaryDirectory(prefix='frontend-publish-') as temporary:
        path = Path(temporary) / 'config.json'
        path.write_text(json.dumps(config, indent=2) + '\n')
        files.insert(-1, (path, 'config.json', 'application/json', 'no-store'))
        for source, key, content_type, cache_control in files:
            aws('s3api', 'put-object', '--bucket', bucket, '--key', key, '--body', str(source),
                '--content-type', content_type, '--cache-control', cache_control,
                '--server-side-encryption', 'AES256')
            manifest[key] = hashlib.sha256(source.read_bytes()).hexdigest()
    invalidation = aws('cloudfront', 'create-invalidation', '--distribution-id', frontend['DistributionId'],
                       '--paths', '/', '/index.html', '/config.json', '/favicon.svg')['Invalidation']['Id']
    evidence = dict(frontend=frontend, runtime=config, sha256=manifest, invalidation=invalidation)
    target = ROOT / 'build/cloud-admin/frontend-publish.json'
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(evidence, indent=2) + '\n')
    print(json.dumps(evidence, indent=2))


if __name__ == '__main__':
    main()