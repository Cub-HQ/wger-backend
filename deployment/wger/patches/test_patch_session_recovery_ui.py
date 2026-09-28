#!/usr/bin/env python3
"""Patch-contract tests and installable pinned-source Vitest behavior coverage.

Run this file with --install REACT_ROOT to install the component tests, then run
npm test -- src/components/Routines/screens/Detail/SessionRecovery.test.tsx there.
"""
from pathlib import Path
import importlib.util
import sys
import tempfile
import unittest

TEST_PATH = 'src/components/Routines/screens/Detail/SessionRecovery.test.tsx'
TEST_SOURCE = r'''import React from 'react';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { QueryClient, QueryClientProvider, useQuery } from '@tanstack/react-query';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import axios from 'axios';
import { WorkoutLogs } from './WorkoutLogs';
import { QueryKey } from '@/core/lib/consts';
import { SESSION_RECOVERIES } from '@/components/Routines/queries/sessionRecovery';

vi.mock('axios', () => ({ default: { get: vi.fn(), delete: vi.fn(), post: vi.fn() } }));
vi.mock('react-i18next', async importOriginal => ({ ...await importOriginal<typeof import('react-i18next')>(), useTranslation: () => Object.assign([(key: string) => key, { language: 'en-au' }], { i18n: { language: 'en-au' } }) }));
vi.mock('@/core/ui/Widgets/Container', () => ({ WgerContainerFullWidth: ({ children }: { children: React.ReactNode }) => <main>{children}</main> }));
vi.mock('@/components/Routines/widgets/LogWidgets', () => ({ ExerciseLog: ({ logEntries }: { logEntries?: { id: string }[] }) => <div>{logEntries?.map(log => <span key={log.id}>Set {log.id}</span>)}</div> }));
vi.mock('@/components/Routines/queries', () => ({
    useRoutineDetailQuery: () => ({ data: { dayDataCurrentIterationFiltered: [{ day: { id: 4, name: 'Strength', isRest: false }, slots: [{ exercises: [{ id: 9 }] }] }] }, isLoading: false }),
    useRoutineLogData: (routineId: number) => useQuery({ queryKey: [QueryKey.ROUTINE_LOG_DATA, routineId], queryFn: async () => (await axios.get('/fixture/routine-logs')).data }),
}));

const rawSession = { id: 'session-one', routine: 7, day: 4, datetime_start: '2026-09-20T10:00:00Z', datetime_end: null, impression: '2', notes: '' };
const session = { id: rawSession.id, routineId: 7, datetimeStart: new Date(rawSession.datetime_start), dayObj: { name: 'Strength' } };
const entry = { session, logs: [{ id: 'set-one', exerciseId: 9, date: new Date(rawSession.datetime_start) }] };
const recovery = { id: 'recovery-one', original_session_id: session.id, routine_id: 7, datetime_start: rawSession.datetime_start, deleted_at: '2026-09-28T10:00:00Z', expires_at: '2026-10-13T10:00:00Z' };
let active: typeof entry[];
let deleted: typeof recovery[];
let client: QueryClient;
const calendarKeys = [[QueryKey.SESSIONS_FULL], [QueryKey.SESSION_SEARCH], [QueryKey.ROUTINE_DETAIL, 7], [QueryKey.ROUTINE_OVERVIEW]];
const clients: QueryClient[] = [];

function show() {
    client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
    clients.push(client);
    for (const key of calendarKeys) client.setQueryData(key, ['cached']);
    return render(<QueryClientProvider client={client}><MemoryRouter initialEntries={['/en-au/routine/7/logs']}><Routes><Route path="/:lang/routine/:routineId/logs" element={<WorkoutLogs />} /></Routes></MemoryRouter></QueryClientProvider>);
}

beforeEach(() => {
    vi.clearAllMocks();
    active = [entry, entry]; // A session may occur in several groups; actions must remain unique.
    deleted = [];
    vi.mocked(axios.get).mockImplementation(async (url) => ({ data: String(url).includes('/fixture/') ? active : deleted }));
    vi.mocked(axios.delete).mockImplementation(async () => { active = []; deleted = [recovery]; return { data: recovery }; });
    vi.mocked(axios.post).mockImplementation(async () => { active = [entry]; deleted = []; return { data: rawSession }; });
});
afterEach(() => { cleanup(); for (const value of clients.splice(0)) value.clear(); vi.useRealTimers(); });

async function openDelete() {
    fireEvent.click(await screen.findByRole('button', { name: 'Delete' }));
    return await screen.findByRole('dialog', { name: 'Delete workout?' });
}

describe('routine logs session recovery', () => {
    it('places unique session actions together on the actual logs route; cancel never deletes', async () => {
        show();
        const view = await screen.findByRole('link', { name: 'View workout' });
        const row = view.closest('article')!;
        expect(within(row).getByRole('link', { name: 'Edit sets' }).getAttribute('href')).toBe('/en-au/routine/session/session-one#edit');
        expect(within(row).getByRole('button', { name: 'Delete' })).toBeTruthy();
        const dialog = await openDelete();
        expect(within(dialog).getByText(/15 days/)).toBeTruthy();
        fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }));
        expect(axios.delete).not.toHaveBeenCalled();
        expect(await screen.findByRole('link', { name: 'View workout' })).toBeTruthy();
    });

    it('sends one confirmed request, prevents repeats, then refreshes active and deleted data', async () => {
        let finish!: (value: { data: typeof recovery }) => void;
        vi.mocked(axios.delete).mockImplementation(() => new Promise(resolve => { finish = resolve; }));
        show();
        const dialog = await openDelete();
        const confirm = within(dialog).getByRole('button', { name: 'Delete workout' });
        fireEvent.click(confirm);
        fireEvent.click(confirm);
        await waitFor(() => expect(axios.delete).toHaveBeenCalledTimes(1));
        expect(String(vi.mocked(axios.delete).mock.calls[0][0])).toMatch(/workoutsession\/session-one\/$/);
        expect((within(dialog).getByRole('button', { name: 'Deleting…' }) as HTMLButtonElement).disabled).toBe(true);
        active = []; deleted = [recovery];
        await act(async () => { finish({ data: recovery }); });
        expect(await screen.findByRole('button', { name: 'Restore workout' })).toBeTruthy();
        await waitFor(() => expect(screen.queryByRole('link', { name: 'View workout' })).toBeNull());
        expect(screen.queryByText('Set set-one')).toBeNull();
        for (const key of calendarKeys) expect(client.getQueryState(key)?.isInvalidated).toBe(true);
        expect(client.getQueryData([QueryKey.ROUTINE_LOG_DATA, 7])).toEqual([]);
        expect(client.getQueryData([SESSION_RECOVERIES, 7])).toEqual([recovery]);
    });

    it('preserves active history on delete failure and reports an accessible error', async () => {
        vi.mocked(axios.delete).mockRejectedValue(new Error('server refused'));
        show();
        const dialog = await openDelete();
        fireEvent.click(within(dialog).getByRole('button', { name: 'Delete workout' }));
        expect(await within(dialog).findByRole('alert')).toHaveTextContent('Could not delete');
        fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }));
        expect(await screen.findByRole('link', { name: 'View workout' })).toBeTruthy();
        expect(screen.getAllByText('Set set-one').length).toBe(2);
        expect(active).toEqual([entry, entry]);
    });

    it('automatically removes expired server recoveries while the logs screen stays open', async () => {
        vi.useFakeTimers();
        deleted = [recovery];
        active = [];
        show();
        // Flush initial routine data, then the mounted recovery query and its notification.
        await act(async () => { await vi.advanceTimersByTimeAsync(1); });
        await act(async () => { await vi.advanceTimersByTimeAsync(1); });
        await act(async () => { await vi.advanceTimersByTimeAsync(1); });
        const region = screen.getByRole('region', { name: 'Deleted workouts' });
        expect(within(region).getByRole('button', { name: 'Restore workout' })).toBeTruthy();
        expect(vi.mocked(axios.get).mock.calls.some(([url]) => String(url).endsWith('/workoutsession/recoveries/?routine=7'))).toBe(true);
        deleted = []; // Server no longer returns the expired recovery.
        await act(async () => { await vi.advanceTimersByTimeAsync(30_001); });
        await act(async () => { await vi.advanceTimersByTimeAsync(1); });
        expect(within(region).queryByRole('button', { name: 'Restore workout' })).toBeNull();
        expect(within(region).getByText('No deleted workouts available to restore.')).toBeTruthy();
    });

    it('restores without snapshot input, prevents repeats and refreshes calendar, logs and recovery caches', async () => {
        active = []; deleted = [recovery];
        let finish!: (value: { data: typeof rawSession }) => void;
        vi.mocked(axios.post).mockImplementation(() => new Promise(resolve => { finish = resolve; }));
        show();
        const restore = await screen.findByRole('button', { name: 'Restore workout' });
        fireEvent.click(restore); fireEvent.click(restore);
        await waitFor(() => expect(axios.post).toHaveBeenCalledTimes(1));
        const [url, body] = vi.mocked(axios.post).mock.calls[0];
        expect(String(url)).toMatch(/workoutsession\/recoveries\/recovery-one\/restore\/$/);
        expect(body).toBeUndefined();
        expect((screen.getByRole('button', { name: 'Restoring…' }) as HTMLButtonElement).disabled).toBe(true);
        active = [entry]; deleted = [];
        await act(async () => { finish({ data: rawSession }); });
        expect(await screen.findByRole('link', { name: 'View workout' })).toBeTruthy();
        expect(await screen.findByText('Set set-one')).toBeTruthy();
        await waitFor(() => expect(screen.queryByRole('button', { name: 'Restore workout' })).toBeNull());
        for (const key of calendarKeys) expect(client.getQueryState(key)?.isInvalidated).toBe(true);
        expect(client.getQueryData([QueryKey.ROUTINE_LOG_DATA, 7])).toEqual([entry]);
        expect(client.getQueryData([SESSION_RECOVERIES, 7])).toEqual([]);
    });

    it('preserves recovery after restore failure and allows retry', async () => {
        active = []; deleted = [recovery];
        vi.mocked(axios.post).mockRejectedValueOnce(new Error('conflict'));
        show();
        fireEvent.click(await screen.findByRole('button', { name: 'Restore workout' }));
        expect(await screen.findByRole('alert')).toHaveTextContent('Could not restore');
        expect((screen.getByRole('button', { name: 'Restore workout' }) as HTMLButtonElement).disabled).toBe(false);
        expect(screen.queryByRole('link', { name: 'View workout' })).toBeNull();
        expect(deleted).toEqual([recovery]);
    });

    it('keeps restore pending until all invalidation promises finish', async () => {
        active = []; deleted = [recovery];
        show();
        const restore = await screen.findByRole('button', { name: 'Restore workout' });
        let finish!: () => void;
        const held = new Promise<void>(resolve => { finish = resolve; });
        const original = client.invalidateQueries.bind(client);
        vi.spyOn(client, 'invalidateQueries').mockImplementation(async (...args) => { await original(...args); await held; });
        fireEvent.click(restore);
        await waitFor(() => expect(axios.post).toHaveBeenCalledTimes(1));
        expect((await screen.findByRole('button', { name: 'Delete' }) as HTMLButtonElement).disabled).toBe(true);
        await act(async () => { finish(); });
        await waitFor(() => expect((screen.getByRole('button', { name: 'Delete' }) as HTMLButtonElement).disabled).toBe(false));
    });
});
'''


def load_patch():
    path = Path(__file__).with_name('patch_session_recovery_ui.py')
    spec = importlib.util.spec_from_file_location('session_recovery_ui_patch', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PatchContractTests(unittest.TestCase):
    def fixture(self, root, module):
        for relative in (module.OVERVIEW, module.EXPORTS, *module.FILES):
            (root / relative).parent.mkdir(parents=True, exist_ok=True)
        (root / module.OVERVIEW).write_text(module.IMPORT_ANCHOR + '\n' + module.BODY_ANCHOR + '\n')
        (root / module.EXPORTS).write_text(module.EXPORT_ANCHOR + '\n')

    def test_missing_anchor_leaves_all_inputs_untouched(self):
        module = load_patch()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.fixture(root, module)
            (root / module.EXPORTS).write_text('drifted exports')
            before = {path: path.read_bytes() for path in root.rglob('*') if path.is_file()}
            with self.assertRaises(SystemExit):
                module.patch(root)
            self.assertEqual(before, {path: path.read_bytes() for path in root.rglob('*') if path.is_file()})

    def test_duplicate_anchor_and_repeat_application_fail_closed(self):
        module = load_patch()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.fixture(root, module)
            overview = root / module.OVERVIEW
            original = overview.read_text()
            overview.write_text(original + module.BODY_ANCHOR)
            with self.assertRaises(SystemExit):
                module.patch(root)
            self.assertFalse(any((root / path).exists() for path in module.FILES))
            overview.write_text(original)
            module.patch(root)
            before = {path: path.read_bytes() for path in root.rglob('*') if path.is_file()}
            with self.assertRaises(SystemExit):
                module.patch(root)
            self.assertEqual(before, {path: path.read_bytes() for path in root.rglob('*') if path.is_file()})


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--install':
        target = Path(sys.argv[2]) / TEST_PATH
        if target.exists():
            raise SystemExit(f'Refusing to overwrite test: {target}')
        target.write_text(TEST_SOURCE)
    else:
        unittest.main()
