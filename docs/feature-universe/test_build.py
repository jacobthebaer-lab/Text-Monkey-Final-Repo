"""Graph and evidence-link validation, without application side effects."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import unittest

from build import validate


class BuildTests(unittest.TestCase):
    def setUp(self):
        self.model = json.loads((Path(__file__).parent / 'model.json').read_text())

    def test_existing_inventory_and_decision_counts(self):
        self.assertEqual(validate(self.model), (183, 15, 10, 94))

    def test_duplicate_flow_and_unknown_tour_system_are_rejected(self):
        self.model['flows'].append(copy.deepcopy(self.model['flows'][0]))
        with self.assertRaisesRegex(ValueError, 'Duplicate decision path'):
            validate(self.model)
        self.model['flows'].pop()
        self.model['tour'].append('missing-system')
        with self.assertRaisesRegex(ValueError, 'Unknown tour'):
            validate(self.model)

    def test_unsafe_ids_and_source_paths_are_rejected(self):
        self.model['categories'][0]['features'][0]['id'] = 'unsafe" onclick="value'
        with self.assertRaisesRegex(ValueError, 'Unsafe model ID'):
            validate(self.model)
        self.setUp()
        self.model['categories'][0]['features'][0]['sources'] = ['../private/path']
        with self.assertRaises(ValueError):
            validate(self.model)

    def test_unsafe_decision_ids_and_statuses_are_rejected(self):
        self.model['flows'][0]['nodes'][0]['id'] = 'unsafe" value="example'
        with self.assertRaisesRegex(ValueError, 'Unsafe decision ID'):
            validate(self.model)
        self.setUp()
        self.model['flows'][0]['nodes'][0]['status'] = 'invalid" value="example'
        with self.assertRaisesRegex(ValueError, 'Invalid decision status'):
            validate(self.model)

    def test_validation_survives_optimized_python(self):
        result = subprocess.run([sys.executable, '-O', '-c',
            'from build import validate; validate({"categories":[],"tour":["missing"],"flows":[]})'],
            cwd=Path(__file__).parent, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Unknown tour', result.stderr)
