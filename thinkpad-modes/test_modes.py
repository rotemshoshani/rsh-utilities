import datetime
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

spec = importlib.util.spec_from_file_location('modes', Path(__file__).with_name('modes.py'))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class ModesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patcher = patch.object(m, 'STATE', Path(self.temp.name))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_busy_mode_does_not_stop_job_or_change_power(self):
        with m.lock(), patch.object(m, 'ctl') as ctl, patch.object(m, 'run') as run:
            with self.assertRaisesRegex(RuntimeError, 'Nothing was stopped'):
                m.mode('laptop')
            ctl.assert_not_called()
            run.assert_not_called()

    def test_unarmed_job_cannot_execute_or_send_email(self):
        (m.STATE / 'server-enabled').touch()
        with patch.object(m, 'run') as run:
            m.job('report')
            run.assert_not_called()

    def test_laptop_stops_timers_before_target_and_restores_power(self):
        (m.STATE / 'server-enabled').touch()
        with patch.object(m, 'ctl', return_value='inactive\n') as ctl, patch.object(m, 'run') as run:
            m.mode('laptop')
            calls = [c.args for c in ctl.call_args_list]
            self.assertLess(calls.index(('stop', *m.TIMERS)), calls.index(('stop', m.TARGET)))
            self.assertFalse((m.STATE / 'server-enabled').exists())
            run.assert_any_call(['sudo', 'systemctl', 'unmask', *m.SLEEP_TARGETS])
            run.assert_any_call(['sudo', 'loginctl', 'disable-linger', 'rshoshani'])

    def test_laptop_repeated_is_safe(self):
        with patch.object(m, 'ctl', return_value='inactive'), patch.object(m, 'run'):
            m.mode('laptop')
            m.mode('laptop')

    def test_server_keeps_schedules_held_and_restarts_its_units(self):
        with patch.object(m, 'ctl', return_value='inactive') as ctl, patch.object(m, 'run'):
            m.mode('server')
            m.mode('server')
            self.assertTrue((m.STATE / 'server-enabled').exists())
            self.assertFalse((m.STATE / 'schedules-armed').exists())
            ctl.assert_any_call('start', 'thinkpad-error-collector.service', *m.TIMERS)

    def test_externally_active_job_blocks_mode_before_sudo(self):
        with patch.object(m, 'ctl', return_value='activating'), patch.object(m, 'run') as run:
            with self.assertRaisesRegex(RuntimeError, 'busy'):
                m.mode('laptop')
            run.assert_not_called()

    def test_window_boundaries_and_timezone(self):
        for hour, expected in [(3, False), (4, True), (7, True), (8, False), (23, False)]:
            self.assertEqual(m.in_window(datetime.datetime(2026, 10, 12, hour, tzinfo=ZoneInfo('Asia/Jerusalem'))), expected)

    def test_outside_window_does_not_update_or_claim(self):
        for name in ('server-enabled', 'schedules-armed'):
            (m.STATE / name).touch()
        with patch.object(m, 'in_window', return_value=False), patch.object(m, 'update_checkout') as update:
            m.job('worker')
            update.assert_not_called()


if __name__ == '__main__':
    unittest.main()
