"""The appendix must preserve every record and never equate aggregate tests with live proof."""
import collections
import json
from pathlib import Path
import subprocess
import unittest

from status_sync import source_paths, public_text, public_links

HERE = Path(__file__).parent


class InventoryTests(unittest.TestCase):
    def test_inventory_and_individual_audit_reasons_are_complete(self):
        model = json.loads((HERE / 'model.json').read_text())
        audit = json.loads((HERE / 'audit.json').read_text())
        rec = json.loads((HERE / 'reconciliation.json').read_text())
        features = {f['id']: f for c in model['categories'] for f in c['features']}
        rows = {f['id']: f for f in audit['features']}
        classified = {f['id']: f for f in rec['features']}
        self.assertEqual(len(rows), 183)
        self.assertEqual(set(features), set(rows))
        self.assertEqual(set(rows), set(classified))
        self.assertEqual(model['sourceRevision'], rec['sourceRevision'])
        self.assertEqual(audit['sourceRevision'], rec['sourceRevision'])
        self.assertEqual(dict(collections.Counter(f['classification'] for f in rec['features'])), rec['counts'])
        for id, row in rows.items():
            self.assertEqual(row['sourcePaths'], source_paths(features[id]))
            self.assertEqual(row['classification'], classified[id]['classification'])
            self.assertTrue(row['behaviorAudit']['reason'])
            self.assertNotEqual(row['behaviorAudit']['status'], 'verified_live')
            self.assertTrue(row['liveEvidence'])
            if 'ownerAudit' in row:
                proof = row['ownerAudit']
                public_links([{'title': 'Scoped owner change', 'url': proof['pullRequest']}])
                public_text(proof['remaining'], 'Public owner limit', 1200)
                self.assertNotEqual(proof['sourceRevision'], model['revision'])

    def test_current_source_paths_resolve_and_original_mapping_is_preserved(self):
        model = json.loads((HERE / 'model.json').read_text())
        root = HERE.resolve().parents[1]
        tree = set(subprocess.check_output(['git', '-C', str(root), 'ls-tree', '-r', '--name-only', model['sourceRevision']], text=True).splitlines())
        for category in model['categories']:
            for feature in category['features']:
                for path in source_paths(feature):
                    self.assertTrue(path in tree or any(p.startswith(path + '/') for p in tree), (feature['id'], path))
        original = subprocess.check_output(['git', '-C', str(root), 'show', model['sourceRevision'] + ':docs/feature-universe/source-coverage.json'])
        self.assertEqual((HERE / 'source-coverage.json').read_bytes(), original)
        coverage = json.loads(original)
        self.assertEqual(sum(len(v) for v in coverage['inputs'].values()), 347)
