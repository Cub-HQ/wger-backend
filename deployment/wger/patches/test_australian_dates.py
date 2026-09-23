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
        dockerfile = (ROOT / "Dockerfile").read_text()
        self.assertIn("COPY --chown=wger:wger formats/en_AU/formats.py /home/wger/src/wger/formats/en_AU/formats.py", dockerfile)

    def test_legacy_english_urls_redirect_to_australian_prefix(self):
        config = NGINX.read_text()
        self.assertIn("location ~ ^/(?:en|en-gb)(/.*)?$", config)
        self.assertIn("return 302 /en-au$1$is_args$args;", config)

    def test_shared_react_helpers_and_pickers_use_australian_dates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixtures = {
                "src/core/lib/date.ts": '''import i18n from 'i18next';
import { DateTime, DateTimeFormatOptions } from "luxon";
export const DAY_MS = 24 * 60 * 60 * 1000;
export function dateTimeToLocaleHHMM(dateTime: Date | null, locale?: string, options?: Intl.DateTimeFormatOptions) {
    if (dateTime == null) return null;
    locale = locale ?? i18n.language;
    options = options ?? { hour: '2-digit', minute: '2-digit' };
    return dateTime.toLocaleTimeString(locale ? [locale] : [], options);
}
export function dateTimeToLocale(dateTime: Date | null, locale?: string, options?: Intl.DateTimeFormatOptions,) {
    if (dateTime == null) return '';
    locale = locale ?? i18n.language;
    options = options ?? { year: '2-digit', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' };
    return dateTime.toLocaleString(locale ? [locale] : [], options);
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
                "src/components/Routines/widgets/forms/SessionForm.tsx": '''<LocalizationProvider dateAdapter={AdapterLuxon} adapterLocale={i18n.language}>
<DatePicker value={selectedDate} />
</LocalizationProvider>
<LocalizationProvider dateAdapter={AdapterLuxon} adapterLocale={i18n.language}>
<TimePicker value={timeStart} />
</LocalizationProvider>
<LocalizationProvider dateAdapter={AdapterLuxon} adapterLocale={i18n.language}>
<TimePicker value={timeEnd} />
</LocalizationProvider>
''',
                "src/components/Routines/widgets/forms/RoutineForm.tsx": '''<LocalizationProvider dateAdapter={AdapterLuxon} adapterLocale={i18n.language}>
<DatePicker value={startDate} />
</LocalizationProvider>
<LocalizationProvider dateAdapter={AdapterLuxon} adapterLocale={i18n.language}>
<DatePicker value={endDate} />
</LocalizationProvider>
''',
                "src/components/Measurements/widgets/EntryDateTimeField.tsx": '''<LocalizationProvider dateAdapter={AdapterLuxon} adapterLocale={i18n.language}>
<DateTimePicker value={value} />
</LocalizationProvider>
''',
                "src/components/Nutrition/widgets/forms/MealForm.tsx": '''<LocalizationProvider dateAdapter={AdapterLuxon} adapterLocale={i18n.language}>
<TimePicker value={value} />
</LocalizationProvider>
''',
                "src/components/Nutrition/widgets/forms/NutritionDiaryEntryForm.tsx": '''<LocalizationProvider dateAdapter={AdapterLuxon} adapterLocale={i18n.language}>
<DateTimePicker
    format="yyyy-MM-dd HH:mm"
    value={dateValue}
/>
</LocalizationProvider>
''',
                "src/components/Nutrition/widgets/forms/PlanForm.tsx": '''<LocalizationProvider dateAdapter={AdapterLuxon} adapterLocale={i18n.language}>
<DatePicker format="yyyy-MM-dd" value={startDateValue} />
</LocalizationProvider>
<LocalizationProvider dateAdapter={AdapterLuxon} adapterLocale={i18n.language}>
<DatePicker format="yyyy-MM-dd" value={endDateValue} />
</LocalizationProvider>
''',
                "src/components/Routines/widgets/forms/SessionForm.test.tsx": '''const formattedDate = new Date().toLocaleDateString(
            'en-us',
            { year: 'numeric', month: '2-digit', day: '2-digit' }
        );
const timeStartFormatted = timeStart.toLocaleString(DateTime.TIME_SIMPLE, { locale: 'en-us' });
const timeEndFormatted = timeEnd.toLocaleString(DateTime.TIME_SIMPLE, { locale: 'en-us' });
''',
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
        date_time = source[source.index("export function dateTimeToLocale("):source.index("export function luxonDateTimeToLocale(")]
        date_only = source[source.index("export function dateToLocale("):]
        javascript = display_locale + "\n" + date_time + date_only
        javascript = javascript.replace(
            "export function dateTimeToLocale(dateTime: Date | null, locale?: string, options?: Intl.DateTimeFormatOptions,)",
            "function dateTimeToLocale(dateTime, locale, options)",
        ).replace(
            "export function dateToLocale(dateTime: Date | null, locale?: string, options?: Intl.DateTimeFormatOptions)",
            "function dateToLocale(dateTime, locale, options)",
        )
        javascript += '''
const date = new Date(2026, 8, 21, 15, 24);
console.log(JSON.stringify([
    dateToLocale(date),
    dateTimeToLocale(date).split(',')[0],
    dateToLocale(date, 'en-US', { year: 'numeric', month: '2-digit', day: '2-digit' }),
]));
'''
        output = subprocess.run(
            ["node", "-e", javascript], check=True, text=True, capture_output=True
        )
        self.assertEqual(json.loads(output.stdout), ["21/09/26", "21/09/26", "21/09/2026"])
        self.assertEqual(source.count("locale = DISPLAY_LOCALE;"), 3)
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


if __name__ == "__main__":
    unittest.main()
