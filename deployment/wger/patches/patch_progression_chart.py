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
range_module.write_text(r'''export const PROGRESSION_CHART_RANGES = [
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

export const mountProgressionChartRangeSetting = () => {
    if (!/^\/[a-z-]+\/user\/preferences\/?$/.test(window.location.pathname)) return;
    const content = document.getElementById("content");
    if (!content || document.getElementById("progression-chart-range")) return;
    const card = document.createElement("div");
    card.className = "card mb-3";
    const body = document.createElement("div");
    body.className = "card-body";
    const label = document.createElement("label");
    label.className = "form-label";
    label.htmlFor = "progression-chart-range";
    label.textContent = "Exercise chart range";
    const select = document.createElement("select");
    select.className = "form-select";
    select.id = "progression-chart-range";
    PROGRESSION_CHART_RANGES.forEach(option => {
        const item = document.createElement("option");
        item.value = option.value;
        item.textContent = option.label;
        select.append(item);
    });
    select.value = loadProgressionChartRange();
    select.addEventListener("change", () => saveProgressionChartRange(select.value as ProgressionChartRange));
    const help = document.createElement("div");
    help.className = "form-text";
    help.textContent = "Changes are saved automatically and apply to every exercise chart.";
    body.append(label, select, help);
    card.append(body);
    content.prepend(card);
};

export const filterProgressionChartData = <T extends { date: Date }>(
    data: T[],
    range: ProgressionChartRange = loadProgressionChartRange(),
    now: Date = new Date(),
): T[] => {
    const months = PROGRESSION_CHART_RANGES.find(option => option.value === range)?.months;
    if (months === null) return data;
    if (months === undefined) return filterProgressionChartData(data, DEFAULT_RANGE, now);
    const cutoff = new Date(now);
    const day = cutoff.getDate();
    cutoff.setDate(1);
    cutoff.setMonth(cutoff.getMonth() - months);
    cutoff.setDate(Math.min(day, new Date(cutoff.getFullYear(), cutoff.getMonth() + 1, 0).getDate()));
    return data.filter(entry => entry.date >= cutoff && entry.date <= now);
};
''')
widgets = "src/components/Routines/widgets/LogWidgets.tsx"
replace(widgets, 'import { dateToLocale, luxonDateTimeToLocale } from "@/core/lib/date";', 'import { dateToLocale } from "@/core/lib/date";\nimport { filterProgressionChartData } from "@/components/Routines/widgets/progressionChartRange";')
replace(
    widgets,
    "export const ExerciseLog = (props: { exercise: Exercise, routineId: number, logEntries: WorkoutLog[] | undefined }) => {",
    "export const ExerciseLog = (props: { exercise: Exercise, routineId: number, logEntries: WorkoutLog[] | undefined, chartEntries?: WorkoutLog[], displayDate?: Date, displayDates?: Map<string, Date> }) => {",
)
replace(widgets, "        date: logEntry.date,", "        date: props.displayDates?.has(logEntry.id) ? new Date(props.displayDates.get(logEntry.id)!.getTime()) : props.displayDate ? new Date(props.displayDate.getTime()) : logEntry.date,")
replace(
    widgets,
    "    const processRowUpdate = (newRow: GridRowModel) => {",
    "    const processRowUpdate = (newRow: GridRowModel, oldRow: GridRowModel) => {",
)
replace(widgets, "            log.date = newRow.date;", "            if (newRow.date.getTime() !== oldRow.date.getTime()) log.date = newRow.date;")
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
    });
    // Every set of one workout shares that workout's date, so hovering one dot lists them all.
    const sets = new Map<number, [number, WorkoutLog][]>();
    result.forEach((logs, set) => logs.forEach(log => sets.set(log.date.getTime(), [...(sets.get(log.date.getTime()) ?? []), [set, log]])));
    const ticks = [...sets.keys()].sort((a, b) => a - b);
    // Bodyweight moves (e.g. superman) log reps only, so chart reps when no set carries weight.
    const byReps = !chartData.some(log => log.weight !== null && log.weight !== 0);""",
)
replace(widgets, "const formatData = (data: WorkoutLog[]) =>", "const formatData = (data: WorkoutLog[], byReps = false) =>")
replace(widgets, "            value: log.weight,", "            value: byReps ? log.repetitions : log.weight,")
replace(widgets, "const formattedData = formatData(value);", "const formattedData = formatData(value, byReps);")
replace(widgets, 'unit="kg"', 'unit={byReps ? " reps" : "kg"}')
replace(
    widgets,
    "                    tickFormatter={unixTime => luxonDateTimeToLocale(DateTime.fromMillis(unixTime))}",
    """                    ticks={ticks}
                    interval={0}
                    padding={{ left: 16, right: 16 }}
                    tickFormatter={unixTime => DateTime.fromMillis(unixTime).toFormat('dd/MM/yy')}""",
)
replace(widgets, "<Tooltip content={ExerciseLogTooltip} />", "<Tooltip content={exerciseLogTooltip(sets)} />")
replace(
    widgets,
    """const ExerciseLogTooltip = ({ active, payload }: TooltipContentProps<ValueType, NameType>) => {
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
};""",
    """const exerciseLogTooltip = (sets: Map<number, [number, WorkoutLog][]>) => ({ active, payload }: TooltipContentProps<ValueType, NameType>) => {
    const time = payload?.[0]?.payload?.time as number | undefined;
    if (!active || time === undefined) {
        return null;
    }
    return <Card>
        <CardContent>
            <Typography variant="body1">{DateTime.fromMillis(time).toFormat('dd/MM/yy')}</Typography>
            {(sets.get(time) ?? []).map(([set, log]) => <Typography variant="body2" key={log.id}>
                Set {set}: {log.weight ? `${log.repetitions} × ${log.weight}kg` : `${log.repetitions} reps`}{log.rir ? `, ${log.rir} RiR` : ''}
            </Typography>)}
        </CardContent>
    </Card>;
};""",
)
replace(widgets, 'name={key?.toString()}', 'name={`Set ${key}`}')
overview = "src/components/Routines/screens/Detail/WorkoutLogs.tsx"
replace(
    overview,
    "    // Group by exercise\n    let groupedWorkoutLogs: Map<number, WorkoutLog[]> = new Map();",
    """    const withWorkoutDate = (log: WorkoutLog, date: Date) => Object.assign(Object.create(Object.getPrototypeOf(log)), log, { date: new Date(date.getTime()) }) as WorkoutLog;

    // Group by exercise
    let groupedWorkoutLogs: Map<number, WorkoutLog[]> = new Map();
    const groupedWorkoutChartLogs: Map<number, WorkoutLog[]> = new Map();
    const workoutDates = new Map<string, Date>();""",
)
replace(
    overview,
    """        routineLogData.logs.forEach(log => {
            const exerciseId = log.exerciseId;
            r.set(exerciseId, r.get(exerciseId) || []);
            r.get(exerciseId)!.push(log);
        });""",
    """        routineLogData.logs.forEach(log => {
            const exerciseId = log.exerciseId;
            const workoutDate = routineLogData.session.datetimeStart;
            r.set(exerciseId, r.get(exerciseId) || []);
            r.get(exerciseId)!.push(log);
            groupedWorkoutChartLogs.set(exerciseId, groupedWorkoutChartLogs.get(exerciseId) || []);
            groupedWorkoutChartLogs.get(exerciseId)!.push(withWorkoutDate(log, workoutDate));
            workoutDates.set(log.id, workoutDate);
        });""",
)
replace(
    overview,
    """    }, groupedWorkoutLogs);

    const plannedDays""",
    """    }, groupedWorkoutLogs);

    for (const logs of groupedWorkoutChartLogs.values()) logs.sort((a, b) => a.date.getTime() - b.date.getTime());

    const plannedDays""",
)
replace(
    overview,
    """                                logEntries={groupedWorkoutLogs.get(exercise.id!)!}
                            />""",
    """                                logEntries={groupedWorkoutLogs.get(exercise.id!)!}
                                displayDates={workoutDates}
                                chartEntries={groupedWorkoutChartLogs.get(exercise.id!)}
                            />""",
)
replace(
    overview,
    """                        logEntries={groupedWorkoutLogs.get(exercise.id!)}
                    />""",
    """                        logEntries={groupedWorkoutLogs.get(exercise.id!)}
                        displayDates={workoutDates}
                        chartEntries={groupedWorkoutChartLogs.get(exercise.id!)}
                    />""",
)


index = "src/index.tsx"
replace(
    index,
    "import App from './App';",
    '''import App from './App';
import { mountProgressionChartRangeSetting } from "@/components/Routines/widgets/progressionChartRange";''',
)
replace(
    index,
    "renderComponentShadowDom('react-page');",
    "renderComponentShadowDom('react-page');\nmountProgressionChartRangeSetting();",
)

wave = "src/components/Routines/widgets/WaveOne.tsx"
replace(wave, 'const present = (value: number | null) =>', 'const withDate = (log: WorkoutLog, date: Date) => Object.assign(Object.create(Object.getPrototypeOf(log)), log, { date: new Date(date.getTime()) }) as WorkoutLog;\nconst present = (value: number | null) =>')
replace(
    wave,
    """                <ExerciseLog exercise={logs[0].exerciseObj!} routineId={session.routineId} logEntries={logs} />""",
    """                <ExerciseLog
                    exercise={logs[0].exerciseObj!}
                    routineId={session.routineId}
                    logEntries={logs}
                    displayDate={session.datetimeStart}
                    chartEntries={(sessionsQuery.data ?? []).flatMap(candidate =>
                        candidate.logs.filter(log => log.exerciseId === logs[0].exerciseId)
                            .map(log => withDate(log, candidate.datetimeStart))
                    ).sort((a, b) => a.date.getTime() - b.date.getTime())}
                />""",
)
replace(
    wave,
    """    const logs = useMemo(() => (sessionsQuery.data ?? []).flatMap(session => session.logs)
        .filter(log => log.exerciseId === exerciseId)""",
    """    const logs = useMemo(() => (sessionsQuery.data ?? []).flatMap(session => session.logs
        .map(log => withDate(log, session.datetimeStart)))
        .filter(log => log.exerciseId === exerciseId)"""
)

print("Applied per-set block progression chart patch")
