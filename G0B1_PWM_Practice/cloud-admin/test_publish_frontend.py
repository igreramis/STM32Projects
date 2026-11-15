import json
from pathlib import Path
import tempfile
import unittest

from publish_frontend import build_files, runtime_config


class FrontendPublishTests(unittest.TestCase):
    def test_runtime_config_has_only_public_fields_and_exact_origin(self):
        frontend = dict(WebsiteUrl='https://example123.cloudfront.net')
        backend = dict(ApiUrl='https://abc.execute-api.ap-southeast-2.amazonaws.com',
                       CognitoDomain='https://example.auth.ap-southeast-2.amazoncognito.com', ClientId='client123')
        config = runtime_config(frontend, backend)
        self.assertEqual(set(config), {'apiBaseUrl', 'cognitoDomain', 'userPoolClientId', 'redirectUri', 'region'})
        self.assertEqual(config['redirectUri'], frontend['WebsiteUrl'] + '/')
        for invalid in ('http://example123.cloudfront.net', 'https://example123.cloudfront.net/path',
                        'https://example123.cloudfront.net.evil.test'):
            with self.assertRaises(AssertionError):
                runtime_config(dict(WebsiteUrl=invalid), backend)

    def test_only_built_assets_and_entrypoints_are_published(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'assets').mkdir()
            (root / 'assets/index-1234abcd.js').write_text('export {}')
            (root / 'index.html').write_text('<script type="module" src="/assets/index-1234abcd.js"></script>')
            (root / 'favicon.svg').write_text('<svg/>')
            (root / 'config.json').write_text(json.dumps({'redirectUri': 'http://localhost:5173/'}))
            (root / 'config.example.json').write_text('{}')
            files = build_files(root)
            self.assertEqual([item[1] for item in files], ['assets/index-1234abcd.js', 'favicon.svg', 'index.html'])
            self.assertEqual(files[-1][3], 'no-store')
            (root / 'assets/index-1234abcd.js.map').write_text('{}')
            with self.assertRaises(AssertionError):
                build_files(root)


if __name__ == '__main__':
    unittest.main()