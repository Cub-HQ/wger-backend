#!/usr/bin/env python3
"""Apply the next #83 phone-workout increment to the reviewed public React source."""
from pathlib import Path
import sys

root = Path(sys.argv[1])

def replace(path: str, old: str, new: str) -> None:
    target = root / path
    text = target.read_text()
    if text.count(old) != 1:
        raise SystemExit(f"Expected one wave-three anchor in {path}, found {text.count(old)}")
    target.write_text(text.replace(old, new))

wave_three = r'''import { NameAutocompleter } from "@/components/Exercises";
import { postExerciseVideo } from "@/components/Exercises/api/video";
import { Exercise } from "@/components/Exercises/models/exercise";
import { addSession, editSession } from "@/components/Routines/api/session";
import { addLogs } from "@/components/Routines/api/workoutLogs";
import { WorkoutSession } from "@/components/Routines/models/WorkoutSession";
import { useFetchRoutineRepUnitsQuery, useFetchRoutineWeighUnitsQuery } from "@/components/Routines/queries/units";
import { QueryKey } from "@/core/lib/consts";
import { makeHeader, makeUrl } from "@/core/lib/url";
import { useQueryClient } from "@tanstack/react-query";
import { Alert, Box, Button, Card, CardContent, Divider, Stack, TextField, Typography } from "@mui/material";
import axios from "axios";
import React, { useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";

const TIMER_KEY = (id: string) => `wger.sessionTimer.${id}`;

const localDateTimeValue = (date: Date) => `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}T${String(date.getHours()).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}`;
const parseLocalDateTime = (value: string) => { const [date, time] = value.split("T"); const [year, month, day] = date.split("-").map(Number); const [hour, minute] = time.split(":").map(Number); return new Date(year, month - 1, day, hour, minute); };

export const SessionTimer = ({ session, onSaved }: { session: WorkoutSession, onSaved: () => Promise<unknown> }) => {
    const stored = localStorage.getItem(TIMER_KEY(session.id!));
    const validStored = stored !== null && session.datetimeEnd === null && Number(stored) === session.datetimeStart.getTime();
    const [started, setStarted] = useState<number | null>(validStored ? Number(stored) : null);
    const [now, setNow] = useState(Date.now());
    useEffect(() => {
        if (!validStored) localStorage.removeItem(TIMER_KEY(session.id!));
    }, [session.id, validStored]);
    useEffect(() => {
        if (started === null) return;
        const timer = window.setInterval(() => setNow(Date.now()), 1000);
        return () => window.clearInterval(timer);
    }, [started]);
    const elapsed = started === null ? 0 : Math.max(0, Math.floor((now - started) / 1000));
    const clock = [Math.floor(elapsed / 3600), Math.floor(elapsed % 3600 / 60), elapsed % 60].map(value => value.toString().padStart(2, "0")).join(":");
    const startTimer = () => {
        if (session.datetimeEnd !== null) return;
        const timestamp = session.datetimeStart.getTime();
        localStorage.setItem(TIMER_KEY(session.id!), String(timestamp));
        setStarted(timestamp); setNow(Date.now());
    };
    const finish = async () => {
        const saved = await editSession(WorkoutSession.clone(session, { datetimeEnd: new Date() }));
        localStorage.removeItem(TIMER_KEY(session.id!)); setStarted(null);
        if (saved.datetimeEnd !== null) await onSaved();
    };
    if (session.datetimeEnd !== null) return null;
    return <Card variant="outlined" sx={{ mb: 2 }}><CardContent>
        <Typography variant="h3" sx={{ textAlign: "center" }}>{clock}</Typography>
        <Button fullWidth size="large" variant="contained" color={started === null ? "primary" : "success"} onClick={started === null ? startTimer : finish}>{started === null ? "Start now" : "Finish workout"}</Button>
        <Typography variant="caption">Timer is stored on this phone and survives lock, backgrounding and reload.</Typography>
    </CardContent></Card>;
};

export const SessionMetadataEditor = ({ session, onSaved }: { session: WorkoutSession, onSaved: () => Promise<unknown> }) => {
    const [notes, setNotes] = useState(session.notes ?? "");
    const [start, setStart] = useState(localDateTimeValue(session.datetimeStart));
    const [end, setEnd] = useState(session.datetimeEnd ? localDateTimeValue(session.datetimeEnd) : "");
    return <Card variant="outlined" sx={{ mb: 2 }}><CardContent><Stack spacing={2}>
        <Typography variant="h6">Session details</Typography>
        <TextField label="Start" type="datetime-local" value={start} onChange={event => setStart(event.target.value)} slotProps={{ inputLabel: { shrink: true } }} />
        <TextField label="End" type="datetime-local" value={end} onChange={event => setEnd(event.target.value)} slotProps={{ inputLabel: { shrink: true } }} />
        <TextField label="Notes" multiline minRows={3} value={notes} onChange={event => setNotes(event.target.value)} />
        <Button variant="contained" onClick={async () => { await editSession(WorkoutSession.clone(session, { notes, datetimeStart: parseLocalDateTime(start), datetimeEnd: end ? parseLocalDateTime(end) : null })); await onSaved(); }}>Save session details</Button>
    </Stack></CardContent></Card>;
};
export const ExerciseDemoLink = ({ exercise }: { exercise: Exercise }) => {
    const { lang = "en" } = useParams();
    const queryClient = useQueryClient();
    const video = exercise.videos?.find(item => item.isMain) ?? exercise.videos?.[0];
    const [videoUrl, setVideoUrl] = useState("");
    const [videoFile, setVideoFile] = useState<File | null>(null);
    const [status, setStatus] = useState<string | null>(null);
    const saveVideo = async () => {
        if (videoFile) await postExerciseVideo({ exerciseId: exercise.id!, author: "Athlete upload", video: videoFile });
        else if (videoUrl.trim()) await axios.post(makeUrl("video"), { exercise: exercise.id, source_url: videoUrl.trim(), license_author: "Athlete link" }, { headers: makeHeader() });
        else return;
        await queryClient.invalidateQueries({ queryKey: [QueryKey.EXERCISES] });
        setStatus("Demo saved"); setVideoUrl(""); setVideoFile(null);
    };
    return <Stack spacing={1}>
        <Stack direction="row" spacing={1} sx={{ alignItems: "center" }}>
            {exercise.mainImage && <img src={exercise.mainImage.url} alt="" width={56} height={56} style={{ objectFit: "cover", borderRadius: 6 }} />}
            <Button component={Link} to={`/${lang}/exercise/${exercise.id}/view`}>{video ? "Watch demo" : "Exercise details"}</Button>
        </Stack>
        <Stack direction={{ xs: "column", sm: "row" }} spacing={1}>
            <TextField size="small" label="Demo video link" value={videoUrl} onChange={event => setVideoUrl(event.target.value)} />
            <Button component="label" variant="outlined">Choose video<input hidden type="file" accept="video/mp4,video/webm,video/ogg" onChange={event => setVideoFile(event.target.files?.[0] ?? null)} /></Button>
            <Button variant="outlined" disabled={!videoFile && !videoUrl.trim()} onClick={saveVideo}>Save demo</Button>
        </Stack>
        {videoFile && <Typography variant="caption">{videoFile.name}</Typography>}{status && <Alert severity="success">{status}</Alert>}
    </Stack>;
};

export const QuickWorkout = () => {
    const { lang = "en" } = useParams();
    const queryClient = useQueryClient();
    const repUnits = useFetchRoutineRepUnitsQuery();
    const weightUnits = useFetchRoutineWeighUnitsQuery();
    const [exercise, setExercise] = useState<Exercise | null>(null);
    const [name, setName] = useState("Quick workout");
    const [repetitions, setRepetitions] = useState("");
    const [weight, setWeight] = useState("");
    const [durationSeconds, setDurationSeconds] = useState("");
    const [distanceKm, setDistanceKm] = useState("");
    const [maxSpeedKph, setMaxSpeedKph] = useState("");
    const [averageSpeedKph, setAverageSpeedKph] = useState("");
    const [paceSecondsPerKm, setPaceSecondsPerKm] = useState("");
    const [inclinePercent, setInclinePercent] = useState("");
    const [calories, setCalories] = useState("");
    const [saved, setSaved] = useState<string | null>(null);
    const save = async () => {
        if (!exercise) return;
        const now = new Date();
        const session = await addSession(new WorkoutSession({ id: null, dayId: null as unknown as number, routineId: null as unknown as number, notes: name, impression: "2", datetimeStart: now, datetimeEnd: null }));
        const rep = repUnits.data?.find(unit => unit.name.toLowerCase() === "repetitions");
        const seconds = repUnits.data?.find(unit => unit.name.toLowerCase() === "seconds");
        const kilometers = repUnits.data?.find(unit => unit.name.toLowerCase() === "kilometers");
        const kg = weightUnits.data?.find(unit => unit.name.toLowerCase() === "kg");
        const kph = weightUnits.data?.find(unit => unit.name.toLowerCase() === "kilometers per hour");
        const entries = [
            { repetitions_unit: rep?.id, repetitions: repetitions === "" ? null : Number(repetitions), weight_unit: kg?.id, weight: weight === "" ? null : Number(weight), average_speed: null, pace: null, incline: null, calories: null },
            { repetitions_unit: seconds?.id, repetitions: durationSeconds === "" ? null : Number(durationSeconds), weight_unit: null, weight: null, average_speed: null, pace: null, incline: null, calories: null },
            { repetitions_unit: kilometers?.id, repetitions: distanceKm === "" ? null : Number(distanceKm), weight_unit: kph?.id, weight: maxSpeedKph === "" ? null : Number(maxSpeedKph), average_speed: averageSpeedKph === "" ? null : Number(averageSpeedKph), pace: paceSecondsPerKm === "" ? null : Number(paceSecondsPerKm), incline: inclinePercent === "" ? null : Number(inclinePercent), calories: calories === "" ? null : Number(calories) },
        ].filter(entry => entry.repetitions !== null || entry.weight !== null || entry.average_speed !== null || entry.pace !== null || entry.incline !== null || entry.calories !== null);
        await addLogs(entries.map(entry => ({ date: now.toISOString(), session: session.id, iteration: null, exercise: exercise.id!, day: null, routine: null, slot_entry: null, ...entry, rir: null })));
        await queryClient.invalidateQueries({ queryKey: [QueryKey.SESSIONS_FULL] }); setSaved(session.id);
    };
    if (saved) return <Box sx={{ p: 2 }}><Alert severity="success">Workout saved.</Alert><Button component={Link} to={`/${lang}/routine/session/${saved}`}>Open workout</Button></Box>;
    return <Box sx={{ maxWidth: 700, mx: "auto", p: 2 }}><Stack spacing={2}>
        <Typography variant="h4">Log a workout</Typography><Typography>No routine picker. Add what you did and save.</Typography>
        <TextField label="Workout name" value={name} onChange={event => setName(event.target.value)} />
        <NameAutocompleter callback={setExercise} />
        {exercise && <ExerciseDemoLink exercise={exercise} />}
        <Stack direction={{ xs: "column", sm: "row" }} spacing={2}><TextField label="Reps" inputMode="decimal" value={repetitions} onChange={event => setRepetitions(event.target.value)} /><TextField label="Weight (kg)" inputMode="decimal" value={weight} onChange={event => setWeight(event.target.value)} /></Stack>
        <Typography variant="h6">Cardio / erg metrics</Typography>
        <Typography variant="caption">Time, distance, speed, pace, incline and calories are stored as structured workout data.</Typography>
        <Stack direction={{ xs: "column", sm: "row" }} spacing={2}><TextField label="Time (seconds)" value={durationSeconds} onChange={event => setDurationSeconds(event.target.value)} /><TextField label="Distance (km)" value={distanceKm} onChange={event => setDistanceKm(event.target.value)} /><TextField label="Max speed (kph)" value={maxSpeedKph} onChange={event => setMaxSpeedKph(event.target.value)} /></Stack>
        <Stack direction={{ xs: "column", sm: "row" }} spacing={2}><TextField label="Average speed (kph)" value={averageSpeedKph} onChange={event => setAverageSpeedKph(event.target.value)} /><TextField label="Pace (seconds/km)" value={paceSecondsPerKm} onChange={event => setPaceSecondsPerKm(event.target.value)} /><TextField label="Incline (%)" value={inclinePercent} onChange={event => setInclinePercent(event.target.value)} /><TextField label="Calories" value={calories} onChange={event => setCalories(event.target.value)} /></Stack>
        <Button variant="contained" disabled={!exercise} onClick={save}>Save workout</Button>
    </Stack></Box>;
};
'''
path = root / "src/components/Routines/widgets/WaveThree.tsx"
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(wave_three)

replace("src/routes.tsx",
'''import { SessionDetail, WorkoutsOverview } from "@/components/Routines/widgets/WaveOne";''',
'''import { SessionDetail, WorkoutsOverview } from "@/components/Routines/widgets/WaveOne";
import { QuickWorkout } from "@/components/Routines/widgets/WaveThree";''')
replace("src/routes.tsx",
'''                    <Route path="workouts" element={<WorkoutsOverview />} />
                    <Route path="session/:sessionId" element={<SessionDetail />} />''',
'''                    <Route path="workouts" element={<WorkoutsOverview />} />
                    <Route path="quick" element={<QuickWorkout />} />
                    <Route path="session/:sessionId" element={<SessionDetail />} />''')
replace("src/app/layout/Header/SubMenus/WorkoutSubMenu.tsx",
'''                <MenuItem component={Link} to={`/${i18n.language}/routine/workouts`}>
                    All workouts
                </MenuItem>''',
'''                <MenuItem component={Link} to={`/${i18n.language}/routine/workouts`}>
                    All workouts
                </MenuItem>
                <MenuItem component={Link} to={`/${i18n.language}/routine/quick`}>
                    Log a workout
                </MenuItem>''')

replace("src/components/Routines/models/WorkoutLog.ts",
'''    weightTarget: number | string | null;
}''',
'''    weightTarget: number | string | null;
    rest: number | string | null;
    restTarget: number | string | null;
}''')
replace("src/components/Routines/models/WorkoutSession.ts",
'''    // get the impression as a translated string''',
'''    static clone(session: WorkoutSession, overrides: Partial<WorkoutSessionParams>) {
        return new WorkoutSession({ id: overrides.id ?? session.id, dayId: overrides.dayId ?? session.dayId, routineId: overrides.routineId ?? session.routineId, datetimeStart: overrides.datetimeStart ?? session.datetimeStart, datetimeEnd: overrides.datetimeEnd !== undefined ? overrides.datetimeEnd : session.datetimeEnd, notes: overrides.notes !== undefined ? overrides.notes : session.notes, impression: overrides.impression ?? session.impression, dayObj: overrides.dayObj ?? session.dayObj, logs: overrides.logs ?? session.logs });
    }

    // get the impression as a translated string''')

replace("src/components/Routines/widgets/WaveOne.tsx",
'''import { Link, useParams } from "react-router-dom";''',
'''import { Link, useParams } from "react-router-dom";
import { ExerciseDemoLink, SessionMetadataEditor, SessionTimer } from "@/components/Routines/widgets/WaveThree";''')
replace("src/components/Routines/widgets/WaveOne.tsx",
'''        <Button startIcon={<EditIcon />} href="#edit" variant="contained" sx={{ mb: 2 }}>Edit workout sets</Button>
        <Divider />''',
'''        <SessionTimer session={session} onSaved={async () => { await queryClient.invalidateQueries({ queryKey: [QueryKey.SESSIONS_FULL] }); }} />
        <SessionMetadataEditor session={session} onSaved={async () => { await queryClient.invalidateQueries({ queryKey: [QueryKey.SESSIONS_FULL] }); }} />
        <Button startIcon={<EditIcon />} href="#edit" variant="contained" sx={{ mb: 2 }}>Edit workout sets</Button>
        <Divider />''')
replace("src/components/Routines/widgets/WaveOne.tsx",
'''                <ExerciseLog exercise={logs[0].exerciseObj!} routineId={session.routineId} logEntries={logs} />
                <Button disabled={adding} onClick={() => addSet(logs.at(-1)!)}>+ Add set</Button>''',
'''                <ExerciseDemoLink exercise={logs[0].exerciseObj!} />
                <ExerciseLog exercise={logs[0].exerciseObj!} routineId={session.routineId} logEntries={logs} />
                <Typography variant="caption">Planned {logs.filter(log => log.slotEntryId !== null).length} · completed {logs.length}</Typography>
                <Button disabled={adding} onClick={() => addSet(logs.at(-1)!)}>+ Add set</Button>''')

form = "src/components/Routines/widgets/forms/SessionLogsForm.tsx"
replace(form,
'''import { useAddRoutineLogsQuery, useRoutineDetailQuery, useSessionOfDay } from "@/components/Routines/queries";''',
'''import { useAddRoutineLogsQuery, useRoutineDetailQuery, useSessionOfDay, useSessionsQuery } from "@/components/Routines/queries";
import { editSession } from "@/components/Routines/api/session";
import { WorkoutSession } from "@/components/Routines/models/WorkoutSession";
import { ExerciseDemoLink } from "@/components/Routines/widgets/WaveThree";''')
replace(form,
'''    const extraLogKey = useRef(0);''',
'''    const extraLogKey = useRef(0);
    const pendingSubstitution = useRef<string | null>(null);''')
replace(form,
'''        await addLogsQuery.mutateAsync(data);
        setSnackbarOpen(true);''',
'''        await addLogsQuery.mutateAsync(data);
        if (session && pendingSubstitution.current) {
            await editSession(WorkoutSession.clone(session, { notes: `${session.notes ?? ""}\n${pendingSubstitution.current}`.trim() }));
            pendingSubstitution.current = null;
        }
        setSnackbarOpen(true);''')
replace(form,
'''    const routineQuery = useRoutineDetailQuery(routineId);''',
'''    const routineQuery = useRoutineDetailQuery(routineId);
    const sessionsQuery = useSessionsQuery();''')
replace(form,
'''                        weightTarget: !hasNoIterationData && config.weight !== null ? config.weight : ''
                    });''',
'''                        weightTarget: !hasNoIterationData && config.weight !== null ? config.weight : '',
                        rest: '',
                        restTarget: ''
                    });''')
replace(form,
'''    const handleCallback = async (exercise: Exercise | null, formik: FormikProps<{''',
'''    const previousFor = (exerciseId: number) => sessionsQuery.data?.flatMap(entry => entry.logs.map(log => ({ entry, log })))
        .filter(item => item.log.exerciseId === exerciseId && item.entry.id !== session?.id && (!session || item.entry.datetimeStart < session.datetimeStart))
        .sort((a, b) => b.entry.datetimeStart.getTime() - a.entry.datetimeStart.getTime())[0]?.log;

    const handleCallback = async (exercise: Exercise | null, formik: FormikProps<{''')
replace(form,
'''                                                        repetitions: formik.values.logs[index].repetitions,
                                                        weight: formik.values.logs[index].weight''',
'''                                                        repetitions: formik.values.logs[index].repetitions,
                                                        repetitionsTarget: formik.values.logs[index].repetitionsTarget ?? "",
                                                        repetitionsUnit: formik.values.logs[index].repetitionsUnit,
                                                        weight: formik.values.logs[index].weight,
                                                        weightTarget: formik.values.logs[index].weightTarget ?? "",
                                                        weightUnit: formik.values.logs[index].weightUnit,
                                                        rir: formik.values.logs[index].rir,
                                                        rirTarget: formik.values.logs[index].rirTarget ?? "",
                                                        rest: formik.values.logs[index].rest ?? "",
                                                        restTarget: formik.values.logs[index].restTarget ?? "",
                                                        slotEntry: null''')
replace(form,
'''        const updatedLogs = formik.values.logs.map((log) => {''',
'''        const reason = window.prompt("Why are you substituting this exercise?", "");
        if (reason === null) return;
        const originalName = formik.values.logs.find(log => exerciseIdToSwap === log.exercise!.id)?.exercise?.getTranslation(language).name;
        pendingSubstitution.current = originalName ? `Substitution: ${exercise.getTranslation(language).name} for ${originalName}. Reason: ${reason}` : null;
        const updatedLogs = formik.values.logs.map((log) => {''')
replace(form,
'''                                            <Grid size={12}>
                                                {exerciseIdToSwap !== formik.values.logs[index].exercise!.id &&
                                                    <Typography variant="h6">
                                                        {formik.values.logs[index].exercise?.getTranslation(language).name}
                                                    </Typography>}''',
'''                                            <Grid size={12}>
                                                {exerciseIdToSwap !== formik.values.logs[index].exercise!.id && <>
                                                    <Typography variant="h6">{formik.values.logs[index].exercise?.getTranslation(language).name}</Typography>
                                                    <ExerciseDemoLink exercise={formik.values.logs[index].exercise!} />
                                                    {previousFor(formik.values.logs[index].exercise!.id!) && <Typography color="text.secondary">Previous: {previousFor(formik.values.logs[index].exercise!.id!)?.repetitions ?? "—"} reps × {previousFor(formik.values.logs[index].exercise!.id!)?.weight ?? "—"} {previousFor(formik.values.logs[index].exercise!.id!)?.weightUnitObj?.name ?? ""}</Typography>}
                                                </>}''')
replace(form,
'''                        {({ insert, remove }) => (<>''',
'''                        {({ insert, remove }) => (<>
                                <Alert severity="info">Added or removed sets affect this session only. Skipped rows are not saved as zeroes.</Alert>''')
print("Applied wger UX wave 3")
