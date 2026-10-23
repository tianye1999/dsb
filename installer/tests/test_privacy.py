import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from privacy import check_stage


class PrivacyTests(unittest.TestCase):
    def test_public_art_is_allowed_but_screenshots_are_not(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ['app/res/mipmap-xxxhdpi/ic_launcher_foreground.png', 'docs/assets/hero-a.png']:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b'\x89PNG\r\n\x1a\n')
            check_stage(root)
            (root / 'docs/assets/screenshot.png').write_bytes(b'\x89PNG')
            with self.assertRaisesRegex(ValueError, 'screenshot.png'):
                check_stage(root)

    def test_device_identifiers_in_json_and_environment_are_refused(self):
        value = 'TEST' + '0123456789'
        for content in [json.dumps({'serial': value}), 'ANDROID_SERIAL="' + value + '"',
                        'adb_serial: ' + value]:
            with self.subTest(content_format=content[:10]), tempfile.TemporaryDirectory() as directory:
                (Path(directory) / 'settings.json').write_text(content)
                with self.assertRaisesRegex(ValueError, 'rule 7'):
                    check_stage(directory)

    def test_filesystem_transport_scheme_is_not_a_device_identifier(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / 'code.py').write_text("serial = 'localfilesystem:' + path\n")
            check_stage(directory)


if __name__ == '__main__':
    unittest.main()
