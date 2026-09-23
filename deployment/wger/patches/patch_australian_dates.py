#!/usr/bin/env python3
"""Give every athlete-facing React date Australian ordering."""
from pathlib import Path
import sys

root = Path(sys.argv[1])


def replace(path: str, old: str, new: str) -> None:
    target = root / path
    text = target.read_text()
    if text.count(old) != 1:
        raise SystemExit(f"Expected one Australian-date anchor in {path}, found {text.count(old)}")
    target.write_text(text.replace(old, new))


def replace_all(path: str, old: str, new: str, count: int) -> None:
    target = root / path
    text = target.read_text()
    if text.count(old) != count:
        raise SystemExit(f"Expected {count} Australian-date anchors in {path}, found {text.count(old)}")
    target.write_text(text.replace(old, new))


dates = "src/core/lib/date.ts"
replace(
    dates,
    "export const DAY_MS = 24 * 60 * 60 * 1000;",
    "export const DAY_MS = 24 * 60 * 60 * 1000;\nconst DISPLAY_LOCALE = 'en-AU';",
)
target = root / dates
text = target.read_text()
old_locale = "    locale = locale ?? i18n.language;"
if text.count(old_locale) != 4:
    raise SystemExit(f"Expected four shared date/time locale anchors in {dates}, found {text.count(old_locale)}")
# Keep the time-only helper caller-selectable; force all three helpers that draw a date.
text = text.replace(old_locale, "    locale = DISPLAY_LOCALE;")
text = text.replace("    locale = DISPLAY_LOCALE;", old_locale, 1)
target.write_text(text)
replace(
    "src/components/Dashboard/MeasurementCard.tsx",
    "import { makeLink, WgerLink } from \"@/core/lib/url\";",
    "import { makeLink, WgerLink } from \"@/core/lib/url\";\nimport { dateToLocale } from \"@/core/lib/date\";",
)
replace(
    "src/components/Dashboard/MeasurementCard.tsx",
    "<TableCell>{entry.date.toLocaleDateString()}</TableCell>",
    "<TableCell>{dateToLocale(entry.date)}</TableCell>",
)
for path, count in {
    "src/components/Routines/widgets/forms/SessionForm.tsx": 3,
    "src/components/Routines/widgets/forms/RoutineForm.tsx": 2,
    "src/components/Measurements/widgets/EntryDateTimeField.tsx": 1,
    "src/components/Nutrition/widgets/forms/MealForm.tsx": 1,
    "src/components/Nutrition/widgets/forms/NutritionDiaryEntryForm.tsx": 1,
    "src/components/Nutrition/widgets/forms/PlanForm.tsx": 2,
}.items():
    replace_all(path, "adapterLocale={i18n.language}", 'adapterLocale="en-AU"', count)
replace(
    "src/components/Nutrition/widgets/forms/NutritionDiaryEntryForm.tsx",
    'format="yyyy-MM-dd HH:mm"',
    'format="dd/MM/yyyy HH:mm"',
)
replace_all(
    "src/components/Nutrition/widgets/forms/PlanForm.tsx",
    'format="yyyy-MM-dd"',
    'format="dd/MM/yyyy"',
    2,
)
replace(
    "src/components/Routines/widgets/forms/SessionForm.test.tsx",
    "const formattedDate = new Date().toLocaleDateString(\n            'en-us',\n            { year: 'numeric', month: '2-digit', day: '2-digit' }\n        );",
    "const formattedDate = new Date().toLocaleDateString(\n            'en-AU',\n            { year: 'numeric', month: '2-digit', day: '2-digit' }\n        );",
)
replace_all(
    "src/components/Routines/widgets/forms/SessionForm.test.tsx",
    "toLocaleString(DateTime.TIME_SIMPLE, { locale: 'en-us' })",
    "toLocaleString(DateTime.TIME_SIMPLE, { locale: 'en-AU' })",
    2,
)
replace(
    "src/core/lib/date.test.ts",
    "import { dateTimeToHHMM, dateToRelative, dateToYYYYMMDD, yyyymmddToDate } from \"@/core/lib/date\";",
    "import { dateTimeToHHMM, dateTimeToLocale, dateToLocale, dateToRelative, dateToYYYYMMDD, luxonDateTimeToLocale, yyyymmddToDate } from \"@/core/lib/date\";\nimport { DateTime } from \"luxon\";",
)
replace(
    "src/core/lib/date.test.ts",
    """    describe(\"test date utility\", () => {
""",
    """    describe(\"test date utility\", () => {

        test('every shared display helper uses Australian day/month/year order', () => {
            const date = new Date(2026, 8, 21, 15, 24);
            expect(dateToLocale(date)).toBe('21/09/26');
            expect(dateTimeToLocale(date).startsWith('21/09/26')).toBe(true);
            expect(dateToLocale(date, 'en-US', { year: 'numeric', month: '2-digit', day: '2-digit' })).toBe('21/09/2026');
            expect(luxonDateTimeToLocale(DateTime.fromJSDate(date)).startsWith('21 Sept 2026')).toBe(true);
        });
""",
)

print("Applied Australian date format to every shared React date display and picker")
