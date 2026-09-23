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


range_module = root / "src/components/Routines/widgets/progressionChartRange.ts"
range_module.write_text('''export const PROGRESSION_CHART_RANGES = [
    { value: "1m", label: "1 month", months: 1 },
    { value: "3m", label: "3 months", months: 3 },
    { value: "6m", label: "6 months", months: 6 },
    { value: "1y", label: "1 year", months: 12 },
    { value: "2y", label: "2 years", months: 24 },
    { value: "3y", label: "3 years", months: 36 },
    { value: "all", label: "All time", months: null },
] as const;
export type ProgressionChartRange = typeof PROGRESSION_CHART_RANGES[number]["value"];
const STORAGE_KEY = "wger.progressionChartRange";
const DEFAULT_RANGE: ProgressionChartRange = "6m";

export const loadProgressionChartRange = (): ProgressionChartRange => {
    try {
        const stored = window.localStorage.getItem(STORAGE_KEY);
        return PROGRESSION_CHART_RANGES.some(option => option.value === stored)
            ? stored as ProgressionChartRange
            : DEFAULT_RANGE;
    } catch { return DEFAULT_RANGE; }
};

export const saveProgressionChartRange = (range: ProgressionChartRange) => {
    try { window.localStorage.setItem(STORAGE_KEY, range); } catch { /* The current page still uses the pick. */ }
};

export const filterProgressionChartData = <T extends { date: Date }>(
    data: T[],
    range: ProgressionChartRange = loadProgressionChartRange(),
    now: Date = new Date(),
): T[] => {
    const months = PROGRESSION_CHART_RANGES.find(option => option.value === range)!.months;
    if (months === null) return data;
    const cutoff = new Date(now);
    const day = cutoff.getDate();
    cutoff.setDate(1);
    cutoff.setMonth(cutoff.getMonth() - months);
    cutoff.setDate(Math.min(day, new Date(cutoff.getFullYear(), cutoff.getMonth() + 1, 0).getDate()));
    return data.filter(entry => entry.date >= cutoff && entry.date <= now);
};
''')


widgets = "src/components/Routines/widgets/LogWidgets.tsx"
replace(widgets, 'import { dateToLocale, luxonDateTimeToLocale } from "@/core/lib/date";', 'import { dateToLocale, luxonDateTimeToLocale } from "@/core/lib/date";\nimport { filterProgressionChartData } from "@/components/Routines/widgets/progressionChartRange";')
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
replace(widgets, 'export const TimeSeriesChart = (props: { data: WorkoutLog[] }) => {', 'export const TimeSeriesChart = (props: { data: WorkoutLog[] }) => {\n\n    const chartData = filterProgressionChartData(props.data);')
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
    const counters = new Map<string, number>();
    const result = new Map<number, WorkoutLog[]>();
    chartData.forEach(log => {
        const session = log.sessionId ?? log.date.toDateString();
        const setNumber = (counters.get(session) ?? 0) + 1;
        counters.set(session, setNumber);
        result.set(setNumber, [...(result.get(setNumber) ?? []), log]);
    });""",
)
replace(widgets, 'name={key?.toString()}', 'name={`Set ${key}`}')

preferences = "src/pages/Preferences/index.tsx"
replace(
    preferences,
    "import React from 'react';",
    '''import { MenuItem, Stack, TextField, Typography } from "@mui/material";
import { loadProgressionChartRange, PROGRESSION_CHART_RANGES, ProgressionChartRange, saveProgressionChartRange } from "@/components/Routines/widgets/progressionChartRange";
import React, { useState } from 'react';''',
)
replace(
    preferences,
    '''        <div>
            Preferences Page
        </div>''',
    '''        <Stack spacing={2} sx={{ maxWidth: 560, p: 3 }}>
            <Typography variant="h4">Settings</Typography>
            <TextField
                select
                label="Exercise chart range"
                value={range}
                onChange={event => {
                    const selected = event.target.value as ProgressionChartRange;
                    setRange(selected);
                    saveProgressionChartRange(selected);
                }}
                helperText="Changes are saved automatically and apply to every exercise chart."
            >
                {PROGRESSION_CHART_RANGES.map(option => <MenuItem key={option.value} value={option.value}>{option.label}</MenuItem>)}
            </TextField>
        </Stack>''',
)
replace(
    preferences,
    "export const Preferences = () => {",
    "export const Preferences = () => {\n    const [range, setRange] = useState<ProgressionChartRange>(loadProgressionChartRange);",
)

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
