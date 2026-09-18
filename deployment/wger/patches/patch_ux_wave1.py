#!/usr/bin/env python3
"""Apply the first wger UX wave to the pinned React source package.

The npm package includes its corresponding TypeScript source. We patch that source,
then build a reviewed main.js override; the live stack is never touched here.
"""
from pathlib import Path
import sys

root = Path(sys.argv[1])


def replace(path: str, old: str, new: str) -> None:
    target = root / path
    text = target.read_text()
    if text.count(old) != 1:
        raise SystemExit(f"Expected one wave-one anchor in {path}, found {text.count(old)}")
    target.write_text(text.replace(old, new))


wave_one = r'''import EditIcon from "@mui/icons-material/Edit";
import { Box, Button, Card, CardActionArea, CardContent, Chip, CircularProgress, Divider, Stack, TextField, Typography } from "@mui/material";
import { addLogs } from "@/components/Routines/api/workoutLogs";
import { WorkoutLog } from "@/components/Routines/models/WorkoutLog";
import { WorkoutSession } from "@/components/Routines/models/WorkoutSession";
import { useFetchRoutineWeighUnitsQuery } from "@/components/Routines/queries/units";
import { useSessionsQuery } from "@/components/Routines/queries/sessions";
import { ExerciseLog, TimeSeriesChart } from "@/components/Routines/widgets/LogWidgets";
import { QueryKey } from "@/core/lib/consts";
import { dateToLocale } from "@/core/lib/date";
import { useQueryClient } from "@tanstack/react-query";
import React, { useEffect, useMemo, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";

const number = (value: number | null) => value === null ? "—" : Number.isInteger(value) ? value.toString() : value.toFixed(1);
const sessionName = (session: WorkoutSession) => {
    const sourceTitle = session.notes?.match(/^Original source title:\s*(.+)$/im)?.[1]?.trim();
    return sourceTitle || session.dayObj?.name || session.notes?.split(/\r?\n/, 1)[0]?.trim() || session.logs[0]?.exerciseObj?.getTranslation().name || "Workout";
};
const sessionUrl = (lang: string, id: string | null) => `/${lang}/routine/session/${id}`;

const previousLogs = (log: WorkoutLog, sessions: WorkoutSession[]) => {
    const current = sessions.find(session => session.id === log.sessionId);
    if (!current) return [];
    return sessions
        .filter(session => session.datetimeStart < current.datetimeStart && session.logs.some(entry => entry.exerciseId === log.exerciseId))
        .sort((a, b) => b.datetimeStart.getTime() - a.datetimeStart.getTime())[0]
        ?.logs.filter(entry => entry.exerciseId === log.exerciseId)
        .sort((a, b) => (a.iteration ?? 0) - (b.iteration ?? 0)) ?? [];
};

export const SetSummary = ({ log, sessions }: { log: WorkoutLog, sessions: WorkoutSession[] }) => {
    const weightUnits = useFetchRoutineWeighUnitsQuery();
    const unit = log.weightUnitObj?.name ?? weightUnits.data?.find(item => item.id === log.weightUnitId)?.name ?? "";
    const prior = previousLogs(log, sessions);
    const index = Math.max(0, [...(sessions.find(session => session.id === log.sessionId)?.logs ?? [])]
        .filter(entry => entry.exerciseId === log.exerciseId)
        .sort((a, b) => (a.iteration ?? 0) - (b.iteration ?? 0))
        .findIndex(entry => entry.id === log.id));
    const previous = prior[index] ?? prior.at(-1);
    const weightDelta = previous?.weight != null && log.weight != null ? log.weight - previous.weight : null;
    const repDelta = previous?.repetitions != null && log.repetitions != null ? log.repetitions - previous.repetitions : null;
    const delta = weightDelta !== null && weightDelta !== 0
        ? { value: weightDelta, unit }
        : repDelta !== null && repDelta !== 0 ? { value: repDelta, unit: "reps" } : null;
    const up = delta !== null && delta.value > 0;
    return <Stack direction={{ xs: "column", sm: "row" }} spacing={0.75} sx={{ alignItems: { sm: "center" } }}>
        <Typography component="span">{number(log.repetitions)} reps × {number(log.weight)}{unit ? ` ${unit}` : ""}</Typography>
        {delta && <Chip size="small" color={up ? "success" : "error"} label={`${up ? "▲" : "▼"} ${delta.value > 0 ? "+" : ""}${number(delta.value)}${delta.unit ? ` ${delta.unit}` : ""} vs last time`} />}
    </Stack>;
};

export const WorkoutsOverview = () => {
    const { lang = "en" } = useParams();
    const sessionsQuery = useSessionsQuery();
    const [search, setSearch] = useState("");
    const [visible, setVisible] = useState(30);
    const sentinel = useRef<HTMLDivElement>(null);
    const sessions = useMemo(() => [...(sessionsQuery.data ?? [])]
        .sort((a, b) => b.datetimeStart.getTime() - a.datetimeStart.getTime())
        .filter(session => `${sessionName(session)} ${session.logs.map(log => log.exerciseObj?.getTranslation().name ?? "").join(" ")}`.toLowerCase().includes(search.toLowerCase())), [sessionsQuery.data, search]);
    useEffect(() => {
        const node = sentinel.current;
        if (!node) return;
        const observer = new IntersectionObserver(entries => entries[0].isIntersecting && setVisible(count => Math.min(count + 30, sessions.length)));
        observer.observe(node);
        return () => observer.disconnect();
    }, [sessions.length]);
    useEffect(() => setVisible(30), [search]);
    if (sessionsQuery.isLoading) return <CircularProgress />;
    if (sessionsQuery.isError) return <Typography color="error">Could not load workouts.</Typography>;
    return <Box sx={{ maxWidth: 900, mx: "auto", p: 2 }}>
        <Typography variant="h4" sx={{ mb: 2 }}>All workouts</Typography>
        <TextField fullWidth label="Search workout or exercise" value={search} onChange={event => setSearch(event.target.value)} sx={{ mb: 2 }} />
        <Stack spacing={1.5}>
            {sessions.slice(0, visible).map(session => <Card key={session.id} variant="outlined">
                <CardActionArea component={Link} to={sessionUrl(lang, session.id)}>
                    <CardContent>
                        <Stack direction="row" sx={{ justifyContent: "space-between", gap: 2 }}>
                            <Box>
                                <Typography variant="h6">{sessionName(session)}</Typography>
                                <Typography color="text.secondary">{dateToLocale(session.datetimeStart)} · {session.logs.length} sets</Typography>
                            </Box>
                            <Typography color="text.secondary">View</Typography>
                        </Stack>
                        {session.logs.slice(0, 2).map(log => <SetSummary key={log.id} log={log} sessions={sessionsQuery.data ?? []} />)}
                    </CardContent>
                </CardActionArea>
            </Card>)}
            {sessions.length === 0 && <Typography color="text.secondary">No workouts match this search.</Typography>}
            <div ref={sentinel} />
        </Stack>
    </Box>;
};

export const SessionDetail = () => {
    const { sessionId = "" } = useParams();
    const sessionsQuery = useSessionsQuery();
    const queryClient = useQueryClient();
    const session = sessionsQuery.data?.find(item => item.id === sessionId);
    const [adding, setAdding] = useState(false);
    if (sessionsQuery.isLoading) return <CircularProgress />;
    if (!session) return <Typography color="error">Workout session not found.</Typography>;
    const grouped = new Map<number, WorkoutLog[]>();
    session.logs.forEach(log => grouped.set(log.exerciseId, [...(grouped.get(log.exerciseId) ?? []), log]));
    const addSet = async (last: WorkoutLog) => {
        setAdding(true);
        try {
            await addLogs([{ date: session.datetimeStart.toISOString(), iteration: last.iteration, exercise: last.exerciseId, session: session.id, routine: session.routineId, slot_entry: last.slotEntryId, repetitions_unit: last.repetitionUnitId, repetitions: last.repetitions, weight_unit: last.weightUnitId, weight: last.weight, rir: last.rir, rest: last.restTime }]);
            await queryClient.invalidateQueries({ queryKey: [QueryKey.SESSIONS_FULL] });
        } finally { setAdding(false); }
    };
    return <Box sx={{ maxWidth: 1100, mx: "auto", p: 2 }}>
        <Typography variant="h4">{sessionName(session)}</Typography>
        <Typography color="text.secondary" sx={{ mb: 2 }}>{dateToLocale(session.datetimeStart)} · stable session {session.id}</Typography>
        <Button startIcon={<EditIcon />} href="#edit" variant="contained" sx={{ mb: 2 }}>Edit workout sets</Button>
        <Divider />
        <Box id="edit">
            {Array.from(grouped.values()).map(logs => <Box key={logs[0].exerciseId} sx={{ mb: 3 }}>
                <ExerciseLog exercise={logs[0].exerciseObj!} routineId={session.routineId} logEntries={logs} />
                <Button disabled={adding} onClick={() => addSet(logs.at(-1)!)}>+ Add set</Button>
            </Box>)}
        </Box>
    </Box>;
};

export const ExerciseProgression = ({ exerciseId }: { exerciseId: number }) => {
    const { lang = "en" } = useParams();
    const sessionsQuery = useSessionsQuery();
    const logs = useMemo(() => (sessionsQuery.data ?? []).flatMap(session => session.logs)
        .filter(log => log.exerciseId === exerciseId)
        .sort((a, b) => a.date.getTime() - b.date.getTime()), [sessionsQuery.data, exerciseId]);
    if (sessionsQuery.isLoading) return <CircularProgress />;
    return <Box sx={{ mt: 4 }}>
        <Typography variant="h5" sx={{ mb: 1 }}>Your progression</Typography>
        {logs.length === 0 ? <Typography color="text.secondary">No recorded sets for this exercise yet.</Typography> : <>
            <TimeSeriesChart data={logs} />
            <Stack divider={<Divider />}>
                {[...logs].reverse().map(log => <Box key={log.id} sx={{ py: 1 }}>
                    <Typography component={Link} to={sessionUrl(lang, log.sessionId)} sx={{ fontWeight: 600 }}>{dateToLocale(log.date)}</Typography>
                    <SetSummary log={log} sessions={sessionsQuery.data ?? []} />
                </Box>)}
            </Stack>
        </>}
    </Box>;
};
'''
wave_one_path = root / "src/components/Routines/widgets/WaveOne.tsx"
wave_one_path.parent.mkdir(parents=True, exist_ok=True)
wave_one_path.write_text(wave_one)

replace("src/routes.tsx",
'''    WorkoutLogs,
    WorkoutStats
} from "@/components/Routines";''',
'''    WorkoutLogs,
    WorkoutStats
} from "@/components/Routines";
import { SessionDetail, WorkoutsOverview } from "@/components/Routines/widgets/WaveOne";''')
replace("src/routes.tsx",
'''                    <Route path="calendar" element={<Calendar />} />
                    <Route path="add" element={<RoutineAdd />} />''',
'''                    <Route path="calendar" element={<Calendar />} />
                    <Route path="workouts" element={<WorkoutsOverview />} />
                    <Route path="session/:sessionId" element={<SessionDetail />} />
                    <Route path="add" element={<RoutineAdd />} />''')

entries = "src/components/Calendar/Components/Entries.tsx"
replace(entries,
'''import type { DayProps } from "./CalendarComponent";''',
'''import type { DayProps } from "./CalendarComponent";
import { SetSummary } from "@/components/Routines/widgets/WaveOne";
import { useSessionsQuery } from "@/components/Routines";
import EditIcon from "@mui/icons-material/Edit";
import { Button, Stack } from "@mui/material";''')
replace(entries,
'''    const [t] = useTranslation();
    const displayWeightUnit = useDisplayWeightUnit();''',
'''    const [t] = useTranslation();
    const lang = document.documentElement.lang || "en";
    const allSessions = useSessionsQuery();
    const displayWeightUnit = useDisplayWeightUnit();''')
replace(entries,
'''                            <List sx={{ pl: 4, pt: 0 }}>
                                {session.logs.map((log) => (
                                    <ListItem key={log.id} dense>
                                        <ListItemText
                                            primary={log.exerciseObj?.getTranslation().name}
                                            secondary={`${log.repetitions} × ${log.weight} `}
                                        />
                                    </ListItem>))}
                            </List>''',
'''                            <List sx={{ pl: 4, pt: 0 }}>
                                {session.logs.map((log) => (
                                    <ListItem key={log.id} dense>
                                        <ListItemText primary={log.exerciseObj?.getTranslation().name} secondary={<SetSummary log={log} sessions={allSessions.data ?? []} />} />
                                    </ListItem>))}
                                <ListItem>
                                    <Stack direction="row" spacing={1}>
                                        <Button href={`/${lang}/routine/session/${session.id}`}>View workout</Button>
                                        <Button href={`/${lang}/routine/session/${session.id}#edit`} startIcon={<EditIcon />}>Edit sets</Button>
                                    </Stack>
                                </ListItem>
                            </List>''')
entries_test = "src/components/Calendar/Components/Entries.test.tsx"
replace(entries_test,
'''        expect(screen.getByText(/^8 × 80/)).toBeInTheDocument();
        expect(screen.getByText(/^8 × 82.5/)).toBeInTheDocument();''',
'''        expect(screen.getByText(/^8 reps × 80 kg/)).toBeInTheDocument();
        expect(screen.getByText(/^8 reps × 82.5 kg/)).toBeInTheDocument();''')
replace(entries_test,
'''        expect(screen.getByText(/^8 × 80/)).toBeInTheDocument();
    });''',
'''        expect(screen.getByText(/^8 reps × 80 kg/)).toBeInTheDocument();
    });''')


workout_menu = "src/app/layout/Header/SubMenus/WorkoutSubMenu.tsx"
replace(workout_menu,
'''                <MenuItem component={Link} to={makeLink(WgerLink.CALENDAR, i18n.language)}>
                    Calendar
                </MenuItem>''',
'''                <MenuItem component={Link} to={makeLink(WgerLink.CALENDAR, i18n.language)}>
                    Calendar
                </MenuItem>
                <MenuItem component={Link} to={`/${i18n.language}/routine/workouts`}>
                    All workouts
                </MenuItem>''')

calendar = "src/components/Calendar/Components/CalendarComponent.tsx"
replace(calendar,
'''import React, { useEffect, useMemo, useState } from 'react';
import { useTranslation } from "react-i18next";''',
'''import React, { useEffect, useMemo, useState } from 'react';
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";''')
replace(calendar,
'''    const currentDate = useMemo(() => new Date(), []);
    const [currentMonth, setCurrentMonth] = useState(currentDate.getMonth());
    const [currentYear, setCurrentYear] = useState(currentDate.getFullYear());''',
'''    const currentDate = useMemo(() => new Date(), []);
    const [searchParams, setSearchParams] = useSearchParams();
    const requestedDate = useMemo(() => {
        const value = searchParams.get("date");
        const parsed = value ? new Date(`${value}T12:00:00`) : currentDate;
        return Number.isNaN(parsed.getTime()) ? currentDate : parsed;
    }, [currentDate, searchParams]);
    const [currentMonth, setCurrentMonth] = useState(requestedDate.getMonth());
    const [currentYear, setCurrentYear] = useState(requestedDate.getFullYear());''')
replace(calendar,
'''        date: currentDate,''',
'''        date: requestedDate,''')
replace(calendar,
'''    const [selectedDay, setSelectedDay] = useState<DayProps>(days.find(day => isSameDay(day.date, currentDate)) || defaultDay);''',
'''    const [selectedDay, setSelectedDay] = useState<DayProps>(days.find(day => isSameDay(day.date, requestedDate)) || defaultDay);''')
replace(calendar,
'''            const todayWithData = days.find(day => isSameDay(day.date, currentDate));
            if (todayWithData) {
                setSelectedDay(todayWithData);
            }''',
'''            const requestedDay = days.find(day => isSameDay(day.date, requestedDate));
            if (requestedDay) {
                setSelectedDay(requestedDay);
            }''')
replace(calendar,
'''    }, [currentDate, days, isSuccess]);''',
'''    }, [days, isSuccess, requestedDate]);''')
replace(calendar,
'''    const handleDayClick = (day: DayProps) => {
        setSelectedDay(day);
    };''',
'''    const handleDayClick = (day: DayProps) => {
        setSelectedDay(day);
        const localDate = `${day.date.getFullYear()}-${String(day.date.getMonth() + 1).padStart(2, "0")}-${String(day.date.getDate()).padStart(2, "0")}`;
        setSearchParams({ date: localDate }, { replace: true });
    };''')

exercise = "src/components/Exercises/screens/Detail/ExerciseDetailView.tsx"
replace(exercise,
'''import { dateTimeToLocale } from "@/core/lib/date";''',
'''import { dateTimeToLocale } from "@/core/lib/date";
import { ExerciseProgression } from "@/components/Routines/widgets/WaveOne";''')
replace(exercise,
'''                <PaddingBox />
            </Grid>''',
'''                <ExerciseProgression exerciseId={exercise.id!} />
                <PaddingBox />
            </Grid>''')

print("Applied wger UX wave 1 to pinned React source")
