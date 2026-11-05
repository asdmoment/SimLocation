"""Local-only session tests: fake systemd/UFW and synthetic credential metadata."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import reflect_session as session


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.base = self.root / 'state'
        self.base.mkdir(mode=0o700)
        self.credentials = self.root / 'credentials'
        self.credentials.mkdir()
        for name in ('ca.pem', 'server.pem', 'server-key.pem'):
            (self.credentials / name).write_text('synthetic test fixture\n')
        (self.credentials / 'server-key.pem').chmod(0o400)
        self.expiry(datetime.now(timezone.utc) + timedelta(hours=3))
        self.proc = self.root / 'proc'
        self.proc.mkdir()
        self.old = self.root / 'old-stop.py'
        self.dropins = {unit: self.root / unit / 'session.conf' for unit in (session.NETWORK, session.WEB)}
        self.calls = []
        self.rules = []
        self.states = {session.NETWORK: 'inactive', session.WEB: 'active'}
        self.failure = None
        self.later_env = False
        for name, value in [('BASE', self.base), ('PROC', self.proc), ('OLD_STOP', self.old),
                            ('DROPINS', self.dropins), ('RECOVER', self.root / 'recover.service'), ('UNIT', self.root / 'simloc-reflect.service'),
                            ('run', self.run_command)]:
            patcher = patch.object(session, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        user = patch.object(session.pwd, 'getpwnam', return_value=SimpleNamespace(pw_uid=123))
        user.start()
        self.addCleanup(user.stop)
        # Tests need no root. Only synthesize the fixture key's ownership metadata.
        real_stat = Path.stat

        def root_key(path, *args, **kwargs):
            info = real_stat(path, *args, **kwargs)
            if path == self.credentials / 'server-key.pem':
                fields = list(info)
                fields[4] = 0
                return os.stat_result(fields)
            return info

        owner = patch.object(Path, 'stat', root_key)
        owner.start()
        self.addCleanup(owner.stop)

    def expiry(self, when):
        (self.credentials / 'expiry.json').write_text(json.dumps({'not_after_utc': when.isoformat()}))

    def run_command(self, *args):
        self.calls.append(args)
        if self.failure and self.failure(args):
            self.failure = None
            raise RuntimeError('injected failure')
        if args[:2] == ('systemctl', 'show'):
            if '--property=EnvironmentFiles' in args:
                path = '/later.env' if self.later_env else str(self.base / 'manual.env')
                return '/etc/simlocation/web.env (ignore_errors=no) ' + path + ' (ignore_errors=no)'
            return self.states.get(args[2], 'inactive')
        if args == ('ufw', 'status'):
            return 'Status: active\n' + '\n'.join(' '.join(rule) for rule in self.rules)
        if args[:2] == ('ufw', 'allow'):
            self.rules.append(args[1:])
        if args[:3] == ('ufw', '--force', 'delete'):
            self.rules.remove(args[3:])
        if args[0] == 'systemctl' and args[1] in ('restart', 'start'):
            self.states[args[2]] = 'active'
            if args[2] == session.WEB:
                self.states[session.NETWORK] = 'active'  # Wants=network.
        if args[:2] == ('systemctl', 'stop'):
            self.states[args[2]] = 'inactive'
        return ''

    def enable(self, seconds=3600):
        session.enable(str(self.credentials), '8.8.8.8', seconds)

    def test_argument_rejection_before_commands(self):
        for seconds in (0, 299, 7201, True):
            with self.subTest(seconds=seconds), self.assertRaises(ValueError):
                self.enable(seconds)
        for ip in ('::1', '127.0.0.1', '10.7.0.1', 'bad'):
            with self.subTest(ip=ip), self.assertRaises(ValueError):
                session.enable(str(self.credentials), ip, 3600)
        with self.assertRaises(ValueError):
            session.enable('relative', '8.8.8.8', 3600)
        self.assertEqual(self.calls, [])

    def test_expiry_strict_safety_margin_and_timezone(self):
        now = datetime.now(timezone.utc)
        self.expiry(now + timedelta(seconds=3900))
        with self.assertRaises(ValueError):
            session.validate(str(self.credentials), '8.8.8.8', 3600, now)
        self.expiry(now + timedelta(seconds=3901))
        session.validate(str(self.credentials), '8.8.8.8', 3600, now)
        self.expiry(datetime(2099, 1, 1))
        with self.assertRaises(ValueError):
            self.enable()

    def test_links_and_key_permissions_rejected(self):
        alias = self.root / 'alias'
        alias.symlink_to(self.credentials, target_is_directory=True)
        with self.assertRaises(ValueError):
            session.validate(str(alias), '8.8.8.8', 3600)
        key = self.credentials / 'server-key.pem'
        for mode in (0o400, 0o600):
            key.chmod(mode)
            session.validate(str(self.credentials), '8.8.8.8', 3600)
        for mode in (0o640, 0o604, 0o440, 0o700):
            key.chmod(mode)
            with self.subTest(mode=oct(mode)), self.assertRaises(ValueError):
                self.enable()
        key.unlink()
        key.symlink_to(self.credentials / 'ca.pem')
        with self.assertRaises(ValueError):
            self.enable()

    def test_worker_and_probe_rejected_without_stops(self):
        pid = self.proc / '123'
        pid.mkdir()
        cmdline = pid / 'cmdline'
        cmdline.write_bytes(b'python\0--_hold-session\0private-location\0')
        with self.assertRaisesRegex(RuntimeError, 'worker'):
            self.enable()
        self.assertEqual(self.calls, [])
        cmdline.unlink()
        self.states['simloc-reflect-probe.service'] = 'active'
        with self.assertRaisesRegex(RuntimeError, 'probe'):
            self.enable()
        self.assertFalse(any(call[:2] == ('systemctl', 'stop') for call in self.calls))

    def test_existing_dropin_is_never_overwritten(self):
        target = self.dropins[session.NETWORK]
        target.parent.mkdir()
        target.write_text('owned by someone else\n')
        with self.assertRaisesRegex(RuntimeError, 'disable first'):
            self.enable()
        session.disable()
        self.assertEqual(target.read_text(), 'owned by someone else\n')
        self.assertFalse((self.base / 'session.json').exists())

    def test_existing_session_is_not_replaced(self):
        self.enable()
        before = (self.base / 'session.json').read_text()
        calls = list(self.calls)
        with self.assertRaisesRegex(RuntimeError, 'disable first'):
            self.enable()
        self.assertEqual((self.base / 'session.json').read_text(), before)
        self.assertEqual(self.calls, calls)

    def test_enable_order_and_bounded_configuration(self):
        self.old.write_text('# fixture only; never executed\n')
        original_run = session.run

        def checked(*args):
            if args[0] == 'systemd-run' and '--unit=' + session.EXPIRE in args:
                self.assertTrue((self.base / 'session.json').exists())
                self.assertFalse((self.base / 'firewall.json').exists())
            return original_run(*args)

        check_worker = session.ensure_no_worker

        def checked_worker():
            self.calls.append(('worker-check',))
            check_worker()

        with patch.object(session, 'run', checked), patch.object(session, 'ensure_no_worker', checked_worker):
            self.enable()
        old_timer = self.calls.index(('systemctl', 'stop', 'simloc-reflect-expire.timer'))
        old_cleanup = self.calls.index(('/usr/bin/python3', '-I', str(self.old)))
        timer = next(i for i, call in enumerate(self.calls) if '--unit=' + session.EXPIRE in call)
        firewall = next(i for i, call in enumerate(self.calls) if call[:2] == ('ufw', 'allow'))
        relay = next(i for i, call in enumerate(self.calls) if '--unit=simloc-reflect' in call)
        network = self.calls.index(('systemctl', 'restart', session.NETWORK))
        web_stop = self.calls.index(('systemctl', 'stop', session.WEB))
        web = self.calls.index(('systemctl', 'start', session.WEB))
        self.assertLess(old_timer, old_cleanup)
        self.assertLess(old_cleanup, timer)
        self.assertLess(timer, firewall)
        self.assertLess(firewall, relay)
        self.assertLess(relay, web_stop)
        worker_checks = [i for i, call in enumerate(self.calls) if call == ('worker-check',)]
        self.assertEqual(len(worker_checks), 2)
        self.assertLess(web_stop, worker_checks[1])
        self.assertLess(worker_checks[1], network)
        self.assertLess(network, web)
        self.assertIn('RuntimeMaxSec=3600', self.calls[relay])
        self.assertIn('--on-active=3600s', self.calls[timer])
        config = json.loads((self.base / 'server.json').read_text())
        self.assertEqual(config['max_seconds'], 3600)
        self.assertEqual(config['probe_uid'], 123)
        self.assertEqual((self.base / 'manual.env').stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.dropins[session.NETWORK].read_text(), session.NETWORK_TEXT)
        self.assertNotIn('EnvironmentFile=\n', self.dropins[session.WEB].read_text())
        enables = [call for call in self.calls if 'enable' in call]
        self.assertEqual(enables, [('systemctl', 'enable', session.RECOVER.name)])
        self.assertNotIn(('systemctl', 'start', session.RECOVER.name), self.calls)
        self.assertEqual(session.RECOVER.read_text(), session.RECOVER_TEXT)
        self.assertTrue(json.loads((self.base / 'session.json').read_text())['boot_recovery'])

    def test_worker_created_after_preflight_blocks_backend_switch(self):
        original_run = session.run

        def spawn_before_web_stops(*args):
            if args == ('systemctl', 'stop', session.WEB):
                pid = self.proc / '456'
                pid.mkdir()
                (pid / 'cmdline').write_bytes(b'python\0--_hold-session\0')
            return original_run(*args)

        with patch.object(session, 'run', spawn_before_web_stops):
            with self.assertRaisesRegex(RuntimeError, 'worker'):
                self.enable()
        web_stop = self.calls.index(('systemctl', 'stop', session.WEB))
        rollback = self.calls.index(('systemctl', 'stop', session.RELAY), web_stop)
        self.assertNotIn(('systemctl', 'restart', session.NETWORK), self.calls[web_stop:rollback])
        self.assertNotIn(('systemctl', 'start', session.WEB), self.calls)
        self.assertFalse((self.base / 'session.json').exists())
        self.assertEqual(self.rules, [])
        self.assertEqual(self.states[session.WEB], 'active')
        self.assertEqual(self.states[session.NETWORK], 'inactive')

    def test_rollback_at_each_side_effect(self):
        stages = [lambda a: '--unit=' + session.EXPIRE in a,
                  lambda a: a[:2] == ('ufw', 'allow'),
                  lambda a: '--unit=simloc-reflect' in a,
                  lambda a: a == ('systemctl', 'stop', session.WEB),
                  lambda a: a == ('systemctl', 'restart', session.NETWORK),
                  lambda a: a == ('systemctl', 'start', session.WEB)]
        for stage in stages:
            with self.subTest(stage=stage):
                self.states.update({session.NETWORK: 'inactive', session.WEB: 'active'})
                self.failure = stage
                with self.assertRaisesRegex(RuntimeError, 'injected'):
                    self.enable()
                self.assertFalse((self.base / 'session.json').exists())
                self.assertEqual(self.rules, [])
                self.assertFalse(any(path.exists() for path in self.dropins.values()))
                self.assertEqual(self.states[session.NETWORK], 'inactive')
                self.assertEqual(self.states[session.WEB], 'active')

    def test_disable_is_idempotent_and_keeps_unowned_rules(self):
        self.enable()
        foreign = ('allow', 'proto', 'tcp', 'from', 'any', 'to', '9.9.9.9', 'port', '9999')
        self.rules.append(foreign)
        session.disable()
        calls = list(self.calls)
        session.disable()
        self.assertEqual(self.calls, calls)
        self.assertEqual(self.rules, [foreign])
        self.assertEqual(self.states[session.NETWORK], 'inactive')
        self.assertEqual(self.states[session.WEB], 'active')
        restore_web = self.calls.index(('systemctl', 'restart', session.WEB),
                                       self.calls.index(('systemctl', 'stop', session.RELAY)))
        restore_network = self.calls.index(('systemctl', 'stop', session.NETWORK), restore_web)
        self.assertLess(restore_web, restore_network)
        self.assertEqual(session.status(), {'session': 'disabled'})

    def test_changed_dropin_is_preserved_with_recovery_state(self):
        self.enable()
        target = self.dropins[session.NETWORK]
        target.write_text('changed externally\n')
        with self.assertRaisesRegex(RuntimeError, 'changed'):
            session.disable()
        self.assertEqual(target.read_text(), 'changed externally\n')
        self.assertTrue((self.base / 'session.json').exists())
        self.assertEqual(self.rules, [])
        self.assertIn(('systemctl', 'stop', session.RELAY), self.calls)

    def test_missing_units_and_interrupted_cleanup_are_retryable(self):
        self.enable()
        self.failure = lambda args: args[:3] == ('ufw', '--force', 'delete')
        with self.assertRaisesRegex(RuntimeError, 'injected'):
            session.disable()
        self.assertTrue((self.base / 'session.json').exists())
        self.assertTrue((self.base / 'firewall.json').exists())
        self.assertNotIn(('systemctl', 'stop', session.EXPIRE + '.timer'), self.calls)
        session.disable()
        self.assertFalse((self.base / 'session.json').exists())
        with patch.object(session, 'run', return_value='not-found') as command:
            session.stop('missing.service')
        command.assert_called_once_with('systemctl', 'show', 'missing.service',
                                        '--property=LoadState', '--value')

    def test_missing_files_and_nonroot_key_rejected(self):
        certificate = self.credentials / 'server.pem'
        certificate.unlink()
        with self.assertRaises(FileNotFoundError):
            self.enable()
        certificate.write_text('synthetic fixture')
        previous_stat = Path.stat

        def wrong_owner(path, *args, **kwargs):
            info = previous_stat(path, *args, **kwargs)
            if path == self.credentials / 'server-key.pem':
                fields = list(info)
                fields[4] = 501
                return os.stat_result(fields)
            return info

        with patch.object(Path, 'stat', wrong_owner), self.assertRaisesRegex(ValueError, 'root-owned'):
            self.enable()
        self.assertEqual(self.calls, [])

    def test_unknown_firewall_rules_and_later_env_rejected(self):
        for port in ('8446', '8400:8500'):
            self.rules[:] = [('allow', port)]
            with self.assertRaisesRegex(RuntimeError, 'UFW rules'):
                self.enable()
            self.assertFalse((self.base / 'session.json').exists())
        self.rules.clear()
        self.later_env = True
        with self.assertRaisesRegex(RuntimeError, 'final Web'):
            self.enable()
        self.assertEqual(self.rules, [])
        self.assertFalse((self.base / 'session.json').exists())

    def test_fleet_wireguard_relay_rule_is_tolerated_but_lookalikes_are_not(self):
        wg = '10.77.77.1 8446/tcp on wg-simloc ALLOW       10.77.77.2                 # SimLocation relay over WireGuard'
        lookalikes = ['8446/tcp                   ALLOW       Anywhere                   # SimLocation relay over WireGuard',
                      '10.77.77.1 8446/tcp on eth0 ALLOW       10.77.77.2                 # SimLocation relay over WireGuard',
                      '10.77.77.1 8446/tcp on wg-simloc ALLOW       10.77.77.2                 # other']
        for line in lookalikes:
            with self.subTest(line=line):
                self.rules[:] = [(line,)]
                with self.assertRaisesRegex(RuntimeError, 'UFW rules'):
                    self.enable()
        self.rules[:] = [(wg,)]
        self.enable()
        self.assertTrue((self.base / 'session.json').exists())
        self.assertIn((wg,), self.rules)

    def test_persistent_session_has_no_expiry_and_survives_reboot(self):
        self.expiry(datetime.now(timezone.utc) + timedelta(days=30))
        self.enable(None)
        unit = session.UNIT.read_text()
        self.assertIn('Restart=always', unit)
        self.assertIn('WantedBy=multi-user.target', unit)
        self.assertNotIn('RuntimeMaxSec', unit)
        self.assertIn('ProtectSystem=strict', unit)
        self.assertIn('ExecStart=/usr/bin/python3 -I -u ', unit)
        self.assertEqual(session.UNIT.stat().st_mode & 0o777, 0o644)
        self.assertIn(('systemctl', 'enable', '--now', session.UNIT.name), self.calls)
        self.assertFalse(any(call[0] == 'systemd-run' for call in self.calls))
        self.assertFalse(session.RECOVER.exists())
        self.assertIsNone(json.loads((self.base / 'server.json').read_text())['max_seconds'])
        self.assertEqual(self.states[session.NETWORK], 'active')
        status = session.status()
        self.assertEqual((status['session'], status['remaining_seconds']), ('persistent', None))
        session.disable()
        self.assertFalse(session.UNIT.exists())
        self.assertIn(('systemctl', 'disable', session.UNIT.name), self.calls)
        self.assertEqual(session.status(), {'session': 'disabled'})
        self.assertEqual(self.rules, [])

    def test_auto_set_only_for_persistent_and_restored_on_disable(self):
        self.expiry(datetime.now(timezone.utc) + timedelta(days=30))
        with self.assertRaisesRegex(ValueError, 'persistent'):
            session.enable(str(self.credentials), '8.8.8.8', 3600, auto_set=True)
        self.assertEqual(self.calls, [])
        session.enable(str(self.credentials), '8.8.8.8', None, auto_set=True)
        self.assertEqual((self.base / 'manual.env').read_text(), 'SIMLOCATION_WEB_AUTO_SET=1\n')
        self.assertTrue(session.status()['auto_set'])
        session.disable()
        self.assertFalse((self.base / 'manual.env').exists())
        self.assertEqual(session.status(), {'session': 'disabled'})

    def test_persistent_without_auto_set_keeps_it_off(self):
        self.expiry(datetime.now(timezone.utc) + timedelta(days=30))
        self.enable(None)
        self.assertEqual((self.base / 'manual.env').read_text(), session.ENV_TEXT)
        self.assertFalse(session.status()['auto_set'])

    def test_persistent_session_needs_three_hours_of_credentials(self):
        self.expiry(datetime.now(timezone.utc) + timedelta(hours=2))
        with self.assertRaisesRegex(ValueError, 'expire'):
            self.enable(None)
        self.assertEqual(self.calls, [])

    def test_persistent_rollback_removes_unit(self):
        self.expiry(datetime.now(timezone.utc) + timedelta(days=30))
        self.failure = lambda a: a == ('systemctl', 'restart', session.NETWORK)
        with self.assertRaisesRegex(RuntimeError, 'injected'):
            self.enable(None)
        self.assertFalse(session.UNIT.exists())
        self.assertFalse((self.base / 'session.json').exists())
        self.assertEqual(self.rules, [])

    def test_status_expiry_and_originally_inactive_web(self):
        self.states[session.WEB] = 'inactive'
        self.enable()
        self.assertNotIn(('systemctl', 'restart', session.WEB), self.calls)
        self.assertNotIn(('systemctl', 'start', session.WEB), self.calls)
        self.assertEqual(session.status()['session'], 'enabled')
        path = self.base / 'session.json'
        state = json.loads(path.read_text())
        state['expires_at'] = 0
        path.write_text(json.dumps(state))
        self.assertEqual(session.status()['session'], 'expired')
        self.assertEqual(session.status()['remaining_seconds'], 0)
        session.disable()  # The independent timer invokes this same entry point.
        self.assertEqual(self.states[session.WEB], 'inactive')
        self.assertEqual(self.states[session.NETWORK], 'inactive')


if __name__ == '__main__':
    unittest.main()
