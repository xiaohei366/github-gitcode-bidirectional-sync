import json
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api import BridgeError
from bridge import workflow_range


class WorkflowEventTests(unittest.TestCase):
    def test_push_uses_the_event_boundaries(self):
        ref, before, after = 'refs/heads/master', '1' * 40, '2' * 40
        with TemporaryDirectory() as directory:
            event = Path(directory) / 'push.json'
            event.write_text(json.dumps({'ref': ref, 'before': before, 'after': after}), encoding='utf-8')
            env = {'GITHUB_EVENT_NAME': 'push', 'GITHUB_REF': ref, 'GITHUB_SHA': after, 'GITHUB_EVENT_PATH': str(event)}
            with patch.dict(os.environ, env, clear=True):
                self.assertEqual(workflow_range(), (before, after))

    def test_manual_run_uses_the_selected_revision(self):
        revision = '1' * 40
        env = {'GITHUB_EVENT_NAME': 'workflow_dispatch', 'GITHUB_SHA': revision}
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(workflow_range(), (None, revision))

    def test_scheduled_event_is_rejected(self):
        with patch.dict(os.environ, {'GITHUB_EVENT_NAME': 'schedule', 'GITHUB_SHA': '1' * 40}, clear=True):
            with self.assertRaises(BridgeError):
                workflow_range()
