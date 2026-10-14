"""Exercise the real route/runner code without starting app services or a database."""
import ast
import copy
from datetime import datetime
from pathlib import Path
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from bulk_edit_patch import build_product_patch


def load_function(name, scope):
    tree = ast.parse(Path(__file__).with_name('app.py').read_text(encoding='utf-8'))
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
    node.decorator_list = []
    exec(compile(ast.Module(body=[node], type_ignores=[]), 'app.py', 'exec'), scope)
    return scope[name]


class BulkRouteTests(unittest.TestCase):
    def test_manual_apply_queues_without_shopify_requests(self):
        thread = Mock()
        data = {'confirmation': 'APPLY 1', 'originals': [{'id': 'p', 'title': 'Old'}],
                'enhancements': [{'originalIndex': 0, 'enhanced': {'product_id': 'p', 'title': 'New'}}]}
        scope = {
            'request': SimpleNamespace(get_json=lambda: data), 'jsonify': lambda value: value,
            'get_current_shop': lambda: SimpleNamespace(id=1, shop_domain='test', access_token='fake'),
            'current_user': SimpleNamespace(id=1), 'uuid': SimpleNamespace(uuid4=lambda: 'job'),
            'datetime': datetime, 'csv_enhance_jobs': {}, 'csv_enhance_jobs_lock': threading.Lock(),
            'threading': SimpleNamespace(Thread=thread), 'build_product_patch': build_product_patch,
            '_create_bulk_edit_snapshot': Mock(return_value='snapshot'), '_save_csv_enhance_jobs': Mock(),
            '_run_csv_apply_job': Mock(), 'logger': Mock(),
        }
        route = load_function('apply_enhanced_shopify_products', scope)
        result, status = route()
        self.assertEqual(status, 202)
        self.assertEqual(result['job_id'], 'job')
        self.assertEqual(scope['csv_enhance_jobs']['job']['products'], data['originals'])
        scope['_run_csv_apply_job'].assert_not_called()
        thread.return_value.start.assert_called_once()
        self.assertEqual(thread.call_args.kwargs['args'], ('job', 'test', 'fake'))

    def run_job(self, success=True, cancelled=False, checkpoint_error=False):
        job = {'status': 'applying', 'products': [{'id': 'p', 'title': 'Old', 'vendor': 'Vendor'}],
               'settings': {'only_fields': ['title']}, 'enhancements': [{'enhanced': {
                   'product_id': 'p', 'title': 'Manually corrected', 'vendor': 'AI changed',
                   '_no_change': True, 'price_lists_json': '[{"price_list_id":"unexpected"}]'}}]}
        job['cancel_requested'] = cancelled
        update = Mock(return_value={'success': success, 'error': '' if success else 'rejected'})
        scope = {'csv_enhance_jobs': {'job': job}, 'csv_enhance_jobs_lock': threading.Lock(),
                 'datetime': datetime, 'build_product_patch': build_product_patch,
                 '_resolve_enhanced_category': lambda value: value, 'update_product_seo_metadata': update,
                 '_apply_category_attribute_metafields': Mock(), '_save_csv_enhance_jobs': Mock(),
                 '_append_csv_job_log': Mock(), 'time': SimpleNamespace(sleep=lambda n: None), 'logger': Mock()}
        if checkpoint_error:
            scope['_save_csv_enhance_jobs'].side_effect = [RuntimeError('database unavailable'), None]
        load_function('_run_csv_apply_job', scope)('job', 'test', 'fake')
        return job, update

    def test_runner_recomputes_stale_no_change_and_enforces_scope(self):
        job, update = self.run_job()
        update.assert_called_once_with('p', {'product_id': 'p', 'title': 'Manually corrected'},
                                       shop_domain='test', access_token='fake')
        self.assertEqual(job['status'], 'applied')

    def test_runner_records_completion_with_errors(self):
        job, update = self.run_job(False)
        self.assertEqual(job['status'], 'applied_with_errors')
        self.assertFalse(job['apply_results'][0]['success'])

    def test_cancellation_stops_before_next_write(self):
        job, update = self.run_job(cancelled=True)
        update.assert_not_called()
        self.assertEqual(job['status'], 'cancelled')

    def test_failed_checkpoint_prevents_shopify_write(self):
        job, update = self.run_job(checkpoint_error=True)
        update.assert_not_called()
        self.assertEqual(job['status'], 'error')
        self.assertEqual(job['in_flight']['product_id'], 'p')

    def test_resume_uses_apply_phase_and_rejects_uncertain_write(self):
        job = {'user_id': 1, 'status': 'paused', 'phase': 'apply'}
        thread = Mock()
        scope = {'csv_enhance_jobs': {'job': job}, 'csv_enhance_jobs_lock': threading.Lock(),
                 '_ensure_csv_job_loaded': Mock(), '_save_csv_enhance_jobs': Mock(),
                 'get_current_shop': lambda: SimpleNamespace(shop_domain='test', access_token='fake'),
                 'current_user': SimpleNamespace(id=1), '_bulk_job_shop_mismatch': lambda *args: False,
                 'jsonify': lambda value: value, 'threading': SimpleNamespace(Thread=thread),
                 '_run_csv_apply_job': Mock(), '_run_csv_enhance_job': Mock(), 'logger': Mock()}
        resume = load_function('resume_bulk_seo_job', scope)
        self.assertTrue(resume('job')['success'])
        self.assertIs(thread.call_args.kwargs['target'], scope['_run_csv_apply_job'])
        thread.reset_mock()
        job.update(status='paused', in_flight={'product_id': 'p'})
        self.assertEqual(resume('job')[1], 409)
        thread.assert_not_called()


if __name__ == '__main__':
    unittest.main()
