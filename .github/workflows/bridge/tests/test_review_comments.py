import io
import sys
import unittest
from contextlib import redirect_stdout
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api import Api, ApiError, BridgeError
from bridge import Bridge
from review_comments import CommentDelivery, plan_comments, preview_comments, read_comments


RAW = 'A **review**.\r\n\r\n- Keep `code`.  \n'
HEAD = 'a' * 40
MEMBER = {'id': 'member-account', 'username': 'reviewer'}
BOT = {'id': 'bot-account', 'username': 'robot'}


def comment(oid, minute, **fields):
    return dict({'id': oid, 'created_at': f'2026-11-01T12:{minute:02}:00Z',
                 'body': RAW, 'user': {'id': 7}}, **fields)


def review(oid, minute, state, body):
    row = comment(oid, minute, state=state, body=body)
    row['submitted_at'] = row.pop('created_at')
    return row


def root(oid, minute, **fields):
    return {**comment(oid, minute), 'path': 'file.py', 'side': 'RIGHT', 'line': 8, **fields}


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.source, self.target = Mock(), Mock()
        self.bridge = SimpleNamespace(github_api=self.source, target_api=self.target)
        self.rows = {'/issues/5/comments': [], '/pulls/5/reviews': [], '/pulls/5/comments': [],
                     '/pulls/5/files': [{'filename': 'file.py', 'sha': HEAD, 'patch': '@@ -8 +8 @@\n-old\n+new'}]}
        self.source.pages.side_effect = self.rows.__getitem__
        self.target.call.return_value = [{'filename': 'file.py', 'blob_id': HEAD,
                                          'patch': {'new_path': 'file.py', 'diff': '@@ -8 +8 @@\n-old\n+new'}}]

    def test_orders_all_sources_by_instant_and_keeps_distinct_receipt_keys_and_markdown(self):
        self.rows['/issues/5/comments'] = [comment(7, 4), comment(50, 1, body=' \t\n'),
                                           comment(51, 1, body=None)]
        self.rows['/pulls/5/reviews'] = [
            review(7, 6, 'APPROVED', None), review(8, 2, 'CHANGES_REQUESTED', RAW),
            review(9, 7, 'COMMENTED', RAW), review(10, 1, 'COMMENTED', ' \r\n'),
            {'id': 11, 'state': 'PENDING', 'body': 'Draft'}, review(12, 9, 'DISMISSED', ''),
        ]
        self.rows['/pulls/5/comments'] = [
            root(15, 8, line=99, original_line=11), root(7, 3), root(13, 1, subject_type='file', line=None),
            root(14, 5, side='LEFT', created_at='2026-11-01T13:05:00+01:00'),
            root(16, 9, in_reply_to_id=7), root(17, 9, pull_request_review_id=11),
        ]
        comments, replies = read_comments(self.source, 5)
        plan = [(item.key, kind, payload) for item, kind, payload in plan_comments(self.bridge, 5, comments, '/pulls/24')]
        self.assertEqual(replies, 1)
        self.assertEqual(plan, [
            ('13', 'pr', {'body': '> `file.py`\n\n' + RAW}),
            ('review:8', 'pr', {'body': '**CHANGES_REQUESTED**\n\n' + RAW}),
            ('7', 'diff', {'body': RAW, 'path': 'file.py', 'position': 8, 'position_type': 'text'}),
            ('issue:7', 'pr', {'body': RAW}), ('14', 'pr', {'body': '> `file.py` · LEFT L8\n\n' + RAW}),
            ('review:7', 'pr', {'body': '**APPROVED**'}), ('review:9', 'pr', {'body': RAW}),
            ('15', 'pr', {'body': '> `file.py` · 原第 11 行 · RIGHT · 已过时/无法挂行\n\n' + RAW}),
            ('review:12', 'pr', {'body': '**DISMISSED**'}), ('16', 'reply', {'body': RAW}),
        ])

    def test_locations_for_ranges_outdated_rows_and_unverified_single_lines(self):
        self.rows['/pulls/5/comments'] = [
            root(1, 1, start_line=4), root(2, 2, start_line=3, start_side='LEFT'),
            root(3, 3, side='LEFT', line=None, original_line=10),
            root(4, 4, position=None), root(5, 5, path='unlisted`name.py', original_line=12),
            root(6, 6, subject_type='file', path='line\nbreak.txt'),
            root(7, 7, outdated=True, original_line=8),
        ]
        comments, _ = read_comments(self.source, 5)
        bodies = [p['body'] for _, kind, p in plan_comments(self.bridge, 5, comments, '/pulls/24') if kind == 'pr']
        self.assertEqual(bodies, [header + '\n\n' + RAW for header in [
            '> `file.py` · RIGHT L4 – RIGHT L8', '> `file.py` · LEFT L3 – RIGHT L8',
            '> `file.py` · 原第 10 行 · LEFT · 已过时/无法挂行', '> `file.py` · RIGHT · 已过时/无法挂行',
            '> ``unlisted`name.py`` · 原第 12 行 · RIGHT · 已过时/无法挂行', '> `line\\nbreak.txt`',
            '> `file.py` · 原第 8 行 · RIGHT · 已过时/无法挂行',
        ]])

    def test_verifies_the_last_file_of_a_large_pr_with_one_gitcode_request(self):
        self.rows['/pulls/5/comments'] = [root(7, 1)]
        comments, _ = read_comments(self.source, 5)
        for count in (100, 107):
            with self.subTest(count=count):
                other_files = [{'filename': f'other-{i}.py'} for i in range(count - 1)]
                source_files = other_files + self.rows['/pulls/5/files']
                github = Api('github', None, 'org/source')
                github.call = Mock(side_effect=[source_files[:100], source_files[100:]])
                gitcode = Api('gitcode', None, 'org/target')
                gitcode.call = Mock(return_value=other_files + self.target.call.return_value)

                bridge = SimpleNamespace(github_api=github, target_api=gitcode)
                plan = [(kind, payload) for _, kind, payload in plan_comments(bridge, 5, comments, '/pulls/24')]
                self.assertEqual(plan, [('diff', {'body': RAW, 'path': 'file.py',
                                                 'position': 8, 'position_type': 'text'})])
                self.assertEqual(github.call.call_args_list, [
                    call('GET', '/pulls/5/files', page=1, per_page=100),
                    call('GET', '/pulls/5/files', page=2, per_page=100),
                ])
                gitcode.call.assert_called_once_with('GET', '/pulls/24/files')

    def test_unexpected_gitcode_file_response_stops_review_planning(self):
        self.rows['/pulls/5/comments'] = [root(7, 1)]
        comments, _ = read_comments(self.source, 5)
        self.target.call.return_value = {'message': 'files unavailable'}
        with self.assertRaisesRegex(BridgeError, r'/pulls/24/files'):
            list(plan_comments(self.bridge, 5, comments, '/pulls/24'))

    def test_preserves_bot_conversation_and_submitted_review_text(self):
        self.rows['/issues/5/comments'] = [comment(1, 1, user={'id': 42, 'type': 'Bot'})]
        self.rows['/pulls/5/reviews'] = [review(1, 2, 'APPROVED', RAW), review(2, 3, 'CHANGES_REQUESTED', ' \t'),
                                          review(3, 4, 'COMMENTED', None)]
        comments, _ = read_comments(self.source, 5)
        self.assertEqual([(c.key, c.body) for c in comments],
                         [('issue:1', RAW), ('review:1', '**APPROVED**\n\n' + RAW),
                          ('review:2', '**CHANGES_REQUESTED**')])
        self.assertEqual(comments[0].user['id'], 42)

    def test_preview_distinguishes_candidates_from_verified_lines_without_writes(self):
        self.rows['/issues/5/comments'] = [comment(7, 1)]
        self.rows['/pulls/5/comments'] = [root(7, 2), root(8, 3, line=99), root(9, 4, in_reply_to_id=7)]
        with redirect_stdout(io.StringIO()) as output:
            preview_comments(self.bridge, 5)
        self.assertIn('pr_comment=2, diff_comment=0, single-line candidates=1', output.getvalue())
        self.target.call.assert_not_called()
        with redirect_stdout(io.StringIO()) as output:
            preview_comments(self.bridge, 5, '/pulls/24', {'issue:7': {'kind': 'pr'}})
        self.assertIn('pr_comment=1, diff_comment=1, single-line candidates=0', output.getvalue())
        self.assertIn('thread replies=1 (nested on code reviews, quoted on ordinary comments)', output.getvalue())
        self.assertIn('unconfirmed writes=1', output.getvalue())
        with redirect_stdout(io.StringIO()) as output:
            preview_comments(self.bridge, 5, records={'9': '120'})
        self.assertIn('thread replies=0', output.getvalue())
        self.target.call.assert_called_once_with('GET', '/pulls/24/files')

    def test_orders_each_thread_after_its_root_even_on_ties_and_excludes_draft_replies(self):
        self.rows['/pulls/5/reviews'] = [{'id': 41, 'state': 'PENDING'}]
        self.rows['/pulls/5/comments'] = [
            root(10, 7, in_reply_to_id=2), root(11, 8, in_reply_to_id=10),
            root(2, 7, subject_type='file'), root(9, 7, in_reply_to_id=2, user={'id': 41}),
            root(4, 6, in_reply_to_id=3), root(3, 8), root(8, 3, in_reply_to_id=2),
            {'id': 12, 'in_reply_to_id': 2, 'pull_request_review_id': 41},
        ]
        items, count = read_comments(self.source, 5)
        self.assertEqual([item.key for item in items], ['2', '8', '9', '10', '3', '4', '11'])
        self.assertEqual([(i.key, i.reply_to, i.body) for i in items if i.reply_to],
                         [(key, parent, RAW) for key, parent in [('8', '2'), ('9', '2'), ('10', '2'),
                                                                  ('4', '3'), ('11', '2')]])
        self.assertEqual(count, 5)

    def test_missing_root_uses_quoted_pr_comments_unless_its_bare_receipt_exists(self):
        self.rows['/pulls/5/comments'] = [root(3, 2, in_reply_to_id=2, path=None),
                                         root(2, 1, in_reply_to_id=1, path='dir/a`b.py')]
        other_records = {'issue:1': '100', 'review:1': '101'}

        def plan(records):
            items, count = read_comments(self.source, 5, records)
            self.assertEqual(count, 2)
            planned = plan_comments(self.bridge, 5, items, '/pulls/24')
            return [(item.key, kind, payload) for item, kind, payload in planned]

        self.assertEqual(plan(other_records), [('2', 'pr', {'body': '> 回复 ``dir/a`b.py``\n\n' + RAW}),
                                               ('3', 'pr', {'body': '> 回复\n\n' + RAW})])
        self.assertEqual(plan({**other_records, '1': '102'}), [('2', 'reply', {'body': RAW}),
                                                               ('3', 'reply', {'body': RAW})])
        with redirect_stdout(io.StringIO()) as output:
            preview_comments(self.bridge, 5, records=other_records)
        self.assertIn('pr_comment=2', output.getvalue())
        self.assertIn('thread replies=0', output.getvalue())
        self.target.call.assert_not_called()

    def test_cyclic_reply_links_stop(self):
        self.rows['/pulls/5/comments'] = [root(2, 1, in_reply_to_id=1), root(1, 1, in_reply_to_id=2)]
        with self.assertRaisesRegex(BridgeError, 'Cyclic'):
            read_comments(self.source, 5)


class ReceiptStore:
    def __init__(self, records=None):
        self.progress = {'pending': {'head': HEAD}}
        self.review_comments = {'24': records or {}}

    def save(self, progress, *, review_comments):
        self.review_comments = deepcopy(review_comments)


class ReceiptTests(unittest.TestCase):
    def setUp(self):
        self.bridge = Bridge.__new__(Bridge)
        self.bridge.bot, self.bridge.target_api = BOT, Mock()
        self.api = self.bridge.target_api
        self.member_api = Mock()
        self.bridge.verified_members = {MEMBER['id']: (MEMBER, self.member_api)}
        self.bridge.member_secrets, self.bridge.unavailable_members = {}, set()
        self.store = ReceiptStore()
        self.intent = {'kind': 'pr', 'account': MEMBER, 'before': [], 'payload': {'body': RAW}}
        self.note = {'id': 123, 'discussion_id': 'discussion', 'comment_type': 'pr_comment', 'body': RAW,
                     'user': {'id': MEMBER['id'], 'login': MEMBER['username']}}
        # GitCode details return an internal user ID and omit comment_type and position for ordinary notes.
        self.detail = {'id': 123, 'discussion_id': 'discussion', 'body': RAW,
                       'user': {'id': '24680', 'login': MEMBER['username']}}

    def delivery(self):
        return CommentDelivery(self.bridge, self.store, '/pulls/24')

    def test_ordinary_comment_uses_the_speaker_and_keeps_an_existing_bare_receipt(self):
        self.store = ReceiptStore({'7': '120'})
        self.bridge.mapping = {'7': MEMBER}
        self.bridge.github_api, self.bridge.verify_pending_refs = Mock(), Mock()
        self.bridge.github_api.pages.side_effect = {
            '/issues/5/comments': [comment(7, 1)], '/pulls/5/reviews': [], '/pulls/5/comments': [],
        }.__getitem__
        existing = dict(self.note, id=121, discussion_id='existing')
        self.api.pages.side_effect = [[existing], [existing, self.note]]
        self.api.call.return_value = self.detail
        self.member_api.call.return_value = {'id': 'discussion', 'note_id': 123, 'body': RAW}
        with redirect_stdout(io.StringIO()):
            self.delivery().run({'number': 5})
            self.delivery().run({'number': 5})
        self.member_api.call.assert_called_once_with('POST', '/pulls/24/comments', {'body': RAW})
        self.api.call.assert_called_once_with('GET', '/pulls/comments/123')
        self.assertEqual(self.store.review_comments['24'], {'7': '120', 'issue:7': '123'})

    def test_rejected_member_then_lost_robot_response_recovers_one_write(self):
        self.member_api.call.side_effect = ApiError('gitcode', 403)
        self.note['user'] = {'id': BOT['id'], 'login': BOT['username']}
        self.detail['user']['login'] = BOT['username']
        self.api.pages.side_effect = [[], [], [self.note]]
        self.api.call.side_effect = [BridgeError('lost response'), self.detail]
        with self.assertRaisesRegex(BridgeError, 'lost response'), redirect_stdout(io.StringIO()):
            self.delivery().post('issue:7', self.intent)
        self.member_api.call.assert_called_once_with('POST', '/pulls/24/comments', {'body': RAW})
        self.api.call.assert_called_once_with('POST', '/pulls/24/comments', {'body': RAW})
        pending = self.store.review_comments['24']['issue:7']
        self.assertEqual((pending['kind'], pending['account']), ('pr', BOT))
        self.delivery().recover()
        self.delivery().recover()
        self.assertEqual(self.store.review_comments['24'], {'issue:7': '123'})
        for args in self.api.pages.call_args_list:
            self.assertEqual(args.kwargs, {'comment_type': 'pr_comment'})
        self.assertEqual(self.api.call.call_count, 2)

    def test_unknown_member_outcome_never_falls_back_and_ambiguous_writes_pause(self):
        self.api.pages.return_value = []
        self.member_api.call.side_effect = BridgeError('lost response')
        with self.assertRaisesRegex(BridgeError, 'lost response'):
            self.delivery().post('review:7', self.intent)
        with self.assertRaisesRegex(BridgeError, 'unconfirmed write'):
            self.delivery().recover()
        self.api.call.assert_not_called()
        self.api.pages.return_value = [self.note, dict(self.note, id=124, discussion_id='other')]
        self.api.call.side_effect = [self.detail, dict(self.detail, id=124, discussion_id='other')]
        with self.assertRaisesRegex(BridgeError, 'Ambiguous PR comment'):
            self.delivery().recover()
        self.member_api.call.assert_called_once()
        self.assertIsInstance(self.store.review_comments['24']['review:7'], dict)

    def test_closed_pr_rejection_clears_only_the_failed_intent(self):
        self.store = ReceiptStore({'7': '120'})
        self.api.pages.return_value = []
        self.member_api.call.side_effect = ApiError('gitcode', 405)
        with self.assertRaises(ApiError):
            self.delivery().post('issue:7', self.intent)
        self.assertEqual(self.store.review_comments['24'], {'7': '120'})
        self.api.call.assert_not_called()

    def test_pr_receipt_requires_a_new_matching_note_from_both_reads(self):
        delivery = self.delivery()
        intent = dict(self.intent, before=['122'], handle='discussion', note_id='123')
        for note in [dict(self.note, id=122), dict(self.note, comment_type='diff_comment'),
                     dict(self.note, id=124), dict(self.note, discussion_id='unrelated'),
                     dict(self.note, body=RAW.strip()), dict(self.note, user={'id': 'other', 'login': 'reviewer'})]:
            with self.subTest(note=note):
                self.assertIsNone(delivery.find_new(intent, [note]))
        self.api.call.assert_not_called()
        for detail in [dict(self.detail, discussion_id='other'), dict(self.detail, body=RAW.strip()),
                       dict(self.detail, id=124), dict(self.detail, user={'login': 'other'}),
                       dict(self.detail, position={'new_line': 8})]:
            with self.subTest(detail=detail):
                self.api.call.return_value = detail
                self.assertIsNone(delivery.find_new(intent, [self.note]))
        self.api.call.return_value = self.detail
        self.assertEqual(delivery.find_new(intent, [self.note]), '123')

    def test_legacy_bare_receipts_and_pending_intents_default_to_diff(self):
        pending = {'account': MEMBER, 'before': [], 'payload': {'body': RAW, 'position': 8, 'path': 'file.py'}}
        self.store = ReceiptStore({'6': '120', '7': pending})
        self.api.pages.return_value = [dict(self.note, comment_type='diff_comment',
                                            diff_position={'start_new_line': 8, 'end_new_line': 8})]
        self.api.call.return_value = dict(self.detail, position={'head_sha': HEAD, 'position_type': 'text',
                                                                 'new_path': 'file.py', 'new_line': 8})
        self.delivery().recover()
        self.api.pages.assert_called_once_with('/pulls/24/comments', comment_type='diff_comment')
        self.assertEqual(self.store.review_comments['24'], {'6': '120', '7': '123'})


class ThreadReceiptTests(unittest.TestCase):
    path = '/pulls/24/discussions/thread/comments'

    def setUp(self):
        self.store = ReceiptStore({'7': '120', 'issue:8': '110', 'review:8': '111'})
        self.source = [root(8, 2, in_reply_to_id=7)]
        self.bridge = Bridge.__new__(Bridge)
        self.bridge.github_api = SimpleNamespace(pages={'/issues/5/comments': [], '/pulls/5/reviews': [],
                                                        '/pulls/5/comments': self.source}.__getitem__)
        self.api, self.member = Mock(), Mock()
        self.bridge.target_api, self.bridge.bot = self.api, BOT
        self.bridge.mapping, self.bridge.verified_members = {'7': MEMBER}, {MEMBER['id']: (MEMBER, self.member)}
        self.bridge.unavailable_members, self.bridge.member_secrets = set(), {}
        self.bridge.verify_pending_refs = Mock()
        self.root = {'id': 120, 'discussion_id': 'thread', 'comment_type': 'diff_comment', 'reply': []}
        self.roots = [self.root]
        self.details = {'120': {'id': 120, 'discussion_id': 'thread'}}
        self.api.pages.side_effect = self.pages
        self.api.call.side_effect = self.read

    def pages(self, path, comment_type=None):
        self.assertEqual(path, '/pulls/24/comments')
        return deepcopy([r for r in self.roots if comment_type is None or comment_type == r['comment_type']])

    def read(self, method, path):
        self.assertEqual(method, 'GET')
        return self.details[path.rsplit('/', 1)[1]]

    def add_child(self, oid, account=MEMBER, body=RAW):
        user = {'id': account['id'], 'login': account['username']}
        self.root['reply'].append({'id': oid, 'user': user, 'body': body})
        self.details[str(oid)] = {'id': oid, 'user': dict(user, id='internal'), 'discussion_id': 'thread', 'body': body}
        if self.root['comment_type'] == 'diff_comment':
            self.details[str(oid)]['position'] = {'new_path': 'file.py', 'new_line': 8}

    def add_member_pr_comment(self, oid, body):
        note = {'id': oid, 'discussion_id': 'ordinary', 'comment_type': 'pr_comment', 'body': body,
                'user': {'id': MEMBER['id'], 'login': MEMBER['username']}}
        self.roots.append(note)
        self.details[str(oid)] = dict(note, user={'id': 'internal', 'login': MEMBER['username']})

    def deliver(self):
        with redirect_stdout(io.StringIO()):
            CommentDelivery(self.bridge, self.store, '/pulls/24').run({'number': 5})

    def test_first_reply_to_a_null_reply_list_recovers_when_the_child_has_no_discussion_id(self):
        self.root['reply'] = None

        def publish(method, path, payload):
            self.root['reply'] = []
            self.add_child(123)
            self.root['reply'][0]['discussion_id'] = None
            raise BridgeError('lost response')

        self.member.call.side_effect = publish
        with self.assertRaisesRegex(BridgeError, 'lost response'):
            self.deliver()
        self.assertEqual(self.store.review_comments['24']['8']['before'], [])
        self.deliver()
        self.deliver()
        self.member.call.assert_called_once_with('POST', self.path, {'body': RAW})
        self.assertEqual(self.store.review_comments['24']['8'], '123')

    def test_malformed_reply_lists_and_conflicting_discussions_stop_before_posting(self):
        for children in ({}, '', False, [None], [{'id': 123, 'discussion_id': 'other'}]):
            with self.subTest(children=children):
                self.root['reply'] = children
                with self.assertRaisesRegex(BridgeError, 'inconsistent discussion reply list'):
                    self.deliver()
                self.member.call.assert_not_called()
                self.assertNotIn('8', self.store.review_comments['24'])

    def test_exact_note_id_selects_the_posted_child_among_identical_replies_on_a_line(self):
        self.add_child(121)

        def publish(method, path, payload):
            self.assertEqual((method, path, payload), ('POST', self.path, {'body': RAW}))
            self.add_child(122)
            self.add_child(123)
            return {'id': 'thread', 'noteId': 123}

        self.member.call.side_effect = publish
        self.deliver()
        self.deliver()
        self.member.call.assert_called_once()
        self.assertEqual(self.store.review_comments['24'], {'7': '120', '8': '123', 'issue:8': '110', 'review:8': '111'})

    def test_lost_response_reconciles_only_unique_new_replies_in_the_right_thread(self):
        self.add_child(121)
        self.member.call.side_effect = BridgeError('lost response')
        with self.assertRaisesRegex(BridgeError, 'lost response'):
            self.deliver()
        intent = self.store.review_comments['24']['8']
        self.assertEqual((intent['kind'], intent['discussion_id'], intent['root_note_id'], intent['before']),
                         ('reply', 'thread', '120', ['121']))
        self.source.clear()
        self.roots.append(dict(self.root, id=155, discussion_id='other', reply=[dict(self.root['reply'][0], id=156)]))
        self.add_child(122, BOT)
        self.add_child(123, body=RAW.strip())
        self.add_child(124)
        self.details['124']['discussion_id'] = 'other'
        self.add_child(125)
        self.details['125']['user']['login'] = 'other'
        with self.assertRaisesRegex(BridgeError, 'unconfirmed write'):
            self.deliver()
        self.add_child(126)
        self.add_child(127)
        with self.assertRaisesRegex(BridgeError, 'Ambiguous discussion reply'):
            self.deliver()
        self.root['reply'].pop()
        self.deliver()
        self.assertEqual(self.store.review_comments['24']['8'], '126')
        self.member.call.assert_called_once()

    def test_definite_member_rejection_retries_the_same_nested_post_as_the_robot(self):
        def reject_member(*args):
            self.add_child(122, BOT)
            raise ApiError('gitcode', 403)

        def robot(method, path, payload=None):
            if method == 'GET':
                return self.read(method, path)
            self.assertEqual((method, path, payload), ('POST', self.path, {'body': RAW}))
            self.add_child(123, BOT)
            raise BridgeError('lost robot response')

        self.member.call.side_effect = reject_member
        self.api.call.side_effect = robot
        with self.assertRaisesRegex(BridgeError, 'lost robot response'):
            self.deliver()
        pending = self.store.review_comments['24']['8']
        self.assertEqual((pending['account'], pending['before']), (BOT, ['122']))
        self.deliver()
        self.member.call.assert_called_once_with('POST', self.path, {'body': RAW})
        self.assertEqual(self.store.review_comments['24']['8'], '123')
        self.assertEqual([c['id'] for c in self.root['reply']], [122, 123])

    def test_list_permission_error_before_writing_keeps_the_member_and_its_receipt_untouched(self):
        self.api.pages.side_effect = ApiError('gitcode', 403)
        with self.assertRaisesRegex(BridgeError, 'HTTP 403'):
            self.deliver()
        self.member.call.assert_not_called()
        self.assertFalse(self.bridge.unavailable_members)
        self.assertNotIn('8', self.store.review_comments['24'])

    def test_ordinary_root_reply_is_visible_and_recovers_as_one_top_level_comment(self):
        self.root['comment_type'] = 'pr_comment'
        expected = '> 回复 `file.py` [原评论](#tid-120)\n\n' + RAW

        def publish(method, path, payload):
            self.assertEqual((method, path, payload), ('POST', '/pulls/24/comments', {'body': expected}))
            self.add_member_pr_comment(124, expected)
            raise BridgeError('lost response')

        self.member.call.side_effect = publish
        with self.assertRaisesRegex(BridgeError, 'lost response'):
            self.deliver()
        record = self.store.review_comments['24']['8']
        self.assertEqual((record['kind'], record['account'], record['payload']), ('pr', MEMBER, {'body': expected}))
        self.source.clear()
        self.deliver()
        self.deliver()
        self.assertEqual(self.store.review_comments['24']['8'], '124')
        self.member.call.assert_called_once()
        self.assertFalse(self.root['reply'])

    def test_recovers_a_legacy_nested_write_on_an_ordinary_root_without_reposting(self):
        self.root['comment_type'] = 'pr_comment'
        self.store.review_comments['24']['8'] = {'kind': 'reply', 'root_note_id': '120', 'discussion_id': 'thread',
                                                 'before': [], 'payload': {'body': RAW}, 'account': MEMBER}
        self.add_child(124)
        self.deliver()
        self.deliver()
        self.assertEqual(self.store.review_comments['24']['8'], '124')
        self.member.call.assert_not_called()

    def test_orphan_recovers_a_single_top_level_comment_under_its_bare_id(self):
        del self.store.review_comments['24']['7']
        expected = '> 回复 `file.py`\n\n' + RAW

        def publish(method, path, payload):
            self.assertEqual((method, path, payload), ('POST', '/pulls/24/comments', {'body': expected}))
            self.add_member_pr_comment(124, expected)
            raise BridgeError('lost response')

        self.member.call.side_effect = publish
        with self.assertRaisesRegex(BridgeError, 'lost response'):
            self.deliver()
        saved = self.store.review_comments['24']['8']
        self.assertEqual((saved['kind'], saved['account'], saved['payload']), ('pr', MEMBER, {'body': expected}))
        self.source[0]['body'] = 'edited upstream'
        self.deliver()
        self.deliver()
        self.assertEqual(self.store.review_comments['24'], {'issue:8': '110', 'review:8': '111', '8': '124'})
        self.member.call.assert_called_once()

    def test_missing_source_root_with_an_unconfirmed_write_pauses_before_posting(self):
        self.store.review_comments['24']['7'] = {'kind': 'pr', 'account': MEMBER, 'before': [],
                                                 'payload': {'body': 'unconfirmed root'}}
        with self.assertRaisesRegex(BridgeError, 'unconfirmed write'):
            self.deliver()
        self.member.call.assert_not_called()
        self.assertNotIn('8', self.store.review_comments['24'])

    def test_delivery_errors_never_switch_to_the_other_reply_endpoint(self):
        for kind in ('diff_comment', 'pr_comment'):
            for status in (405, 501, 502):
                with self.subTest(kind=kind, status=status):
                    self.store.review_comments['24'].pop('8', None)
                    self.root['comment_type'] = kind
                    self.member.call.reset_mock()
                    self.member.call.side_effect = ApiError('gitcode', status)
                    with self.assertRaisesRegex(BridgeError, str(status)):
                        self.deliver()
                    nested = kind == 'diff_comment'
                    path = self.path if nested else '/pulls/24/comments'
                    body = RAW if nested else '> 回复 `file.py` [原评论](#tid-120)\n\n' + RAW
                    self.member.call.assert_called_once_with('POST', path, {'body': body})
                    record = self.store.review_comments['24'].get('8')
                    if status == 502:
                        self.assertEqual(record['kind'], 'reply' if nested else 'pr')
                    else:
                        self.assertIsNone(record)

    def test_readback_failure_after_a_successful_nested_post_keeps_the_nested_intent(self):
        def publish(*args):
            self.add_child(123)
            self.api.pages.side_effect = ApiError('gitcode', 405)
            return {'id': 'thread', 'note_id': 123}

        self.member.call.side_effect = publish
        with self.assertRaises(ApiError):
            self.deliver()
        self.assertEqual(self.store.review_comments['24']['8']['kind'], 'reply')
        self.api.pages.side_effect = self.pages
        self.deliver()
        self.assertEqual(self.store.review_comments['24']['8'], '123')
        self.member.call.assert_called_once_with('POST', self.path, {'body': RAW})

    def test_root_detail_must_match_a_root_on_this_pr_before_posting(self):
        self.details['120']['discussion_id'] = 'other'
        with self.assertRaisesRegex(BridgeError, 'Could not confirm reply root'):
            self.deliver()
        self.member.call.assert_not_called()
        self.assertNotIn('8', self.store.review_comments['24'])
