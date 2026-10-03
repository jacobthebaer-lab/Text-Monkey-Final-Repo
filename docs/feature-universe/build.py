"""Build the offline, self-contained feature explorer. No third-party packages."""
from pathlib import Path
import json
import base64

HERE = Path(__file__).resolve().parent

def validate(model):
    categories = model['categories']
    features = [f for c in categories for f in c['features']]
    ids = [f['id'] for f in features]
    assert len(ids) == len(set(ids)), 'Duplicate feature IDs'
    assert len({c['id'] for c in categories}) == len(categories), 'Duplicate categories'
    assert not set(ids) & {c['id'] for c in categories}, 'Category and feature IDs collide'
    for feature in features:
        assert feature['status'] in {'implemented', 'partial', 'planned', 'historical'}, feature['id']
        assert feature['title'] and feature['summary'] and feature['sources'], feature['id']
        assert set(feature.get('related', [])) <= set(ids), feature['id']
    all_nodes = []
    for flow in model['flows']:
        nodes = flow['nodes']
        node_ids = {node['id'] for node in nodes}
        all_nodes.extend(node_ids)
        assert len(node_ids) == len(nodes), f'Duplicate node in {flow["id"]}'
        assert nodes and nodes[0].get('choices'), f'Missing entry in {flow["id"]}'
        reached = set()
        def walk(node_id):
            if node_id in reached:
                return
            reached.add(node_id)
            node = next(node for node in nodes if node['id'] == node_id)
            assert node.get('choices') or node.get('end'), f'Missing outcome: {node_id}'
            assert set(node.get('features', [])) <= set(ids), node_id
            for choice in node.get('choices', []):
                assert choice['next'] in node_ids, f'Broken branch: {choice}'
                walk(choice['next'])
        walk(nodes[0]['id'])
        assert reached == node_ids, f'Unreachable nodes in {flow["id"]}: {node_ids - reached}'
    assert len(all_nodes) == len(set(all_nodes)), 'Decision IDs must be globally unique'
    return len(features), len(categories), len(model['flows']), len(all_nodes)

def build():
    model = json.loads((HERE / 'model.json').read_text())
    counts = validate(model)
    coverage = json.loads((HERE / 'source-coverage.json').read_text())
    feature_ids = {f['id'] for c in model['categories'] for f in c['features']}
    for audit, mapping in coverage['inputs'].items():
        assert len(mapping) == coverage['input_counts'][audit], f'Incomplete {audit} audit coverage'
        assert set(mapping.values()) <= feature_ids, f'Missing mapped feature in {audit}'
    payload = json.dumps(model, ensure_ascii=False, separators=(',', ':')).replace('</', '<\\/')
    icon = 'data:image/png;base64,' + base64.b64encode((HERE.parents[1] / 'web/texty/public/brand/textmonkey-favicon-192.png').read_bytes()).decode()
    output = (HERE / 'template.html').read_text().replace('/*__ICON__*/', icon).replace('/*__STYLE__*/', (HERE / 'styles.css').read_text()).replace('/*__MODEL__*/', payload).replace('/*__SCRIPT__*/', (HERE / 'explorer.js').read_text())
    assert '/*__' not in output, 'Unresolved template token'
    (HERE / 'index.html').write_text(output)
    print(f'Built offline explorer: {counts[0]} features, {counts[1]} systems, {counts[2]} decision paths, {counts[3]} decision nodes; {len(output.encode()):,} bytes.')

if __name__ == '__main__':
    build()
