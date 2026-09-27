"""Real-disk transactional invariants; no fake SQLite or network."""
import hashlib
import json
import marshal
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from production.contracts import DomainError
from production.store import Store


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "state.sqlite"
        self.store = Store(self.path)
        self.store.create_project("project_1", {"title": "The Last Bowl"}, "author_1")

    def tearDown(self):
        self.tmp.cleanup()

    def test_external_records_and_replays_preserve_logical_history(self):
        from production.contracts import content_hash
        from production.record_payloads import RecordPayloads
        from production.review_payloads import ReviewPayloads
        from production.tests.fixtures import Authority, Transport
        transport = Transport(Authority())
        self.store.record_payloads = RecordPayloads(ReviewPayloads(transport))
        body = {'state': 'ready', 'messages': ['complete evidence ' * 20000]}
        calls = []
        def create(conn):
            calls.append(1)
            return self.store.create_object('project_1', 'review-run', body, 'review_runner_service', conn=conn)
        result = self.store.run_idempotent('external', 'one', {'request': 1}, create)
        self.assertEqual(result['digest'], content_hash(body))
        self.store.append_revision('project_1', result['object_id'], 1,
                                   {**body, 'state': 'completed'}, 'review_runner_service')
        with self.store.transaction(write=False) as db:
            self.assertLess(db.execute('SELECT sum(length(body)) FROM revisions').fetchone()[0], 5000)
            raw = db.execute("SELECT result FROM idempotency WHERE scope='external'").fetchone()[0]
            self.assertLess(len(raw), 2000)
            self.assertEqual(self.store.decode_replay(raw), result)
        restarted = Store(self.path)
        restarted.record_payloads = RecordPayloads(ReviewPayloads(transport))
        self.assertEqual(restarted.get_object('project_1', result['object_id'], revision=1), result)
        self.assertEqual(restarted.run_idempotent('external', 'one', {'request': 1}, create), result)
        self.assertEqual(calls, [1])
        self.assertEqual(restarted.get_object('project_1', result['object_id'])['body']['state'], 'completed')
        with self.assertRaises(DomainError) as conflict:
            restarted.run_idempotent('external', 'one', {'request': 2}, create)
        self.assertEqual(conflict.exception.code, 'idempotency_conflict')
        self.assertEqual(calls, [1])

    def test_readonly_external_body_fetches_once_with_isolated_return_values(self):
        from production.record_payloads import RecordPayloads
        from production.review_payloads import ReviewPayloads
        from production.tests.fixtures import Authority, Transport
        transport = Transport(Authority())
        self.store.record_payloads = RecordPayloads(ReviewPayloads(transport))
        body = {'state': 'ready', 'messages': ['evidence ' * 20000]}
        item = self.store.create_object('project_1', 'review-run', body, 'review_runner_service')
        with self.store.transaction(write=False) as db:
            one = self.store.get_object('project_1', item['object_id'], conn=db)
            reads = len(transport.authority.gets)
            one['body']['messages'].append('forged')
            for _ in range(3):
                self.assertEqual(self.store.get_object('project_1', item['object_id'], conn=db), item)
            self.assertEqual(len(transport.authority.gets), reads)
        self.store.append_revision('project_1', item['object_id'], 1,
                                   {**body, 'state': 'completed'}, 'review_runner_service')
        with self.store.transaction(write=False) as db:
            self.assertEqual(self.store.get_object('project_1', item['object_id'], conn=db)['revision'], 2)
            self.assertEqual(self.store.get_object('project_1', item['object_id'], revision=1, conn=db), item)

    def test_readonly_body_decodes_once_and_returned_mutations_are_isolated(self):
        item = self.store.create_object('project_1', 'shot', {'nested': {'items': ['original']}}, 'author_1')
        with self.store.transaction(write=False) as conn, patch('production.store.json.loads', wraps=json.loads) as loads:
            one = self.store.get_object('project_1', item['object_id'], conn=conn)
            one['body']['nested']['items'].append('forged')
            two = self.store.get_object('project_1', item['object_id'], revision=1, conn=conn)
            self.assertEqual(two['body'], item['body'])
            self.assertEqual(loads.call_count, 1)
            with self.assertRaises(DomainError):
                self.store.get_object('project_other', item['object_id'], conn=conn)
        self.store.append_revision('project_1', item['object_id'], 1, {'updated': True}, 'author_1')
        with self.store.transaction(write=False) as conn:
            self.assertEqual(self.store.get_object('project_1', item['object_id'], conn=conn)['revision'], 2)
            self.assertEqual(self.store.get_object('project_1', item['object_id'], revision=1, conn=conn)['body'], item['body'])

    def test_body_cache_eviction_never_limits_readable_content(self):
        first = self.store.create_object('project_1', 'shot', {'value': 'one'}, 'author_1')
        second = self.store.create_object('project_1', 'shot', {'value': 'two'}, 'author_1')
        with patch('production.store.READ_BODY_CACHE_ENTRIES', 1), self.store.transaction(write=False) as conn, \
                patch('production.store.json.loads', wraps=json.loads) as loads:
            for item in (first, second, first):
                self.assertEqual(self.store.get_object('project_1', item['object_id'], conn=conn)['body'], item['body'])
            self.assertEqual(loads.call_count, 3)
        with patch('production.store.READ_BODY_CACHE_BYTES', 1), self.store.transaction(write=False) as conn, \
                patch('production.store.json.loads', wraps=json.loads) as loads:
            for _ in range(2):
                self.assertEqual(self.store.get_object('project_1', first['object_id'], conn=conn)['body'], first['body'])
            self.assertEqual(loads.call_count, 2)

    def test_body_cache_closes_on_error_and_never_caches_writer_reads(self):
        item = self.store.create_object('project_1', 'shot', {'value': 1}, 'author_1')
        with patch('production.store.json.loads', wraps=json.loads) as loads:
            with self.assertRaises(RuntimeError), self.store.transaction(write=False) as conn:
                self.store.get_object('project_1', item['object_id'], conn=conn)
                raise RuntimeError('abort')
            with self.store.transaction(write=False) as conn:
                self.store.get_object('project_1', item['object_id'], conn=conn)
            self.assertEqual(loads.call_count, 2)
        with self.store.transaction() as conn, patch('production.store.json.loads', wraps=json.loads) as loads:
            for _ in range(2):
                self.store.get_object('project_1', item['object_id'], conn=conn)
            self.assertEqual(loads.call_count, 2)
            self.store.append_revision('project_1', item['object_id'], 1, {'value': 2}, 'author_1', conn=conn)
            self.assertEqual(self.store.get_object('project_1', item['object_id'], conn=conn)['body'], {'value': 2})

    def test_overlapping_read_snapshots_keep_their_own_decoded_versions(self):
        item = self.store.create_object('project_1', 'shot', {'value': 1}, 'author_1')
        opened, updated = threading.Event(), threading.Event()
        def old_reader():
            with self.store.transaction(write=False) as conn:
                before = self.store.get_object('project_1', item['object_id'], conn=conn)
                opened.set()
                self.assertTrue(updated.wait(5))
                return before, self.store.get_object('project_1', item['object_id'], conn=conn)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(old_reader)
            self.assertTrue(opened.wait(5))
            self.store.append_revision('project_1', item['object_id'], 1, {'value': 2}, 'author_1')
            with self.store.transaction(write=False) as conn:
                current = self.store.get_object('project_1', item['object_id'], conn=conn)
                updated.set()
                before, retained = future.result(timeout=5)
        self.assertEqual(before, retained)
        self.assertEqual(retained['revision'], 1)
        self.assertEqual(current['revision'], 2)

    def test_shared_body_cache_reuses_decode_across_transactions_and_isolates_mutations(self):
        item = self.store.create_object('project_1', 'shot', {'nested': {'items': ['original']}}, 'author_1')
        reader = Store(self.path)
        with patch.object(reader, 'decode_record', wraps=reader.decode_record) as decode:
            with reader.transaction(write=False) as conn:
                one = reader.get_object('project_1', item['object_id'], conn=conn)
                self.assertEqual(one, item)
                one['body']['nested']['items'].append('forged')
            with reader.transaction(write=False) as conn:
                self.assertEqual(reader.get_object('project_1', item['object_id'], conn=conn), item)
                with self.assertRaises(DomainError) as missing:
                    reader.get_object('project_other', item['object_id'], conn=conn)
                self.assertEqual(missing.exception.code, 'not_found')
            self.assertEqual(decode.call_count, 1)
            changed = self.store.append_revision('project_1', item['object_id'], 1, {'updated': True}, 'author_1')
            with reader.transaction(write=False) as conn:
                self.assertEqual(reader.get_object('project_1', item['object_id'], conn=conn), changed)
                self.assertEqual(reader.get_object('project_1', item['object_id'], revision=1, conn=conn), item)
            self.assertEqual(decode.call_count, 2)

    def test_shared_body_cache_reuses_decode_without_readonly_scope_and_is_instance_local(self):
        item = self.store.create_object('project_1', 'shot', {'items': ['original']}, 'author_1')
        reader = Store(self.path)
        with patch.object(reader, 'decode_record', wraps=reader.decode_record) as decode:
            with reader.transaction() as conn:
                one = reader.get_object('project_1', item['object_id'], conn=conn)
                one['body']['items'].append('forged')
                self.assertEqual(reader.get_object('project_1', item['object_id'], conn=conn), item)
            self.assertEqual(reader.get_object('project_1', item['object_id']), item)
            self.assertEqual(decode.call_count, 1)
        other = Store(self.path)
        with patch.object(other, 'decode_record', wraps=other.decode_record) as decode:
            self.assertEqual(other.get_object('project_1', item['object_id']), item)
            self.assertEqual(decode.call_count, 1)

    def test_shared_body_cache_lru_bounds_and_oversized_bodies(self):
        items = [self.store.create_object('project_1', 'shot', {'value': value}, 'author_1')
                 for value in ('one', 'two', 'six')]
        oversized = self.store.create_object('project_1', 'shot', {'value': 'large' * 100}, 'author_1')
        size = len(marshal.dumps(items[0]['body']))
        for byte_bound, entry_bound in ((size * 2, 100), (size * 100, 2)):
            with self.subTest(bytes=byte_bound, entries=entry_bound), \
                    patch('production.store.SHARED_BODY_CACHE_BYTES', byte_bound), \
                    patch('production.store.SHARED_BODY_CACHE_ENTRIES', entry_bound):
                reader = Store(self.path)
                with patch.object(reader, 'decode_record', wraps=reader.decode_record) as decode:
                    for index in (0, 1, 0, 2, 0, 1):
                        self.assertEqual(reader.get_object('project_1', items[index]['object_id']), items[index])
                        cache = reader._shared_bodies
                        self.assertLessEqual(cache.encoded_bytes, byte_bound)
                        self.assertLessEqual(len(cache.entries), entry_bound)
                        self.assertEqual(cache.encoded_bytes, sum(map(len, cache.entries.values())))
                    self.assertEqual(decode.call_count, 4)
        with patch('production.store.SHARED_BODY_CACHE_BYTES', size):
            reader = Store(self.path)
            with patch.object(reader, 'decode_record', wraps=reader.decode_record) as decode:
                for _ in range(2):
                    self.assertEqual(reader.get_object('project_1', oversized['object_id']), oversized)
                self.assertEqual(decode.call_count, 2)
                self.assertEqual(reader._shared_bodies.encoded_bytes, 0)
                self.assertFalse(reader._shared_bodies.entries)

    def test_shared_body_cache_concurrent_misses_decode_outside_lock(self):
        item = self.store.create_object('project_1', 'shot', {'items': ['original']}, 'author_1')
        reader = Store(self.path)
        barrier = threading.Barrier(2)
        def decode(physical, digest):
            barrier.wait(timeout=5)
            return physical
        with patch.object(reader, 'decode_record', side_effect=decode) as calls, ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(reader.get_object, 'project_1', item['object_id']) for _ in range(2)]
            results = [future.result(timeout=10) for future in futures]
            self.assertEqual(calls.call_count, 2)
        self.assertEqual(results, [item, item])
        results[0]['body']['items'].append('forged')
        self.assertEqual(results[1], item)
        self.assertEqual(reader.get_object('project_1', item['object_id']), item)
        self.assertEqual(len(reader._shared_bodies.entries), 1)
        self.assertEqual(reader._shared_bodies.encoded_bytes, len(marshal.dumps(item['body'])))

    def test_append_history_and_reopen(self):
        item = self.store.create_object("project_1", "shot", {"action": "Wait"}, "author_1")
        changed = self.store.append_revision("project_1", item["object_id"], 1, {"action": "Offer bowl"}, "author_2")
        self.assertEqual(changed["revision"], 2)
        reopened = Store(self.path)
        history = reopened.history("project_1", item["object_id"])
        self.assertEqual([r["body"]["action"] for r in history], ["Wait", "Offer bowl"])
        self.assertNotEqual(history[0]["digest"], history[1]["digest"])
        self.assertEqual(reopened.get_object("project_1", item["object_id"], revision=1)["author"], "author_1")

    def test_cross_project_and_foreign_keys(self):
        item = self.store.create_object("project_1", "shot", {}, "author_1")
        self.store.create_project("project_2", {}, "author_1")
        with self.assertRaises(DomainError):
            self.store.get_object("project_2", item["object_id"])
        with self.assertRaises(DomainError):
            self.store.create_object("missing", "shot", {}, "author_1")
        with self.store.transaction() as conn:
            self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("INSERT INTO objects(project_id,object_id,kind,current_revision) VALUES ('missing','bad','shot',1)")

    def test_history_and_events_reject_rewrite(self):
        with self.store.transaction() as conn:
            for sql in ("UPDATE revisions SET author='forged'", "DELETE FROM revisions", "UPDATE events SET kind='forged'", "DELETE FROM events"):
                with self.subTest(sql=sql), self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(sql)
        self.assertEqual(len(self.store.events("project_1")), 1)

    def test_atomic_rollback_across_revision_budget_and_event(self):
        self.store.set_budget("project_1", 100, "USD-micros")
        with self.assertRaises(RuntimeError), self.store.transaction() as conn:
            self.store.append_revision("project_1", "project_1", 1, {"changed": True}, "author_1", conn=conn)
            self.store.reserve("project_1", "attempt_1", 80, "USD-micros", conn=conn)
            raise RuntimeError("crash before commit")
        self.assertEqual(self.store.get_object("project_1", "project_1")["revision"], 1)
        self.assertEqual(self.store.budget("project_1")["reserved"], 0)
        self.assertEqual(len(self.store.events("project_1")), 2)

    def test_caught_suboperation_cannot_leave_partial_reservation(self):
        self.store.set_budget("project_1", 100, "USD-micros")
        with self.store.transaction() as conn:
            with self.assertRaises(sqlite3.IntegrityError):
                self.store.reserve("project_1", "bad", 70, "USD-micros", object_id="missing", conn=conn)
            self.store.create_object("project_1", "shot", {}, "author_1", conn=conn)
        self.assertEqual(self.store.budget("project_1")["reserved"], 0)
        self.assertEqual(len(self.store.list_objects("project_1", kind="shot")), 1)

    def test_two_connections_compare_and_swap(self):
        barrier = threading.Barrier(2)
        def update(value):
            barrier.wait()
            try:
                self.store.append_revision("project_1", "project_1", 1, {"value": value}, "author_1")
                return "ok"
            except DomainError as exc:
                return exc.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(update, [1, 2]))
        self.assertCountEqual(results, ["ok", "revision_conflict"])
        self.assertEqual(len(self.store.history("project_1", "project_1")), 2)

    def test_idempotency_replays_result_conflicts_and_rolls_back(self):
        calls = []
        def mutate(conn):
            calls.append(1)
            return self.store.create_object("project_1", "shot", {"x": 1}, "author_1", conn=conn)
        first = self.store.run_idempotent("agent_1/project_1/create", "one", {"x": 1}, mutate)
        second = self.store.run_idempotent("agent_1/project_1/create", "one", {"x": 1}, mutate)
        self.assertEqual(first, second)
        self.assertEqual(len(calls), 1)
        with self.assertRaises(DomainError) as err:
            self.store.run_idempotent("agent_1/project_1/create", "one", {"x": 2}, mutate)
        self.assertEqual(err.exception.code, "idempotency_conflict")
        def fail(conn):
            self.store.create_object("project_1", "shot", {}, "author_1", object_id="will_rollback", conn=conn)
            raise RuntimeError("interrupted")
        with self.assertRaises(RuntimeError):
            self.store.run_idempotent("scope", "retry", {}, fail)
        self.assertEqual(self.store.run_idempotent("scope", "retry", {}, lambda _: "safe"), "safe")
        with self.assertRaises(DomainError):
            self.store.get_object("project_1", "will_rollback")

    def test_concurrent_same_key_runs_once(self):
        barrier = threading.Barrier(2)
        calls = []
        def mutate(conn):
            calls.append(1)
            return self.store.create_object("project_1", "shot", {}, "author_1", conn=conn)
        def run(_):
            barrier.wait()
            return self.store.run_idempotent("scope", "same", {}, mutate)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(run, [1, 2]))
        self.assertEqual(results[0], results[1])
        self.assertEqual(len(calls), 1)

    def test_integer_budget_and_immutable_unit(self):
        for amount in (True, 1.5, -1, 2**63):
            with self.subTest(amount=amount), self.assertRaises(DomainError):
                self.store.set_budget("project_1", amount, "USD-micros")
        self.store.set_budget("project_1", 100, "USD-micros")
        self.store.reserve("project_1", "r_1", 80, "USD-micros")
        for ceiling, unit in ((70, "USD-micros"), (100, "credits")):
            with self.assertRaises(DomainError):
                self.store.set_budget("project_1", ceiling, unit)
        with self.assertRaises(DomainError):
            self.store.reserve("project_1", "r_1", 81, "USD-micros")
        self.assertEqual(self.store.budget("project_1")["reserved"], 80)
        self.assertEqual([e["kind"] for e in self.store.events("project_1")],
                         ["object.created", "budget.changed", "budget.reserved"])

    def test_reservation_race_and_unknown_retention(self):
        self.store.set_budget("project_1", 100, "USD-micros")
        barrier = threading.Barrier(2)
        def reserve(key):
            barrier.wait()
            try:
                self.store.reserve("project_1", key, 70, "USD-micros")
                return key
            except DomainError as exc:
                return exc.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(reserve, ["attempt_1", "attempt_2"]))
        self.assertEqual(results.count("budget_exceeded"), 1)
        winner = next(x for x in results if x != "budget_exceeded")
        self.store.settle("project_1", winner, None)
        self.assertEqual(self.store.budget("project_1")["reserved"], 70)
        self.store.settle("project_1", winner, 40)
        self.store.settle("project_1", winner, 40)
        self.assertEqual(self.store.budget("project_1")["spent"], 40)
        self.assertEqual(self.store.budget("project_1")["reserved"], 0)
        with self.assertRaises(DomainError):
            self.store.settle("project_1", winner, 41)

    def test_cost_overrun_recorded_and_wrong_units_denied(self):
        self.store.set_budget("project_1", 100, "USD-micros")
        with self.assertRaises(DomainError):
            self.store.reserve("project_1", "attempt_bad", 10, "credits")
        self.store.reserve("project_1", "attempt_1", 80, "USD-micros")
        self.store.settle("project_1", "attempt_1", 120)
        self.assertEqual(self.store.budget("project_1")["spent"], 120)
        with self.assertRaises(DomainError):
            self.store.reserve("project_1", "attempt_2", 1, "USD-micros")

    def test_native_buckets_are_independent_and_default_is_legacy(self):
        self.store.set_budget("project_1", 100, "fake-units")
        self.store.set_budget("project_1", 20, "HF-credits", budget_key="hf_main")
        self.store.set_budget("project_1", 700, "USD-atoms", budget_key="deepseek")
        self.assertEqual(self.store.budget("project_1")["budget_key"], "legacy")
        self.store.reserve("project_1", "hf_job", 20, "HF-credits", budget_key="hf_main")
        self.store.reserve("project_1", "review", 600, "USD-atoms", budget_key="deepseek")
        with self.assertRaises(DomainError) as error:
            self.store.reserve("project_1", "hf_extra", 1, "HF-credits", budget_key="hf_main")
        self.assertEqual(error.exception.code, "budget_exceeded")
        rows = self.store.list_budgets("project_1")
        self.assertEqual([(r["budget_key"], r["reserved"], r["unit"]) for r in rows],
            [("deepseek", 600, "USD-atoms"), ("hf_main", 20, "HF-credits"), ("legacy", 0, "fake-units")])
        self.assertEqual(Store(self.path).list_budgets("project_1"), rows)
        self.store.create_project("empty_project", {}, "author")
        self.assertEqual(self.store.list_budgets("empty_project"), [])
        with self.assertRaises(DomainError):
            self.store.list_budgets("missing")

    def test_reservation_replay_cannot_switch_same_unit_account(self):
        for key in ("account_a", "account_b"):
            self.store.set_budget("project_1", 100, "quota", budget_key=key)
        original = self.store.reserve("project_1", "shared_id", 30, "quota", budget_key="account_a")
        self.assertEqual(self.store.reserve("project_1", "shared_id", 30, "quota", budget_key="account_a"), original)
        with self.assertRaises(DomainError) as error:
            self.store.reserve("project_1", "shared_id", 30, "quota", budget_key="account_b")
        self.assertEqual(error.exception.code, "idempotency_conflict")
        self.assertEqual(self.store.budget("project_1", budget_key="account_b")["reserved"], 0)
        self.assertEqual(self.store.settle("project_1", "shared_id", None)["budget_key"], "account_a")
        settled = Store(self.path).settle("project_1", "shared_id", 25)
        self.assertEqual((settled["budget_key"], settled["reserved"], settled["spent"]), ("account_a", 0, 25))
        self.assertEqual(self.store.settle("project_1", "shared_id", 25), settled)
        self.assertEqual(self.store.budget("project_1", budget_key="account_b")["spent"], 0)
        with self.assertRaises(TypeError):
            self.store.settle("project_1", "shared_id", 25, budget_key="account_b")
        events = [e for e in self.store.events("project_1") if e["kind"] in ("budget.unknown", "budget.settled")]
        self.assertEqual([e["body"]["budget_key"] for e in events], ["account_a", "account_a"])

    def test_bucket_key_validation_and_unit_immutability(self):
        for key in ("", "UPPER", "../account", "a/b", " x", "x ", "a" * 65, None, True, "é"):
            for call in (lambda key=key: self.store.set_budget("project_1", 10, "unit", budget_key=key),
                         lambda key=key: self.store.budget("project_1", budget_key=key),
                         lambda key=key: self.store.reserve("project_1", "r", 1, "unit", budget_key=key)):
                with self.subTest(key=key), self.assertRaises(DomainError) as error:
                    call()
                self.assertEqual(error.exception.code, "invalid_input")
        self.store.set_budget("project_1", 10, "quota", budget_key="a")
        with self.assertRaises(DomainError):
            self.store.set_budget("project_1", 10, "credits", budget_key="a")
        self.store.set_budget("project_1", 0, "credits", budget_key="b")
        self.assertEqual(self.store.budget("project_1", budget_key="b")["ceiling"], 0)

    def test_reservation_bucket_foreign_key_and_project_unique_id(self):
        self.store.set_budget("project_1", 20, "quota", budget_key="a")
        self.store.set_budget("project_1", 20, "quota", budget_key="b")
        self.store.create_project("project_2", {}, "author")
        self.store.set_budget("project_2", 20, "quota", budget_key="other_project_only")
        with self.store.transaction() as conn:
            for key in ("missing", "other_project_only"):
                with self.subTest(key=key), self.assertRaises(sqlite3.IntegrityError):
                    conn.execute("INSERT INTO reservations(project_id,reservation_id,budget_key,amount,state) VALUES (?,?,?,?,?)",
                                 ("project_1", "r", key, 1, "held"))
        self.store.reserve("project_1", "r", 1, "quota", budget_key="a")
        with self.store.transaction() as conn, self.assertRaises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO reservations(project_id,reservation_id,budget_key,amount,state) VALUES (?,?,?,?,?)",
                         ("project_1", "r", "b", 1, "held"))

    def test_named_bucket_races_and_unknown_are_isolated(self):
        for key in ("a", "b"):
            self.store.set_budget("project_1", 100, "quota", budget_key=key)
        barrier = threading.Barrier(4)
        def reserve(pair):
            key, identifier = pair
            barrier.wait()
            try:
                return self.store.reserve("project_1", identifier, 70, "quota", budget_key=key)
            except DomainError as error:
                return error.code
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(reserve, [("a", "a1"), ("a", "a2"), ("b", "b1"), ("b", "b2")]))
        self.assertEqual(results.count("budget_exceeded"), 2)
        winners = [r for r in results if isinstance(r, dict)]
        self.assertCountEqual([r["budget_key"] for r in winners], ["a", "b"])
        for winner in winners:
            self.store.settle("project_1", winner["reservation_id"], None)
        self.assertEqual([b["reserved"] for b in Store(self.path).list_budgets("project_1")], [70, 70])
        with self.assertRaises(RuntimeError), self.store.transaction() as conn:
            for winner in winners:
                self.store.settle("project_1", winner["reservation_id"], 50, conn=conn)
            raise RuntimeError("rollback both independent settlements")
        self.assertEqual([(b["reserved"], b["spent"]) for b in self.store.list_budgets("project_1")], [(70, 0), (70, 0)])

    def test_same_reservation_race_across_buckets_has_one_winner(self):
        for key in ("a", "b"):
            self.store.set_budget("project_1", 100, "quota", budget_key=key)
        barrier = threading.Barrier(2)
        def reserve(key):
            barrier.wait()
            try:
                self.store.reserve("project_1", "same", 50, "quota", budget_key=key)
                return "ok"
            except DomainError as error:
                return error.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(reserve, ["a", "b"]))
        self.assertCountEqual(results, ["ok", "idempotency_conflict"])
        self.assertCountEqual([b["reserved"] for b in self.store.list_budgets("project_1")], [0, 50])

    def test_v1_refusal_does_not_change_journal_schema_or_bytes(self):
        legacy = Path(self.tmp.name) / "legacy.sqlite"
        with sqlite3.connect(legacy) as conn:
            conn.execute("CREATE TABLE budgets(project_id TEXT PRIMARY KEY,ceiling INTEGER,unit TEXT,reserved INTEGER,spent INTEGER)")
            conn.execute("INSERT INTO budgets VALUES ('old',100,'old-unit',60,20)")
            conn.execute("PRAGMA user_version=1")
        before = hashlib.sha256(legacy.read_bytes()).hexdigest()
        with self.assertRaises(DomainError) as error:
            Store(legacy)
        self.assertEqual(error.exception.code, "release_mismatch")
        self.assertEqual(hashlib.sha256(legacy.read_bytes()).hexdigest(), before)
        self.assertFalse(Path(str(legacy) + "-wal").exists())
        with sqlite3.connect(legacy) as conn:
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "delete")
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT * FROM budgets").fetchone(), ("old", 100, "old-unit", 60, 20))

    def test_newer_schema_is_not_mutated(self):
        with sqlite3.connect(self.path) as conn:
            conn.execute("PRAGMA user_version=999")
        with self.assertRaises(DomainError):
            Store(self.path)
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 999)

    def activation(self, rid, revision=0):
        if revision == 0:
            self.store.create_project("platform_system", {"internal": True}, "operator")
            return self.store.create_object("platform_system", "release-activation",
                {"release_id": rid, "epoch": 1}, "operator", object_id="active_release")
        return self.store.append_revision("platform_system", "active_release", revision,
            {"release_id": rid, "epoch": revision + 1}, "operator")

    def test_consistent_backup_and_no_overwrite(self):
        target = Path(self.tmp.name) / "backup.sqlite"
        self.store.backup(target)
        self.assertEqual(Store(target).get_object("project_1", "project_1")["body"]["title"], "The Last Bowl")
        with self.assertRaises(FileExistsError):
            self.store.backup(target)


if __name__ == "__main__":
    unittest.main()
