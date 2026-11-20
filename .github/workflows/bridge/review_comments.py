"""Mirror submitted PR discussions before merging the destination PR."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import re
from urllib.parse import quote

from api import ApiError, BridgeError, same_account


HUNK = re.compile(r'^@@ -[0-9]+(?:,[0-9]+)? \+([0-9]+)(?:,([0-9]+))? @@')
COMMENT_LABEL = {'diff': 'inline comment', 'pr': 'PR comment', 'reply': 'discussion reply'}


def right_lines(patch):
    """Only count right-side lines actually present in a published diff hunk."""
    lines = {}
    current, remaining = 0, 0
    for row in patch.split('\n'):
        match = HUNK.match(row)
        if match:
            current = int(match[1])
            remaining = int(match[2]) if match[2] is not None else 1
        elif remaining and row.startswith((' ', '+')):
            lines[current] = row[1:]
            current += 1
            remaining -= 1
    return lines


@dataclass
class SourceComment:
    key: str
    time: datetime
    user: dict | None
    body: str
    candidate: dict | None = None  # A single RIGHT line still needs matching source and target diffs.
    reply_to: str | None = None
    reply_path: str | None = None
    orphaned: bool = False


def source_comment(row, prefix, body, candidate=None, *, reply_to=None):
    user, source_id = row.get('user'), str(row.get('id'))
    if (not source_id.isdecimal() or 'user' not in row or not isinstance(body, str)
            or (user is not None and (not isinstance(user, dict) or not str(user.get('id')).isdecimal()))):
        raise BridgeError('GitHub returned an incomplete PR comment')
    stamp = row.get('submitted_at' if prefix == 'review:' else 'created_at')
    try:
        time = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
        if time.tzinfo is None:
            raise ValueError
    except (AttributeError, ValueError) as error:
        raise BridgeError(f'Invalid GitHub comment timestamp for {prefix}{source_id}') from error
    return SourceComment(prefix + source_id, time, user, body, candidate,
                         reply_to=reply_to, reply_path=row.get('path') if reply_to else None)


def markdown_path(path):
    # A Git path can contain a newline or a backtick. Keep it in one Markdown code span on one line.
    path = path.replace('\r', r'\r').replace('\n', r'\n')
    fence = '`' * (1 + max(map(len, re.findall(r'`+', path)), default=0))
    pad = ' ' if path.startswith(('`', ' ')) or path.endswith(('`', ' ')) else ''
    return f'{fence}{pad}{path}{pad}{fence}'


def located_body(row, label=''):
    return f'> {markdown_path(row["path"])}{label}\n\n{row["body"]}'


def quoted_reply_body(item, root_note_id=None):
    header = '> 回复'
    if item.reply_path:
        header += ' ' + markdown_path(item.reply_path)
    if root_note_id is not None:
        header += f' [原评论](#tid-{root_note_id})'
    return f'{header}\n\n{item.body}'


def outdated_body(row):
    original = row.get('original_line')
    label = f' · 原第 {original} 行' if type(original) is int and original > 0 else ''
    side = row.get('side')
    if side in ('LEFT', 'RIGHT'):
        label += ' · ' + side
    return located_body(row, label + ' · 已过时/无法挂行')


def review_root(row):
    if not isinstance(row.get('path'), str) or not row['path'] or not isinstance(row.get('body'), str):
        raise BridgeError('GitHub returned a review comment without a path or body')
    subject, side, line = row.get('subject_type', 'line'), row.get('side'), row.get('line')
    start, start_side = row.get('start_line'), row.get('start_side') or side
    if subject == 'file':
        body = located_body(row)
    elif (subject != 'line' or type(line) is not int or line < 1
          or row.get('position', 1) is None or row.get('outdated') or row.get('is_outdated')
          or side not in ('LEFT', 'RIGHT') or start_side not in ('LEFT', 'RIGHT')
          or (start is not None and (type(start) is not int or start < 1))):
        body = outdated_body(row)
    elif side == 'RIGHT' and start_side == side and start in (None, line):
        return source_comment(row, '', row['body'], candidate=row)
    else:
        location = f'{side} L{line}'
        if start is not None and (start != line or start_side != side):
            location = f'{start_side} L{start} – {location}'
        elif start_side != side:
            location = f'{start_side} – {location}'
        body = located_body(row, ' · ' + location)
    return source_comment(row, '', body)


def read_comments(api, number, records=None):
    comments, pending_reviews = [], set()
    for row in api.pages(f'/issues/{number}/comments'):
        body = row.get('body')
        if body and body.strip():
            comments.append(source_comment(row, 'issue:', body))
    for row in api.pages(f'/pulls/{number}/reviews'):
        state, body = row['state'].upper(), row.get('body') or ''
        if state == 'PENDING':
            pending_reviews.add(str(row['id']))
            continue
        if state == 'COMMENTED':
            if not body.strip():
                continue
        elif state in ('APPROVED', 'CHANGES_REQUESTED', 'DISMISSED'):
            body = f'**{state}**' + ('\n\n' + body if body.strip() else '')
        else:
            raise BridgeError(f'Unknown GitHub review state {state}')
        comments.append(source_comment(row, 'review:', body))
    for row in api.pages(f'/pulls/{number}/comments'):
        if str(row.get('pull_request_review_id')) in pending_reviews:
            continue
        parent = row.get('in_reply_to_id')
        if parent is None:
            comments.append(review_root(row))
        else:
            if not re.fullmatch(r'[1-9][0-9]*', str(parent)):
                raise BridgeError('GitHub returned an invalid review reply parent')
            comments.append(source_comment(row, '', row.get('body'), reply_to=str(parent)))
    return order_comments(comments, records or {})


def order_comments(comments, records):
    roots = {item.key: item.time for item in comments if item.reply_to is None}
    replies = {item.key: item for item in comments if item.reply_to is not None}
    for item in replies.values():
        parent = item.reply_to
        seen = {item.key}
        while parent in replies:
            if parent in seen:
                raise BridgeError(f'Cyclic GitHub review reply at comment {item.key}')
            seen.add(parent)
            parent = replies[parent].reply_to
        item.reply_to = parent
        item.orphaned = parent not in roots and parent not in records

    # If timestamps tie or a reply predates its root, release the root first, then the replies in source order.
    def order(item):
        return (max(item.time, roots.get(item.reply_to, item.time)), item.reply_to is not None,
                item.time, int(item.key) if item.reply_to else item.key)

    return sorted(comments, key=order), len(replies)


def review_files(api, pr_path, wanted, *, destination=False):
    files = {}
    for row in api.pages(pr_path + '/files'):
        path = row.get('filename')
        if path not in wanted:
            continue
        patch = row.get('patch')
        if destination and isinstance(patch, dict):
            if patch.get('too_large') or patch.get('deleted_file') or patch.get('new_path') != path:
                continue
            patch = patch.get('diff')
        blob = row.get('blob_id' if destination else 'sha')
        if isinstance(patch, str) and isinstance(blob, str) and re.fullmatch(r'[0-9a-f]{40}', blob):
            files[path] = (blob, right_lines(patch))
    return files


def exact_line(comment, source_files, target_files):
    path, line = comment['path'], comment['line']
    source, target = source_files.get(path), target_files.get(path)
    if not source or not target or source[0] != target[0]:
        return False
    return line in source[1] and line in target[1] and source[1][line] == target[1][line]


def plan_comments(bridge, number, comments, target_path):
    paths = {item.candidate['path'] for item in comments if item.candidate}
    source = review_files(bridge.github_api, f'/pulls/{number}', paths) if paths else {}
    target = review_files(bridge.target_api, target_path, paths, destination=True) if paths and target_path else {}
    for item in comments:
        row = item.candidate
        if item.orphaned:
            yield item, 'pr', {'body': quoted_reply_body(item)}
        elif item.reply_to is not None:
            yield item, 'reply', {'body': item.body}
        elif row is None:
            yield item, 'pr', {'body': item.body}
        elif target_path is None and row['path'] in source and row['line'] in source[row['path']][1]:
            yield item, 'candidate', None  # A read-only preview cannot create the destination diff.
        elif exact_line(row, source, target):
            yield item, 'diff', {'body': item.body, 'path': row['path'], 'position': row['line'], 'position_type': 'text'}
        else:
            yield item, 'pr', {'body': outdated_body(row)}


def preview_comments(bridge, number, target_path=None, records=None):
    records = records or {}
    comments, _ = read_comments(bridge.github_api, number, records)
    fresh = [item for item in comments if item.key not in records]
    unconfirmed = sum(isinstance(record, dict) for record in records.values())
    counts = {'pr': 0, 'diff': 0, 'candidate': 0, 'reply': 0}
    for _, kind, _ in plan_comments(bridge, number, fresh, target_path):
        counts[kind] += 1
    print(f'REVIEW preview GitHub PR #{number} | pr_comment={counts["pr"]}, diff_comment={counts["diff"]}, '
          f'single-line candidates={counts["candidate"]} (pr_comment if unverified), '
          f'thread replies={counts["reply"]} (nested on code reviews, quoted on ordinary comments), '
          f'unconfirmed writes={unconfirmed}')


class CommentDelivery:
    """Keep a write intent and the verified destination note ID outside comment text."""

    def __init__(self, bridge, store, path):
        self.bridge, self.store, self.path = bridge, store, path
        self.number = path.rsplit('/', 1)[1]
        self.records = dict(store.review_comments.get(self.number, {}))
        self.discussions = {}

    def save(self, source_id, record):
        updated = dict(self.records)
        if record is None:
            updated.pop(source_id)
        else:
            updated[source_id] = record
        self.store.save(self.store.progress, review_comments={**self.store.review_comments, self.number: updated})
        self.records = updated

    def discussion_for(self, source_root):
        note_id = self.records.get(source_root)
        if not isinstance(note_id, str) or not re.fullmatch(r'[1-9][0-9]*', note_id):
            raise BridgeError(f'Review root {source_root} has no confirmed destination note')
        if note_id not in self.discussions:
            detail = self.bridge.target_api.call('GET', '/pulls/comments/' + note_id)
            discussion = detail.get('discussion_id')
            if (str(detail.get('id')) != note_id or not isinstance(discussion, str)
                    or not discussion or discussion in ('.', '..')):
                raise BridgeError(f'Could not verify the discussion of destination root {note_id}')
            thread = {'root_note_id': note_id, 'discussion_id': discussion}
            self.discussions[note_id] = (thread, self.thread_root(thread)['comment_type'])
        return self.discussions[note_id]

    def thread_root(self, intent):
        # An unfiltered PR list includes both kinds of roots, including those saved by older controllers.
        for root in self.bridge.target_api.pages(self.path + '/comments'):
            if (str(root['id']) == intent['root_note_id'] and root.get('discussion_id') == intent['discussion_id']
                    and root.get('comment_type') in ('diff_comment', 'pr_comment')):
                return root
        raise BridgeError(f'Could not confirm reply root {intent["root_note_id"]} in destination PR #{self.number}')

    def notes(self, intent):
        kind = intent.get('kind', 'diff')
        if kind == 'reply':
            root = self.thread_root(intent)
            # Child list entries carry the mapped account ID. Their enclosing root supplies the discussion ID.
            discussion = root['discussion_id']
            children = root.get('reply')
            if children is None:
                return []
            if not isinstance(children, list) or any(
                    not isinstance(row, dict) or row.get('discussion_id') not in (None, discussion)
                    for row in children):
                raise BridgeError('Destination returned an inconsistent discussion reply list')
            return [{**row, 'discussion_id': discussion} for row in children]
        if kind not in ('diff', 'pr'):
            raise BridgeError('Unknown pending comment kind')
        return list(self.bridge.target_api.pages(self.path + '/comments', comment_type=kind + '_comment'))

    def matches_note(self, intent, note, note_id):
        payload = intent['payload']
        kind = intent.get('kind', 'diff')
        if (note.get('body') != payload['body'] or not same_account(note.get('user') or {}, intent['account'])
                or not note.get('discussion_id')):
            return False
        if kind == 'diff':
            line = payload['position']
            span = note.get('diff_position') or {}
            if span.get('end_new_line') != line or span.get('start_new_line') not in (None, line):
                return False
        elif kind == 'reply':
            if note.get('discussion_id') != intent['discussion_id'] or note_id == intent['root_note_id']:
                return False
        elif note.get('comment_type') != 'pr_comment':
            return False
        if not re.fullmatch(r'[1-9][0-9]*', note_id):
            raise BridgeError('Destination returned an invalid comment ID')
        detail = self.bridge.target_api.call('GET', '/pulls/comments/' + note_id)
        if (str(detail.get('id')) != note_id or detail.get('discussion_id') != note['discussion_id']
                or detail.get('body') != payload['body']):
            return False
        if kind in ('pr', 'reply'):
            # The detail has an internal user ID. The scoped list supplies the mapped ID.
            login = (detail.get('user') or {}).get('login') or ''
            return (login.casefold() == intent['account']['username'].casefold()
                    and (kind == 'reply' or not detail.get('position')))
        pos = detail.get('position') or {}
        return (not detail.get('is_outdated') and pos.get('position_type') == 'text'
                and pos.get('new_path') == payload['path'] and pos.get('new_line') == line
                and pos.get('head_sha') == self.store.progress['pending']['head'])

    def find_new(self, intent, notes):
        matches_found = set()
        before = set(intent['before']) | {saved for saved in self.records.values() if isinstance(saved, str)}
        handle = intent.get('handle')
        for note in notes:
            note_id = str(note['id'])
            if note_id in before or (handle and handle not in (note_id, str(note.get('discussion_id')))):
                continue
            if intent.get('note_id') and note_id != intent['note_id']:
                continue
            if self.matches_note(intent, note, note_id):
                matches_found.add(note_id)
        if len(matches_found) > 1:
            label = COMMENT_LABEL[intent.get('kind', 'diff')]
            raise BridgeError(f'Ambiguous {label} receipt. Check the pending record before retrying.')
        return next(iter(matches_found), None)

    def post(self, source_id, intent):
        kind = intent['kind']
        path = self.path + '/comments'
        if kind == 'reply':
            path = f'{self.path}/discussions/{quote(intent["discussion_id"], safe="")}/comments'

        def publish(account, api):
            # On a member rejection, the robot starts with its own pre-write snapshot.
            try:
                before = [str(note['id']) for note in self.notes(intent)]
            except ApiError as error:
                # This read uses the target client. Only a member's POST can trigger a robot retry.
                raise BridgeError(f'Could not read destination comments before publishing: {error}') from error
            identity = {'id': account['id'], 'username': account['username']}
            attempt = {**intent, 'account': identity, 'before': before}
            self.save(source_id, attempt)
            try:
                response = api.call('POST', path, attempt['payload'])
            except ApiError as error:
                # Clear only a definite rejection. Unknown outcomes keep the
                # actual account and original note list for reconciliation.
                if error.code in (400, 401, 403, 404, 405, 422, 501):
                    self.save(source_id, None)
                raise
            return attempt, response

        intent, response = self.bridge.post_as(intent['account'], publish)
        handle = str(response.get('id') or '') if isinstance(response, dict) else ''
        intent = dict(intent, handle=handle)
        if isinstance(response, dict):
            note_id = response.get('note_id', response.get('noteId'))
            if note_id is not None:
                intent['note_id'] = str(note_id)
        self.save(source_id, intent)
        found = self.find_new(intent, self.notes(intent))
        if not found:
            raise BridgeError(f'Could not confirm the {COMMENT_LABEL[kind]} before merge. Check the pending receipt.')
        self.save(source_id, found)

    def post_reply(self, item, intent):
        thread, root_type = self.discussion_for(item.reply_to)
        if root_type == 'pr_comment':
            # GitCode stores nested replies to ordinary notes but omits them from the web discussion.
            intent = {**intent, 'kind': 'pr', 'payload': {'body': quoted_reply_body(item, thread['root_note_id'])}}
        else:
            intent = {**intent, **thread}
        self.post(item.key, intent)

    def recover(self):
        # Recover an interrupted write even if GitHub has since edited or removed the source comment.
        for source_id, record in list(self.records.items()):
            if isinstance(record, dict):
                found = self.find_new(record, self.notes(record))
                if not found:
                    raise BridgeError(f'Comment {source_id} has an unconfirmed write. Check the '
                                      'receipt and the destination PR before authorizing another POST.')
                self.save(source_id, found)

    def run(self, original):
        number = str(original['number'])
        self.recover()
        comments, replies = read_comments(self.bridge.github_api, number, self.records)
        fresh = [item for item in comments if item.key not in self.records]
        if fresh:
            self.bridge.verify_pending_refs(self.store.progress['pending'])
            for item, kind, payload in plan_comments(self.bridge, number, fresh, self.path):
                if item.key not in self.records:
                    account = self.bridge.mapped_account(item.user)
                    intent = {'kind': kind, 'account': account, 'payload': payload}
                    if kind == 'reply':
                        self.post_reply(item, intent)
                    else:
                        self.post(item.key, intent)
        print(f'REVIEW GitHub PR #{number} | confirmed {len(self.records)}, source replies {replies}')
