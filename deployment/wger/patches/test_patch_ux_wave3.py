import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

MODULE = Path(__file__).with_name('patch_ux_wave3.py')


class WaveThreePatchTest(unittest.TestCase):
    def test_source_contains_every_remaining_phone_contract(self):
        source = MODULE.read_text()
        contracts = [
            'Log a workout', 'No routine picker', 'SessionTimer', 'localStorage.setItem(TIMER_KEY',
            'Finish workout', 'Substitution:', 'Reason:', 'Previous:', 'ExerciseDemoLink',
            'SessionMetadataEditor', 'Planned {logs.filter', 'Skipped rows are not saved as zeroes',
            'Time', 'Distance', 'Speed', 'calories', 'incline', 'pace'
        ]
        for contract in contracts:
            with self.subTest(contract=contract):
                self.assertIn(contract, source)

    def test_build_uses_published_wave_three_source_once(self):
        script = Path(__file__).with_name('prepare-react.sh').read_text()
        self.assertIn('3066f7693ac00632ad14ea0ef025371156f91d0d', script)
        self.assertNotIn('patch_ux_wave3.py', script)
        self.assertEqual(script.count('npm run build'), 1)


if __name__ == '__main__':
    unittest.main()
