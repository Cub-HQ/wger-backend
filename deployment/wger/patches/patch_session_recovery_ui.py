#!/usr/bin/env python3
"""Install session recovery on the pinned routine-logs surface, failing closed on drift."""
from pathlib import Path
import sys

API = r'''import axios from 'axios';
import { ApiPath } from '@/core/lib/consts';
import { makeHeader, makeUrl } from '@/core/lib/url';

export interface SessionRecovery {
    id: string;
    original_session_id: string;
    routine_id: number | null;
    deleted_at: string;
    expires_at: string;
    datetime_start: string;
}

export const deleteSession = async (id: string): Promise<SessionRecovery> => {
    const response = await axios.delete<SessionRecovery>(makeUrl(ApiPath.SESSION, { id }), { headers: makeHeader() });
    return response.data;
};

export const getSessionRecoveries = async (routineId: number): Promise<SessionRecovery[]> => {
    const response = await axios.get<SessionRecovery[]>(
        makeUrl(ApiPath.SESSION, { objectMethod: 'recoveries', query: { routine: routineId } }),
        { headers: makeHeader() }
    );
    return response.data;
};

export const restoreSession = async (id: string): Promise<void> => {
    await axios.post(
        makeUrl(ApiPath.SESSION, { objectMethod: `recoveries/${encodeURIComponent(id)}/restore` }),
        undefined,
        { headers: makeHeader() }
    );
};
'''

QUERIES = r'''import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { deleteSession, getSessionRecoveries, restoreSession } from '@/components/Routines/api/sessionRecovery';
import { QueryKey } from '@/core/lib/consts';

export const SESSION_RECOVERIES = 'session-recoveries';

const invalidateRecoveryReads = async (client: ReturnType<typeof useQueryClient>, routineId: number) => {
    await Promise.all([
        [QueryKey.SESSIONS_FULL],
        [QueryKey.SESSION_SEARCH],
        [QueryKey.ROUTINE_LOG_DATA, routineId],
        [QueryKey.ROUTINE_DETAIL, routineId],
        [QueryKey.ROUTINE_OVERVIEW],
        [SESSION_RECOVERIES, routineId],
    ].map(queryKey => client.invalidateQueries({ queryKey })));
};

export const useSessionRecoveriesQuery = (routineId: number) => useQuery({
    queryKey: [SESSION_RECOVERIES, routineId],
    queryFn: () => getSessionRecoveries(routineId),
    // Availability is server-owned, including expiry while this page stays open.
    refetchInterval: 30_000,
});

export const useDeleteSessionQuery = (routineId: number) => {
    const client = useQueryClient();
    return useMutation({
        mutationFn: deleteSession,
        retry: false,
        onSuccess: () => invalidateRecoveryReads(client, routineId),
    });
};

export const useRestoreSessionQuery = (routineId: number) => {
    const client = useQueryClient();
    return useMutation({
        mutationFn: restoreSession,
        retry: false,
        onSuccess: () => invalidateRecoveryReads(client, routineId),
    });
};
'''

COMPONENT = r'''import React, { useRef, useState } from 'react';
import { Alert, Box, Button, Dialog, DialogActions, DialogContent, DialogContentText, DialogTitle, Stack, Typography } from '@mui/material';
import { Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { WorkoutSession } from '@/components/Routines/models/WorkoutSession';
import { useDeleteSessionQuery, useRestoreSessionQuery, useSessionRecoveriesQuery } from '@/components/Routines/queries/sessionRecovery';

export const SessionRecoveryControls = ({ routineId, sessions }: { routineId: number; sessions: WorkoutSession[] }) => {
    const { i18n } = useTranslation();
    const recoveries = useSessionRecoveriesQuery(routineId);
    const deletion = useDeleteSessionQuery(routineId);
    const restoration = useRestoreSessionQuery(routineId);
    const [selected, setSelected] = useState<WorkoutSession | null>(null);
    const [message, setMessage] = useState('');
    const [error, setError] = useState('');
    // Lock synchronously as well as disabling controls: double clicks can precede a render.
    const inFlight = useRef(false);
    const pending = deletion.isPending || restoration.isPending;
    const uniqueSessions = Array.from(new Map(sessions.filter(session => session.id).map(session => [session.id, session])).values());
    const label = (session: WorkoutSession) => `${session.dayObj?.name || 'Workout'} — ${session.datetimeStart.toLocaleString()}`;

    const confirmDelete = async () => {
        if (!selected?.id || inFlight.current) return;
        inFlight.current = true;
        setError('');
        setMessage('');
        try {
            await deletion.mutateAsync(selected.id);
            setSelected(null);
            setMessage('Workout deleted. You can restore it from Deleted workouts for 15 days.');
        } catch {
            setError('Could not delete this workout. Your workout has not been hidden. Please try again.');
        } finally {
            inFlight.current = false;
        }
    };

    const restore = async (id: string) => {
        if (inFlight.current) return;
        inFlight.current = true;
        setError('');
        setMessage('');
        try {
            await restoration.mutateAsync(id);
            setMessage('Workout restored, including its sets.');
        } catch {
            setError('Could not restore this workout. It may have expired or conflict with existing data. Refresh and try again.');
        } finally {
            inFlight.current = false;
        }
    };

    return <Stack spacing={2} sx={{ my: 3 }}>
        {message && <Alert severity="success" role="status">{message}</Alert>}
        {error && !selected && <Alert severity="error">{error}</Alert>}
        <Box component="section" aria-labelledby="logged-workouts-title">
            <Typography id="logged-workouts-title" variant="h5" component="h2">Logged workouts</Typography>
            {uniqueSessions.length === 0 && <Typography>No logged workouts.</Typography>}
            {uniqueSessions.map(session => <Stack key={session.id} component="article" aria-label={label(session)} spacing={1} sx={{ my: 2 }}>
                <Typography>{label(session)}</Typography>
                <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1}>
                    <Button component={Link} to={`/${i18n.language}/routine/session/${session.id}`}>View workout</Button>
                    <Button component={Link} to={`/${i18n.language}/routine/session/${session.id}#edit`}>Edit sets</Button>
                    <Button color="error" disabled={pending} onClick={() => { setError(''); setSelected(session); }}>Delete</Button>
                </Stack>
            </Stack>)}
        </Box>
        <Box component="section" aria-labelledby="deleted-workouts-title" aria-busy={recoveries.isFetching || restoration.isPending}>
            <Typography id="deleted-workouts-title" variant="h5" component="h2">Deleted workouts</Typography>
            <Typography>Deleted workouts can be restored with all their sets for 15 days.</Typography>
            {recoveries.isPending && <Typography role="status">Loading deleted workouts…</Typography>}
            {recoveries.isError && <Alert severity="error">Could not load deleted workouts. <Button onClick={() => { void recoveries.refetch(); }}>Retry</Button></Alert>}
            {recoveries.isSuccess && recoveries.data.length === 0 && <Typography>No deleted workouts available to restore.</Typography>}
            {!recoveries.isError && recoveries.data?.map(recovery => <Stack key={recovery.id} component="article" aria-label={`Deleted workout ${new Date(recovery.datetime_start).toLocaleString()}`} spacing={1} sx={{ my: 2 }}>
                <Typography>Workout — {new Date(recovery.datetime_start).toLocaleString()}</Typography>
                <Typography>Available until {new Date(recovery.expires_at).toLocaleString()}</Typography>
                <Button disabled={pending} onClick={() => { void restore(recovery.id); }}>
                    {restoration.isPending && restoration.variables === recovery.id ? 'Restoring…' : 'Restore workout'}
                </Button>
            </Stack>)}
        </Box>
        <Dialog open={selected !== null} onClose={() => { if (!inFlight.current) { setSelected(null); setError(''); } }} aria-labelledby="delete-workout-title" aria-describedby="delete-workout-description">
            <DialogTitle id="delete-workout-title">Delete workout?</DialogTitle>
            <DialogContent>
                <DialogContentText id="delete-workout-description">{selected && label(selected)}. This removes the workout and all its sets. You can restore them from Deleted workouts for 15 days.</DialogContentText>
                {error && <Alert severity="error">{error}</Alert>}
            </DialogContent>
            <DialogActions>
                <Button disabled={pending} onClick={() => { setSelected(null); setError(''); }}>Cancel</Button>
                <Button color="error" disabled={pending} onClick={() => { void confirmDelete(); }}>{deletion.isPending ? 'Deleting…' : 'Delete workout'}</Button>
            </DialogActions>
        </Dialog>
    </Stack>;
};
'''

FILES = {
    'src/components/Routines/api/sessionRecovery.ts': API,
    'src/components/Routines/queries/sessionRecovery.ts': QUERIES,
    'src/components/Routines/screens/Detail/SessionRecovery.tsx': COMPONENT,
}
OVERVIEW = 'src/components/Routines/screens/Detail/WorkoutLogs.tsx'
EXPORTS = 'src/components/Routines/queries/index.ts'
IMPORT_ANCHOR = 'import { makeLink, WgerLink } from "@/core/lib/url";'
BODY_ANCHOR = '            {plannedDays.map((dayData) =>'
EXPORT_ANCHOR = '} from "./sessions";'


def patch(root: Path) -> None:
    # Validate every anchor and destination before modifying any source.
    changes = {}
    overview = (root / OVERVIEW).read_text()
    exports = (root / EXPORTS).read_text()
    for anchor in (IMPORT_ANCHOR, BODY_ANCHOR):
        if overview.count(anchor) != 1:
            raise SystemExit(f'Expected exactly one anchor in {OVERVIEW}: {anchor!r}')
    if exports.count(EXPORT_ANCHOR) != 1:
        raise SystemExit(f'Expected exactly one session export in {EXPORTS}')
    for path in FILES:
        if (root / path).exists():
            raise SystemExit(f'Refusing to overwrite existing recovery source: {path}')
    changes[OVERVIEW] = overview.replace(IMPORT_ANCHOR, IMPORT_ANCHOR + '\nimport { SessionRecoveryControls } from "./SessionRecovery";').replace(
        BODY_ANCHOR,
        '            <SessionRecoveryControls routineId={routineId} sessions={routineLogDataQuery.data!.map(entry => entry.session)} />\n\n' + BODY_ANCHOR,
    )
    changes[EXPORTS] = exports.replace(EXPORT_ANCHOR, EXPORT_ANCHOR + '\n\nexport { useDeleteSessionQuery, useRestoreSessionQuery, useSessionRecoveriesQuery } from "./sessionRecovery";')
    for path, content in {**changes, **FILES}.items():
        (root / path).write_text(content)


if __name__ == '__main__':
    patch(Path(sys.argv[1]))
    print('Applied routine logs session recovery patch')
