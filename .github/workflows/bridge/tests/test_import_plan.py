import sys
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api import BridgeError
from bridge import Bridge
from git_ops import Git, Remote
from import_plan import plan_import
from state_store import GitStore, checkpoint


BOT = {'id': 'bot', 'username': 'robot', 'email': 'robot@example.org'}
MEMBER = {'id': 'member', 'username': 'developer', 'email': 'developer@example.org'}


class ImportPlanTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.git = Git(temp.name)
        self.bridge = Bridge.__new__(Bridge)
        self.bridge.git, self.bridge.bot = self.git, BOT
        self.bridge.author = lambda oid, emails: MEMBER
        self.bridge.github_api = Mock()
        self.bridge.source_merge_commits = {}
        self.confirmed = {}
        self.store = SimpleNamespace(confirmed_master=self.confirmed.get)

    def commit(self, files, message, *parents):
        entries = []
        for path, content in sorted(files.items()):
            blob = self.git.run('hash-object', '-w', '--stdin', input_data=content).stdout.strip()
            entries.append(f'100644 blob {blob}\t{path}\n')
        tree = self.git.run('mktree', input_data=''.join(entries)).stdout.strip()
        headers = [f'tree {tree}', *('parent ' + p for p in parents),
                   'author Original <author@example.org> 1700000000 +0530',
                   'committer Integrator <merger@example.org> 1710000000 -0700']
        raw = ('\n'.join(headers) + '\n\n' + message).encode()
        return self.git.run('hash-object', '-w', '-t', 'commit', '--stdin',
                            binary=True, input_data=raw).stdout.strip().decode()

    def plan(self, outer, topic, master, *, landed=None, **fields):
        original = dict({'number': 78, 'merged': True, 'head': {'sha': topic}}, **fields)
        self.bridge.github_api.pages.return_value = [{'event': 'merged', 'commit_id': landed or outer}]
        self.bridge.source_merge_commits.clear()
        with redirect_stdout(StringIO()):
            return plan_import(self.bridge, self.store, [outer], master, original=original)

    def test_native_merge_preserves_internal_merge_and_every_parent_diff(self):
        base = self.commit({}, 'base')
        mid = self.commit({'one': 'upstream'}, 'main one', base)
        main = self.commit({'one': 'upstream', 'two': 'later'}, 'main two', mid)
        target_base = self.commit({}, 'aligned base')
        # Another identical tree is reachable. The confirmed landing pins the actual fork.
        duplicate = self.commit({}, 'empty change', target_base)
        target_mid = self.git.rebuild(mid, [duplicate], BOT)
        target_main = self.git.rebuild(main, [target_mid], BOT)
        self.confirmed.update({base: target_base, mid: target_mid})
        first = self.commit({'topic': 'feature'}, 'first\r\n\r\nRaw text  ', base)
        merged = self.commit({'one': 'upstream', 'topic': 'feature'}, 'Merge main into topic', first, mid)
        topic = self.commit({'one': 'upstream', 'topic': 'final'}, 'last', merged)
        outer = self.commit({'one': 'upstream', 'two': 'later', 'topic': 'final'}, 'GitHub merge', main, topic)

        plan = self.plan(outer, topic, target_main)
        self.assertTrue(plan.native_merge)
        self.assertEqual(plan.count, 3)
        self.assertEqual(plan.topic_parents, {base: target_base, mid: target_mid})
        inner, = self.git.parents(plan.head)
        rebuilt_first, reused_mid = self.git.parents(inner)
        self.assertEqual(self.git.parents(rebuilt_first), [target_base])
        self.assertEqual(reused_mid, target_mid)
        for source, target in [(first, rebuilt_first), (merged, inner), (topic, plan.head)]:
            self.git.verify_identity(target, MEMBER)
            self.assertEqual(self.git.tree(source), self.git.tree(target))
            self.assertEqual([self.git.tree(p) for p in self.git.parents(source)],
                             [self.git.tree(p) for p in self.git.parents(target)])
            raw = self.git.run('cat-file', 'commit', target, binary=True).stdout
            self.assertIn(b'1700000000 +0530\ncommitter developer <developer@example.org> 1710000000 -0700', raw)
            self.assertEqual(raw.split(b'\n\n', 1)[1],
                             self.git.run('cat-file', 'commit', source, binary=True).stdout.split(b'\n\n', 1)[1])
        self.assertNotEqual(self.git.tree(plan.head), self.git.tree(outer))
        self.assertEqual(self.git.merge_tree(target_main, plan.head), self.git.tree(outer))
        preview = plan.preview_master(self.bridge, target_main, outer)
        self.assertEqual(self.git.parents(preview), [target_main, plan.head])
        self.assertEqual(self.git.tree(preview), self.git.tree(outer))

    def test_missing_fork_preserves_original_topic_diff_and_outer_resolution(self):
        fork = self.commit({'old': 'original'}, 'lost fork')
        main = self.commit({'new': 'main'}, 'main', fork)
        master = self.commit({'new': 'main'}, 'destination baseline')
        topic = self.commit({'old': 'edited'}, 'business change', fork)
        outer = self.commit({'new': 'main', 'old': 'edited'}, 'source merge', main, topic)
        plan = self.plan(outer, topic, master)
        self.assertFalse(plan.native_merge)
        _, rebuilt = self.git.parents(plan.head)
        anchor, = self.git.parents(rebuilt)
        self.assertEqual(self.git.tree(anchor), self.git.tree(fork))
        self.assertEqual(self.git.tree(rebuilt), self.git.tree(topic))
        self.assertEqual(self.git.merge_tree(master, plan.head), self.git.tree(outer))
        self.assertEqual(self.git.parents(anchor), [master])

    def test_conflict_and_clean_merge_with_extra_resolution_keep_the_outer_merge(self):
        for conflict in (True, False):
            with self.subTest(conflict=conflict):
                base = self.commit({'f': 'base\n'}, 'base')
                target_base = self.git.rebuild(base, [], BOT)
                main_files = {'f': 'main\n'} if conflict else {'f': 'base\n', 'other': 'main'}
                main = self.commit(main_files, 'main', base)
                master = self.git.rebuild(main, [target_base], BOT)
                topic = self.commit({'f': 'topic\n'}, 'business', base)
                outer = self.commit({**main_files, 'f': 'manual edit\n'}, 'resolved on merge', main, topic)
                plan = self.plan(outer, topic, master)
                self.assertFalse(plan.native_merge)
                self.assertEqual(plan.topic_parents, {base: target_base})
                parent, business = self.git.parents(plan.head)
                self.assertEqual(parent, master)
                self.assertEqual(self.git.parents(business), [target_base])
                self.assertEqual(self.git.merge_tree(master, business) is None, conflict)
                self.assertEqual(self.git.merge_tree(master, plan.head), self.git.tree(outer))

    def test_unproven_outer_merge_and_ambiguous_parent_keep_full_graph(self):
        base = self.commit({}, 'base')
        a = self.git.rebuild(base, [], BOT)
        b = self.git.rebuild(base, [], MEMBER)
        main = self.commit({'main': 'a'}, 'main', base)
        master = self.git.rebuild(main, [a, b], BOT)
        topic = self.commit({'f': 'data'}, 'feature', base)
        outer = self.commit({'main': 'a', 'f': 'data'}, 'Merge pull request #1', main, topic)
        self.assertFalse(self.plan(outer, topic, master).native_merge)
        self.confirmed[base] = a
        self.assertTrue(self.plan(outer, topic, master).native_merge)
        for fields in ({'merged': False}, {'head': {'sha': main}}, {'landed': main}):
            self.assertFalse(self.plan(outer, topic, master, **fields).native_merge)

    def test_associated_prs_use_the_merged_event_and_reuse_it_for_the_plan(self):
        base = self.commit({}, 'base')
        master = self.git.rebuild(base, [], BOT)
        topic = self.commit({'feature': 'data'}, 'business', base)
        outer = self.commit({'feature': 'data'}, 'merge', base, topic)
        self.bridge.source_branch = 'master'
        self.bridge.binding = {'source': ['example/project', 1, 'refs/heads/master']}
        base_ref = {'ref': 'master', 'repo': {'id': 1}}
        responses = {
            f'/commits/{outer}/pulls': [{'number': n, 'base': base_ref, 'merged_at': '2026-01-01T00:00:00Z'}
                                        for n in (78, 79)],
            '/issues/78/events': [{'event': 'referenced', 'commit_id': topic}, {'event': 'merged', 'commit_id': outer}],
            '/issues/79/events': [{'event': 'merged', 'commit_id': base}],
        }
        self.bridge.github_api.pages.side_effect = responses.__getitem__
        self.assertEqual(self.bridge.source_pull(outer), '78')
        pr = {'number': 78, 'merged': True, 'head': {'sha': topic}}
        self.bridge.github_api.pages.reset_mock()
        with redirect_stdout(StringIO()):
            plan = plan_import(self.bridge, self.store, [outer], master, original=pr)
        self.bridge.github_api.pages.assert_not_called()
        self.assertEqual((plan.native_merge, plan.count, self.git.parents(plan.head)), (True, 1, [master]))

    def test_missing_or_conflicting_merge_events_cannot_confirm_a_landing(self):
        first, second = '1' * 40, '2' * 40
        for rows in ([], [{'event': 'referenced', 'commit_id': first}],
                     [{'event': 'merged', 'commit_id': x} for x in (first, second)]):
            with self.subTest(rows=rows):
                self.bridge.github_api.pages.return_value = rows
                with self.assertRaisesRegex(BridgeError, 'merged event commit for GitHub PR #78'):
                    self.bridge.source_merge_sha(78)

    def test_git_failure_is_distinct_from_merge_conflict(self):
        tip = self.commit({}, 'tip')
        with self.assertRaises(BridgeError):
            self.git.merge_tree(tip, 'f' * 40)

    def test_fresh_runner_finds_confirmed_landing_across_old_pending_snapshots(self):
        remote = Remote('gitcode', 'example/target', None, 'robot')
        remote.url = str(self.git.directory)
        binding = {'source': ['example/source', 1], 'destination': ['example/target', 2]}
        first = self.commit({}, 'first')
        second = self.commit({'new': 'file'}, 'second', first)
        landed = self.commit({}, 'landing')
        other = self.commit({}, 'same tree, later attempt', landed)
        with TemporaryDirectory() as writer, TemporaryDirectory() as reader:
            store = GitStore(Git(writer), remote, binding, BOT)
            store.save(checkpoint(first, landed))
            store.save(checkpoint(first, other, {'source': second, 'head': other}))
            store.save(checkpoint(second, other))
            restored = GitStore(Git(reader), remote, binding, BOT)
            self.assertEqual(restored.git.run('rev-parse', '--is-shallow-repository').stdout.strip(), 'true')
            self.assertEqual(restored.confirmed_master(first), landed)


if __name__ == '__main__':
    unittest.main()
