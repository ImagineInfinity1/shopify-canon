"""Checkpoint integration tests against an isolated, disposable database."""
import unittest
from flask import Flask
from extensions import db
import bulk_job_store as store


class JobStoreTests(unittest.TestCase):
    def setUp(self):
        app = Flask(__name__)
        app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite://'
        db.init_app(app)
        self.context = app.app_context()
        self.context.push()
        db.create_all()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()

    def test_incremental_checkpoint_and_owner_isolation(self):
        metadata = {'user_id': 7, 'status': 'applying', 'apply_index': 0}
        store.save_snapshot('job', metadata, {'products': [(0, {'id': 'p'})]})
        metadata['apply_index'] = 1
        store.save_snapshot('job', metadata, {'apply_results': [(0, {'success': True})]})
        db.session.remove()
        self.assertIsNone(store.load_job('job', 8))
        job = store.load_job('job', 7)
        self.assertEqual(job['products'], [{'id': 'p'}])
        self.assertEqual(job['apply_results'], [{'success': True}])
        self.assertEqual(job['apply_index'], 1)
        self.assertEqual(store.BulkJobItem.query.count(), 2)

    def test_replacement_removes_stale_rows(self):
        meta = {'user_id': 7}
        store.save_snapshot('job', meta, {'enhancements': [(0, 'old'), (1, 'stale')]})
        store.save_snapshot('job', meta, {'enhancements': [(0, 'new')]}, reset=True)
        self.assertEqual(store.load_job('job', 7)['enhancements'], ['new'])

    def test_restart_preserves_phase_and_uncertain_write(self):
        store.save_snapshot('job', {'user_id': 7, 'status': 'applying',
                                  'in_flight': {'product_id': 'p'}}, {})
        store.pause_interrupted_jobs()
        job = store.load_job('job', 7)
        self.assertEqual(job['status'], 'paused')
        self.assertEqual(job['phase'], 'apply')
        self.assertEqual(job['in_flight']['product_id'], 'p')

    def test_incomplete_checkpoint_refuses_resume(self):
        store.save_snapshot('job', {'user_id': 7}, {'products': [(1, 'missing-first')]})
        with self.assertRaisesRegex(RuntimeError, 'incomplete'):
            store.load_job('job', 7)

    def test_owner_cannot_change(self):
        store.save_snapshot('job', {'user_id': 7}, {})
        with self.assertRaises(ValueError):
            store.save_snapshot('job', {'user_id': 8}, {})


if __name__ == '__main__':
    unittest.main()
