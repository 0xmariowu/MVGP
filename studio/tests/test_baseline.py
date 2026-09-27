import copy
import json
import sqlite3
import unittest

from studio.baseline import _path, compare, take


def store():
    conn = sqlite3.connect(':memory:')
    conn.executescript("""
        CREATE TABLE projects (project_id TEXT);
        CREATE TABLE objects (project_id TEXT, object_id TEXT, kind TEXT, current_revision INTEGER);
        CREATE TABLE revisions (project_id TEXT, object_id TEXT, revision INTEGER, body TEXT, digest TEXT, author TEXT);
        CREATE TABLE budgets (project_id TEXT, budget_key TEXT, unit TEXT, ceiling INTEGER, reserved INTEGER, spent INTEGER);
        CREATE TABLE reservations (project_id TEXT, reservation_id TEXT, budget_key TEXT, amount INTEGER, actual INTEGER, state TEXT);
        INSERT INTO projects VALUES ('platform_system'), ('p1'), ('p2');
        INSERT INTO objects VALUES ('p1', 'pick1', 'human-take-selection', 1);
        INSERT INTO revisions VALUES ('p1', 'pick1', 1, '{}', 'd1', 'decision_service');
        INSERT INTO budgets VALUES ('p1', 'fal_owner', 'usd_micro', 100, 0, 10);
        INSERT INTO reservations VALUES ('p1', 'r1', 'fal_owner', 10, 10, 'settled');
        INSERT INTO reservations VALUES ('p1', 'r2', 'hf_owner', 5, NULL, 'unknown');
    """)
    def membership(oid, pid, enabled):
        conn.execute("INSERT INTO objects VALUES ('platform_system', ?, 'project-membership', 1)", (oid,))
        conn.execute("INSERT INTO revisions VALUES ('platform_system', ?, 1, ?, 'x', 'member_project_service')",
                     (oid, json.dumps({'member_id': 'm1', 'project_id': pid, 'enabled': enabled})))
    membership('mem1', 'p1', True)
    membership('mem2', 'p2', False)
    return conn


class Baseline(unittest.TestCase):
    def test_counts_and_visible_set_by_enabled_memberships(self):
        b = take(store(), {'owner_member_ids': ['m1']})
        self.assertEqual(b['projects']['p1']['records']['human-take-selection']['count'], 1)
        self.assertEqual(b['visible'], ['p1'])
        self.assertNotIn('platform_system', b['projects'])

    def test_lean_rule_shows_all_but_hidden(self):
        self.assertEqual(take(store(), {'hidden_projects': ['p2']})['visible'], ['p1'])

    def test_equal_baselines_compare_equal(self):
        b = take(store(), {'owner_member_ids': ['m1']})
        self.assertEqual(compare(b, copy.deepcopy(b)), [])

    def test_a_changed_pick_is_a_difference(self):
        conn = store()
        before = take(conn, {'owner_member_ids': ['m1']})
        conn.execute("UPDATE revisions SET digest='d2' WHERE object_id='pick1'")
        self.assertTrue(compare(before, take(conn, {'owner_member_ids': ['m1']})))

    def test_new_projects_after_the_baseline_are_ignored(self):
        conn = store()
        before = take(conn, {'owner_member_ids': ['m1']})
        conn.execute("INSERT INTO projects VALUES ('p3')")
        self.assertEqual(compare(before, take(conn, {'hidden_projects': ['p2', 'p3']})), [])

    def test_a_newly_visible_old_project_is_a_difference(self):
        conn = store()
        before = take(conn, {'owner_member_ids': ['m1']})
        self.assertTrue(compare(before, take(conn, {'hidden_projects': []})))

    def test_intended_settlement_ignored_only_for_its_budget_key(self):
        conn = store()
        before = take(conn, {'owner_member_ids': ['m1']})
        conn.execute("UPDATE reservations SET state='settled', actual=5 WHERE reservation_id='r2'")
        after = take(conn, {'owner_member_ids': ['m1']})
        self.assertTrue(compare(before, after))
        self.assertEqual(compare(before, after, ignore_reservations={'hf_owner'}), [])

    def test_uri_is_accepted(self):
        self.assertEqual(_path('file:/x/m.sqlite?mode=ro'), '/x/m.sqlite')
        self.assertEqual(_path('/x/m.sqlite'), '/x/m.sqlite')


class StudioPathTests(unittest.TestCase):
    def test_idle_and_baseline_defaults_follow_the_studio_override(self):
        import os
        import runpy
        from pathlib import Path
        from types import ModuleType
        from unittest.mock import mock_open, patch
        root = Path(__file__).resolve().parents[1]
        with patch.dict(os.environ, {'MVGP_STUDIO': '/tmp/custom-studio'}):
            idle = runpy.run_path(str(root / 'idle_check.py'))
        self.assertEqual(idle['LIVE_DB'], '/tmp/custom-studio/state/live/metadata.sqlite')
        module = ModuleType('studio.idle_check')
        module.__dict__.update(idle)
        with patch.dict('sys.modules', {'studio.idle_check': module}):
            baseline = runpy.run_path(str(root / 'baseline.py'))
        opened = mock_open(read_data='{"hidden_projects": []}')
        with patch('builtins.open', opened), patch('sqlite3.connect', return_value=store()) as connected, patch('sys.stdout'):
            self.assertEqual(baseline['main']([]), 0)
        opened.assert_called_once_with('/tmp/custom-studio/config/live/api.json')
        connected.assert_called_once_with('file:/tmp/custom-studio/state/live/metadata.sqlite?immutable=1', uri=True)


if __name__ == '__main__':
    unittest.main()
