"""Choose the GitCode PR head before publishing any branch or review."""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache


@dataclass
class ImportPlan:
    head: str
    count: int
    authors: dict
    topic_parents: dict | None = None
    native_merge: bool = False

    def preview_master(self, bridge, master, source):
        if not self.native_merge:
            return self.head
        # Later groups in the same read-only check need the merged tree and both parents.
        return bridge.git.rebuild(source, [master, self.head], bridge.bot,
                                  maintenance_message='Preview GitCode merge\n')


def is_outer_merge(bridge, original, batch, graph):
    if len(batch) != 1 or len(graph) < 2 or not original:
        return False
    outer = graph[-1]
    if (len(outer) != 3 or outer[0] != batch[0] or not (original.get('merged') is True or original.get('merged_at'))
            or (original.get('head') or {}).get('sha') != outer[2]):
        return False
    # REST 2026-03-10 exposes the landing SHA on the merged event.
    landed = bridge.source_merge_sha(original['number'])
    if landed != outer[0]:
        print(f'FULL GRAPH {outer[0]} | GitHub PR #{original["number"]} was merged at {landed}')
        return False
    return True


def topic_boundaries(git, store, graph, master, history):
    _, base, topic = graph[-1]
    business = {row[0] for row in graph[:-1]}
    if git.tree(base) != git.tree(master) or topic not in business or any(len(row) == 1 for row in graph[:-1]):
        return None
    boundaries = {}
    for _, *parents in graph[:-1]:
        for parent in parents:
            if parent in business or parent in boundaries:
                continue
            if parent == base:
                target = master
            else:
                candidates = history().get(git.tree(parent), [])
                if not candidates:
                    return None
                target = store.confirmed_master(parent)
                if target is None:
                    target = parent if parent in candidates else git.matching_import(parent, candidates)
                if target not in candidates:
                    return None
            boundaries[parent] = target
    return boundaries


def native_merge_failure(git, translated, master, head, source):
    if len(set(translated.values())) != len(translated):
        return 'distinct source commits translated to one destination commit'
    if git.is_ancestor(head, master):
        return 'topic head is already in destination master'
    tree = git.merge_tree(master, head)
    if tree is None:
        return 'automatic merge has conflicts'
    if tree != git.tree(source):
        return 'automatic merge tree differs from the GitHub merge'
    return None


def plan_import(bridge, store, batch, master, *, original=None, topic_parents=None, try_native=True, preview=False,
                alignment=False, pinned=None, legacy_emails=None):
    git, source = bridge.git, batch[-1]
    if alignment:
        head = git.rebuild(source, [master], bridge.bot, maintenance_message=f'Align master to GitHub {source}\n')
        return ImportPlan(head, 0, {})

    base, graph = git.introduced_commits(batch[0], source)

    @cache
    def history():
        return git.history_by_tree(master)

    # Only a newly planned, confirmed two-parent GitHub PR may select the native merge.
    # A resumed import receives its saved boundaries, or stays on the old full-graph path.
    if is_outer_merge(bridge, original, batch, graph):
        topic_parents = topic_boundaries(git, store, graph, master, history)
        if topic_parents is None:
            print(f'FULL GRAPH {source} | no verified destination fork point')

    translated = {base: master} if base else {}
    if topic_parents is not None:
        translated.update(topic_parents)
    authors = {}
    for commit, *parents in graph:
        if commit == source and topic_parents is not None and try_native:
            head = translated[parents[1]]
            reason = native_merge_failure(git, translated, master, head, source)
            if reason is None:
                print(f'NATIVE MERGE {source} | verified GitCode merge tree')
                return ImportPlan(head, len(graph) - 1, authors, topic_parents, native_merge=True)
            print(f'FULL GRAPH {source} | {reason}')
        for parent in parents:
            if parent in translated:
                continue
            # The full-graph path retains the original parent tree even for a pre-sync fork.
            tree = git.tree(parent)
            by_tree = history()
            if tree not in by_tree:
                message = f'Anchor imported branch at GitHub {parent}\n'
                by_tree[tree] = [git.rebuild(parent, [master], bridge.bot, maintenance_message=message)]
            translated[parent] = by_tree[tree][0]
        dest_parents = [translated[p] for p in parents]
        if not parents and commit == batch[0]:
            dest_parents = [master]
        author = pinned.get(commit, bridge.bot) if pinned is not None else bridge.author(commit, legacy_emails or {})
        if pinned is None and author['id'] != bridge.bot['id']:
            authors[commit] = author
        translated[commit] = git.rebuild(commit, dest_parents, author)
        if preview:
            print(f'{commit} | {"bot" if author["username"] == bridge.bot["username"] else "mapped"}')
    return ImportPlan(translated[source], len(graph), authors, topic_parents)
