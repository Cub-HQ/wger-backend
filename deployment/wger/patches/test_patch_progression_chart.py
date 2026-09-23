import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

MODULE = Path(__file__).with_name("patch_progression_chart.py")

MUSCLE_PATCH = Path(__file__).with_name("patch_muscle_diagram.py")

LIVE_SETTING_FIXTURES = {
    "src/components/Routines/widgets/LogWidgets.tsx": '''import { dateToLocale, luxonDateTimeToLocale } from "@/core/lib/date";
export const ExerciseLog = (props: { exercise: Exercise, routineId: number, logEntries: WorkoutLog[] | undefined }) => {
<TimeSeriesChart data={logEntries} key={props.exercise.id} />
export const TimeSeriesChart = (props: { data: WorkoutLog[] }) => {
    // Group by rep count
    //
    // We draw series based on the same reps, as otherwise the chart wouldn't
    // make much sense
    const result: Map<number, WorkoutLog[]> = props.data.reduce(function (r, a) {
        r.set(a.repetitions, r.get(a.repetitions) || []);
        r.get(a.repetitions)!.push(a);
        return r;
    }, new Map());
name={key?.toString()}''',
    "src/components/Routines/widgets/WaveOne.tsx": '''                <ExerciseLog exercise={logs[0].exerciseObj!} routineId={session.routineId} logEntries={logs} />''',
    "src/index.tsx": '''import App from './App';
renderComponentShadowDom('react-page');''',
}


def apply_patch(root: Path):
    for relative, content in LIVE_SETTING_FIXTURES.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    with patch("sys.argv", [str(MODULE), str(root)]):
        spec = importlib.util.spec_from_file_location("progression_chart_patch", MODULE)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)


class ProgressionChartLiveSettingTest(unittest.TestCase):
    def test_live_django_settings_page_mounts_range_control(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            apply_patch(root)
            index = (root / "src/index.tsx").read_text()
            self.assertIn("mountProgressionChartRangeSetting", index)
            self.assertIn("mountProgressionChartRangeSetting();", index)




class ProgressionChartPatchTest(unittest.TestCase):
    def test_charts_block_history_by_set_number(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            apply_patch(root)

            widgets = (root / "src/components/Routines/widgets/LogWidgets.tsx").read_text()
            wave = (root / "src/components/Routines/widgets/WaveOne.tsx").read_text()
            range_module = root / "src/components/Routines/widgets/progressionChartRange.ts"
            self.assertTrue(range_module.is_file())
            self.assertIn("filterProgressionChartData(props.data)", widgets)
            self.assertIn("const counters = new Map<string, number>()", widgets)
            self.assertIn("const session = log.sessionId ?? log.date.toDateString()", widgets)
            self.assertIn("const setNumber = (counters.get(session) ?? 0) + 1", widgets)
            self.assertNotIn("a.iteration ?? 1", widgets)
            self.assertIn("name={`Set ${key}`}", widgets)
            self.assertIn("chartEntries={", wave)
            self.assertIn("candidate.routineId === session.routineId", wave)
            self.assertIn("log.exerciseId === logs[0].exerciseId", wave)
            self.assertIn("a.date.getTime() - b.date.getTime()", wave)

            start = widgets.index("    const counters =")
            grouping = widgets[start:widgets.index("    });", start) + len("    });")]
            grouping = grouping.replace("new Map<string, number>()", "new Map()")
            grouping = grouping.replace("new Map<number, WorkoutLog[]>()", "new Map()")
            logs = [
                {"sessionId": "aug", "date": "2026-08-20", "weight": weight}
                for weight in (45, 50, 55)
            ] + [
                {"sessionId": "sep", "date": "2026-09-03", "weight": weight}
                for weight in (50, 55, 60)
            ]
            script = f'''const props = {{data: JSON.parse(process.argv[1]).map(log => ({{...log, date: new Date(log.date)}}))}};
const chartData = props.data;
{grouping}
console.log(JSON.stringify(Array.from(result, ([setNumber, entries]) => [setNumber, entries.map(log => [log.date.toISOString().slice(0, 10), log.weight])])))'''
            output = subprocess.run(
                ["node", "-e", script, json.dumps(logs)],
                check=True,
                text=True,
                capture_output=True,
            )
            self.assertEqual(json.loads(output.stdout), [
                [1, [["2026-08-20", 45], ["2026-09-03", 50]]],
                [2, [["2026-08-20", 50], ["2026-09-03", 55]]],
                [3, [["2026-08-20", 55], ["2026-09-03", 60]]],
            ])
            # Erase only this generated module's type syntax; execute its real bodies on Node 18+.
            range_source = "\n".join(
                line for line in range_module.read_text().splitlines()
                if not line.startswith("export type ")
            )
            for annotation in (
                " as const", " as ProgressionChartRange", ": ProgressionChartRange",
                "<T extends { date: Date }>", ": T[]", ": Date",
            ):
                range_source = range_source.replace(annotation, "")
            executable_range_module = range_module.with_suffix(".mjs")
            executable_range_module.write_text(range_source)

            range_script = f'''const values = new Map();
const elements = new Map();
class Element {{
    constructor(tagName) {{ this.tagName = tagName; this.children = []; this.listeners = {{}}; this._value = ""; this.textContent = ""; }}
    set id(value) {{ this._id = value; elements.set(value, this); }}
    get id() {{ return this._id; }}
    set value(value) {{ this._value = this.tagName !== "select" || this.children.some(child => child.value === value) ? value : ""; }}
    get value() {{ return this._value || (this.tagName === "select" ? this.children[0]?.value ?? "" : ""); }}
    append(...children) {{ this.children.push(...children); }}
    prepend(...children) {{ this.children.unshift(...children); }}
    addEventListener(type, listener) {{ this.listeners[type] = listener; }}
}}
const mountSetting = () => {{
    elements.delete("progression-chart-range");
    const content = new Element("div"); content.id = "content";
    range.mountProgressionChartRangeSetting();
    return {{content, select: elements.get("progression-chart-range")}};
}};
globalThis.document = {{
    createElement: tagName => new Element(tagName),
    getElementById: id => elements.get(id) ?? null,
}};
globalThis.window = {{
    location: {{pathname: "/en/user/preferences"}},
    localStorage: {{
        getItem: key => values.get(key) ?? null,
        setItem: (key, value) => values.set(key, value),
    }},
}};
const range = await import({json.dumps(executable_range_module.as_uri())});
const input = [
    ["old", "2026-06-22T12:00:00Z"],
    ["edge", "2026-06-23T12:00:00Z"],
    ["recent", "2026-09-20T12:00:00Z"],
    ["future", "2026-09-24T12:00:00Z"],
].map(([id, date]) => ({{id, date: new Date(date)}}));
const initial = range.loadProgressionChartRange();
const firstMount = mountSetting();
const select = firstMount.select;
const settingInitial = select.value;
select.value = "3m";
select.listeners.change();
const returningSetting = mountSetting().select.value;
const storedSetting = range.loadProgressionChartRange();
range.saveProgressionChartRange("all");
const allTimeSetting = mountSetting().select.value;
console.log(JSON.stringify({{
    options: range.PROGRESSION_CHART_RANGES.map(option => option.value),
    initial,
    stored: storedSetting,
    setting: {{cards: firstMount.content.children.length, initial: settingInitial, returning: returningSetting, allTime: allTimeSetting, labels: select.children.map(option => option.textContent)}},
    threeMonths: range.filterProgressionChartData(input, "3m", new Date("2026-09-23T12:00:00Z")).map(entry => entry.id),
    monthEnd: range.filterProgressionChartData([
        {{id: "february", date: new Date("2026-02-28T12:00:00Z")}},
        {{id: "march", date: new Date("2026-03-31T12:00:00Z")}},
    ], "1m", new Date("2026-03-31T12:00:00Z")).map(entry => entry.id),
    all: range.filterProgressionChartData(input, "all", new Date("2026-09-23T12:00:00Z")).map(entry => entry.id),
}}));'''
            range_output = subprocess.run(
                ["node", "--input-type=module", "-e", range_script],
                check=True,
                text=True,
                capture_output=True,
            )
            self.assertEqual(json.loads(range_output.stdout), {
                "options": ["1m", "3m", "6m", "1y", "2y", "3y", "all"],
                "initial": "6m",
                "stored": "3m",
                "setting": {"cards": 1, "initial": "6m", "returning": "3m", "allTime": "all", "labels": ["1 month", "3 months", "6 months", "1 year", "2 years", "3 years", "All time"]},
                "threeMonths": ["edge", "recent"],
                "monthEnd": ["february", "march"],
                "all": ["old", "edge", "recent", "future"],
            })

    def test_bundle_fingerprint_checks_real_bytes(self):
        spec = importlib.util.spec_from_file_location("muscle_patch_for_chart_range", MUSCLE_PATCH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "main.js"
            target = Path(directory) / "react-main.js.next"
            fixture = b"height:`400px`,width:`200px`,backgroundImage:muscular_system_back.svg"
            approved = {hashlib.sha256(fixture).hexdigest()}
            source.write_bytes(fixture.replace(b"400", b"401"))
            with self.assertRaisesRegex(SystemExit, "Pinned React source changed"):
                module.patch_bundle(source, target, approved)
            self.assertFalse(target.exists())
            source.write_bytes(fixture)
            with self.assertRaisesRegex(SystemExit, "Pinned React source changed"):
                module.patch_bundle(source, target)
            self.assertFalse(target.exists())
            module.patch_bundle(source, target, approved)
            self.assertEqual(target.read_text(), "height:`auto`,width:`200px`,maxWidth:`100%`,aspectRatio:`1 / 2`,backgroundSize:`contain`,backgroundImage:muscular_system_back.svg")


if __name__ == "__main__":
    unittest.main()
