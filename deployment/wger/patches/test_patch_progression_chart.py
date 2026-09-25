import ast
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


def wave_one_source() -> str:
    assignment = next(
        node for node in ast.parse(MODULE.with_name("patch_ux_wave1.py").read_text()).body
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "wave_one" for target in node.targets
        )
    )
    return ast.literal_eval(assignment.value)


LIVE_SETTING_FIXTURES = {
    "src/components/Routines/widgets/LogWidgets.tsx": '''import { dateToLocale, luxonDateTimeToLocale } from "@/core/lib/date";
export const ExerciseLog = (props: { exercise: Exercise, routineId: number, logEntries: WorkoutLog[] | undefined }) => {
    const initialRows: GridRowsProp = logEntries.map((logEntry: WorkoutLog) => ({
        id: logEntry.id,
        date: logEntry.date,
        repetitions: logEntry.repetitions,
        weight: logEntry.weight,
        rir: logEntry.rir,
        entry: logEntry
    }));
    const processRowUpdate = (newRow: GridRowModel) => {
        const log = newRow.entry;
        if (log !== undefined) {
            log.date = newRow.date;
            log.repetitions = newRow.repetitions;
            log.weight = newRow.weight;
            log.rir = newRow.rir;
            editLogQuery.mutate(log);
        }
        return newRow;
    };
<TimeSeriesChart data={logEntries} key={props.exercise.id} />
export const TimeSeriesChart = (props: { data: WorkoutLog[] }) => {
const formatData = (data: WorkoutLog[]) =>
    data.map((log) => {
        return {
            id: log.id,
            value: log.weight,
            time: log.date.getTime(),
            entry: log,
        };
    });
    // Group by rep count
    //
    // We draw series based on the same reps, as otherwise the chart wouldn't
    // make much sense
    const result: Map<number, WorkoutLog[]> = props.data.reduce(function (r, a) {
        r.set(a.repetitions, r.get(a.repetitions) || []);
        r.get(a.repetitions)!.push(a);
        return r;
    }, new Map());
                    tickFormatter={unixTime => luxonDateTimeToLocale(DateTime.fromMillis(unixTime))}
                    unit="kg"
                        const formattedData = formatData(value);
                <Tooltip content={ExerciseLogTooltip} />
const ExerciseLogTooltip = ({ active, payload }: TooltipContentProps<ValueType, NameType>) => {
    if (active) {
        // TODO: translate rir
        let rir = '';
        if (payload?.[1].payload?.entry.rir) {
            rir = `, ${payload?.[1].payload?.entry.rir} RiR`;
        }

        return <Card>
            <CardContent>
                <Typography variant="body1">
                    {luxonDateTimeToLocale(DateTime.fromMillis(payload?.[0].value as number))}
                </Typography>

                <Typography variant="body2">
                    {payload?.[1].payload?.entry.repetitions} × {payload?.[1].value}{payload?.[1].unit}{rir}
                </Typography>
            </CardContent>
        </Card>;
    }
    return null;
};
name={key?.toString()}''',
    "src/components/Routines/widgets/WaveOne.tsx": wave_one_source(),
    "src/components/Routines/screens/Detail/WorkoutLogs.tsx": '''import { ExerciseLog } from "@/components/Routines/widgets/LogWidgets";
    // Group by exercise
    let groupedWorkoutLogs: Map<number, WorkoutLog[]> = new Map();

    groupedWorkoutLogs = routineLogDataQuery.data!.reduce((r, routineLogData) => {
        routineLogData.logs.forEach(log => {
            const exerciseId = log.exerciseId;
            r.set(exerciseId, r.get(exerciseId) || []);
            r.get(exerciseId)!.push(log);
        });
        return r;
    }, groupedWorkoutLogs);

    const plannedDays = routineQuery.data!.dayDataCurrentIterationFiltered;
                            <ExerciseLog
                                key={exercise.id}
                                routineId={routineId}
                                exercise={exercise}
                                logEntries={groupedWorkoutLogs.get(exercise.id!)!}
                            />
                    <ExerciseLog
                        key={exercise.id}
                        routineId={routineId}
                        exercise={exercise}
                        logEntries={groupedWorkoutLogs.get(exercise.id!)}
                    />''',
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
    def test_session_dates_and_exact_exercise_history_by_set_number(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            apply_patch(root)

            widgets = (root / "src/components/Routines/widgets/LogWidgets.tsx").read_text()
            wave = (root / "src/components/Routines/widgets/WaveOne.tsx").read_text()
            range_module = root / "src/components/Routines/widgets/progressionChartRange.ts"
            workout_logs = (root / "src/components/Routines/screens/Detail/WorkoutLogs.tsx").read_text()
            self.assertTrue(range_module.is_file())
            self.assertIn("filterProgressionChartData(props.data)", widgets)
            self.assertIn("const counters = new Map<string, number>()", widgets)
            self.assertIn("const session = log.sessionId ?? log.date.toDateString()", widgets)
            self.assertIn("const setNumber = (counters.get(session) ?? 0) + 1", widgets)
            self.assertNotIn("a.iteration ?? 1", widgets)
            self.assertIn("name={`Set ${key}`}", widgets)
            self.assertIn("chartEntries={", wave)
            self.assertNotIn("candidate.routineId === session.routineId", wave)
            self.assertIn("log.exerciseId === logs[0].exerciseId", wave)
            self.assertIn("a.date.getTime() - b.date.getTime()", wave)

            self.assertIn("withWorkoutDate", workout_logs)
            self.assertIn("const workoutDates = new Map", workout_logs)
            self.assertIn("groupedWorkoutChartLogs", workout_logs)
            self.assertEqual(workout_logs.count("displayDates={workoutDates}"), 2)
            self.assertEqual(workout_logs.count("chartEntries={groupedWorkoutChartLogs.get(exercise.id!)}"), 2)
            start = widgets.index("    const counters =")
            with_date = wave.split("const withDate = ", 1)[1].split(";\n", 1)[0]
            with_date = with_date.replace("(log: WorkoutLog, date: Date)", "(log, date)").replace(" as WorkoutLog", "")
            grouping = widgets[start:widgets.index("    });", start) + len("    });")]
            grouping = grouping.replace("new Map<string, number>()", "new Map()")
            grouping = grouping.replace("new Map<number, WorkoutLog[]>()", "new Map()")
            rows = widgets[widgets.index("logEntries.map"):widgets.index("    }));") + len("    }))")]
            rows = rows.replace(": WorkoutLog", "").replace("get(logEntry.id)!", "get(logEntry.id)")
            save_start = widgets.index("    const processRowUpdate")
            save = widgets[save_start:widgets.index("    };", save_start) + len("    };")]
            save = save.replace("(newRow: GridRowModel, oldRow: GridRowModel)", "(newRow, oldRow)")
            formatter = widgets[widgets.index("const formatData"):widgets.index("    // A set")]
            formatter = formatter.replace(": WorkoutLog[]", "")
            chart = wave.split("chartEntries={", 1)[1].split("}\n", 1)[0]
            history = wave.split("const logs = useMemo(() => ", 1)[1].split(", [sessionsQuery.data", 1)[0]
            legend_expression = widgets[widgets.index("name={") + 6:widgets.rindex("}")]
            overview_grouping = workout_logs[workout_logs.index("    const withWorkoutDate ="):workout_logs.index("    const plannedDays")]
            overview_grouping = overview_grouping.replace("(log: WorkoutLog, date: Date)", "(log, date)").replace(" as WorkoutLog", "")
            overview_grouping = overview_grouping.replace(": Map<number, WorkoutLog[]>", "").replace("new Map<string, Date>()", "new Map()")
            overview_grouping = overview_grouping.replace("data!", "data").replace("get(exerciseId)!", "get(exerciseId)")
            script = r'''const assert = require('node:assert/strict');
class Log {
    constructor(values) { Object.assign(this, values); }
    toJson() { return {id: this.id, date: this.date.toISOString(), session: this.sessionId, weight: this.weight}; }
}
const day = date => date.toISOString().slice(0, 10);
const withDate = WITH_DATE;
const sessionDataRows = [
    {id: 'sep', routineId: 10, datetimeStart: new Date('2026-09-03T12:00:00Z')},
    {id: 'aug', routineId: 10, datetimeStart: new Date('2026-08-20T12:00:00Z')},
    {id: 'other-block', routineId: 20, datetimeStart: new Date('2026-09-03T18:00:00Z')},
];
for (const dates of [['2026-09-20', '2026-09-20', '2026-09-20'], ['1999-01-01', '2099-01-01', '1970-01-01']]) {
    const logs = sessionDataRows.flatMap((session, index) => [1, 2, 3].map((set, i) => new Log({
        id: `${session.id}-${set}`, sessionId: session.id, exerciseId: 42,
        date: new Date(dates[i]), weight: 45 + index * 5 + i * 5,
        iteration: 1, repetitions: 8,
        exerciseObj: {id: 42, name: 'Bench press'},
    })));
    logs.push(new Log({id: 'similar', sessionId: 'aug', exerciseId: 43,
        date: new Date('2099-01-01'), weight: 999, exerciseObj: {id: 43, name: 'Bench press'}}));
    const orphan = new Log({id: 'orphan', sessionId: 'missing', exerciseId: 42, date: new Date('2000-01-01')});
    logs.push(orphan);
    const result = sessionDataRows.map(session => ({...session, logs: logs.filter(log => log.sessionId === session.id)}));
    const sessionsQuery = {data: result};
    for (const session of result) {
        const logEntries = session.logs;
        const props = {displayDate: session.datetimeStart};
        const rows = ROWS;
        assert.deepEqual(rows.map(row => day(row.date)), logEntries.map(() => day(session.datetimeStart)));
        assert.ok(rows.every(row => row.date !== session.datetimeStart));
        assert.ok(rows.every(row => row.entry instanceof Log && row.entry.toJson().id === row.id));
        const originalDate = logEntries[0].date.toISOString();
        const newRow = {...rows[0], weight: rows[0].weight + 2.5};
        const oldRow = rows[0];
        let saved;
        const editLogQuery = {mutate: log => { saved = log.toJson(); }};
SAVE
        processRowUpdate(newRow, oldRow);
        assert.equal(saved.date, originalDate);
        assert.equal(saved.weight, newRow.weight);
        assert.equal(logEntries[0].date.toISOString(), originalDate);
        const changedDate = new Date('2026-09-04T12:00:00Z');
        processRowUpdate({...newRow, date: changedDate}, newRow);
        assert.equal(saved.date, changedDate.toISOString());
    }
    const session = result.find(item => item.id === 'sep');
    const blockChart = (() => { const logs = session.logs; return CHART; })();
    assert.deepEqual(blockChart.map(log => log.id), ['aug-1', 'aug-2', 'aug-3', 'sep-1', 'sep-2', 'sep-3', 'other-block-1', 'other-block-2', 'other-block-3']);
    assert.ok(blockChart.every(log => log instanceof Log));
    for (const item of blockChart) {
        const owner = result.find(session => session.id === item.sessionId);
        assert.ok(item.date !== owner.datetimeStart);
        assert.ok(item instanceof Log);
    }
    const exerciseId = 42;
    const history = HISTORY;
    assert.deepEqual(history.map(log => log.id), blockChart.map(log => log.id));
    assert.ok(history.every(log => log.exerciseId === exerciseId && log.exerciseObj.id === exerciseId));
    assert.deepEqual([...history].reverse().map(log => [day(log.date), log.sessionId]), [
        ...Array(3).fill(['2026-09-03', 'other-block']),
        ...Array(3).fill(['2026-09-03', 'sep']), ...Array(3).fill(['2026-08-20', 'aug'])]);
    assert.equal(day(orphan.date), '2000-01-01');
    for (const data of [blockChart, history]) {
        const props = {data};
        const chartData = props.data;
GROUPING
FORMATTER
        assert.deepEqual(Array.from(result.keys()), [1, 2, 3]);
        for (const [key, entries] of result) {
            assert.equal(LEGEND, `Set ${key}`);
            assert.deepEqual(entries.map(log => log.id), data.filter(log => log.id.endsWith(`-${key}`)).map(log => log.id));
            assert.deepEqual(formatData(entries).map(point => day(new Date(point.time))),
                ['2026-08-20', '2026-09-03', '2026-09-03']);
        }
    }
    const routineLogDataQuery = {data: result.map(session => ({session, logs: session.logs}))};
ROUTINE_OVERVIEW
    const logEntries = groupedWorkoutLogs.get(42);
    const props = {displayDates: workoutDates};
    const overviewRows = ROWS;
    assert.deepEqual(overviewRows.map(row => day(row.date)), [
        ...Array(3).fill('2026-09-03'), ...Array(3).fill('2026-08-20'), ...Array(3).fill('2026-09-03')]);
    assert.ok(overviewRows.every((row, index) => row.entry === logEntries[index]));
    assert.deepEqual(groupedWorkoutChartLogs.get(42).map(log => day(log.date)), [
        ...Array(3).fill('2026-08-20'), ...Array(3).fill('2026-09-03'), ...Array(3).fill('2026-09-03')]);
    const overviewChartLogs = groupedWorkoutChartLogs.get(42);
    const overviewSessions = new Map(result.map(session => [session.id, session]));
    assert.ok(overviewChartLogs.every(log => log instanceof Log));
    assert.ok(overviewChartLogs.every(log => log.date !== overviewSessions.get(log.sessionId).datetimeStart));
    assert.ok(groupedWorkoutChartLogs.get(42).every(log => !logEntries.includes(log)));
}
console.log('session tables, block charts, exercise history: dates/bindings/Set 1–3 passed');'''
            for marker, code in {
                "WITH_DATE": with_date,
                "ROWS": rows,
                "SAVE": save,
                "CHART": chart,
                "HISTORY": history,
                "GROUPING": grouping,
                "FORMATTER": formatter,
                "ROUTINE_OVERVIEW": overview_grouping,
                "LEGEND": legend_expression,
            }.items():
                script = script.replace(marker, code)
            output = subprocess.run(["node", "-e", script], text=True, capture_output=True)
            self.assertEqual(output.returncode, 0, output.stderr)

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

    def test_reviewed_bundle_digest_matches_real_pinned_build(self):
        spec = importlib.util.spec_from_file_location("muscle_patch_for_real_build", MUSCLE_PATCH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        real_build_digest = "5b86d77381357d4e40c45b7f2646268237cb8aaf9a0387e77012a7879eec70f9"
        self.assertIn(real_build_digest, module.APPROVED_SHA256)
        for rejected_digest in (
            "9af475d575a2ccafb8af916a3e8689086dd95c079b5551c1e222776a827387be",
            "0ca0f1ed195bf4fbea25a1438533e8ab42413b100b29e05e653f7bef935bc511",
            "21f0ed5b28e25ac1d430763e59a20613b3e24e4d11f90bed5202746c78d96268",
        ):
            with self.subTest(digest=rejected_digest):
                self.assertNotIn(rejected_digest, module.APPROVED_SHA256)

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
