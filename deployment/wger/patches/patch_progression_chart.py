#!/usr/bin/env python3
"""Patch the pinned React source so session charts show per-set block history."""
from pathlib import Path
import sys

root = Path(sys.argv[1])


def replace(path: str, old: str, new: str) -> None:
    target = root / path
    text = target.read_text()
    if text.count(old) != 1:
        raise SystemExit(f"Expected one progression-chart anchor in {path}, found {text.count(old)}")
    target.write_text(text.replace(old, new))


widgets = "src/components/Routines/widgets/LogWidgets.tsx"
replace(
    widgets,
    "export const ExerciseLog = (props: { exercise: Exercise, routineId: number, logEntries: WorkoutLog[] | undefined }) => {",
    "export const ExerciseLog = (props: { exercise: Exercise, routineId: number, logEntries: WorkoutLog[] | undefined, chartEntries?: WorkoutLog[] }) => {",
)
replace(
    widgets,
    "<TimeSeriesChart data={logEntries} key={props.exercise.id} />",
    "<TimeSeriesChart data={props.chartEntries ?? logEntries} key={props.exercise.id} />",
)
replace(
    widgets,
    """    // Group by rep count
    //
    // We draw series based on the same reps, as otherwise the chart wouldn't
    // make much sense
    const result: Map<number, WorkoutLog[]> = props.data.reduce(function (r, a) {
        r.set(a.repetitions, r.get(a.repetitions) || []);
        r.get(a.repetitions)!.push(a);
        return r;
    }, new Map());""",
    """    // A set keeps the same colour across workout dates so its progression is visible.
    const result: Map<number, WorkoutLog[]> = props.data.reduce(function (r, a) {
        const setNumber = a.iteration ?? 1;
        r.set(setNumber, r.get(setNumber) || []);
        r.get(setNumber)!.push(a);
        return r;
    }, new Map());""",
)
replace(widgets, 'name={key?.toString()}', 'name={`Set ${key}`}')

wave = "src/components/Routines/widgets/WaveOne.tsx"
replace(
    wave,
    """                <ExerciseLog exercise={logs[0].exerciseObj!} routineId={session.routineId} logEntries={logs} />""",
    """                <ExerciseLog
                    exercise={logs[0].exerciseObj!}
                    routineId={session.routineId}
                    logEntries={logs}
                    chartEntries={(sessionsQuery.data ?? []).flatMap(candidate => candidate.routineId === session.routineId
                        ? candidate.logs.filter(log => log.exerciseId === logs[0].exerciseId)
                        : []).sort((a, b) => a.date.getTime() - b.date.getTime())}
                />""",
)

print("Applied per-set block progression chart patch")
