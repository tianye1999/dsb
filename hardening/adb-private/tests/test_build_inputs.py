import base64
import importlib.util
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('build_inputs', Path(__file__).parents[1] / 'build_inputs.py')
build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)


class BuildInputsTests(unittest.TestCase):
    def test_cache_is_revalidated_before_use(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'libc.a'
            p.write_bytes(b'wrong')
            content = b'official object fixture'
            with patch.object(build.urllib.request, 'urlopen', return_value=io.BytesIO(base64.b64encode(content))) as url:
                build.fetch(p, 'https://android.googlesource.com/fixture', build.blob(content))
                self.assertEqual(p.read_bytes(), content)
                url.assert_called_once()
            with patch.object(build.urllib.request, 'urlopen') as url:
                build.fetch(p, 'https://android.googlesource.com/fixture', build.blob(content))
                url.assert_not_called()

    def test_bad_download_never_replaces_cache(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'libc.a'
            p.write_bytes(b'old')
            with patch.object(build.urllib.request, 'urlopen', side_effect=lambda *a, **kw: io.BytesIO(base64.b64encode(b'bad'))), \
                 patch.object(build.time, 'sleep'):
                with self.assertRaises(build.BuildError):
                    build.fetch(p, 'https://android.googlesource.com/fixture', build.blob(b'good'))
            self.assertEqual(p.read_bytes(), b'old')


if __name__ == '__main__':
    unittest.main()
