import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

MODULE = Path(__file__).with_name("patch_progression_chart.py")


class ProgressionChartPatchTest(unittest.TestCase):
    def test_charts_block_history_by_set_number(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixtures = {
                "src/components/Routines/widgets/LogWidgets.tsx": '''export const ExerciseLog = (props: { exercise: Exercise, routineId: number, logEntries: WorkoutLog[] | undefined }) => {
<TimeSeriesChart data={logEntries} key={props.exercise.id} />
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
            }
            for relative, content in fixtures.items():
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content)

            with patch("sys.argv", [str(MODULE), str(root)]):
                spec = importlib.util.spec_from_file_location("progression_chart_patch", MODULE)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)

            widgets = (root / "src/components/Routines/widgets/LogWidgets.tsx").read_text()
            wave = (root / "src/components/Routines/widgets/WaveOne.tsx").read_text()
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


if __name__ == "__main__":
    unittest.main()
