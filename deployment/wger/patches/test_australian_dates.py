import importlib.util
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).parents[1]
NGINX = ROOT / "config" / "nginx.conf"
SOURCE = Path(__file__).parents[3]
FORMATS = SOURCE / "wger" / "formats" / "en_AU" / "formats.py"


class AustralianDatesTest(unittest.TestCase):
    def test_django_forces_australian_language_and_numeric_date(self):
        source = (ROOT / "overrides" / "settings-main.py").read_text()
        formats = FORMATS.read_text()
        namespace = {}
        exec(formats, namespace)
        date = __import__('datetime').date(2026, 9, 21)
        django_to_strftime = lambda value: value.replace('d', '%d').replace('m', '%m').replace('Y', '%Y')

        self.assertIn("LANGUAGE_CODE = 'en-au'", source)
        self.assertIn("LANGUAGES = (('en-au', 'Australian English'),)", source)
        self.assertIn("AVAILABLE_LANGUAGES = LANGUAGES", source)
        self.assertIn("FORMAT_MODULE_PATH = 'wger.formats'", source)
        self.assertEqual(date.strftime(django_to_strftime(namespace['DATE_FORMAT'])), "21/09/2026")
        self.assertEqual(date.strftime(django_to_strftime(namespace['SHORT_DATE_FORMAT'])), "21/09/2026")
        self.assertEqual(date.strftime(django_to_strftime(namespace['DATETIME_FORMAT']).split()[0]), "21/09/2026")
        self.assertEqual(date.strftime(django_to_strftime(namespace['SHORT_DATETIME_FORMAT']).split()[0]), "21/09/2026")
        self.assertEqual(namespace['DATE_INPUT_FORMATS'][0], '%Y-%m-%d')


    def test_legacy_english_urls_redirect_to_australian_prefix(self):
        config = NGINX.read_text()
        self.assertIn("location ~ ^/(?:en|en-gb)(/.*)?$", config)
        self.assertIn("return 302 /en-au$1$is_args$args;", config)
        self.assertIn("absolute_redirect off;", config)

    def test_human_facing_templates_use_shared_numeric_formats(self):
        history = (SOURCE / "wger/exercises/templates/history/overview.html").read_text()
        account = (SOURCE / "wger/core/templates/user/api_key.html").read_text()

        self.assertIn('{{ day.grouper|date:"SHORT_DATE_FORMAT" }}', history)
        self.assertIn("event.stream.timestamp|date:'SHORT_DATETIME_FORMAT'", history)
        self.assertEqual(account.count('date:"SHORT_DATETIME_FORMAT"'), 2)
        for text in (history, account):
            self.assertNotRegex(text, r"\|date:[\"']?(l, j F Y|Y-m-d)")

        formats = {}
        exec(FORMATS.read_text(), formats)
        render_script = f'''
from datetime import datetime
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from django.conf import settings
from django.template import Context, Engine
with TemporaryDirectory() as directory:
    package = Path(directory) / "custom_formats" / "en_AU"
    package.mkdir(parents=True)
    (package.parent / "__init__.py").write_text("")
    (package / "__init__.py").write_text("")
    (package / "formats.py").write_text("SHORT_DATE_FORMAT = {formats["SHORT_DATE_FORMAT"]!r}\\nSHORT_DATETIME_FORMAT = {formats["SHORT_DATETIME_FORMAT"]!r}\\n")
    sys.path.insert(0, directory)
    settings.configure(USE_I18N=True, USE_TZ=False, LANGUAGE_CODE="en-au", FORMAT_MODULE_PATH="custom_formats", INSTALLED_APPS=[])
    import django
    django.setup()
    template = Engine().from_string('{{{{ value|date:"SHORT_DATE_FORMAT" }}}}|{{{{ value|date:"SHORT_DATETIME_FORMAT" }}}}')
    print(template.render(Context({{"value": datetime(2026, 9, 21, 15, 24)}})))
'''
        result = subprocess.run(
            [sys.executable, "-c", render_script], text=True, capture_output=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        rendered = result.stdout.strip()
        self.assertEqual(rendered, "21/09/2026|21/09/2026 15:24")
    def test_pdf_footer_uses_australian_numeric_date(self):
        pdf = (SOURCE / "wger/utils/pdf.py").read_text()
        self.assertEqual(pdf.count("datetime.date.today().strftime('%d/%m/%Y')"), 1)
        self.assertNotIn("%d.%m.%Y", pdf)


if __name__ == "__main__":
    unittest.main()
