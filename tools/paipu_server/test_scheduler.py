import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import server


class AnalysisSchedulerTest(unittest.TestCase):
    def test_jobs_run_in_submission_order_without_overlap(self):
        first_started = threading.Event()
        release_first = threading.Event()
        second_started = threading.Event()
        all_finished = threading.Event()
        order = []

        def target(job_id):
            order.append(f"start-{job_id}")
            if job_id == 1:
                first_started.set()
                release_first.wait(timeout=2)
            else:
                second_started.set()
            order.append(f"end-{job_id}")
            if job_id == 2:
                all_finished.set()

        scheduler = server.AnalysisScheduler(target)
        scheduler.submit(1)
        self.assertTrue(first_started.wait(timeout=1))
        scheduler.submit(2)
        self.assertFalse(second_started.wait(timeout=0.05))
        release_first.set()
        self.assertTrue(all_finished.wait(timeout=1))
        self.assertEqual(order, ["start-1", "end-1", "start-2", "end-2"])

    def test_prune_finished_jobs_preserves_active_and_recent_statuses(self):
        jobs = {
            "old-error": {"status": "error", "updated_at": 1},
            "old-done": {"status": "done", "updated_at": 2},
            "new-error": {"status": "error", "updated_at": 3},
            "new-done": {"status": "done", "updated_at": 4},
            "queued": {"status": "queued", "updated_at": 0},
            "running": {"status": "running", "updated_at": 0},
        }
        with patch.object(server, "JOBS", jobs):
            server.prune_finished_jobs(keep=2)

        self.assertEqual(set(jobs), {"new-error", "new-done", "queued", "running"})

    def test_pending_queue_has_a_hard_limit(self):
        first_started = threading.Event()
        release_first = threading.Event()
        all_finished = threading.Event()
        completed = []

        def target(job_id):
            if job_id == 1:
                first_started.set()
                release_first.wait(timeout=2)
            completed.append(job_id)
            if job_id == 2:
                all_finished.set()

        scheduler = server.AnalysisScheduler(target, max_pending=1)
        self.assertTrue(scheduler.submit(1))
        self.assertTrue(first_started.wait(timeout=1))
        self.assertTrue(scheduler.submit(2))
        self.assertFalse(scheduler.submit(3))
        release_first.set()
        self.assertTrue(all_finished.wait(timeout=1))
        self.assertEqual(completed, [1, 2])


if __name__ == "__main__":
    unittest.main()
