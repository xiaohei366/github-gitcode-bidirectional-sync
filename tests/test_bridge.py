from contextlib import contextmanager, redirect_stdout
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, call, patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / '.github' / 'workflows' / 'bridge'))

from api import Api, ApiError, BridgeError
from bridge import Bridge, commit_email, config_read, main, workflow_range
from git_ops import Git, MASTER_REF, Remote, STATE_REF
from state_store import GitStore, StateError


BOT = {'id': 'gc-bot', 'username': 'sync-bot', 'email': 'bot@example.com'}
MAPPED = {'id': 'gc-alice', 'username': 'alice-gc', 'email': 'alice@example.com'}
BOB = {'id': 'gc-bob', 'username': 'bob-gc', 'email': 'bob@example.com'}
ALICE_SECRET = 'GITCODE_MEMBER_ALICE_TOKEN'
BOB_SECRET = 'GITCODE_MEMBER_BOB_TOKEN'
GITHUB = 'public/project'
GITCODE = 'private/internal-project'
SOURCE = 'refs/heads/main'


def public_account(account):
    return {'id': account['id'], 'username': account['username']}


def make_tree(git, files):
    rows = []
    for name, content in sorted(files.items()):
        if isinstance(content, dict):
            entry = b'040000 tree ' + make_tree(git, content)
        else:
            entry = b'100644 blob ' + git.run('hash-object', '-w', '--stdin', binary=True,
                                                input_data=content).stdout.strip()
        rows.append(entry + b'\t' + name.encode() + b'\n')
    return git.run('mktree', binary=True, input_data=b''.join(rows)).stdout.strip()


def make_commit(git, files, message, parents=(), encoding=None):
    tree = make_tree(git, files)
    headers = [b'tree ' + tree]
    headers.extend(b'parent ' + parent.encode() for parent in parents)
    headers.append(b'author Raw github name <source@example.com> 1700000000 +0800')
    headers.append(b'committer Raw github account <different@example.com> 1700000010 +0000')
    if encoding:
        headers.append(b'encoding ' + encoding)
    return git.run('hash-object', '-t', 'commit', '-w', '--stdin', binary=True,
                   input_data=b'\n'.join(headers) + b'\n\n' + message).stdout.strip().decode()


class FixtureApi:
    def __init__(self, fixture, provider, token, repo):
        self.fixture, self.provider, self.token, self.repo = fixture, provider, token, repo
        self.account = {'bot-token': BOT, 'alice-token': MAPPED, 'bob-token': BOB, 'wrong-token': BOB}.get(token)

    def request(self, method, path):
        self.fixture.calls.append((self.provider, method, path))
        assert method == 'GET' and self.provider == 'gitcode'
        if path == '/emails':
            if self.account is not BOT:
                return [{'email': self.account['email'], 'state': 'confirmed'}]
            if self.fixture.bot_email is None:
                return []
            return [{'email': self.fixture.bot_email, 'state': 'confirmed'}]
        if path == '/user':
            if self.account is None:
                raise ApiError('gitcode', 401)
            return {'id': self.account['id'], 'login': self.account['username'],
                    'email': self.fixture.bot_profile_email if self.account is BOT else self.account['email']}
        members = {'/users/alice-gc': MAPPED, '/users/bob-gc': BOB, '/users/sync-bot': BOT}
        member = members[path]
        return {'id': member['id'], 'login': member['username']}

    def pages(self, path, **params):
        self.fixture.calls.append((self.provider, 'GET', path))
        if path.startswith('/pulls/') and path.endswith('/files'):
            return iter(deepcopy(self.fixture.review_files[self.provider]))
        if self.provider == 'gitcode' and path.endswith('/comments'):
            kind = params.get('comment_type')
            assert kind in (None, 'pr_comment', 'diff_comment')
            return iter(deepcopy([note for note in self.fixture.review_notes.get(path.split('/')[2], [])
                                  if kind is None or note['comment_type'] == kind]))
        if self.provider == 'github':
            if path.startswith('/issues/') and path.endswith('/events'):
                return iter(deepcopy(self.fixture.source_events.get(path.split('/')[2], [])))
            if path.startswith('/issues/') and path.endswith('/comments'):
                return iter(deepcopy(self.fixture.source_issue_comments.get(path.split('/')[2], [])))
            if path.startswith('/pulls/') and path.endswith('/reviews'):
                return iter(deepcopy(self.fixture.source_reviews.get(path.split('/')[2], [])))
            if path.startswith('/pulls/') and path.endswith('/comments'):
                return iter(deepcopy(self.fixture.source_comments.get(path.split('/')[2], [])))
            assert path.startswith('/commits/') and path.endswith('/pulls')
            return iter(self.fixture.source_pulls.get(path.split('/')[2], []))
        assert path == '/pulls' and params['base'] == 'master'
        return iter(deepcopy([pull for pull in self.fixture.target_pulls.values()
                              if pull['user']['login'] == params['author']]))

    def call(self, method, path='', body=None):
        self.fixture.calls.append((self.provider, method, path))
        code = self.fixture.deny_once.pop((self.token, method, path), None)
        if code:
            raise ApiError(self.provider, code)
        if not path:
            assert method == 'GET'
            return self.fixture.info[self.repo]
        if self.provider == 'github':
            assert method == 'GET'
            if path.startswith('/pulls/'):
                return deepcopy(self.fixture.pull_details[path.split('/')[-1]])
            assert path.startswith('/commits/')
            return {'author': self.fixture.authors.get(path.split('/')[-1])}
        if method == 'GET' and path.startswith('/pulls/comments/'):
            return deepcopy(self.fixture.review_details[path.rsplit('/', 1)[1]])
        if method == 'POST' and path.endswith('/comments'):
            number = path.split('/')[2]
            assert self.fixture.target_pulls[number]['state'] == 'open'
            inline = 'path' in body
            if inline:
                assert set(body) == {'body', 'position', 'position_type', 'path'} and body['position_type'] == 'text'
            else:
                assert set(body) == {'body'}
            self.fixture.review_writes.append((self.token, deepcopy(body)))
            notes = self.fixture.review_notes.setdefault(number, [])
            note_id = 100 + len(notes) + sum(len(note['reply']) for note in notes if note.get('reply'))
            if '/discussions/' in path:
                assert not inline
                discussion = path.split('/')[4]
                root = next(note for note in notes if note['discussion_id'] == discussion)
                child = {'id': note_id, 'body': body['body'], 'discussion_id': None,
                         'user': {'id': self.account['id'], 'login': self.account['username']}}
                if root.get('reply') is None:
                    root['reply'] = []
                root['reply'].append(child)
                detail = {**child, 'discussion_id': discussion,
                          'user': {'id': 'internal-user-id', 'login': self.account['username']}}
                if root['comment_type'] == 'diff_comment':
                    detail['position'] = deepcopy(self.fixture.review_details[str(root['id'])]['position'])
                self.fixture.review_details[str(note_id)] = detail
                self.fixture.maybe_lose_response('reply')
                return {'id': discussion, 'note_id': note_id, 'body': body['body']}
            shared = {'id': note_id, 'discussion_id': f'discussion-{note_id}', 'body': body['body']}
            note = {**shared, 'reply': None, 'user': {'id': self.account['id'], 'login': self.account['username']},
                    'comment_type': 'diff_comment' if inline else 'pr_comment'}
            detail = {**shared, 'user': {'id': 'internal-user-id', 'login': self.account['username']}}
            if inline:
                note['diff_position'] = {'start_new_line': body['position'], 'end_new_line': body['position'],
                                         'position_type': 'text'}
                detail.update(is_outdated=False, position={'new_path': self.fixture.returned_note_path or body['path'],
                              'new_line': body['position'], 'head_sha': self.fixture.target_pulls[number]['head']['sha'],
                              'position_type': 'text'})
            notes.append(note)
            self.fixture.review_details[str(note_id)] = detail
            self.fixture.maybe_lose_response('comment')
            return {'id': f'discussion-{note_id}', 'note_id': note_id, 'body': body['body']}
        self.fixture.pr_calls.append((method, self.token))
        if method == 'POST':
            assert path == '/pulls' and body['base'] == 'master'
            assert body['draft'] and not body['squash'] and not body['close_related_issue']
            branch = body['head']
            head = self.fixture.target.run('rev-parse', f'refs/heads/{branch}').stdout.strip()
            number = str(len(self.fixture.target_pulls) + 1)
            self.fixture.target_pulls[number] = {
                'number': int(number), 'state': 'open',
                'user': {'id': self.account['id'], 'login': self.account['username']},
                'head': {'ref': branch, 'sha': head}, 'base': {'ref': body['base']},
                'draft': body['draft'], 'title': body['title'], 'body': body['body'],
            }
            self.fixture.maybe_lose_response('create')
            return {'number': int(number)}

        number = path.split('/')[2]
        pull = self.fixture.target_pulls[number]
        if method == 'PATCH':
            pull.update(body)
            if self.fixture.trim_published_body:
                pull['body'] = pull['body'].rstrip()
            if self.fixture.change_branch_before_merge:
                self.fixture.target.run('update-ref', f'refs/heads/{pull["head"]["ref"]}', self.fixture.master)
        elif method == 'PUT':
            assert path == f'/pulls/{number}/merge'
            assert body == {'merge_method': 'merge', 'squash': False, 'force_merge': False}
            assert not pull['draft']
            assert f'](https://gitcode.com/{GITCODE}/pull/{number})' in pull['body']
            assert f'](https://github.com/{GITHUB}/' in pull['body']
            self.fixture.merge_pull(pull)
            self.fixture.maybe_lose_response('merge')
        else:
            assert method == 'GET'
        return deepcopy(pull)


class BridgeTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.source = Git(self.root / 'source')
        self.target = Git(self.root / 'target')
        # Local pushes must finish touching the temporary remote before cleanup.
        self.target.run('config', 'receive.autogc', 'false')
        self.info = {
            GITHUB: {'id': 1, 'full_name': GITHUB, 'default_branch': 'main'},
            GITCODE: {'id': 2, 'full_name': GITCODE, 'private': 1},
        }
        self.calls, self.authors = [], {}
        self.pr_calls = []
        self.source_pulls, self.pull_details, self.target_pulls = {}, {}, {}
        self.source_comments, self.source_events = {}, {}
        self.source_issue_comments, self.source_reviews = {}, {}
        self.review_files = {'github': [], 'gitcode': []}
        self.review_notes, self.review_writes = {}, []
        self.review_details, self.returned_note_path = {}, None
        self.lose_response = None
        self.deny_once = {}
        self.trim_published_body = False
        self.change_branch_before_merge = False
        self.bot_email = BOT['email']
        self.bot_profile_email = None
        self.source_files = {'common.txt': b'first\nsecond\nthird\n'}
        self.source_base = self.source_commit(self.source_files, b'initial public', [])
        self.target_files = dict(self.source_files)
        self.master = self.change_master(self.target_files, [])
        self.event_before = self.source_base
        self.cfg = {'gitcode': {'repo': GITCODE, 'bot': BOT},
                    'members': [{'github': {'id': '10', 'username': 'alice-gh'}, 'gitcode': public_account(MAPPED),
                                 'gitcode_pr_token_secret': ALICE_SECRET}]}
        self.member_tokens = {ALICE_SECRET: 'alice-token', BOB_SECRET: 'bob-token'}
        self.run_bridge(before=None)
        self.calls.clear()

    def remote(self, provider, name, token, username):
        remote = Remote(provider, name, token, username)
        repo = {GITHUB: self.source, GITCODE: self.target}[name]
        remote.url = str(repo.directory)
        return remote

    @contextmanager
    def session(self, token='bot-token', *, before=..., after=None):
        with TemporaryDirectory(dir=self.root) as directory:
            root = Path(directory)
            with patch('bridge.Api', lambda *args: FixtureApi(self, *args)), patch('bridge.Remote', self.remote):
                bridge = Bridge(self.cfg, Git(root / 'import'), GITHUB, token, member_tokens=self.member_tokens)
                store = GitStore(Git(root / 'state'), bridge.target, bridge.binding, BOT)
                yield bridge, store

    def event_range(self, before, after):
        after = after or self.source.run('rev-parse', SOURCE).stdout.strip()
        if before is ...:
            before = None if self.event_before == after else self.event_before
        return before, after

    def run_bridge(self, *, check=False, before=..., after=None):
        before, after = self.event_range(before, after)
        with self.session(before=before, after=after) as (bridge, store), redirect_stdout(io.StringIO()) as output:
            bridge.run(store, before, after, check=check)
        return output.getvalue()

    def source_commit(self, files, message, parents, account=None, ref=SOURCE, encoding=None):
        oid = make_commit(self.source, files, message, parents, encoding=encoding)
        self.source.run('update-ref', ref, oid)
        self.authors[oid] = {'id': account} if account else None
        return oid

    def change_master(self, files, parents):
        oid = make_commit(self.target, files, b'private change', parents)
        self.target.run('update-ref', MASTER_REF, oid)
        return oid

    def target_head(self):
        return self.target.run('rev-parse', MASTER_REF).stdout.strip()

    def source_pull(self, commit, number, *, opener=10, merged=True, branch='main', title=None, body=None,
                    topic_head=None):
        self.pull_details[str(number)] = {'number': number, 'title': title or f'Source PR {number}', 'body': body,
                                          'user': {'id': opener} if opener is not None else None}
        if merged:
            self.source_events[str(number)] = [{'event': 'merged', 'commit_id': commit}]
        if topic_head:
            self.pull_details[str(number)].update(merged=merged, head={'sha': topic_head})
        self.source_pulls.setdefault(commit, []).append({
            'number': number, 'merged_at': '2026-11-16' if merged else None,
            'base': {'ref': branch, 'repo': {'id': 1}},
        })

    def merge_pull(self, pull, merger=BOT):
        master, head = self.target_head(), pull['head']['sha']
        tree = self.target.run('merge-tree', '--write-tree', master, head).stdout.strip()
        merge = self.target.run('-c', f'user.name={merger["username"]}', '-c', f'user.email={merger["email"]}',
                                'commit-tree', tree, '-p', master, '-p', head, '-m', 'Merge import').stdout.strip()
        self.target.run('update-ref', MASTER_REF, merge)
        self.target.run('update-ref', '-d', f'refs/heads/{pull["head"]["ref"]}')
        pull['state'] = 'merged'

    def maybe_lose_response(self, operation):
        if self.lose_response == operation:
            self.lose_response = None
            raise BridgeError('response lost')

    def test_cli_loads_separate_members_and_private_config(self):
        cfg_file = self.root / 'bridge.json'
        cfg_file.write_text(json.dumps({'gitcode': self.cfg['gitcode']}))
        members_file = self.root / 'members.json'
        members = [{'github': {'id': str(i * 10), 'username': str(i)}, 'gitcode': public_account(account),
                    'gitcode_pr_token_secret': secret}
                   for i, (account, secret) in enumerate(((MAPPED, ALICE_SECRET), (BOB, BOB_SECRET)), 1)]
        members_file.write_text(json.dumps(members))
        env = {'PATH': os.environ['PATH'], 'GITHUB_REPOSITORY': GITHUB, 'GITHUB_TOKEN': 'source-read',
               'GITHUB_EVENT_NAME': 'workflow_dispatch', 'GITHUB_SHA': self.source_base,
               'GITCODE_ROBOT_TOKEN': 'bot-token', **self.member_tokens}
        clients = []

        def api(provider, token, repo):
            clients.append((provider, token))
            return FixtureApi(self, provider, token, repo)

        before = self.target.run('show-ref').stdout
        args = ['bridge', '--config', str(cfg_file), '--members', str(members_file), '--check']
        with patch.dict(os.environ, env, clear=True), patch.object(sys, 'argv', args):
            with patch('bridge.Api', api), patch('bridge.Remote', self.remote), redirect_stdout(io.StringIO()) as output:
                main()
        self.assertIn('CHECK 0 source commits', output.getvalue())
        self.assertCountEqual(clients, [('github', 'source-read'), ('gitcode', 'bot-token'),
                                        ('gitcode', 'alice-token'), ('gitcode', 'bob-token')])
        self.assertEqual(self.target.run('show-ref').stdout, before)

    def test_import_preserves_individual_authors_trees_and_raw_messages(self):
        first_message = b'Fixes #19\r\n\r\nMessage with spaces  \r\n\r\nCo-authored-by: A <a@source.test>\r\n'
        first_files = {**self.source_files, 'image.bin': b'\x00binary data\xff'}
        first = self.source_commit(first_files, first_message, [self.source_base], account=10)
        second_message = b'caf\xe9\n\nNo newline at the end'
        second_files = {**first_files, 'common.txt': b'first\nchanged\nthird\n'}
        second = self.source_commit(second_files, second_message, [first], account=11, encoding=b'ISO-8859-1')
        self.source_commit({'unmerged.txt': b'pending\n'}, b'not merged', [second], ref='refs/heads/topic')

        target_refs = self.target.run('show-ref').stdout
        output = self.run_bridge(check=True)
        self.assertIn(f'{first} | mapped', output)
        self.assertIn(f'{second} | bot', output)
        for value in (BOT['email'], BOT['username'], MAPPED['email'], MAPPED['username'], GITCODE):
            self.assertNotIn(value, output)
        self.assertEqual(self.target.run('show-ref').stdout, target_refs)
        self.assertTrue(all(method == 'GET' for _, method, _ in self.calls))

        self.assertIn('IMPORTED 2', self.run_bridge())
        head = self.target_head()
        imported = self.target.first_parent_range(self.target_pulls['1']['head']['sha'], self.master)
        self.assertEqual(len(imported), 2)
        self.assertTrue(self.target.is_ancestor(imported[-1], head))
        self.assertIn(f'](https://github.com/{GITHUB}/compare/{self.source_base}...{second})',
                      self.target_pulls['1']['body'])
        for oid, message, account in zip(imported, [first_message, second_message], [MAPPED, BOT]):
            raw = self.target.run('cat-file', 'commit', oid, binary=True).stdout
            headers, actual = raw.split(b'\n\n', 1)
            self.assertEqual(actual, message)
            identity = f'{account["username"]} <{account["email"]}>'.encode()
            self.assertIn(b'author ' + identity + b' 1700000000 +0800', headers.splitlines())
            self.assertIn(b'committer ' + identity + b' 1700000010 +0000', headers.splitlines())
        self.assertIn(b'encoding ISO-8859-1', headers)
        self.assertEqual(self.target.tree(imported[0]), self.source.tree(first))
        self.assertEqual(self.target.tree(head), self.source.tree(second))
        self.assertIn('CURRENT', self.run_bridge())
        self.assertEqual(self.target_head(), head)
        self.assertEqual(self.source.run('rev-parse', SOURCE).stdout.strip(), second)
        with self.session() as (_, store):
            self.assertEqual(store.progress['master'], head)
            self.assertEqual(store.progress['cursor'], second)
        state = self.target.run('cat-file', 'commit', self.target.run('rev-parse', STATE_REF).stdout.strip(),
                                binary=True).stdout
        self.assertIn(b'author sync-bot <bot@example.com> ', state)
        self.assertIn(b'committer sync-bot <bot@example.com> ', state)
        self.assertTrue(all(method == 'GET' for provider, method, _ in self.calls if provider == 'github'))

    def test_normal_merge_rebuilds_each_topic_author_message_and_parent_tree(self):
        main_files = {**self.source_files, 'main-only': b'new on main\n'}
        base = self.source_commit(main_files, b'new main', [self.source_base])
        self.event_before = base
        self.cfg['members'].append({'github': {'id': '20', 'username': 'bob-gh'}, 'gitcode': public_account(BOB),
                                       'gitcode_pr_token_secret': BOB_SECRET})
        self.master = self.change_master(main_files, [])
        self.target.run('update-ref', '-d', STATE_REF)
        self.run_bridge(before=None, after=base)
        side_files = {**self.source_files, 'side': b'from the PR\n'}
        first = self.source_commit(side_files, b'mapped topic message', [self.source_base],
                                   account=10, ref='refs/heads/topic')
        second = self.source_commit({**side_files, 'more': b'next'}, b'unmapped topic message', [first],
                                    ref='refs/heads/topic')
        merge_message = b'Merge pull request #56\n\nExact message\n'
        source = self.source_commit({**main_files, **side_files, 'more': b'next', 'resolved': b'merge resolution\n'},
                                    merge_message, [base, second], account=10)
        self.source_pull(source, 56, opener=20, topic_head=second)
        self.assertIn('IMPORTED 3', self.run_bridge())
        pull = self.target_pulls['1']
        self.assertEqual(pull['user']['login'], BOB['username'])
        head = pull['head']['sha']
        for original, rev, account in ((first, head + '^2^', MAPPED), (second, head + '^2', BOT),
                                       (source, head, MAPPED)):
            imported = self.target.run('rev-parse', rev).stdout.strip()
            self.target.verify_identity(imported, account)
            self.assertEqual(self.target.tree(imported), self.source.tree(original))
            self.assertEqual(self.target.run('rev-parse', rev + '^^{tree}').stdout,
                             self.source.run('rev-parse', original + '^^{tree}').stdout)
            self.assertEqual(self.target.run('cat-file', 'commit', imported, binary=True).stdout.split(b'\n\n', 1)[1],
                             self.source.run('cat-file', 'commit', original, binary=True).stdout.split(b'\n\n', 1)[1])
        anchor = self.target.run('rev-parse', head + '^2^^').stdout.strip()
        self.target.verify_identity(anchor, BOT)
        self.assertIn('Anchor imported branch', self.target.run('show', '-s', '--format=%B', anchor).stdout)
        self.target.verify_identity(self.target_head(), BOT)
        self.assertEqual(self.target.run('show', f'{head}:resolved').stdout, 'merge resolution\n')

    def test_native_pr_uses_confirmed_old_fork_and_resumes_the_same_review_and_landing(self):
        review = self.review_pr()
        topic = self.source.run('rev-parse', SOURCE).stdout.strip()
        files = {**self.source_files, 'upstream': b'keep parallel work'}
        main = self.source_commit(files, b'main change', [self.source_base])
        self.source_pull(main, 4)
        self.run_bridge(after=main)
        old_master = self.target_head()
        outer = self.source_commit({**files, 'common.txt': b'first\nchanged\nthird\n', 'moved.txt': b'different\n'},
                                   b'outer merge', [main, topic])
        self.source_pull(outer, 5, topic_head=topic)
        self.source_comments['5'] = [review, dict(review, id=12, in_reply_to_id=11)]
        refs = self.target.run('show-ref').stdout
        self.assertIn('CHECK 1 source commits', self.run_bridge(check=True))
        self.assertEqual(self.target.run('show-ref').stdout, refs)
        self.lose_response = 'comment'
        with self.assertRaisesRegex(BridgeError, 'response lost'):
            self.run_bridge()
        with self.session() as (_, store):
            pending = store.progress['pending']
            self.assertTrue(pending['native_merge'])
            self.assertEqual(pending['topic_parents'], {self.source_base: self.master})
            head = pending['head']
            self.assertEqual(self.target.parents(head), [self.master])
            self.assertEqual(self.target.tree(head), self.source.tree(topic))
            self.assertNotEqual(self.target.tree(head), self.source.tree(outer))
        self.member_tokens.clear()
        self.pull_details['5']['head']['sha'] = main
        self.lose_response = 'merge'
        with self.assertRaisesRegex(BridgeError, 'response lost'):
            self.run_bridge()
        self.run_bridge()
        landed = self.target_head()
        self.assertEqual(self.target.parents(landed), [old_master, head])
        self.assertEqual(self.target.tree(landed), self.source.tree(outer))
        self.target.verify_identity(head, MAPPED)
        self.target.verify_identity(landed, BOT)
        self.assertEqual(len(self.target_pulls), 2)
        self.assertEqual([token for token, _ in self.review_writes], ['alice-token', 'bot-token'])
        self.assertEqual(self.review_details['100']['position']['head_sha'], head)
        self.assertEqual(self.review_details['101']['discussion_id'], self.review_details['100']['discussion_id'])
        with self.session() as (_, store):
            self.assertEqual(store.progress, {'cursor': outer, 'master': landed, 'pending': None})
            self.assertEqual(store.review_comments, {'2': {'11': '100', '12': '101'}})

    def test_pre_upgrade_full_graph_pending_keeps_its_original_pr(self):
        topic = self.source_commit({'topic': b'one'}, b'business', [self.source_base])
        outer = self.source_commit({'topic': b'one'}, b'outer', [self.source_base, topic])
        self.source_pull(outer, 5)
        self.lose_response = 'create'
        with self.assertRaisesRegex(BridgeError, 'response lost'):
            self.run_bridge()
        head = self.target_pulls['1']['head']['sha']
        with self.session() as (_, store):
            self.assertNotIn('native_merge', store.progress['pending'])
        self.source_pull(outer, 5, topic_head=topic)
        self.run_bridge()
        self.assertEqual(len(self.target_pulls), 1)
        self.assertEqual(self.target.parents(self.target_head()), [self.master, head])
        self.assertEqual(len(self.target.parents(head)), 2)

    def test_missing_merge_event_retries_before_publishing_a_branch_or_creating_a_pr(self):
        topic = self.source_commit({'topic': b'change'}, b'business', [self.source_base])
        outer = self.source_commit({'topic': b'change'}, b'outer', [self.source_base, topic])
        self.source_pull(outer, 78, topic_head=topic)
        self.source_events['78'] = [{'event': 'closed', 'commit_id': outer}]
        refs = self.target.run('show-ref').stdout
        with self.assertRaisesRegex(BridgeError, 'merged event commit for GitHub PR #78'):
            self.run_bridge()
        self.assertEqual(self.target.run('show-ref').stdout, refs)
        self.assertFalse(self.target_pulls)
        self.source_events['78'].append({'event': 'merged', 'commit_id': outer})
        self.assertIn('NATIVE MERGE', self.run_bridge())
        self.assertEqual(self.target.run('rev-list', '--count', self.master + '..' + self.target_head()).stdout.strip(),
                         '2')

    def test_native_landing_requires_the_actual_merge_commit_to_match_the_source_tree(self):
        topic = self.source_commit({'topic': b'new'}, b'business', [self.source_base])
        outer = self.source_commit({'topic': b'new'}, b'outer', [self.source_base, topic])
        self.source_pull(outer, 5, topic_head=topic)

        def wrong_landing_with_later_repair(pull):
            head = pull['head']['sha']
            wrong = self.change_master({'wrong': b'landing'}, [self.master, head])
            self.change_master({'topic': b'new'}, [wrong])
            pull['state'] = 'merged'

        with patch.object(self, 'merge_pull', wrong_landing_with_later_repair):
            with self.assertRaisesRegex(BridgeError, 'different tree or topology'):
                self.run_bridge()
        with self.assertRaisesRegex(BridgeError, 'different tree or topology'):
            self.run_bridge()
        with self.session() as (_, store):
            self.assertEqual(store.progress['cursor'], self.source_base)
            self.assertEqual(store.progress['pending']['source'], outer)

    def test_native_pr_can_land_in_one_run_after_member_creation_falls_back_to_robot(self):
        topic = self.source_commit({'topic': b'new'}, b'business', [self.source_base], account=10)
        outer = self.source_commit({'topic': b'new'}, b'outer', [self.source_base, topic])
        self.source_pull(outer, 5, topic_head=topic)
        self.deny_once['alice-token', 'POST', '/pulls'] = 403
        self.assertIn('NATIVE MERGE', self.run_bridge())
        pull = self.target_pulls['1']
        self.assertEqual(pull['user']['id'], BOT['id'])
        self.assertEqual(self.target.parents(self.target_head()), [self.master, pull['head']['sha']])
        self.target.verify_identity(pull['head']['sha'], MAPPED)
        with self.session() as (_, store):
            self.assertIsNone(store.progress['pending'])

    def test_native_pr_reuses_its_head_after_the_base_advances_to_the_same_tree(self):
        topic = self.source_commit({'topic': b'new'}, b'business', [self.source_base])
        outer = self.source_commit({'topic': b'new'}, b'outer', [self.source_base, topic])
        self.source_pull(outer, 5, topic_head=topic)
        self.deny_once['bot-token', 'PUT', '/pulls/1/merge'] = 403
        with self.assertRaisesRegex(BridgeError, 'Could not merge'):
            self.run_bridge()
        head = self.target_pulls['1']['head']['sha']
        advanced = self.change_master(self.target_files, [self.master])
        self.run_bridge()
        self.assertEqual(len(self.target_pulls), 1)
        self.assertEqual(self.target.parents(self.target_head()), [advanced, head])
        self.assertEqual(self.target.tree(self.target_head()), self.source.tree(outer))

    def test_lost_merge_response_recovers_on_a_new_runner_and_imports_the_next_commit(self):
        files = {**self.source_files, 'new.txt': b'one'}
        first = self.source_commit(files, b'one', [self.source_base])
        self.lose_response = 'merge'
        with self.assertRaisesRegex(BridgeError, 'response lost'):
            self.run_bridge()
        landed = self.target_head()
        with self.session() as (_, store):
            pending = store.progress['pending']
            self.assertEqual(pending['source'], first)
            self.assertEqual(pending['number'], '1')
            self.assertTrue(self.target.is_ancestor(pending['head'], landed))
        second = self.source_commit({**files, 'another.txt': b'two'}, b'two', [first])
        self.run_bridge(before=self.source_base, after=first)
        self.event_before = first
        self.assertIn('IMPORTED 1', self.run_bridge())
        head = self.target_head()
        self.assertEqual(self.target.first_parent_range(head, self.master), [landed, head])
        self.assertEqual(len(self.target_pulls), 2)
        with self.session() as (_, store):
            self.assertEqual(store.progress['master'], head)
            self.assertEqual(store.progress['cursor'], second)

    def test_direct_push_reuses_the_pr_and_preserves_a_destination_note(self):
        upstream = self.source_commit({**self.source_files, 'new.txt': b'upstream'}, b'upstream', [self.source_base])
        self.lose_response = 'create'
        with self.assertRaisesRegex(BridgeError, 'response lost'):
            self.run_bridge()
        self.target_pulls['1']['body'] += '\n\nMaintainer note'
        self.assertIn('IMPORTED 1', self.run_bridge())
        self.assertEqual(len(self.target_pulls), 1)
        body = self.target_pulls['1']['body']
        self.assertIn('Maintainer note', body)
        self.assertIn(f'](https://github.com/{GITHUB}/compare/{self.source_base}...{upstream})', body)
        self.assertIn(f'- [GitCode PR #1](https://gitcode.com/{GITCODE}/pull/1)', body)

    def test_lost_create_recovers_source_title_and_raw_markdown_over_a_legacy_draft(self):
        source = self.source_commit({'new.txt': b'new'}, b'change', [self.source_base])
        self.source_pull(source, 15, title='[docs] Draft for review', body='Initial text.\n')
        self.lose_response = 'create'
        with self.assertRaisesRegex(BridgeError, 'response lost'):
            self.run_bridge()
        source_link = f'[GitHub PR #15](https://github.com/{GITHUB}/pull/15)'
        target_link = f'[GitCode PR #1](https://gitcode.com/{GITCODE}/pull/1)'
        pull = self.target_pulls['1']
        self.assertEqual(pull['title'], '[docs] Draft for review')
        self.assertEqual(pull['body'], source_link + '\n\nInitial text.\n')
        # The old controller published link bullets without the GitHub description.
        pull.update(title='同步 GitHub PR #15', body=f'- {source_link}\n- {target_link}')
        body = '\r\n## 验收\r\n\r\n- [x] Ready  \r\n\r\n```text\r\n  sample\r\n```\r\n' + source_link + '\r\n'
        title = '请保留 [v2] 标题 & Markdown'
        self.pull_details['15'].update(title=title, body=body)
        self.run_bridge()
        self.assertEqual((pull['title'], pull['body']), (title, f'{source_link} <-> {target_link}\n\n{body}'))
        self.assertEqual(len(self.target_pulls), 1)

    def test_changed_published_markdown_stops_before_merge_and_is_repaired_on_retry(self):
        source = self.source_commit({'new.txt': b'new'}, b'change', [self.source_base])
        self.source_pull(source, 15, body='Original Markdown.  \n')
        self.trim_published_body = True
        with self.assertRaisesRegex(BridgeError, 'confirm the published'):
            self.run_bridge()
        self.assertEqual(self.target_head(), self.master)
        self.assertEqual(self.target_pulls['1']['state'], 'open')
        self.trim_published_body = False
        self.run_bridge()
        self.assertEqual(self.target_pulls['1']['state'], 'merged')
        self.assertTrue(self.target_pulls['1']['body'].endswith('\n\nOriginal Markdown.  \n'))
        self.assertEqual(len(self.target_pulls), 1)

    def test_recovery_repairs_an_already_merged_pending_pr_without_merging_again(self):
        source = self.source_commit({'new.txt': b'new'}, b'change', [self.source_base])
        self.source_pull(source, 15, body='')
        self.lose_response = 'merge'
        with self.assertRaisesRegex(BridgeError, 'response lost'):
            self.run_bridge()
        pull = self.target_pulls['1']
        head = self.target_head()
        pull.update(title='同步 GitHub PR #15', body='Legacy description')
        self.run_bridge()
        self.assertEqual(pull['state'], 'merged')
        self.assertEqual(self.target_head(), head)
        self.assertEqual(self.pr_calls.count(('PUT', 'bot-token')), 1)
        self.assertEqual((pull['title'], pull['body']), ('Source PR 15',
                         f'[GitHub PR #15](https://github.com/{GITHUB}/pull/15) <-> '
                         f'[GitCode PR #1](https://gitcode.com/{GITCODE}/pull/1)'))

    def test_confirmed_non_robot_merge_warns_and_checkpoints_the_verified_import(self):
        source = self.source_commit({'file': b'change'}, b'mapped change', [self.source_base], account=10)
        self.source_pull(source, 15)
        merge = self.merge_pull
        with patch.object(self, 'merge_pull', lambda pull: merge(pull, BOB)):
            output = self.run_bridge()
        master = self.target_head()
        self.assertIn('WARNING GitCode PR #1: merge commit attribution', output)
        self.assertIn(f'its tree matches GitHub {source}', output)
        self.target.verify_identity(master, BOB)
        self.target.verify_identity(self.target_pulls['1']['head']['sha'], MAPPED)
        self.assertEqual(self.target.tree(master), self.source.tree(source))
        with self.session() as (_, store):
            self.assertEqual(store.progress, {'cursor': source, 'master': master, 'pending': None})

    def test_manual_apply_recovers_a_manual_merge_and_imports_the_next_pr_once(self):
        first = self.source_commit({'file': b'first'}, b'first', [self.source_base], account=10)
        self.source_pull(first, 15)
        self.deny_once['bot-token', 'PUT', '/pulls/1/merge'] = 403
        with self.assertRaisesRegex(BridgeError, 'Could not merge'):
            self.run_bridge()
        self.merge_pull(self.target_pulls['1'], BOB)
        landed = self.target_head()
        # Some GitCode responses expose merged_at on a closed PR.
        self.target_pulls['1'].update(state='closed', merged_at='2026-11-19T01:00:00Z')
        refs = self.target.run('show-ref').stdout
        self.assertIn('WARNING GitCode PR #1', self.run_bridge(check=True, after=first))
        self.assertEqual(self.target.run('show-ref').stdout, refs)
        second = self.source_commit({'file': b'next'}, b'next', [first])
        self.source_pull(second, 16)
        self.calls.clear()
        output = self.run_actions('workflow_dispatch', second)
        self.assertIn('WARNING GitCode PR #1: merge commit attribution', output)
        self.assertIn(f'its tree matches GitHub {first}', output)
        self.assertTrue(self.target.is_ancestor(landed, self.target_head()))
        self.assertNotIn(('gitcode', 'PUT', '/pulls/1/merge'), self.calls)
        self.assertEqual([p['title'] for p in self.target_pulls.values()], ['Source PR 15', 'Source PR 16'])
        self.assertEqual(self.target.tree(self.target_head()), self.source.tree(second))
        with self.session() as (_, store):
            self.assertEqual(store.progress, {'cursor': second, 'master': self.target_head(), 'pending': None})
        self.assertNotIn('WARNING', self.run_actions('workflow_dispatch', second))
        self.assertEqual(len(self.target_pulls), 2)

    def test_non_robot_merge_requires_both_merged_pr_status_and_a_matching_tree(self):
        source = self.source_commit({'file': b'change'}, b'change', [self.source_base])
        self.deny_once['bot-token', 'PUT', '/pulls/1/merge'] = 403
        with self.assertRaisesRegex(BridgeError, 'Could not merge'):
            self.run_bridge()
        pull = self.target_pulls['1']
        self.merge_pull(pull, BOB)
        landed = self.target_head()
        pull['state'] = 'open'
        with self.assertRaisesRegex(BridgeError, 'confirm the prepared GitCode PR was merged'):
            self.run_bridge()
        pull['state'] = 'merged'
        self.deny_once['bot-token', 'GET', '/pulls/1'] = 503
        with self.assertRaisesRegex(ApiError, '503'):
            self.run_bridge()
        self.change_master({'file': b'unexpected contents'}, [landed])
        with self.assertRaisesRegex(BridgeError, 'different tree from GitHub'):
            self.run_bridge()
        with self.session() as (_, store):
            self.assertEqual(store.progress['cursor'], self.source_base)
            self.assertEqual(store.progress['pending']['number'], '1')
        self.assertEqual(len(self.target_pulls), 1)

    def test_equal_trees_cannot_substitute_for_the_prepared_commits(self):
        files = {'file': b'same tree'}
        source = self.source_commit(files, b'change', [self.source_base])

        def merge_without_prepared_head(pull):
            self.change_master(files, [self.master])
            pull['state'] = 'merged'

        with patch.object(self, 'merge_pull', merge_without_prepared_head):
            with self.assertRaisesRegex(BridgeError, 'does not contain the prepared commits'):
                self.run_bridge()
        self.assertEqual(self.target.tree(self.target_head()), self.source.tree(source))
        with self.session() as (_, store):
            self.assertEqual(store.progress['cursor'], self.source_base)
            self.assertEqual(store.progress['pending']['source'], source)

    def test_preview_rejects_missing_source_text_without_writing(self):
        source = self.source_commit({'new.txt': b'new'}, b'change', [self.source_base])
        self.source_pull(source, 15)
        del self.pull_details['15']['body']
        initial_refs = self.target.run('show-ref').stdout
        with self.assertRaisesRegex(BridgeError, 'original GitHub PR title and Markdown'):
            self.run_bridge(check=True)
        self.assertEqual(self.target.run('show-ref').stdout, initial_refs)
        self.assertFalse(self.target_pulls)

    def test_two_mapped_openers_and_unmapped_openers_use_their_own_accounts_in_one_run(self):
        self.cfg['members'].append({'github': {'id': '20', 'username': 'bob-gh'}, 'gitcode': public_account(BOB),
                                       'gitcode_pr_token_secret': BOB_SECRET})
        previous = self.source_base
        cases = [(10, 20, MAPPED, BOB), (20, 10, BOB, MAPPED), (99, 10, BOT, MAPPED), (None, 20, BOT, BOB)]
        for number, (opener, writer, _, _) in enumerate(cases, 1):
            previous = self.source_commit({'part': str(number).encode()}, b'change', [previous], account=writer)
            self.source_pull(previous, number, opener=opener, body='A source description.\n')
        self.run_bridge(check=True)
        self.assertFalse(self.pr_calls)
        self.run_bridge()
        for number, (_, _, opener, writer) in enumerate(cases, 1):
            pull = self.target_pulls[str(number)]
            self.assertEqual(pull['user'], {'id': opener['id'], 'login': opener['username']})
            self.assertEqual(pull['state'], 'merged')
            self.target.verify_identity(pull['head']['sha'], writer)
        self.assertEqual([t for method, t in self.pr_calls if method == 'POST'],
                         ['alice-token', 'bob-token', 'bot-token', 'bot-token'])
        self.assertTrue(all(t == 'bot-token' for method, t in self.pr_calls if method != 'POST'))

    def test_lost_create_finds_the_mapped_pr_and_only_needs_the_bot_to_finish(self):
        upstream = self.source_commit({'new.txt': b'change'}, b'change', [self.source_base])
        self.source_pull(upstream, 5, opener=10)
        self.lose_response = 'create'
        with self.assertRaisesRegex(BridgeError, 'response lost'):
            self.run_bridge()
        with self.session() as (_, store):
            self.assertEqual(store.progress['pending']['creator'], {'id': MAPPED['id'], 'username': MAPPED['username']})
            self.assertNotIn('number', store.progress['pending'])
        self.member_tokens.clear()
        self.assertIn('IMPORTED 1', self.run_bridge())
        self.assertEqual(len(self.target_pulls), 1)
        self.assertEqual(self.pr_calls, [('POST', 'alice-token'), ('GET', 'bot-token'),
                                         ('PATCH', 'bot-token'), ('GET', 'bot-token'), ('PUT', 'bot-token')])

    def test_existing_pending_pr_keeps_its_real_creator_after_mapping_changes(self):
        upstream = self.source_commit({'new.txt': b'change'}, b'change', [self.source_base])
        self.source_pull(upstream, 7, opener=20)
        self.lose_response = 'create'
        with self.assertRaisesRegex(BridgeError, 'response lost'):
            self.run_bridge()
        self.cfg['members'].append({'github': {'id': '20', 'username': 'bob-gh'}, 'gitcode': public_account(BOB),
                                    'gitcode_pr_token_secret': BOB_SECRET})
        self.run_bridge()
        self.assertEqual(len(self.target_pulls), 1)
        self.assertEqual(self.target_pulls['1']['user']['login'], BOT['username'])
        self.assertEqual(self.target_pulls['1']['state'], 'merged')

    def test_missing_expired_and_restored_pat_affect_only_that_member(self):
        self.cfg['members'].append({'github': {'id': '20', 'username': 'bob-gh'}, 'gitcode': public_account(BOB),
                                    'gitcode_pr_token_secret': BOB_SECRET})
        previous = self.source_base
        for index, credential in enumerate((None, 'expired-token', 'bob-token', 'alice-token')):
            if credential is None:
                self.member_tokens.pop(ALICE_SECRET)
            else:
                self.member_tokens[ALICE_SECRET] = credential
            expected = MAPPED if credential == 'alice-token' else BOT
            for author, account in ((10, expected), (20, BOB)):
                number = len(self.target_pulls) + 1
                revision = self.source_commit({'file': f'{index}/{author}'.encode()}, b'change', [previous], account=author)
                self.source_pull(revision, number, opener=author)
                self.run_bridge(before=previous)
                pull = self.target_pulls[str(number)]
                self.assertEqual(pull['user']['login'], account['username'])
                self.target.verify_identity(pull['head']['sha'], account)
                self.assertEqual(pull['state'], 'merged')
                previous = revision

    def test_missing_opener_field_stops_before_a_bot_pr_is_created(self):
        upstream = self.source_commit({'new.txt': b'change'}, b'change', [self.source_base])
        self.source_pull(upstream, 8)
        del self.pull_details['8']['user']
        with self.assertRaisesRegex(BridgeError, 'GitHub PR opener'):
            self.run_bridge()
        self.assertFalse(self.pr_calls)

    def test_changed_sync_branch_does_not_merge(self):
        upstream = self.source_commit({**self.source_files, 'new.txt': b'upstream'}, b'upstream', [self.source_base])
        self.change_branch_before_merge = True
        with self.assertRaisesRegex(BridgeError, 'sync branch changed before merge'):
            self.run_bridge()
        refs = self.target.run('show-ref').stdout
        with self.assertRaisesRegex(BridgeError, 'differs from the prepared commits'):
            self.run_bridge(check=True)
        self.assertEqual(self.target.run('show-ref').stdout, refs)
        self.assertEqual(self.target_head(), self.master)
        with self.session() as (_, store):
            self.assertEqual(store.progress['cursor'], self.source_base)
            self.assertEqual(store.progress['pending']['source'], upstream)

    def test_groups_rebased_commits_into_their_own_github_pr_pairs(self):
        files = {**self.source_files, 'first': b'one'}
        first = self.source_commit(files, b'part one', [self.source_base])
        files['second'] = b'two'
        second = self.source_commit(files, b'part two', [first])
        files['third'] = b'three'
        third = self.source_commit(files, b'other PR', [second])
        self.source_pull(first, 41)
        self.source_pull(first, 91, merged=False)
        self.source_pull(second, 41)
        self.source_pull(second, 92, branch='other')
        self.source_pull(third, 42)
        self.assertIn('IMPORTED 3', self.run_bridge())
        self.assertEqual(len(self.target_pulls), 2)
        for number, github_number in [('1', 41), ('2', 42)]:
            pull = self.target_pulls[number]
            self.assertEqual(pull['title'], f'Source PR {github_number}')
            self.assertEqual(pull['body'],
                             f'[GitHub PR #{github_number}](https://github.com/{GITHUB}/pull/{github_number}) <-> '
                             f'[GitCode PR #{number}](https://gitcode.com/{GITCODE}/pull/{number})')

    def test_source_rewrite_stops_with_master_unchanged(self):
        first = self.source_commit({'new': b'one'}, b'one', [self.source_base])
        self.run_bridge()
        target_refs = self.target.run('show-ref').stdout
        rewritten = self.source_commit({'new': b'two'}, b'two', [self.source_base])
        with self.assertRaisesRegex(BridgeError, 'first-parent history'):
            self.run_bridge(before=first, after=rewritten)
        self.assertEqual(self.target.run('show-ref').stdout, target_refs)

    def test_drift_after_checkpoint_is_aligned_before_new_source_commits(self):
        initial_files = {**self.source_files, 'imported': b'previous import\n'}
        first = self.source_commit(initial_files, b'one', [self.source_base])
        self.run_bridge()
        landed = self.target_head()
        second = self.source_commit({**initial_files, 'common.txt': b'upstream wins\n'},
                                    b'update', [first], account=10)
        self.change_master({**initial_files, 'private.txt': b'destination only', 'common.txt': b'drift'}, [landed])
        self.event_before = first
        result = self.run_bridge()
        self.assertIn(f'ALIGN {first}', result)
        self.assertIn('IMPORTED 1', result)
        self.assertEqual(len(self.target_pulls), 3)
        alignment = self.target_pulls['2']['head']['sha']
        self.assertEqual(self.target.tree(alignment), self.source.tree(first))
        self.target.verify_identity(alignment, BOT)
        self.target.verify_identity(self.target_pulls['3']['head']['sha'], MAPPED)
        self.assertEqual(self.target.tree(self.target_head()), self.source.tree(second))
        with self.session() as (_, store):
            self.assertEqual(store.progress, {'cursor': second, 'master': self.target_head(), 'pending': None})

    def test_initial_alignment_with_zero_backlog_replaces_the_entire_tree_via_one_pr(self):
        source = self.source_commit({'common.txt': b'GitHub', 'source.bin': b'\0source'}, b'baseline',
                                    [self.source_base], account=10)
        target = self.change_master({'common.txt': b'GitCode', 'private.txt': b'private'}, [self.master])
        self.event_before = source
        self.target.run('update-ref', '-d', STATE_REF)
        refs = self.target.run('show-ref').stdout
        preview = self.run_bridge(check=True)
        self.assertIn('LIVE DIFF', preview)
        self.assertIn(f'ALIGN {source} | add 1, change 1, delete 1', preview)
        self.assertIn('CHECK 0 source commits', preview)
        self.assertNotIn('private.txt', preview)
        self.assertEqual(self.target.run('show-ref').stdout, refs)
        self.assertEqual(self.pr_calls, [])

        self.lose_response = 'create'
        with self.assertRaisesRegex(BridgeError, 'response lost'):
            self.run_bridge()
        self.run_bridge()
        self.assertEqual(len(self.target_pulls), 1)
        pull = self.target_pulls['1']
        self.assertEqual(pull['user']['login'], BOT['username'])
        self.assertIn(f'](https://github.com/{GITHUB}/commit/{source})', pull['body'])
        self.assertIn(f'](https://gitcode.com/{GITCODE}/pull/1)', pull['body'])
        self.target.verify_identity(pull['head']['sha'], BOT)
        self.target.verify_identity(self.target_head(), BOT)
        self.assertEqual(self.target.tree(self.target_head()), self.source.tree(source))
        self.assertTrue(self.target.is_ancestor(target, self.target_head()))
        self.assertEqual(self.target.run('show', f'{target}:private.txt').stdout, 'private')
        refs = self.target.run('show-ref').stdout
        current = self.run_bridge(check=True)
        self.assertIn('LIVE MATCH', current)
        self.assertNotIn('ALIGN', current)
        self.run_bridge()
        self.assertEqual(self.target.run('show-ref').stdout, refs)
        self.assertEqual(len(self.target_pulls), 1)

    def test_first_manual_apply_finds_deployment_and_preserves_missed_prs(self):
        self.target.run('update-ref', '-d', STATE_REF)
        self.change_master({'target-only': b'old'}, [self.master])
        workflow = {'.github': {'workflows': {'bridge.yml': b'name: Sync merged code'}}}
        deploy = self.source_commit({**self.source_files, **workflow}, b'Deploy sync', [self.source_base], account=10)
        self.source_pull(deploy, 8)
        feature = self.source_commit({**workflow, 'file': b'one'}, b'new feature', [deploy])
        self.source_pull(feature, 9, opener=99)
        self.run_actions('workflow_dispatch', feature)
        self.assertEqual(len(self.target_pulls), 3)
        baseline, deployment, change = self.target_pulls.values()
        self.assertEqual(self.target.tree(baseline['head']['sha']), self.source.tree(self.source_base))
        self.assertEqual([deployment['title'], change['title']], ['Source PR 8', 'Source PR 9'])
        self.assertEqual(self.target.tree(self.target_head()), self.source.tree(feature))

    def test_rewritten_target_history_stops_before_alignment(self):
        self.run_bridge()
        self.change_master({'unrelated': b'new history'}, [])
        refs = self.target.run('show-ref').stdout
        with self.assertRaisesRegex(BridgeError, 'no longer contains the last confirmed commit'):
            self.run_bridge()
        self.assertEqual(self.target.run('show-ref').stdout, refs)

    def test_concurrent_target_change_requires_exact_tree_before_checkpoint_confirmation(self):
        source = self.source_commit({**self.source_files, 'new': b'upstream'}, b'update', [self.source_base])
        merge = self.merge_pull

        def merge_after_private_change(pull):
            self.change_master({**self.source_files, 'private': b'race'}, [self.target_head()])
            merge(pull)

        with patch.object(self, 'merge_pull', merge_after_private_change):
            with self.assertRaisesRegex(BridgeError, 'changed during the merge'):
                self.run_bridge()
        with self.session() as (_, store):
            self.assertEqual(store.progress['cursor'], self.source_base)
            self.assertEqual(store.progress['pending']['source'], source)
        self.lose_response = 'create'
        with self.assertRaisesRegex(BridgeError, 'response lost'):
            self.run_bridge()
        with self.session() as (_, store):
            self.assertEqual(store.progress['cursor'], self.source_base)
            self.assertTrue(store.progress['pending']['alignment'])
        self.run_bridge()
        self.assertEqual(len(self.target_pulls), 2)
        self.assertEqual(self.target.tree(self.target_head()), self.source.tree(source))
        with self.session() as (_, store):
            self.assertEqual(store.progress, {'cursor': source, 'master': self.target_head(), 'pending': None})

    def test_legacy_pending_with_mixed_authorship_is_blocked_then_rebuilt_after_closure(self):
        source = self.source_commit({**self.source_files, 'mapped': b'change'}, b'update',
                                    [self.source_base], account=10)
        self.lose_response = 'create'
        with self.assertRaisesRegex(BridgeError, 'response lost'):
            self.run_bridge()
        pull = self.target_pulls['1']
        raw = self.target.run('cat-file', 'commit', pull['head']['sha'], binary=True).stdout
        mixed = raw.replace(b'committer alice-gc <alice@example.com>', b'committer sync-bot <bot@example.com>')
        old = self.target.run('hash-object', '-t', 'commit', '-w', '--stdin',
                              binary=True, input_data=mixed).stdout.strip().decode()
        with self.assertRaisesRegex(BridgeError, 'author and committer'):
            self.target.verify_identity(old, MAPPED)
        branch = f'bridge/github-{source[:12]}-{old[:20]}'
        pull['head'] = {'ref': branch, 'sha': old}
        self.target.run('update-ref', f'refs/heads/{branch}', old)
        with self.session() as (_, store):
            pending = dict(store.progress['pending'], head=old, branch=branch)
            pending.pop('base')
            pending.pop('alignment')
            store.save({**store.progress, 'pending': pending})
        with self.assertRaisesRegex(BridgeError, 'pending PR is obsolete'):
            self.run_bridge()
        self.assertEqual(self.target_head(), self.master)
        pull['state'] = 'closed'
        self.run_bridge()
        self.target.verify_identity(self.target_pulls['2']['head']['sha'], MAPPED)
        self.assertFalse(self.target.is_ancestor(old, self.target_head()))

    def test_bot_identity_is_required_on_public_and_private_repositories(self):
        with self.assertRaisesRegex(BridgeError, 'token does not belong'):
            with self.session(token='wrong-token'):
                self.fail('wrong account passed preflight')
        self.bot_email = 'wrong@example.com'
        with self.assertRaisesRegex(BridgeError, 'email must be confirmed'):
            self.run_bridge(check=True)
        self.bot_email = BOT['email']
        self.info[GITCODE]['private'] = 0
        self.info[GITCODE]['public'] = True
        self.assertIn('LIVE MATCH', self.run_bridge(check=True))
        source = self.source_commit({'shared': b'public'}, b'public update', [self.source_base], account=10)
        self.source_pull(source, 4)
        self.run_bridge()
        self.assertEqual(self.target_pulls['1']['state'], 'merged')
        self.target.verify_identity(self.target_pulls['1']['head']['sha'], MAPPED)

    def test_gitcode_noreply_address_must_match_authenticated_profile(self):
        alias = 'generated.sync-bot@noreply.gitcode.com'
        self.bot_email = None
        self.bot_profile_email = alias
        self.cfg['gitcode']['bot'] = {**BOT, 'email': alias}
        self.assertIn('CHECK 0 source commits', self.run_bridge(check=True))
        with self.assertRaisesRegex(BridgeError, 'token does not belong'):
            with self.session(token='wrong-token'):
                self.fail('matching email bypassed the account identity check')

        for configured, profile in (
                (alias, 'other.sync-bot@noreply.gitcode.com'),
                (BOT['email'], BOT['email']),
                ('generated.sync-bot@noreply.gitcode.com.example',
                 'generated.sync-bot@noreply.gitcode.com.example')):
            with self.subTest(configured=configured, profile=profile):
                self.cfg['gitcode']['bot']['email'] = configured
                self.bot_profile_email = profile
                with self.assertRaisesRegex(BridgeError, 'email must be confirmed'):
                    self.run_bridge(check=True)

    def test_incomplete_existing_checkpoint_cannot_start_a_fresh_overwrite(self):
        with self.session() as (_, store):
            store.save({})
        self.change_master({'private-only': b'keep until state is restored'}, [self.master])
        refs = self.target.run('show-ref').stdout
        with self.assertRaisesRegex(StateError, 'restore bridge state'):
            self.run_bridge()
        self.assertEqual(self.target.run('show-ref').stdout, refs)
        self.assertFalse(self.pr_calls)

    def test_stale_writer_cannot_overwrite_state(self):
        self.run_bridge()
        with self.session() as (_, stale), self.session() as (_, fresh):
            fresh.save({**fresh.progress, 'master': '1' * 40})
            with self.assertRaises(StateError):
                stale.save({**stale.progress, 'master': '2' * 40})

    def test_legacy_checkpoint_renames_only_on_apply_and_keeps_review_receipts(self):
        self.source_comments['5'] = [self.review_pr()]
        self.run_bridge()
        original = self.target.run('rev-parse', 'refs/heads/github-sync').stdout.strip()
        self.target.run('branch', '-m', 'github-sync', 'bridge-state')
        before = self.target.run('show-ref').stdout
        self.assertIn('CHECK 0 source commits', self.run_bridge(check=True))
        self.assertEqual(self.target.run('show-ref').stdout, before)

        self.assertIn('MIGRATED checkpoint branch to github-sync', self.run_bridge())
        self.assertEqual(self.target.run('rev-parse', 'refs/heads/github-sync').stdout.strip(), original)
        self.assertNotIn('refs/heads/bridge-state', self.target.run('show-ref').stdout)
        self.assertIn('CURRENT', self.run_bridge())
        self.assertEqual(len(self.target_pulls), 1)
        self.assertEqual(len(self.review_writes), 1)
        with self.session() as (_, store):
            self.assertEqual(store.review_comments, {'1': {'11': '100'}})

    def test_checkpoint_rename_rejects_an_occupied_destination_and_keeps_both_refs(self):
        self.run_bridge()
        self.target.run('branch', '-m', 'github-sync', 'bridge-state')
        with self.session() as (bridge, store):
            self.target.run('update-ref', 'refs/heads/github-sync', self.master)
            refs = self.target.run('show-ref').stdout
            with self.assertRaisesRegex(StateError, 'Could not rename'):
                bridge.run(store, None, self.source_base)
        with self.assertRaisesRegex(StateError, 'Multiple state branches exist'):
            self.run_bridge(check=True)
        self.assertEqual(self.target.run('show-ref').stdout, refs)
        self.assertFalse(self.pr_calls)

    def test_checkpoint_rename_preserves_a_newer_legacy_tip(self):
        self.run_bridge()
        self.target.run('branch', '-m', 'github-sync', 'bridge-state')
        with self.session() as (bridge, store):
            record = self.target.run('show', f'{store.tip}:progress.json', binary=True).stdout
            newer = make_commit(self.target, {'progress.json': record}, b'Advance checkpoint', [store.tip])
            self.target.run('update-ref', 'refs/heads/bridge-state', newer, store.tip)
            refs = self.target.run('show-ref').stdout
            with self.assertRaisesRegex(StateError, 'Could not rename'):
                bridge.run(store, None, self.source_base)
        self.assertEqual(self.target.run('show-ref').stdout, refs)

    def test_a_missing_push_and_its_revert_create_two_prs_in_source_order(self):
        first = self.source_commit({'feature': b'enabled'}, b'Enable feature', [self.source_base], account=10)
        revert = self.source_commit(self.source_files, b'Revert feature', [first])
        self.source_pull(first, 10, opener=10, body='Enable it.')
        self.source_pull(revert, 11, opener=99, body='Withdraw it.')
        original_tree = self.source.tree(self.source_base)
        self.assertEqual(self.source.tree(revert), original_tree)

        self.assertIn('IMPORTED 2', self.run_actions('push', revert, before=first))
        self.assertEqual(self.target.tree(self.target_pulls['1']['head']['sha']), self.source.tree(first))
        self.assertEqual([p['title'] for p in self.target_pulls.values()], ['Source PR 10', 'Source PR 11'])
        self.assertEqual([p['user']['login'] for p in self.target_pulls.values()], ['alice-gc', 'sync-bot'])
        self.target.verify_identity(self.target_pulls['1']['head']['sha'], MAPPED)
        self.target.verify_identity(self.target_pulls['2']['head']['sha'], BOT)
        self.assertEqual(self.target.tree(self.target_head()), original_tree)
        refs = self.target.run('show-ref').stdout
        self.run_bridge(before=self.source_base, after=first)
        self.run_bridge(before=first, after=revert)
        self.assertEqual(self.target.run('show-ref').stdout, refs)
        self.assertEqual(len(self.target_pulls), 2)
        with self.session(before=first, after=revert) as (_, store):
            record = json.loads(self.target.run('show', f'{store.tip}:progress.json').stdout)
            self.assertEqual(record['progress'], {'cursor': revert, 'master': self.target_head(), 'pending': None})
            self.assertNotIn('events', record)

    def run_actions(self, event_name, after, before=None, *, forced=False, operation='apply'):
        config, members = self.root / 'config.json', self.root / 'members.json'
        config.write_text(json.dumps({'gitcode': self.cfg['gitcode']}))
        members.write_text(json.dumps(self.cfg['members']))
        env = {'PATH': os.environ['PATH'], 'GITHUB_ACTIONS': 'true', 'GITHUB_REPOSITORY': GITHUB,
               'GITHUB_EVENT_NAME': event_name, 'GITHUB_REF': SOURCE, 'GITHUB_SHA': after,
               'GITHUB_TOKEN': 'read', 'GITCODE_ROBOT_TOKEN': 'bot-token', **self.member_tokens}
        if event_name == 'push':
            event = self.root / 'push.json'
            event.write_text(json.dumps({'ref': SOURCE, 'before': before, 'after': after,
                                         'created': False, 'forced': forced}))
            env['GITHUB_EVENT_PATH'] = str(event)
        args = ['bridge', '--config', str(config), '--members', str(members), '--' + operation]
        with patch.dict(os.environ, env, clear=True), patch.object(sys, 'argv', args):
            with patch('bridge.Api', lambda *args: FixtureApi(self, *args)), patch('bridge.Remote', self.remote):
                with redirect_stdout(io.StringIO()) as output:
                    main()
        return output.getvalue()

    def test_cli_push_fetches_event_after_with_mapping_from_the_checkout(self):
        first = self.source_commit({'feature': b'enabled'}, b'Enable feature', [self.source_base], account=10)
        self.source_commit(self.source_files, b'Revert feature', [first])
        self.source_pull(first, 27)
        self.run_actions('push', first, before=self.source_base)
        self.assertEqual(self.target.tree(self.target_head()), self.source.tree(first))
        self.assertEqual(self.target_pulls['1']['user']['id'], MAPPED['id'])
        self.target.verify_identity(self.target_pulls['1']['head']['sha'], MAPPED)

    def test_manual_apply_catches_the_last_missed_push_and_an_older_run_is_a_noop(self):
        first = self.source_commit({'file': b'one'}, b'one', [self.source_base])
        after = self.source_commit({'file': b'two'}, b'two', [first])
        self.source_pull(first, 40)
        self.source_pull(after, 41)
        self.assertIn('IMPORTED 2', self.run_actions('workflow_dispatch', after))
        self.assertEqual([p['title'] for p in self.target_pulls.values()], ['Source PR 40', 'Source PR 41'])
        refs = self.target.run('show-ref').stdout
        self.run_bridge(before=None, after=first)
        self.run_bridge(before=first, after=after)
        self.assertEqual(self.target.run('show-ref').stdout, refs)

    def test_flagged_force_push_is_rejected_even_when_after_matches_the_cursor(self):
        newer = self.source_commit({'file': b'not imported'}, b'later', [self.source_base])
        self.source.run('update-ref', SOURCE, self.source_base, newer)
        refs = self.target.run('show-ref').stdout
        with self.assertRaisesRegex(BridgeError, 'Force push'):
            self.run_actions('push', self.source_base, before=newer, forced=True)
        self.assertFalse(self.calls)
        self.assertEqual(self.target.run('show-ref').stdout, refs)

    def test_manual_apply_reports_a_rewound_live_branch_until_its_history_is_restored(self):
        cursor = self.source_commit({'file': b'C5'}, b'C5', [self.source_base])
        self.run_actions('push', cursor, before=self.source_base)
        self.source.run('update-ref', SOURCE, self.source_base, cursor)
        refs = self.target.run('show-ref').stdout
        for _ in range(2):
            with self.assertRaisesRegex(BridgeError, 'rewound'):
                self.run_actions('workflow_dispatch', self.source_base)
        self.assertEqual(self.target.run('show-ref').stdout, refs)
        self.assertEqual(len(self.target_pulls), 1)
        self.source.run('update-ref', SOURCE, cursor, self.source_base)
        self.assertIn('CURRENT', self.run_actions('workflow_dispatch', cursor))
        next_commit = self.source_commit({'file': b'C6'}, b'C6', [cursor])
        self.assertIn('IMPORTED 1', self.run_actions('workflow_dispatch', next_commit))
        self.assertEqual(len(self.target_pulls), 2)

    def test_older_queued_events_skip_only_while_live_first_parent_history_covers_the_cursor(self):
        old = self.source_commit({'file': b'old'}, b'old', [self.source_base])
        cursor = self.source_commit({'file': b'confirmed'}, b'confirmed', [old])
        self.run_actions('workflow_dispatch', cursor)
        self.source_commit({'file': b'later'}, b'later', [cursor])
        refs = self.target.run('show-ref').stdout
        self.assertIn('CURRENT', self.run_actions('push', old, before=self.source_base))
        self.assertIn('CURRENT', self.run_actions('workflow_dispatch', old))
        # The cursor remains reachable on a side branch after this rewrite.
        self.source_commit({'file': b'rewritten'}, b'new mainline', [old, cursor])
        with self.assertRaisesRegex(BridgeError, 'first-parent history'):
            self.run_actions('workflow_dispatch', old)
        self.assertEqual(self.target.run('show-ref').stdout, refs)

    def test_manual_apply_preserves_pending_pr_when_its_source_is_dropped(self):
        pending = self.source_commit({'file': b'pending'}, b'pending', [self.source_base])
        self.deny_once['bot-token', 'PUT', '/pulls/1/merge'] = 403
        with self.assertRaisesRegex(BridgeError, 'Could not merge'):
            self.run_actions('push', pending, before=self.source_base)
        refs = self.target.run('show-ref').stdout
        self.source.run('update-ref', SOURCE, self.source_base, pending)
        self.calls.clear()
        with self.assertRaisesRegex(BridgeError, 'rewound'):
            self.run_actions('workflow_dispatch', self.source_base)
        rewritten = self.source_commit({'file': b'other'}, b'other', [self.source_base])
        with self.assertRaisesRegex(BridgeError, 'first-parent history'):
            self.run_actions('workflow_dispatch', rewritten)
        self.assertFalse(any(method != 'GET' for _, method, _ in self.calls))
        self.assertEqual(self.target.run('show-ref').stdout, refs)
        self.assertEqual(self.target_pulls['1']['state'], 'open')

    def test_idle_manual_apply_only_fetches_the_shallow_checkpoint_and_probes_master(self):
        refs = self.target.run('show-ref').stdout
        with patch.object(Git, 'run', autospec=True, side_effect=Git.run) as git:
            output = self.run_actions('workflow_dispatch', self.source_base)
        remote_calls = [(args[1:], kwargs['remote'].provider)
                        for args, kwargs in git.call_args_list if kwargs.get('remote')]
        fetches = [(provider, args[-1], '--depth=1' in args)
                   for args, provider in remote_calls if 'fetch' in args]
        self.assertEqual(fetches, [('gitcode', STATE_REF, True)])
        probes = [(provider, args[-1]) for args, provider in remote_calls if 'ls-remote' in args and MASTER_REF in args]
        self.assertEqual(probes, [('gitcode', MASTER_REF)])
        self.assertFalse(any('push' in args for args, _ in remote_calls))
        self.assertIn('CURRENT', output)
        self.assertEqual(self.target.run('show-ref').stdout, refs)
        self.assertFalse(self.target_pulls)

    def test_matching_cursor_fetches_both_histories_for_check_or_changed_master(self):
        for check in (True, False):
            with self.subTest(check=check):
                if not check:
                    # An additional commit can have the same tree but a different SHA.
                    self.change_master(self.target_files, [self.master])
                with self.session() as (bridge, store), patch.object(bridge.git, 'fetch', wraps=bridge.git.fetch) as fetch:
                    with redirect_stdout(io.StringIO()):
                        bridge.run(store, None, self.source_base, check=check)
                    self.assertEqual(fetch.call_args_list,
                                     [call(bridge.source, self.source_base), call(bridge.target, MASTER_REF)])

    def test_failed_idle_master_probe_does_not_report_success_or_fetch_histories(self):
        with self.session() as (bridge, store):
            before = self.target.run('show-ref').stdout
            with patch.object(bridge.git, 'tip', side_effect=BridgeError('master probe failed')), \
                    patch.object(bridge.git, 'fetch', wraps=bridge.git.fetch) as fetch:
                with self.assertRaisesRegex(BridgeError, 'master probe failed'):
                    bridge.run(store, None, self.source_base)
            fetch.assert_not_called()
            self.assertEqual(self.target.run('show-ref').stdout, before)

    def test_new_push_finishes_unconfirmed_initial_alignment_and_imports_its_own_pr(self):
        self.target.run('update-ref', '-d', STATE_REF)
        self.change_master({'destination-only': b'drift'}, [self.master])
        self.lose_response = 'create'
        with self.assertRaisesRegex(BridgeError, 'response lost'):
            self.run_bridge(before=None, after=self.source_base)
        first = self.source_commit({'feature': b'one'}, b'one', [self.source_base])
        self.source_pull(first, 42)
        self.run_bridge(before=self.source_base, after=first)
        self.assertEqual(len(self.target_pulls), 2)
        self.assertEqual(self.target_pulls['1']['state'], 'merged')
        self.assertEqual(self.target_pulls['2']['title'], 'Source PR 42')
        self.assertEqual(self.target.tree(self.target_head()), self.source.tree(first))

    def test_partly_applied_multibatch_push_resumes_at_its_second_pr(self):
        first = self.source_commit({'file': b'one'}, b'one', [self.source_base])
        second = self.source_commit({'file': b'two'}, b'two', [first])
        third = self.source_commit({'file': b'three'}, b'three', [second])
        self.source_pull(first, 31)
        self.source_pull(second, 32)
        merge = self.merge_pull

        def lose_second_response(pull):
            merge(pull)
            if pull['title'] == 'Source PR 32':
                raise BridgeError('second merge response lost')

        with patch.object(self, 'merge_pull', lose_second_response):
            with self.assertRaisesRegex(BridgeError, 'second merge response lost'):
                self.run_bridge(before=self.source_base, after=second)
        with self.session(before=self.source_base, after=second) as (_, store):
            self.assertEqual(store.progress['cursor'], first)
            self.assertEqual(store.progress['pending']['source'], second)
        self.run_bridge(before=second, after=third)
        self.assertEqual(self.target.tree(self.target_head()), self.source.tree(third))
        self.assertEqual(len(self.target_pulls), 3)

    def test_event_state_migration_uses_latest_ancestry_and_keeps_comment_receipts(self):
        first = self.source_commit({'new': b'one'}, b'one', [self.source_base])
        second = self.source_commit({'new': b'two'}, b'two', [first])
        self.run_bridge(after=first)
        earlier_master = self.target_head()
        self.run_bridge(before=first, after=second)
        later_master = self.target_head()
        with self.session() as (bridge, _):
            binding = bridge.binding
        review = {'57': {'101': '202'}}
        def completed(landed, **kwargs):
            return dict(master=landed, done=True, applied=1, pending=None, **kwargs)
        events = {'push/' + second: completed(later_master, before=first),
                  'push/' + first: completed(earlier_master, before=self.source_base),
                  'snapshot/' + self.source_base: completed(self.master)}
        record = {'repositories': binding, 'events': events, 'review_comments': review}
        old = make_commit(self.target, {'progress.json': json.dumps(record).encode()}, b'Legacy state')
        self.target.run('update-ref', '-d', STATE_REF)
        self.target.run('update-ref', 'refs/heads/gtihub-sync', old)
        refs = self.target.run('show-ref').stdout
        self.run_bridge(before=None, after=first, check=True)
        self.assertEqual(self.target.run('show-ref').stdout, refs)
        self.run_bridge(before=None, after=first)
        record = json.loads(self.target.run('show', f'{STATE_REF}:progress.json').stdout)
        self.assertEqual(record['progress'], {'cursor': second, 'master': later_master, 'pending': None})
        self.assertEqual(record['review_comments'], review)
        self.assertNotIn('events', record)
        self.assertNotIn('refs/heads/gtihub-sync', self.target.run('show-ref').stdout)
        self.assertTrue(self.target.is_ancestor(old, self.target.run('rev-parse', STATE_REF).stdout.strip()))
        self.assertEqual(len(self.target_pulls), 2)

    def test_newer_run_adopts_an_old_partially_applied_push_and_reuses_its_open_pr(self):
        first = self.source_commit({'file': b'one'}, b'one', [self.source_base], account=10)
        second = self.source_commit({'file': b'two'}, b'two', [first], account=10)
        third = self.source_commit({'file': b'three'}, b'three', [second])
        for number, commit in enumerate([first, second, third], 1):
            self.source_pull(commit, number)
        self.run_bridge(after=first)
        self.lose_response = 'create'
        with self.assertRaisesRegex(BridgeError, 'response lost'):
            self.run_bridge(before=first, after=second)
        with self.session() as (bridge, store):
            pending = dict(store.progress['pending'])
            pending['member_emails'] = {a['id']: a['email'] for a in pending.pop('authors').values()}
            events = {
                'snapshot/' + self.source_base: {'done': True, 'applied': 0, 'master': self.master, 'pending': None},
                'push/' + second: {'done': False, 'before': self.source_base, 'applied': 1,
                                   'master': self.target_head(), 'pending': pending},
            }
            record = {'repositories': bridge.binding, 'events': events, 'review_comments': {'98': {'12': '90'}}}
        old = make_commit(self.target, {'progress.json': json.dumps(record).encode()}, b'Old checkpoint')
        self.target.run('update-ref', STATE_REF, old)
        self.member_tokens.clear()
        self.run_bridge(before=None, after=third)
        self.assertEqual(len(self.target_pulls), 3)
        self.assertTrue(all(pr['state'] == 'merged' for pr in self.target_pulls.values()))
        self.assertEqual(self.target_pulls['2']['user']['id'], MAPPED['id'])
        self.assertEqual(self.target_pulls['3']['user']['id'], BOT['id'])
        self.target.verify_identity(self.target_pulls['2']['head']['sha'], MAPPED)
        with self.session() as (_, store):
            self.assertEqual(store.progress, {'cursor': third, 'master': self.target_head(), 'pending': None})
            self.assertEqual(store.review_comments, record['review_comments'])

    def test_legacy_staged_pr_finishes_when_the_original_member_and_pat_are_gone(self):
        source = self.source_commit({'new': b'one'}, b'mapped', [self.source_base], account=10)
        self.source_pull(source, 3)
        self.lose_response = 'create'
        with self.assertRaisesRegex(BridgeError, 'response lost'):
            self.run_bridge()
        with self.session() as (_, store):
            pending = dict(store.progress['pending'])
            pending.pop('authors')
            self.assertNotIn('member_emails', pending)
            store.save({**store.progress, 'pending': pending})
        self.cfg['members'].clear()
        self.member_tokens.clear()
        self.run_actions('workflow_dispatch', source)
        self.assertEqual(len(self.target_pulls), 1)
        self.assertEqual(self.target_pulls['1']['state'], 'merged')
        self.target.verify_identity(self.target_pulls['1']['head']['sha'], MAPPED)

    def test_stale_run_finishes_ci_blocked_newer_pending_before_skipping_its_revision(self):
        first = self.source_commit({'file': b'one'}, b'one', [self.source_base], account=10)
        self.source_pull(first, 15)
        self.deny_once['bot-token', 'PUT', '/pulls/1/merge'] = 403
        with self.assertRaisesRegex(BridgeError, 'Could not merge'):
            self.run_bridge(after=first)
        self.assertEqual(self.target_pulls['1']['state'], 'open')
        self.assertEqual(self.target_head(), self.master)
        self.member_tokens.clear()
        self.run_bridge(before=None, after=self.source_base)
        self.assertEqual(len(self.target_pulls), 1)
        self.assertEqual(self.target_pulls['1']['state'], 'merged')
        self.assertEqual(self.target.tree(self.target_head()), self.source.tree(first))
        with self.session() as (_, store):
            self.assertEqual(store.progress['cursor'], first)

    def test_member_rejected_on_pr_creation_records_robot_before_retrying(self):
        source = self.source_commit({'file': b'text'}, b'update', [self.source_base], account=10)
        self.source_pull(source, 7)
        self.deny_once['alice-token', 'POST', '/pulls'] = 403
        self.lose_response = 'create'
        with self.assertRaisesRegex(BridgeError, 'response lost'):
            self.run_bridge()
        with self.session() as (_, store):
            self.assertEqual(store.progress['pending']['creator'], public_account(BOT))
        self.run_bridge(before=None)
        self.assertEqual(len(self.target_pulls), 1)
        self.assertEqual(self.target_pulls['1']['user']['login'], BOT['username'])
        self.assertEqual(self.target_pulls['1']['state'], 'merged')
        self.target.verify_identity(self.target_pulls['1']['head']['sha'], MAPPED)

    def test_inline_member_rejection_and_lost_robot_response_keep_just_one_note(self):
        self.source_comments['5'] = [self.review_pr()]
        self.deny_once['alice-token', 'POST', '/pulls/1/comments'] = 401
        self.lose_response = 'comment'
        with self.assertRaisesRegex(BridgeError, 'response lost'):
            self.run_bridge()
        with self.session() as (_, store):
            self.assertEqual(store.review_comments['1']['11']['account'], public_account(BOT))
        self.run_bridge(before=None)
        self.assertEqual([token for token, _ in self.review_writes], ['bot-token'])
        self.assertEqual(self.target_pulls['1']['user']['login'], MAPPED['username'])
        self.assertEqual(self.target_pulls['1']['state'], 'merged')

    def test_uncertain_member_pr_request_is_not_repeated_as_robot(self):
        source = self.source_commit({'file': b'text'}, b'update', [self.source_base])
        self.source_pull(source, 7)
        self.deny_once['alice-token', 'POST', '/pulls'] = 503
        with self.assertRaises(ApiError):
            self.run_bridge()
        self.assertFalse(self.target_pulls)
        with self.session() as (_, store):
            self.assertEqual(store.progress['pending']['creator'], public_account(MAPPED))
        self.run_bridge(before=None)
        self.assertEqual(self.target_pulls['1']['user']['login'], MAPPED['username'])

    def test_cursor_only_on_a_secondary_parent_is_a_rewrite(self):
        confirmed = self.source_commit({'file': b'one'}, b'confirmed', [self.source_base])
        self.run_bridge(after=confirmed)
        other = self.source_commit({'file': b'new line'}, b'rewritten main', [self.source_base])
        merged = self.source_commit({'file': b'merged'}, b'new merge', [other, confirmed])
        refs = self.target.run('show-ref').stdout
        with self.assertRaisesRegex(BridgeError, 'first-parent history'):
            self.run_bridge(before=None, after=merged)
        self.assertEqual(self.target.run('show-ref').stdout, refs)

    def test_divergent_completed_events_stop_before_the_old_branch_is_renamed(self):
        first = self.source_commit({'file': b'one'}, b'one', [self.source_base])
        second = self.source_commit({'file': b'two'}, b'two', [self.source_base])
        with self.session() as (bridge, _):
            event = {'done': True, 'applied': 1, 'pending': None, 'master': self.master, 'before': self.source_base}
            record = {'repositories': bridge.binding, 'events': {'push/' + first: event, 'push/' + second: event}}
        legacy = make_commit(self.target, {'progress.json': json.dumps(record).encode()}, b'Divergent receipts')
        self.target.run('update-ref', '-d', STATE_REF)
        self.target.run('update-ref', 'refs/heads/gtihub-sync', legacy)
        refs = self.target.run('show-ref').stdout
        with self.assertRaisesRegex(StateError, 'receipts diverge'):
            self.run_bridge(before=None, after=second)
        self.assertEqual(self.target.run('show-ref').stdout, refs)
        self.assertFalse(self.target_pulls)

    def test_stage_interruption_reuses_the_event_intent(self):
        first = self.source_commit({'new': b'one'}, b'one', [self.source_base])
        with patch.object(Git, 'stage', side_effect=BridgeError('stage interrupted')):
            with self.assertRaisesRegex(BridgeError, 'stage interrupted'):
                self.run_bridge()
        self.assertFalse(self.target_pulls)
        self.run_bridge()
        self.assertEqual(len(self.target_pulls), 1)
        self.assertEqual(self.target.tree(self.target_head()), self.source.tree(first))

    def review_pr(self):
        files = {'common.txt': b'first\nchanged\nthird\n', 'moved.txt': b'different\n'}
        commit = self.source_commit(files, b'change for review', [self.source_base], account=10)
        self.source_pull(commit, 5, opener=10)
        blob = self.source.run('rev-parse', commit + ':common.txt').stdout.strip()
        diff = '@@ -1,3 +1,3 @@\n first\n-second\n+changed\n third\n'
        self.review_files['github'] = [{'filename': 'common.txt', 'sha': blob, 'patch': diff}]
        self.review_files['gitcode'] = [{'filename': 'common.txt', 'blob_id': blob,
                                         'patch': {'new_path': 'common.txt', 'diff': diff}}]
        return {'id': 11, 'user': {'id': 10}, 'created_at': '2026-11-01T12:00:00Z',
                'path': 'common.txt', 'line': 2, 'side': 'RIGHT',
                'position': 3, 'subject_type': 'line', 'body': 'Review first.\n\n- Keep **this** text.  \n'}

    def test_inline_comments_use_each_reviewer_and_preserve_identical_roots(self):
        self.cfg['members'].append({'github': {'id': '20', 'username': 'bob-gh'}, 'gitcode': public_account(BOB),
                                       'gitcode_pr_token_secret': BOB_SECRET})
        base = self.review_pr()
        self.source_comments['5'] = [dict(base, user={'id': 20}), dict(base, id=12, user={'id': 77}),
                                     dict(base, id=13, user={'id': 20}), dict(base, id=14, in_reply_to_id=11)]
        native = {'id': 9, 'discussion_id': 'native', 'body': base['body'], 'comment_type': 'diff_comment',
                  'user': {'id': BOB['id'], 'login': BOB['username']}, 'diff_file': 'common.txt',
                  'diff_position': {'start_new_line': 2, 'end_new_line': 2, 'position_type': 'text'}}
        self.review_notes['1'] = [native]
        self.assertIn('single-line candidates=3', self.run_bridge(check=True))
        self.assertFalse(self.review_writes)
        self.assertIn('confirmed 4, source replies 1', self.run_bridge())
        self.assertEqual([t for t, _ in self.review_writes], ['bob-token', 'bot-token', 'bob-token', 'alice-token'])
        self.assertEqual(len(self.review_notes['1']), 4)
        for _, payload in self.review_writes[:3]:
            self.assertEqual(payload, {'body': base['body'], 'path': base['path'],
                                       'position': 2, 'position_type': 'text'})
        self.assertEqual(self.review_writes[3][1], {'body': base['body']})
        self.assertEqual([c['id'] for c in self.review_notes['1'][1]['reply']], [104])
        self.assertEqual(self.target_pulls['1']['user']['id'], MAPPED['id'])
        self.assertEqual(self.target_pulls['1']['state'], 'merged')
        with self.session() as (_, store):
            self.assertEqual(store.review_comments, {'1': {'11': '101', '12': '102', '13': '103', '14': '104'}})
        self.run_bridge()
        self.assertEqual(len(self.review_writes), 4)

    def test_unanchorable_roots_become_located_pr_comments_before_merge(self):
        base = self.review_pr()
        self.review_files['github'].append({'filename': 'changed.txt', 'sha': 'a'*40,
                                            'patch': '@@ -0,0 +1,2 @@\n+one\n+two'})
        self.review_files['gitcode'].append({'filename': 'changed.txt', 'blob_id': 'b'*40,
                                             'patch': {'new_path': 'changed.txt', 'diff': '@@ -0,0 +1,2 @@\n+one\n+two'}})
        variants = [dict(line=None, original_line=2), dict(side='LEFT'), dict(start_line=1),
                    dict(subject_type='file'), dict(line=99), dict(path='changed.txt'),
                    dict(path='absent.txt'), dict(position=None), dict(start_side='LEFT')]
        self.source_comments['5'] = [dict(base, id=i+1, **fields) for i, fields in enumerate(variants)]
        self.assertIn('confirmed 9', self.run_bridge())
        self.assertEqual([note['comment_type'] for note in self.review_notes['1']], ['pr_comment'] * 9)
        for _, payload in self.review_writes:
            self.assertEqual(set(payload), {'body'})
            self.assertTrue(payload['body'].startswith('> '))
            self.assertTrue(payload['body'].endswith('\n\n' + base['body']))
        self.assertEqual(self.target_pulls['1']['state'], 'merged')

    def test_cli_previews_then_syncs_mixed_comments_as_their_speakers_in_order(self):
        root = self.review_pr()
        source = self.source.run('rev-parse', SOURCE).stdout.strip()
        self.cfg['members'].append({'github': {'id': '20', 'username': 'bob-gh'}, 'gitcode': public_account(BOB),
                                    'gitcode_pr_token_secret': BOB_SECRET})
        del self.member_tokens[BOB_SECRET]
        self.source_comments['5'] = [root, dict(root, id=12, subject_type='file', user={'id': 20},
                                                created_at='2026-11-01T12:04:00Z'), dict(root, id=13, in_reply_to_id=11)]
        self.source_issue_comments['5'] = [{'id': 11, 'user': {'id': 20}, 'body': 'First.\r\n\r\n**Markdown**  \n',
                                            'created_at': '2026-11-01T11:59:00Z'}]
        self.source_reviews['5'] = [{'id': 11, 'user': {'id': 10}, 'state': 'CHANGES_REQUESTED',
                                     'submitted_at': '2026-11-01T12:02:00Z', 'body': 'Change this.'},
                                    {'id': 12, 'user': {'id': 10}, 'state': 'COMMENTED',
                                     'body': '  ', 'submitted_at': '2026-11-01T12:01:00Z'}]
        refs = self.target.run('show-ref').stdout
        preview = self.run_actions('workflow_dispatch', source, operation='check')
        self.assertIn('pr_comment=3, diff_comment=0, single-line candidates=1', preview)
        self.assertIn('thread replies=1', preview)
        self.assertEqual(self.target.run('show-ref').stdout, refs)
        self.assertFalse(self.review_writes)
        self.assertFalse(any(method != 'GET' for _, method, _ in self.calls))
        self.run_actions('push', source, before=self.source_base)
        self.assertEqual([token for token, _ in self.review_writes],
                         ['bot-token', 'alice-token', 'alice-token', 'alice-token', 'bot-token'])
        self.assertEqual([n['comment_type'] for n in self.review_notes['1']],
                         ['pr_comment', 'diff_comment', 'pr_comment', 'pr_comment'])
        self.assertEqual([n['body'] for n in self.review_notes['1']],
                         [self.source_issue_comments['5'][0]['body'], root['body'], '**CHANGES_REQUESTED**\n\nChange this.',
                          '> `common.txt`\n\n' + root['body']])
        with self.session() as (_, store):
            self.assertEqual(store.review_comments['1'], {'issue:11': '100', '11': '101', '13': '102',
                                                         'review:11': '103', '12': '104'})
        self.assertEqual(self.review_notes['1'][1]['reply'][0]['body'], root['body'])
        self.assertEqual(self.target_pulls['1']['state'], 'merged')
        self.run_actions('workflow_dispatch', source)
        self.assertEqual(len(self.review_writes), 5)

    def test_rejected_reply_recovers_as_robot_then_the_next_member_replies_to_the_same_root(self):
        root = dict(self.review_pr(), subject_type='file', user={'id': 77})
        first = dict(root, id=12, in_reply_to_id=11, user={'id': 10}, body='First **reply**.\r\n  \n')
        second = dict(first, id=13, user={'id': 20}, body='Follow-up with markup and spacing.  \n',
                      created_at='2026-11-01T12:01:00Z')
        self.cfg['members'].append({'github': {'id': '20', 'username': 'bob-gh'}, 'gitcode': public_account(BOB),
                                    'gitcode_pr_token_secret': BOB_SECRET})
        self.source_comments['5'] = [second, first, root]
        self.deny_once[('alice-token', 'POST', '/pulls/1/discussions/discussion-100/comments')] = 403
        self.lose_response = 'reply'
        with self.assertRaisesRegex(BridgeError, 'response lost'):
            self.run_bridge()
        with self.session() as (_, store):
            receipt = store.review_comments['1']['12']
            self.assertEqual((receipt['kind'], receipt['root_note_id'], receipt['discussion_id']),
                             ('reply', '100', 'discussion-100'))
            self.assertEqual(receipt['account']['id'], BOT['id'])
            self.assertEqual(store.progress['cursor'], self.source_base)
        self.assertEqual(self.target_pulls['1']['state'], 'open')
        self.source_comments['5'] = [second]  # The first reply and its parent have since disappeared upstream.
        self.assertIn('unconfirmed writes=1', self.run_bridge(check=True))
        self.run_bridge()
        self.run_bridge()
        self.assertEqual(self.review_writes, [('bot-token', {'body': '> `common.txt`\n\n' + root['body']}),
                                              ('bot-token', {'body': first['body']}),
                                              ('bob-token', {'body': second['body']})])
        self.assertEqual([c['id'] for c in self.review_notes['1'][0]['reply']], [101, 102])
        with self.session() as (_, store):
            self.assertIsNone(store.progress['pending'])
            self.assertEqual(store.review_comments['1'], {'11': '100', '12': '101', '13': '102'})
        self.assertEqual(self.target_pulls['1']['state'], 'merged')

    def test_orphan_replies_recover_as_pr_comments_and_allow_the_other_reviews_and_merge(self):
        base = self.review_pr()
        after = self.source.run('rev-parse', SOURCE).stdout.strip()
        first = dict(base, id=12, in_reply_to_id=11, body='Keep `code`.\r\n\r\n- Follow up.  \n')
        second = dict(first, id=13, user={'id': 77}, created_at='2026-11-01T12:01:00Z')
        other = dict(base, id=14, created_at='2026-11-01T12:02:00Z')
        self.source_comments['5'] = [other, second, first]
        preview = self.run_actions('workflow_dispatch', after, operation='check')
        self.assertIn('pr_comment=2, diff_comment=0, single-line candidates=1', preview)
        self.assertIn('thread replies=0', preview)
        self.assertFalse(self.review_writes)

        expected = {'body': '> 回复 `common.txt`\n\n' + first['body']}
        self.lose_response = 'comment'
        with self.assertRaisesRegex(BridgeError, 'response lost'):
            self.run_actions('push', after, before=self.source_base)
        with self.session() as (_, store):
            record = store.review_comments['1']['12']
            self.assertEqual((record['kind'], record['account']['id'], record['payload']), ('pr', MAPPED['id'], expected))
            self.assertEqual(store.progress['cursor'], self.source_base)

        self.source_comments['5'] = [other, second]
        self.run_actions('workflow_dispatch', after)
        self.run_actions('workflow_dispatch', after)
        self.assertEqual(self.review_writes[:2], [('alice-token', expected), ('bot-token', expected)])
        self.assertEqual(len(self.review_writes), 3)
        self.assertEqual([note['comment_type'] for note in self.review_notes['1']],
                         ['pr_comment', 'pr_comment', 'diff_comment'])
        self.assertFalse(any('/discussions/' in path for _, method, path in self.calls if method == 'POST'))
        with self.session() as (_, store):
            self.assertEqual(store.review_comments['1'], {'12': '100', '13': '101', '14': '102'})
            self.assertEqual(store.progress['cursor'], after)
        self.assertEqual(self.target_pulls['1']['state'], 'merged')

    def test_uncertain_pr_comment_after_manual_merge_holds_cursor_until_confirmed(self):
        root = self.review_pr()
        self.source_issue_comments['5'] = [dict(root, body='Discussion before merge')]
        self.lose_response = 'comment'
        with self.assertRaisesRegex(BridgeError, 'response lost'):
            self.run_bridge()
        with self.session() as (_, store):
            self.assertEqual(store.review_comments['1']['issue:11']['kind'], 'pr')
        self.source_issue_comments['5'].clear()
        detail = self.review_details['100']
        discussion = detail['discussion_id']
        detail['discussion_id'] = 'other discussion'
        with self.session() as (bridge, store):
            prepare = bridge.prepare_pull

            def merged_during_retry(*args):
                self.merge_pull(self.target_pulls['1'])
                return prepare(*args)

            with patch.object(bridge, 'prepare_pull', side_effect=merged_during_retry):
                with self.assertRaisesRegex(BridgeError, 'unconfirmed write'), redirect_stdout(io.StringIO()):
                    bridge.run(store, self.source_base, self.source.run('rev-parse', SOURCE).stdout.strip())
        self.assertEqual(self.target_pulls['1']['state'], 'merged')
        with self.session() as (_, store):
            self.assertEqual(store.progress['cursor'], self.source_base)
        refs = self.target.run('show-ref').stdout
        self.assertIn('GitCode PR already merged, unconfirmed writes=1', self.run_bridge(check=True))
        self.assertEqual(self.target.run('show-ref').stdout, refs)
        detail['discussion_id'] = discussion
        self.run_bridge()
        self.assertEqual(len(self.review_writes), 1)
        with self.session() as (_, store):
            self.assertIsNone(store.progress['pending'])
            self.assertEqual(store.review_comments['1'], {'issue:11': '100'})

    def test_lost_inline_post_recovers_without_reposting_or_following_edits(self):
        base = self.review_pr()
        self.source_comments['5'] = [base]
        self.lose_response = 'comment'
        with self.assertRaisesRegex(BridgeError, 'response lost'):
            self.run_bridge()
        self.assertEqual(self.target_pulls['1']['state'], 'open')
        self.source_comments['5'][0]['body'] = 'An edit made after the first publication'
        self.member_tokens.clear()
        self.assertIn('confirmed 1', self.run_bridge())
        self.assertEqual(len(self.review_writes), 1)
        self.assertEqual(self.review_notes['1'][0]['body'], self.review_writes[0][1]['body'])
        self.assertNotEqual(self.review_notes['1'][0]['body'], self.source_comments['5'][0]['body'])

    def test_uncertain_inline_write_never_blindly_reposts_or_merges(self):
        self.source_comments['5'] = [self.review_pr()]
        self.lose_response = 'comment'
        with self.assertRaisesRegex(BridgeError, 'response lost'):
            self.run_bridge()
        old = self.review_notes['1'].pop()
        with self.assertRaisesRegex(BridgeError, 'unconfirmed write'):
            self.run_bridge()
        self.review_notes['1'].extend([old, dict(old, id=301)])
        self.review_details['301'] = dict(self.review_details['100'], id=301)
        with self.assertRaisesRegex(BridgeError, 'Ambiguous inline comment'):
            self.run_bridge()
        self.assertEqual(len(self.review_writes), 1)
        self.assertEqual(self.target_pulls['1']['state'], 'open')

    def test_inline_note_detail_must_confirm_the_file_and_revision(self):
        base = self.review_pr()
        self.source_comments['5'] = [base]
        self.returned_note_path = 'other.txt'
        with self.assertRaisesRegex(BridgeError, 'Could not confirm the inline comment'):
            self.run_bridge()
        self.assertEqual(self.target_pulls['1']['state'], 'open')
        detail = self.review_details['100']
        detail['position'].update(new_path='common.txt', head_sha='0' * 40)
        with self.assertRaisesRegex(BridgeError, 'unconfirmed write'):
            self.run_bridge()
        detail['position']['head_sha'] = self.target_pulls['1']['head']['sha']
        self.run_bridge()
        self.assertEqual(len(self.review_writes), 1)
        self.assertEqual(self.target_pulls['1']['state'], 'merged')

    def test_reviewer_credential_mismatch_falls_back_without_changing_the_opener(self):
        base = self.review_pr()
        self.cfg['members'].append({'github': {'id': '20', 'username': 'bob-gh'}, 'gitcode': public_account(BOB),
                                    'gitcode_pr_token_secret': BOB_SECRET})
        self.source_comments['5'] = [dict(base, user={'id': 20}), dict(base, id=12, user={'id': 10})]
        self.member_tokens[BOB_SECRET] = 'alice-token'
        self.run_bridge()
        self.assertEqual([token for token, _ in self.review_writes], ['bot-token', 'alice-token'])
        self.assertEqual(self.target_pulls['1']['user']['login'], MAPPED['username'])
        self.assertEqual(self.target_pulls['1']['state'], 'merged')


class ConfigAndAccessTests(unittest.TestCase):
    def test_private_config_and_public_members_are_disjoint(self):
        example = Path(__file__).resolve().parents[1] / 'bridge.example.json'
        cfg = json.loads(example.read_text())
        self.assertEqual(set(cfg), {'gitcode'})
        with self.assertRaisesRegex(BridgeError, 'email bound'):
            config_read(example)
        with TemporaryDirectory() as directory:
            filename, members = Path(directory) / 'cfg.json', Path(directory) / 'members.json'
            cfg['gitcode']['bot']['email'] = BOT['email']
            filename.write_text(json.dumps(cfg))
            public_members = example.parent / '.github' / 'gitcode-members.json'
            self.assertEqual(config_read(filename)['members'], json.loads(public_members.read_text()))
            members.write_text('[]')
            self.assertEqual(config_read(filename, members)['members'], [])
            for key, value in [('identities', []), ('schema', 4), ('github', {'start_from': '1' * 40})]:
                filename.write_text(json.dumps({**cfg, key: value}))
                with self.assertRaisesRegex(BridgeError, 'only gitcode'):
                    config_read(filename, members)

    def test_member_secret_slots_are_distinct_and_cannot_select_other_credentials(self):
        entries = [{'github': {'id': str(i), 'username': str(i)}, 'gitcode': public_account(account),
                    'gitcode_pr_token_secret': secret}
                   for i, (account, secret) in enumerate(((MAPPED, ALICE_SECRET), (BOB, BOB_SECRET)), 1)]
        with TemporaryDirectory() as directory:
            filename, members = Path(directory) / 'cfg.json', Path(directory) / 'members.json'
            filename.write_text(json.dumps({'gitcode': {'repo': GITCODE, 'bot': BOT}}))
            members.write_text(json.dumps(entries))
            self.assertEqual(config_read(filename, members)['members'], entries)
            for slot in (ALICE_SECRET, 'GITCODE_MEMBER_1_TOKEN', 'GITCODE_MEMBER_02_TOKEN',
                         'GITCODE_MEMBER_bob_TOKEN', 'GITCODE_MEMBER_BOB-NAME_TOKEN', 'BRIDGE_PR_CREATOR_TOKEN',
                         'GITCODE_ROBOT_TOKEN', 'GITHUB_TOKEN', 'SYNC_GIT_HTTPS_TOKEN'):
                entries[1]['gitcode_pr_token_secret'] = slot
                members.write_text(json.dumps(entries))
                with self.assertRaisesRegex(BridgeError, 'gitcode_pr_token_secret'):
                    config_read(filename, members)
            entries[1]['gitcode']['email'] = MAPPED['email']
            members.write_text(json.dumps(entries))
            with self.assertRaisesRegex(BridgeError, 'email only in the bot configuration'):
                config_read(filename, members)

    def test_member_email_comes_from_its_authenticated_profile_or_confirmations(self):
        api = Mock()
        alias = 'member-id.member@noreply.gitcode.com'
        self.assertEqual(commit_email(api, {'email': alias}), alias)
        api.request.assert_not_called()
        api.request.return_value = [
            {'email': 'unverified@example.com', 'state': 'pending'},
            {'email': 'first@example.com', 'state': 'confirmed'},
            {'email': 'second@example.com', 'state': 'confirmed'},
        ]
        self.assertEqual(commit_email(api, {'email': 'second@example.com'}), 'second@example.com')
        with self.assertRaisesRegex(BridgeError, 'Select a confirmed email'):
            commit_email(api, {'email': 'unverified@example.com'})
        api.request.return_value = {'email': 'only@example.com', 'state': 'confirmed'}
        self.assertEqual(commit_email(api, {'email': None}), 'only@example.com')

    def test_push_payload_validates_ref_revision_and_branch_creation(self):
        before, after = '1' * 40, '2' * 40
        with TemporaryDirectory() as directory:
            event_file = Path(directory) / 'event'
            env = {'GITHUB_EVENT_NAME': 'push', 'GITHUB_SHA': after, 'GITHUB_REF': SOURCE,
                   'GITHUB_EVENT_PATH': str(event_file)}
            event = {'before': before, 'after': after, 'ref': SOURCE, 'created': False}
            with patch.dict(os.environ, env):
                event_file.write_text(json.dumps(event))
                self.assertEqual(workflow_range(), (before, after))
                for change in ({'after': before}, {'ref': 'refs/heads/topic'},
                               {'before': '0' * 40}, {'created': True}, {'deleted': True}):
                    event_file.write_text(json.dumps({**event, **change}))
                    with self.assertRaises(BridgeError):
                        workflow_range()
                event_file.write_text(json.dumps({**event, 'before': '0' * 40, 'created': True}))
                self.assertEqual(workflow_range(), (None, after))

    def test_git_https_credentials_follow_the_selected_remote(self):
        with TemporaryDirectory() as directory:
            git = Git(directory)
            for remote in (Remote('github', GITHUB, 'source-read', 'x-access-token'),
                           Remote('gitcode', GITCODE, 'bot-token', BOT['username'])):
                result = git.run('credential', 'fill', remote=remote, input_data=f'url={remote.url}\n\n')
                fields = dict(line.split('=', 1) for line in result.stdout.splitlines() if line)
                self.assertEqual(fields['username'], remote.username)
                self.assertEqual(fields['password'], remote.token)

    def test_source_write_guards(self):
        with self.assertRaisesRegex(BridgeError, 'read-only'):
            Api('github', 'unused', GITHUB).call('POST', '/pulls')
        with TemporaryDirectory() as directory:
            git = Git(directory)
            source = Remote('github', GITHUB, 'unused', 'unused')
            with self.assertRaisesRegex(BridgeError, 'destination bridge branches'):
                git.stage(source, 'bridge/test', '0' * 40)
            with self.assertRaisesRegex(BridgeError, 'Only the destination'):
                git.publish_state(source, '0' * 40, None)
            with self.assertRaisesRegex(BridgeError, 'Only the destination'):
                git.rename_legacy_state(source, 'refs/heads/gtihub-sync', '0' * 40)
        self.assertEqual(str(ApiError('gitcode', 403)), 'Destination API request failed (HTTP 403)')


if __name__ == '__main__':
    unittest.main()
