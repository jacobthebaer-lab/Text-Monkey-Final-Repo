#!/usr/bin/env python3
"""Build a sanitized feature-status feed from an already-fetched Git ref.

No fetch, network request, credential export, deployment or model call occurs.
The canonical origin must be jacobthebaer-lab/text-monkey. A source change means
review is needed, never that a feature became implemented or verified.

Reviews are a JSON object: {"schemaVersion": 1, "features": {"feature-id": {
  "status": "partial", "progress": "verified", "summary": "Public review note.",
  "sourceRevision": "<reviewed commit SHA>", "checkedAt": "<ISO UTC time>",
  "links": [{"title": "Reviewed change", "url": "https://github.com/..."}]
}}}. An empty object is also accepted. Reviews are trusted, deliberately public
editorial input, not a place for raw messages, logs or pasted model output. Basic
validation rejects common private data and secrets; it is not a general-purpose
PII classifier. Invalid input fails before the previous output is replaced.

A review is reusable only if it is at the current commit, or its commit is an
ancestor and the feature's tracked sources have not changed since that review.
Stale reviews are ignored and visibly require review. Feed checkedAt/generatedAt
are the current source check; a reviewed feature retains the review checkedAt
and sourceRevision so its audit age is not disguised by a fresh scheduled tick.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit, unquote

CANONICAL = "jacobthebaer-lab/text-monkey"
GITHUB = f"https://github.com/{CANONICAL}"
DEFAULT_REF = "origin/codex/complete-text-monkey"
STATUSES = {"implemented", "partial", "planned", "historical"}
PROGRESS = {"unchanged", "changed", "in_progress", "blocked", "verified"}
SHA = re.compile(r"^[0-9a-fA-F]{7,64}$")
FEATURE_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,99}$")
PRIVATE = re.compile(
    r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|"
    r"(?:\+\d[\d ()-]{7,}\d)|(?:\b\d{3}[-. ]\d{3}[-. ]\d{4}\b)|"
    r"\b(?:\d[ ()-]*){10,}\b|"
    r"(?:/Users/|/home/|[A-Za-z]:\\\\)|"
    r"\b(?:gh[pousr]_[A-Za-z0-9]+|github_pat_[A-Za-z0-9_]+|sk-[A-Za-z0-9_-]{12,})|"
    r"\b(?:bearer\s+|api[_ -]?key\s*[:=]|password\s*[:=]|secret\s*[:=])",
    re.IGNORECASE,
)


class FeedError(ValueError):
    """A safe, operator-facing validation failure with no raw Git/file content."""


def public_text(value, label, limit=600):
    if (not isinstance(value, str) or not value.strip() or len(value) > limit
            or any(ord(c) < 32 or ord(c) == 127 for c in value)
            or '<' in value or '>' in value or PRIVATE.search(value)
            or 'http://' in value.lower() or 'https://' in value.lower()):
        raise FeedError(f"{label} must be a short public note without private data, markup or raw URLs")
    return value.strip()


def iso_time(value, label, now):
    if not isinstance(value, str):
        raise FeedError(f"{label} must be an ISO timestamp with a timezone")
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        raise FeedError(f"{label} must be an ISO timestamp with a timezone") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise FeedError(f"{label} must include its timezone")
    if parsed > now + timedelta(minutes=5):
        raise FeedError(f"{label} cannot be in the future")
    return parsed.astimezone(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def read_json(path, label):
    try:
        if Path(path).stat().st_size > 5_000_000:
            raise FeedError(f"{label} exceeds the input size limit")
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise FeedError(f"Cannot read valid {label} JSON") from None


class Git:
    def __init__(self, repo):
        self.repo = Path(repo).resolve()
        origin = self.run('remote', 'get-url', 'origin').strip()
        if origin not in {
            f'https://github.com/{CANONICAL}.git', f'https://github.com/{CANONICAL}',
            f'git@github.com:{CANONICAL}.git', f'git@github.com:{CANONICAL}',
            f'ssh://git@github.com/{CANONICAL}.git', f'ssh://git@github.com/{CANONICAL}',
        }:
            raise FeedError('Repository origin is not the canonical Text Monkey repository')
        self.diff_cache = {}
        self.ancestor_cache = {}

    def run(self, *args, accept=(0,), binary=False):
        env = {**os.environ, 'GIT_PAGER': 'cat', 'GIT_OPTIONAL_LOCKS': '0'}
        try:
            result = subprocess.run(
                ['git', '-C', str(self.repo), '--no-pager', *args],
                capture_output=True, timeout=30, env=env, check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise FeedError('Local Git command could not complete') from None
        if result.returncode not in accept:
            raise FeedError('Local Git check failed; verify repository, fetched ref and commit availability')
        return result.stdout if binary else result.stdout.decode('utf-8', errors='strict')

    def commit(self, ref, *, sha_only=False):
        if (not isinstance(ref, str) or not ref or ref.startswith('-')
                or len(ref) > 200 or re.search(r'[\s\x00-\x1f]', ref)
                or (sha_only and not SHA.fullmatch(ref))):
            raise FeedError('Invalid commit or reference')
        value = self.run('rev-parse', '--verify', '--end-of-options', f'{ref}^{{commit}}').strip()
        if not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', value):
            raise FeedError('Git reference did not resolve to a commit')
        return value

    def ancestor(self, older, newer):
        key = (older, newer)
        if key not in self.ancestor_cache:
            bases = self.run('merge-base', older, newer, accept=(0, 1,)).splitlines()
            self.ancestor_cache[key] = older in bases
        return self.ancestor_cache[key]

    def changed_paths(self, older, newer):
        key = (older, newer)
        if key not in self.diff_cache:
            raw = self.run('diff', '--name-only', '--no-renames', '-z', older, newer, '--', binary=True)
            try:
                self.diff_cache[key] = {p.decode('utf-8') for p in raw.split(b'\0') if p}
            except UnicodeDecodeError:
                raise FeedError('Source paths must be valid UTF-8') from None
        return self.diff_cache[key]


def source_paths(feature):
    paths = []
    sources = feature.get('sources', [])
    if not isinstance(sources, list):
        raise FeedError('Sources must be a list')
    for source in sources:
        if isinstance(source, dict):
            # Chat metadata is evidence, not a local file or live chat query.
            if not isinstance(source.get('chatId'), str) or not isinstance(source.get('title'), str):
                raise FeedError('Invalid chat source metadata')
            continue
        if not isinstance(source, str):
            raise FeedError('Invalid source path')
        path = re.sub(r':\d+$', '', source).rstrip('/')
        parts = path.split('/')
        if (not path or path.startswith('/') or '\\' in path or ':' in path
                or any(p in ('', '.', '..') for p in parts)
                or any(ord(c) < 32 or ord(c) == 127 for c in path)):
            raise FeedError('Source paths must be repository-relative paths')
        paths.append(str(PurePosixPath(path)))
    return paths


def affected(paths, changed):
    return any(change == path or change.startswith(path + '/') for path in paths for change in changed)


def public_links(value):
    if not isinstance(value, list) or len(value) > 5:
        raise FeedError('Review links must be a list of at most five links')
    clean = []
    for link in value:
        if not isinstance(link, dict) or set(link) != {'title', 'url'}:
            raise FeedError('Invalid review link')
        title = public_text(link['title'], 'Link title', 100)
        url = link['url']
        if not isinstance(url, str) or len(url) > 1000:
            raise FeedError('Invalid review link URL')
        try:
            p = urlsplit(url)
        except ValueError:
            raise FeedError('Invalid review link URL') from None
        if (p.scheme != 'https' or p.netloc != 'github.com' or p.query or p.fragment
                or not re.fullmatch(r'/' + re.escape(CANONICAL) + r'/(?:commit/[0-9a-f]{40,64}|pull/[1-9][0-9]*|blob/[0-9a-f]{40,64}/[A-Za-z0-9_./%+-]+)', p.path)
                or any(part in ('.', '..') for part in unquote(p.path).split('/'))
                or PRIVATE.search(unquote(p.path))):
            raise FeedError('Review links must be canonical repository commit, pull request or immutable source URLs without credentials')
        clean.append({'title': title, 'url': url})
    return clean


def review_rows(data, ids, git, now):
    if not isinstance(data, dict):
        raise FeedError('Reviews must be a JSON object')
    if not data:
        return {}
    if set(data) - {'schemaVersion', 'features'} or (type(data.get('schemaVersion', 1)) is not int or data.get('schemaVersion', 1) != 1) or not isinstance(data.get('features'), dict):
        raise FeedError('Reviews must use schemaVersion 1 and a features object')
    result = {}
    required = {'status', 'progress', 'summary', 'sourceRevision', 'checkedAt'}
    for id, row in data['features'].items():
        if id not in ids:
            raise FeedError('Review refers to an unknown feature ID')
        if not isinstance(row, dict) or not required <= set(row) or set(row) - required - {'links'}:
            raise FeedError('Review fields are missing or unsupported')
        if (not isinstance(row['status'], str) or row['status'] not in STATUSES
                or not isinstance(row['progress'], str) or row['progress'] not in PROGRESS):
            raise FeedError('Review status or progress is invalid')
        result[id] = {
            'status': row['status'], 'progress': row['progress'],
            'summary': public_text(row['summary'], 'Review summary'),
            'sourceRevision': git.commit(row['sourceRevision'], sha_only=True),
            'checkedAt': iso_time(row['checkedAt'], 'Review checkedAt', now),
            'links': public_links(row.get('links', [])),
        }
    return result


def pull_request_rows(data):
    """Consume only caller-sanitized metadata; never query or echo PR contents."""
    if not isinstance(data, list) or len(data) > 100:
        raise FeedError('Pull requests must be a list of at most 100 records')
    output, seen = [], set()
    for row in data:
        if not isinstance(row, dict) or set(row) != {'number', 'url', 'state', 'files'}:
            raise FeedError('Pull requests require only number, url, state and files')
        number = row['number']
        if type(number) is not int or not 1 <= number <= 99999999 or number in seen:
            raise FeedError('Pull request numbers must be unique positive integers')
        seen.add(number)
        if row['url'] != f'{GITHUB}/pull/{number}':
            raise FeedError('Pull request URL must match its canonical repository number')
        if not isinstance(row['state'], str) or row['state'] not in {'OPEN', 'CLOSED', 'MERGED'}:
            raise FeedError('Invalid pull request state')
        if not isinstance(row['files'], list) or len(row['files']) > 10000 or any(not isinstance(p, str) for p in row['files']):
            raise FeedError('Pull request files must be a bounded list of source paths')
        paths = source_paths({'sources': row['files']})
        if row['state'] == 'OPEN':
            output.append({'number': number, 'url': row['url'], 'files': paths})
    return output


def build_feed(repo, ref, model, reviews=None, pull_requests=None, *, now=None):
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise FeedError('Current time must have a timezone')
    current_time = now.astimezone(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')
    git = Git(repo)
    if not isinstance(model, dict) or not isinstance(model.get('categories'), list):
        raise FeedError('Model must contain feature categories')
    baseline = git.commit(model.get('revision'), sha_only=True)
    current = git.commit(ref)
    if not git.ancestor(baseline, current):
        raise FeedError('Model baseline is not an ancestor of the selected fetched ref')
    rows, paths = {}, {}
    for category in model['categories']:
        if not isinstance(category, dict) or not isinstance(category.get('features'), list):
            raise FeedError('Invalid model category')
        for feature in category['features']:
            if not isinstance(feature, dict):
                raise FeedError('Invalid model feature')
            id = feature.get('id')
            if not isinstance(id, str) or not FEATURE_ID.fullmatch(id) or id in rows:
                raise FeedError('Feature IDs must be unique safe identifiers')
            if not isinstance(feature.get('status'), str) or feature.get('status') not in STATUSES:
                raise FeedError('Invalid model feature status')
            rows[id] = feature
            paths[id] = source_paths(feature)
    if not rows:
        raise FeedError('Model must contain at least one feature')
    reviewed = review_rows({} if reviews is None else reviews, rows, git, now)
    pulls = pull_request_rows([] if pull_requests is None else pull_requests)
    changed = git.changed_paths(baseline, current)
    output = {}
    for id, feature in rows.items():
        change = affected(paths[id], changed)
        summary = ('Tracked source changed since the audited baseline. Review is needed; the implementation status has not been upgraded.' if change else
                   'No tracked source changes since the audited baseline. This is a source check, not live operation or delivery verification.' if paths[id] else
                   'This feature has chat evidence only. Its audited baseline is retained; Git cannot verify later conversation progress.')
        row = {
            'status': feature['status'], 'progress': 'changed' if change else 'unchanged',
            'summary': summary, 'checkedAt': current_time, 'sourceRevision': baseline,
            'links': [{'title': 'Audited source baseline', 'url': f'{GITHUB}/commit/{baseline}'}],
        }
        review = reviewed.get(id)
        if review:
            revision = review['sourceRevision']
            valid = revision == current or (bool(paths[id]) and git.ancestor(revision, current)
                                           and not affected(paths[id], git.changed_paths(revision, current)))
            if valid:
                row = dict(review)
                if not row['links']:
                    row['links'] = [{'title': 'Reviewed source revision', 'url': f'{GITHUB}/commit/{revision}'}]
            else:
                row['progress'] = 'changed'
                row['summary'] = 'The previous review is stale for this source revision. Fresh review is required; the audited baseline status is retained.'
        touching = [pr for pr in pulls if affected(paths[id], pr['files'])]
        if touching:
            # A pending PR never proves completion. Current unreviewed mainline
            # changes remain review-needed; a valid explicit blocked review wins.
            if row['progress'] not in {'changed', 'blocked'}:
                row['progress'] = 'in_progress'
                row['checkedAt'] = current_time
                row['summary'] = 'An open pull request changes this feature’s source. Review and merge pending.'
            for pr in touching[:5]:
                link = {'title': f"Pull request #{pr['number']}", 'url': pr['url']}
                if link not in row['links']:
                    row['links'].append(link)
            row['links'] = row['links'][:5]
        output[id] = row
    branch = ref.removeprefix('refs/remotes/').removeprefix('origin/').removeprefix('refs/heads/')
    if not re.fullmatch(r'[A-Za-z0-9_./-]{1,200}', branch):
        raise FeedError('Selected reference cannot be published as a branch label')
    return {
        'schemaVersion': 1, 'checkedAt': current_time, 'generatedAt': current_time,
        'repository': {'revision': current, 'branch': branch}, 'features': output,
        'sync': {'mode': 'repository-events', 'intervalMinutes': 60},
    }


def write_feed(path, feed):
    path = Path(path)
    if path.is_symlink():
        raise FeedError('Output must not be a symlink')
    temp = None
    try:
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=path.parent, prefix='.status-', suffix='.tmp', delete=False) as file:
            temp = Path(file.name)
            json.dump(feed, file, ensure_ascii=False, indent=2)
            file.write('\n')
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp, path)
    except OSError:
        raise FeedError('Could not atomically save status feed') from None
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--repo', required=True, help='Canonical local repository with the selected ref already fetched')
    parser.add_argument('--ref', default=DEFAULT_REF, help='Already-fetched Git ref; no fetch is performed')
    parser.add_argument('--model', required=True)
    parser.add_argument('--reviews', help='Optional sanitized, source-bound editorial review JSON')
    parser.add_argument('--pull-requests', help='Optional sanitized pull-request metadata JSON; no titles, bodies or GitHub queries are read')
    parser.add_argument('--output', required=True)
    args = parser.parse_args(argv)
    try:
        if Path(args.output).resolve() in {Path(p).resolve() for p in [args.model, args.reviews, args.pull_requests] if p}:
            raise FeedError('Output must not overwrite a model or review input')
        feed = build_feed(args.repo, args.ref, read_json(args.model, 'model'),
                          read_json(args.reviews, 'reviews') if args.reviews else {},
                          read_json(args.pull_requests, 'pull requests') if args.pull_requests else [])
        write_feed(args.output, feed)
    except (FeedError, UnicodeError) as error:
        print(f'Status sync failed: {error}', file=sys.stderr)
        return 2
    changed = sum(f['progress'] == 'changed' for f in feed['features'].values())
    print(f'Saved sanitized feed: {len(feed["features"])} features, {changed} need review.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
