import importlib.util
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).parents[1]
NGINX = ROOT / "config" / "nginx.conf"
TEMPLATE_PATCH = Path(__file__).with_name("patch_australian_template_dates.py")
PDF_PATCH = Path(__file__).with_name("patch_australian_pdf.py")


class AustralianDatesTest(unittest.TestCase):
    def test_django_forces_australian_language_and_numeric_date(self):
        source = (ROOT / "overrides" / "settings-main.py").read_text()
        formats = (ROOT / "formats" / "en_AU" / "formats.py").read_text()
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
        compose = (ROOT / "compose.yaml").read_text()
        self.assertIn("history-overview.html:/home/wger/src/wger/exercises/templates/history/overview.html:ro", compose)
        self.assertIn("api-key.html:/home/wger/src/wger/core/templates/user/api_key.html:ro", compose)

    def test_pinned_human_facing_templates_use_shared_numeric_formats(self):
        spec = importlib.util.spec_from_file_location("patch_australian_template_dates", TEMPLATE_PATCH)
        patch = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(patch)

        history = patch.patch_text("history-overview", '''
<h6>{{ day.grouper|date:"l, j F Y" }}</h6>
<small title="{{ event.stream.timestamp|date:'Y-m-d H:i:s' }}">
''')
        account = patch.patch_text("api-key", '''
<td>{{ session.long_lived.created|date:"Y-m-d H:i" }}</td>
<td>{{ session.expire_date|date:"Y-m-d H:i" }}</td>
''')

        self.assertIn('{{ day.grouper|date:"SHORT_DATE_FORMAT" }}', history)
        self.assertIn("event.stream.timestamp|date:'SHORT_DATETIME_FORMAT'", history)
        self.assertEqual(account.count('date:"SHORT_DATETIME_FORMAT"'), 2)

        formats = {}
        exec((ROOT / "formats" / "en_AU" / "formats.py").read_text(), formats)
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
    def test_pinned_pdf_footer_uses_australian_numeric_date(self):
        spec = importlib.util.spec_from_file_location("patch_australian_pdf", PDF_PATCH)
        patch = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(patch)

        source = "date = datetime.date.today().strftime('%d.%m.%Y')"
        self.assertEqual(
            patch.patch_text(source),
            "date = datetime.date.today().strftime('%d/%m/%Y')",
        )


if __name__ == "__main__":
    unittest.main()
