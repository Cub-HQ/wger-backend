import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

MODULE = Path(__file__).with_name('patch_ux_wave1.py')


class WaveOnePatchTest(unittest.TestCase):
    def test_patches_routes_calendar_entries_and_progression(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixtures = {
                'src/routes.tsx': '''    WorkoutLogs,\n    WorkoutStats\n} from "@/components/Routines";\n                    <Route path="calendar" element={<Calendar />} />\n                    <Route path="add" element={<RoutineAdd />} />''',
                'src/components/Calendar/Components/Entries.tsx': '''import type { DayProps } from "./CalendarComponent";\n    const [t] = useTranslation();\n    const displayWeightUnit = useDisplayWeightUnit();\n                            <List sx={{ pl: 4, pt: 0 }}>\n                                {session.logs.map((log) => (\n                                    <ListItem key={log.id} dense>\n                                        <ListItemText\n                                            primary={log.exerciseObj?.getTranslation().name}\n                                            secondary={`${log.repetitions} × ${log.weight} `}\n                                        />\n                                    </ListItem>))}\n                            </List>''',
                'src/components/Calendar/Components/Entries.test.tsx': '''        expect(screen.getByText(/^8 × 80/)).toBeInTheDocument();\n        expect(screen.getByText(/^8 × 82.5/)).toBeInTheDocument();\n        expect(screen.getByText(/^8 × 80/)).toBeInTheDocument();\n    });''',
                'src/components/Calendar/Components/CalendarComponent.tsx': '''import React, { useEffect, useMemo, useState } from 'react';\nimport { useTranslation } from "react-i18next";\n    const currentDate = useMemo(() => new Date(), []);\n    const [currentMonth, setCurrentMonth] = useState(currentDate.getMonth());\n    const [currentYear, setCurrentYear] = useState(currentDate.getFullYear());\n        date: currentDate,\n    const [selectedDay, setSelectedDay] = useState<DayProps>(days.find(day => isSameDay(day.date, currentDate)) || defaultDay);\n            const todayWithData = days.find(day => isSameDay(day.date, currentDate));\n            if (todayWithData) {\n                setSelectedDay(todayWithData);\n            }\n    }, [currentDate, days, isSuccess]);\n    const handleDayClick = (day: DayProps) => {\n        setSelectedDay(day);\n    };''',
                'src/components/Exercises/screens/Detail/ExerciseDetailView.tsx': '''import { dateTimeToLocale } from "@/core/lib/date";\n                <PaddingBox />\n            </Grid>''',
                'src/app/layout/Header/SubMenus/WorkoutSubMenu.tsx': '''                <MenuItem component={Link} to={makeLink(WgerLink.CALENDAR, i18n.language)}>\n                    Calendar\n                </MenuItem>''',
            }
            for relative, content in fixtures.items():
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
            with patch('sys.argv', [str(MODULE), str(root)]):
                spec = importlib.util.spec_from_file_location('wave_one_patch', MODULE)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
            routes = (root / 'src/routes.tsx').read_text()
            entries = (root / 'src/components/Calendar/Components/Entries.tsx').read_text()
            entries_test = (root / 'src/components/Calendar/Components/Entries.test.tsx').read_text()
            calendar = (root / 'src/components/Calendar/Components/CalendarComponent.tsx').read_text()
            detail = (root / 'src/components/Exercises/screens/Detail/ExerciseDetailView.tsx').read_text()
            menu = (root / 'src/app/layout/Header/SubMenus/WorkoutSubMenu.tsx').read_text()
            wave = (root / 'src/components/Routines/widgets/WaveOne.tsx').read_text()
            self.assertIn('path="workouts"', routes)
            self.assertIn('path="session/:sessionId"', routes)
            self.assertIn('reps ×', wave)
            self.assertIn('Time ${duration', wave)
            self.assertIn('Distance ${number', wave)
            self.assertIn('"Speed" : "Load"', wave)
            self.assertIn('No recorded metrics', wave)
            self.assertIn('value !== null && value !== undefined', wave)
            self.assertIn('vs last time', wave)
            self.assertIn('IntersectionObserver', wave)
            self.assertIn('ExerciseProgression', detail)
            self.assertIn('Edit sets', entries)
            self.assertIn('All workouts', menu)
            self.assertIn('iteration: last.iteration', wave)
            self.assertIn('useFetchRoutineWeighUnitsQuery', wave)
            self.assertIn('Original source title:', wave)
            self.assertNotIn('component={Link}', entries)
            self.assertIn('8 reps × 80 kg', entries_test)
            self.assertIn('setSearchParams({ date: localDate }', calendar)


if __name__ == '__main__':
    unittest.main()
