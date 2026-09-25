import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).parents[1]
PATCH = Path(__file__).with_name("patch_australian_dates.py")
NGINX = ROOT / "config" / "nginx.conf"
TEMPLATE_PATCH = Path(__file__).with_name("patch_australian_template_dates.py")
PDF_PATCH = Path(__file__).with_name("patch_australian_pdf.py")


class AustralianDatesTest(unittest.TestCase):
    def test_django_forces_australian_language_and_numeric_date(self):
        source = (ROOT / "settings-main.py").read_text()
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
        dockerfile = (ROOT / "Dockerfile").read_text()
        self.assertIn("COPY --chown=wger:wger formats/en_AU/formats.py /home/wger/src/wger/formats/en_AU/formats.py", dockerfile)
        deploy_bridge = (ROOT.parents[1] / "runtime" / "deploy_wger.py").read_text()
        for name in (
            "formats/en_AU/formats.py",
            "patches/patch_australian_dates.py",
            "patches/patch_australian_template_dates.py",
            "patches/patch_australian_pdf.py",
        ):
            self.assertIn(repr(name), deploy_bridge)


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


    def test_shared_react_helpers_and_pickers_use_australian_dates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixtures = {
                "src/core/lib/date.ts": '''import i18n from 'i18next';
import { DateTime, DateTimeFormatOptions } from "luxon";
export const DAY_MS = 24 * 60 * 60 * 1000;
export function dateTimeToLocale(dateTime: Date | null, locale?: string, options?: Intl.DateTimeFormatOptions,) {
    if (dateTime == null) return '';
    locale = locale ?? i18n.language;
    options = options ?? { year: '2-digit', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' };
    return dateTime.toLocaleString(locale ? [locale] : [], options);
}
export function dateTimeToLocaleHHMM(dateTime: Date | null, locale?: string, options?: Intl.DateTimeFormatOptions) {
    if (dateTime == null) return null;
    locale = locale ?? i18n.language;
    options = options ?? { hour: '2-digit', minute: '2-digit' };
    return dateTime.toLocaleTimeString(locale ? [locale] : [], options);
}
export function luxonDateTimeToLocale(dateTime: DateTime | null, locale?: string, options?: DateTimeFormatOptions,) {
    if (dateTime == null) return '';
    locale = locale ?? i18n.language;
    options = options ?? DateTime.DATE_MED;
    return dateTime.toLocaleString(options, { locale: locale });
}
export function dateToLocale(dateTime: Date | null, locale?: string, options?: Intl.DateTimeFormatOptions) {
    if (dateTime == null) return '';
    locale = locale ?? i18n.language;
    options = options ?? { year: '2-digit', month: '2-digit', day: '2-digit' };
    return dateTime.toLocaleString(locale ? [locale] : [], options);
}
''',
                "src/core/lib/date.test.ts": '''import { dateTimeToHHMM, dateToRelative, dateToYYYYMMDD, yyyymmddToDate } from "@/core/lib/date";
    describe("test date utility", () => {
''',
                "src/components/Dashboard/MeasurementCard.tsx": '''import { makeLink, WgerLink } from "@/core/lib/url";
<TableCell>{entry.date.toLocaleDateString()}</TableCell>
''',
                "src/components/Routines/widgets/forms/SessionForm.tsx": '<LocalizationProvider dateAdapter={AdapterLuxon} adapterLocale={i18n.language}>\n' * 3,
                "src/components/Routines/widgets/forms/RoutineForm.tsx": '<LocalizationProvider dateAdapter={AdapterLuxon} adapterLocale={i18n.language}>\n' * 2,
                "src/components/Measurements/widgets/EntryDateTimeField.tsx": '<LocalizationProvider dateAdapter={AdapterLuxon} adapterLocale={i18n.language}>\n',
                "src/components/Nutrition/widgets/forms/MealForm.tsx": '<LocalizationProvider dateAdapter={AdapterLuxon} adapterLocale={i18n.language}>\n',
                "src/components/Nutrition/widgets/forms/NutritionDiaryEntryForm.tsx": '<LocalizationProvider dateAdapter={AdapterLuxon} adapterLocale={i18n.language}>\n<DateTimePicker format="yyyy-MM-dd HH:mm" />\n',
                "src/components/Nutrition/widgets/forms/PlanForm.tsx": '<LocalizationProvider dateAdapter={AdapterLuxon} adapterLocale={i18n.language}>\n<DatePicker format="yyyy-MM-dd" />\n' * 2,
                "src/components/Routines/widgets/forms/SessionForm.test.tsx": """const formattedDate = new Date().toLocaleDateString(
            'en-us',
            { year: 'numeric', month: '2-digit', day: '2-digit' }
        );
const timeStartFormatted = timeStart.toLocaleString(DateTime.TIME_SIMPLE, { locale: 'en-us' });
const timeEndFormatted = timeEnd.toLocaleString(DateTime.TIME_SIMPLE, { locale: 'en-us' });
""",
            }
            for relative, content in fixtures.items():
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content)
            subprocess.run([sys.executable, str(PATCH), str(root)], check=True)
            source = (root / "src/core/lib/date.ts").read_text()
            measurement = (root / "src/components/Dashboard/MeasurementCard.tsx").read_text()
            picker_sources = "\n".join(
                (root / path).read_text()
                for path in fixtures
                if path.endswith(".tsx") and path != "src/components/Dashboard/MeasurementCard.tsx"
            )

        display_locale = re.search(r"const DISPLAY_LOCALE = .*?;", source).group()
        date_time = source[source.index("export function dateTimeToLocale("):source.index("export function dateTimeToLocaleHHMM(")]
        time_only = source[source.index("export function dateTimeToLocaleHHMM("):source.index("export function luxonDateTimeToLocale(")]
        luxon = source[source.index("export function luxonDateTimeToLocale("):source.index("export function dateToLocale(")]
        date_only = source[source.index("export function dateToLocale("):]
        javascript = display_locale + "\n" + date_time + luxon + date_only
        javascript = javascript.replace(
            "export function dateTimeToLocale(dateTime: Date | null, locale?: string, options?: Intl.DateTimeFormatOptions,)",
            "function dateTimeToLocale(dateTime, locale, options)",
        ).replace(
            "export function luxonDateTimeToLocale(dateTime: DateTime | null, locale?: string, options?: DateTimeFormatOptions,)",
            "function luxonDateTimeToLocale(dateTime, locale, options)",
        ).replace(
            "export function dateToLocale(dateTime: Date | null, locale?: string, options?: Intl.DateTimeFormatOptions)",
            "function dateToLocale(dateTime, locale, options)",
        )
        javascript += '''
const DateTime = { fromJSDate: (value) => ({
    toLocaleString: (options, config) => value.toLocaleDateString(config.locale, options),
}) };
const date = new Date(2026, 8, 21, 15, 24);
console.log(JSON.stringify([
    dateToLocale(date),
    dateTimeToLocale(date).split(',')[0],
    luxonDateTimeToLocale(DateTime.fromJSDate(date)),
    dateToLocale(date, 'en-US', { year: 'numeric', month: '2-digit', day: '2-digit' }),
    dateToLocale(new Date(2026, 0, 5), 'en-US', { month: '2-digit', day: '2-digit' }),
    luxonDateTimeToLocale(DateTime.fromJSDate(new Date(2026, 0, 5)), 'en-US', { year: '2-digit', month: 'long', day: 'numeric' }),
]));
'''
        output = subprocess.run(
            ["node", "-e", javascript], check=True, text=True, capture_output=True
        )
        self.assertEqual(json.loads(output.stdout), ["21/09/2026"] * 4 + ["05/01/2026"] * 2)
        self.assertEqual(source.count("locale = DISPLAY_LOCALE;"), 3)
        self.assertIn("locale = locale ?? i18n.language;", time_only)
        self.assertNotIn("locale = DISPLAY_LOCALE;", time_only)
        self.assertIn("dateToLocale(entry.date)", measurement)
        self.assertNotIn("adapterLocale={i18n.language}", picker_sources)
        self.assertEqual(picker_sources.count('adapterLocale="en-AU"'), 10)
        self.assertNotIn('format="yyyy-MM-dd', picker_sources)
        self.assertIn('format="dd/MM/yyyy HH:mm"', picker_sources)
        self.assertEqual(picker_sources.count('format="dd/MM/yyyy"'), 2)

    def test_build_applies_shared_date_patch_before_compiling(self):
        script = (ROOT / "patches" / "prepare-react.sh").read_text()
        self.assertIn('patch_australian_dates.py" "$BUILD_DIR"', script)
        self.assertLess(script.index("patch_australian_dates.py"), script.index("npm run typecheck"))
        self.assertIn("history-overview.html.next", script)
        self.assertIn("api-key.html.next", script)
        self.assertIn("pdf.py.next", script)


if __name__ == "__main__":
    unittest.main()
