import tempfile
import unittest
from pathlib import Path

from local_worker.listing_cannon_local_worker import cleanup_listing_artifacts, ensure_dirs


class WorkerCleanupTests(unittest.TestCase):
    def test_cleanup_removes_only_generated_copies(self):
        with tempfile.TemporaryDirectory() as temp_root:
            root = Path(temp_root) / 'worker'
            source_root = Path(temp_root) / 'originals'
            source_root.mkdir()
            original = source_root / 'art.jpg'
            original.write_bytes(b'original')
            dirs = ensure_dirs(root)
            processing = dirs['processing'] / 'art.jpg'
            processing.write_bytes(original.read_bytes())
            output_dir = dirs['output'] / 'art'
            output_dir.mkdir()
            rendered = output_dir / '01_room.jpg'
            rendered.write_bytes(b'rendered')
            render_work = dirs['work'] / output_dir.name
            render_work.mkdir()
            (render_work / 'temp.psd').write_bytes(b'work')
            analysis_dir = dirs['work'] / 'analysis_uploads'
            analysis_dir.mkdir()
            analysis = analysis_dir / 'art_analysis.jpg'
            analysis.write_bytes(b'analysis')

            cleanup_listing_artifacts(processing, [rendered], analysis, dirs, log=lambda _message: None)

            self.assertTrue(original.exists())
            self.assertFalse(processing.exists())
            self.assertFalse(output_dir.exists())
            self.assertFalse(render_work.exists())
            self.assertFalse(analysis.exists())

    def test_cleanup_refuses_external_source(self):
        with tempfile.TemporaryDirectory() as temp_root:
            root = Path(temp_root) / 'worker'
            dirs = ensure_dirs(root)
            external = Path(temp_root) / 'original.jpg'
            external.write_bytes(b'original')
            cleanup_listing_artifacts(external, [], None, dirs, log=lambda _message: None)
            self.assertTrue(external.exists())


if __name__ == '__main__':
    unittest.main()
