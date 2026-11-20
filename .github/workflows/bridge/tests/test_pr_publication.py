import sys
import unittest
from http.client import IncompleteRead
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call, patch
from urllib.error import URLError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api import Api, ApiError, ApiTransportError, BridgeError
from bridge import Bridge


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.bridge = Bridge.__new__(Bridge)
        self.bridge.source = SimpleNamespace(repo='example/source')
        self.bridge.target = SimpleNamespace(repo='example/target')
        self.bridge.target_api = Mock()
        self.bridge.bot = {'id': '11', 'username': 'robot'}
        self.pending = {'branch': 'bridge/topic', 'head': 'a' * 40, 'source': 'b' * 40, 'github_pull': '7',
                        'creator': {'id': '12', 'username': 'member'}}
        self.progress = {'cursor': 'c' * 40, 'pending': self.pending}
        self.store = Mock()
        self.path = '/pulls/12'
        self.original = {'title': 'Original title', 'body': 'A **review**.\r\n\r\n- Keep `code`.  \r\n'}
        self.source_link = '[GitHub PR #7](https://github.com/example/source/pull/7)'
        self.target_link = '[GitCode PR #12](https://gitcode.com/example/target/pull/12)'
        self.draft = {'number': 12, 'user': {'id': 12, 'login': 'member'}, 'base': {'ref': 'master'},
                      'head': {'ref': self.pending['branch'], 'sha': self.pending['head']}, 'state': 'open',
                      'draft': True, 'title': 'WIP: Original title', 'body': self.source_link}
        self.fields = {'title': self.original['title'], 'body': self.source_link + ' <-> ' + self.target_link
                       + '\n\n' + self.original['body'], 'draft': False}
        self.ready = {**self.draft, **self.fields}
        timer = patch('bridge.sleep')
        self.sleep = timer.start()
        self.addCleanup(timer.stop)

    def publish(self, reads, writes=(), *, direct=False):
        reads, writes = iter(reads), iter(writes)

        def respond(method, path, fields=None):
            self.assertEqual(path, self.path)
            self.assertIn(method, ('GET', 'PATCH'))
            value = next(reads) if method == 'GET' else next(writes)
            if isinstance(value, Exception):
                raise value
            return value

        self.bridge.target_api.call.side_effect = respond
        return self.bridge.prepare_pull(self.store, self.progress, '12', None if direct else self.original)

    def patches(self):
        return [args[2] for args, _ in self.bridge.target_api.call.call_args_list if args[0] == 'PATCH']

    def test_confirmed_publication_needs_one_read(self):
        self.assertEqual(self.publish([self.ready]), (self.path, False))
        self.assertEqual(self.bridge.target_api.call.call_count, 1)
        self.sleep.assert_not_called()

    def test_waits_for_stale_reads_without_writing_twice(self):
        self.assertEqual(self.publish([self.draft, self.draft, self.draft, self.ready], [self.ready]),
                         (self.path, False))
        self.assertEqual(self.patches(), [self.fields])
        self.assertEqual(self.sleep.call_args_list, [call(2), call(5)])

    def test_repairs_only_the_field_still_missing_after_the_first_update(self):
        partial = {**self.ready, 'body': self.draft['body']}
        self.assertEqual(self.publish([self.draft] + [partial] * 4 + [self.ready], [self.ready, None]),
                         (self.path, False))
        self.assertEqual(self.patches(), [self.fields, {'body': self.fields['body']}])
        self.assertEqual(self.sleep.call_args_list, [call(2), call(5), call(10)])

    def test_confirms_an_uncertain_write_and_retries_temporary_read_errors(self):
        for failure in (ApiTransportError('response lost'), ApiError('gitcode', 502)):
            with self.subTest(failure=failure):
                self.bridge.target_api.reset_mock()
                self.assertEqual(self.publish([ApiError('gitcode', 429), self.draft,
                                               ApiError('gitcode', 503), self.ready], [failure]), (self.path, False))
                self.assertEqual(self.patches(), [self.fields])

    def test_an_unknown_last_read_cannot_authorize_a_retry(self):
        with self.assertRaisesRegex(BridgeError, 'still pending'):
            self.publish([self.draft, self.draft] + [ApiError('gitcode', 503)] * 3, [None])
        self.assertEqual(self.patches(), [self.fields])
        self.assertEqual(self.store.save.call_args.args[0]['pending']['number'], '12')

    def test_keeps_the_known_number_when_initial_reads_are_unavailable(self):
        with self.assertRaisesRegex(BridgeError, 'still pending'):
            self.publish([ApiTransportError('timed out')] * 4)
        self.assertEqual(self.patches(), [])
        self.assertEqual(self.store.save.call_args.args[0]['pending']['number'], '12')

    def test_persistent_mismatch_stops_and_identifies_only_the_missing_fields(self):
        partial = {**self.ready, 'body': self.fields['body'].rstrip()}
        with self.assertRaisesRegex(BridgeError, r'GitCode PR #12: body\.') as caught:
            self.publish([self.draft] + [partial] * 8, [None, None])
        self.assertNotIn(self.original['body'].strip(), str(caught.exception))
        self.assertEqual(self.patches(), [self.fields, {'body': self.fields['body']}])

    def test_rejects_permanent_errors_and_changed_pr_identity(self):
        for entry in (ApiError('gitcode', 403), ApiError('gitcode', 422)):
            with self.subTest(error=entry), self.assertRaises(ApiError):
                self.publish([self.draft], [entry])
        self.sleep.assert_not_called()
        for changes in ({'number': 99}, {'base': {'ref': 'other'}}, {'head': {'ref': 'other'}},
                        {'user': {'id': 13, 'login': 'other'}}, {'state': 'closed'}):
            with self.subTest(changes=changes), self.assertRaises(BridgeError):
                self.publish([self.draft, {**self.ready, **changes}], [None])

    def test_observes_a_merge_during_readback(self):
        landed = {**self.ready, 'state': 'merged', 'head': None}
        self.assertEqual(self.publish([self.draft, landed], [None]), (self.path, True))
        self.assertEqual(self.patches(), [self.fields])

    def test_direct_push_repair_keeps_notes_added_while_waiting(self):
        self.pending['github_pull'] = None
        source = f'[GitHub 变更](https://github.com/example/source/compare/{"c" * 40}...{"b" * 40})'
        initial = {**self.draft, 'body': '- ' + source}
        updated = {**initial, 'body': initial['body'] + '\n\nMaintainer note', 'draft': False}
        final = {**updated, 'body': updated['body'] + '\n\n- ' + self.target_link}
        self.assertEqual(self.publish([initial] + [updated] * 4 + [final], [None, None], direct=True),
                         (self.path, False))
        self.assertEqual(self.patches(), [{'body': initial['body'] + '\n- ' + self.target_link, 'draft': False},
                                          {'body': final['body']}])

    def test_api_classifies_a_lost_connection(self):
        api = Api('gitcode', '', 'example/target')
        api.opener = Mock()
        for error in (URLError('connection reset'), IncompleteRead(b'')):
            with self.subTest(error=error), self.assertRaises(ApiTransportError):
                api.opener.open.side_effect = error
                api.call('PATCH', self.path, self.fields)


if __name__ == '__main__':
    unittest.main()
