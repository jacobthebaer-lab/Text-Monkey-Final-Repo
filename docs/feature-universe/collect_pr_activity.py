"""Collect only PR numbers, URLs and changed paths for local status matching.

The collector runs inside GitHub with its existing read-only repository token.
Titles, bodies, authors, conversations and patches never enter the public feed.
"""
import argparse
import json
from pathlib import Path
import subprocess
from urllib.parse import quote

REPOSITORY = 'jacobthebaer-lab/text-monkey'
BRANCH = 'codex/complete-text-monkey'


def gh_pages(endpoint):
    result = subprocess.run(['gh', 'api', '--paginate', '--slurp', endpoint], capture_output=True, text=True, timeout=120)
    if result.returncode:
        raise RuntimeError('Repository activity could not be checked; existing published status is retained.')
    pages = json.loads(result.stdout)
    if not isinstance(pages, list) or any(not isinstance(page, list) for page in pages):
        raise ValueError('Unexpected repository response')
    return [row for page in pages for row in page]


def pr_paths(number):
    # GraphQL requests paths only; REST files responses also transfer patches.
    query = "query($number:Int!, $endCursor:String){repository(owner:\"jacobthebaer-lab\",name:\"text-monkey\"){pullRequest(number:$number){files(first:100,after:$endCursor){nodes{path}pageInfo{hasNextPage endCursor}}}}}"
    response = subprocess.run(['gh', 'api', 'graphql', '--paginate', '--slurp', '-F', f'number={number}', '-f', f'query={query}'], capture_output=True, text=True, timeout=120)
    if response.returncode:
        raise RuntimeError('Could not check pull request paths')
    result = []
    for page in json.loads(response.stdout):
        nodes = page['data']['repository']['pullRequest']['files']['nodes']
        if not isinstance(nodes, list) or any(not isinstance(node.get('path'), str) for node in nodes):
            raise ValueError('Unexpected pull request file paths')
        result.extend(node['path'] for node in nodes)
    return result


def collect():
    pulls = gh_pages(f'repos/{REPOSITORY}/pulls?state=open&base={quote(BRANCH, safe='')}&per_page=100')
    result = []
    for pull in pulls:
        number = pull.get('number')
        if type(number) is not int or number < 1:
            raise ValueError('Invalid pull request number')
        files = pr_paths(number)
        if len(files) >= 3000:
            raise ValueError('Pull request exceeds complete changed-file coverage; retain previous feed')
        paths = sorted(set(files))
        result.append({'number': number, 'url': f'https://github.com/{REPOSITORY}/pull/{number}', 'state': 'OPEN', 'files': paths})
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        result = collect()
        args.output.write_text(json.dumps(result))
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, subprocess.TimeoutExpired) as error:
        raise SystemExit('Could not obtain complete repository activity. No status was published.') from error
    print(f'Collected changed paths for {len(result)} open pull requests; no private message content included.')


if __name__ == '__main__':
    main()
