"""Build the offline, self-contained feature explorer. No third-party packages."""
from pathlib import Path
import json
import base64
import re

from status_sync import source_paths

HERE = Path(__file__).resolve().parent

def require(condition, message):
    # Build validation must remain active under python -O, including in CI.
    if not condition:
        raise ValueError(message)


def validate(model):
    categories = model['categories']
    features = [f for c in categories for f in c['features']]
    ids = [f['id'] for f in features]
    category_ids = [c['id'] for c in categories]
    flows = model['flows']
    require(len(ids) == len(set(ids)), 'Duplicate feature IDs')
    require(len(set(category_ids)) == len(categories), 'Duplicate categories')
    require(not set(ids) & set(category_ids), 'Category and feature IDs collide')
    require(len({f['id'] for f in flows}) == len(flows), 'Duplicate decision path IDs')
    require(set(model.get('tour', [])) <= set(category_ids), 'Unknown tour system')
    for id in ids + category_ids + [f['id'] for f in flows]:
        require(isinstance(id, str) and re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,99}', id), 'Unsafe model ID')
    if 'sourceRevision' in model:
        require(isinstance(model['sourceRevision'], str) and
                re.fullmatch(r'[a-f0-9]{40}|[a-f0-9]{64}', model['sourceRevision']), 'Invalid source snapshot')
    for feature in features:
        require(feature['status'] in {'implemented', 'partial', 'planned', 'historical'}, feature['id'])
        require(feature['title'] and feature['summary'] and feature['sources'], feature['id'])
        require(set(feature.get('related', [])) <= set(ids), feature['id'])
        source_paths(feature)
    all_nodes = []
    for flow in flows:
        nodes = flow['nodes']
        for node in nodes:
            require(isinstance(node['id'], str) and re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,99}', node['id']), 'Unsafe decision ID')
            require(node.get('status', 'implemented') in {'implemented', 'partial', 'planned', 'historical'}, 'Invalid decision status')
        node_ids = {node['id'] for node in nodes}
        all_nodes.extend(node_ids)
        require(len(node_ids) == len(nodes), f'Duplicate node in {flow["id"]}')
        require(nodes and nodes[0].get('choices'), f'Missing entry in {flow["id"]}')
        reached = set()
        def walk(node_id):
            if node_id in reached:
                return
            reached.add(node_id)
            node = next(node for node in nodes if node['id'] == node_id)
            require(node.get('choices') or node.get('end'), f'Missing outcome: {node_id}')
            require(set(node.get('features', [])) <= set(ids), node_id)
            for choice in node.get('choices', []):
                require(choice['next'] in node_ids, f'Broken branch: {choice}')
                walk(choice['next'])
        walk(nodes[0]['id'])
        require(reached == node_ids, f'Unreachable nodes in {flow["id"]}: {node_ids - reached}')
    require(len(all_nodes) == len(set(all_nodes)), 'Decision IDs must be globally unique')
    return len(features), len(categories), len(flows), len(all_nodes)

def build():
    model = json.loads((HERE / 'model.json').read_text())
    counts = validate(model)
    coverage = json.loads((HERE / 'source-coverage.json').read_text())
    feature_ids = {f['id'] for c in model['categories'] for f in c['features']}
    for audit, mapping in coverage['inputs'].items():
        require(len(mapping) == coverage['input_counts'][audit], f'Incomplete {audit} audit coverage')
        require(set(mapping.values()) <= feature_ids, f'Missing mapped feature in {audit}')
    payload = json.dumps(model, ensure_ascii=False, separators=(',', ':')).replace('</', '<\\/')
    icon = 'data:image/png;base64,' + base64.b64encode((HERE.parents[1] / 'web/texty/public/brand/textmonkey-favicon-192.png').read_bytes()).decode()
    output = (HERE / 'template.html').read_text().replace('/*__ICON__*/', icon).replace('/*__STYLE__*/', (HERE / 'styles.css').read_text()).replace('/*__MODEL__*/', payload).replace('/*__SCRIPT__*/', (HERE / 'explorer.js').read_text())
    require('/*__' not in output, 'Unresolved template token')
    (HERE / 'index.html').write_text(output)
    public = HERE / 'dist'
    public.mkdir(exist_ok=True)
    unexpected = {p.name for p in public.iterdir()} - {'index.html', '_headers'}
    require(not unexpected, f'Unexpected deployment assets: {unexpected}')
    (public / 'index.html').write_text(output)
    (public / '_headers').write_text("/*\n  Cache-Control: no-cache\n  X-Content-Type-Options: nosniff\n  Referrer-Policy: no-referrer\n  Content-Security-Policy: default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'\n")
    print(f'Built offline explorer: {counts[0]} features, {counts[1]} systems, {counts[2]} decision paths, {counts[3]} decision nodes; {len(output.encode()):,} bytes.')

if __name__ == '__main__':
    build()
