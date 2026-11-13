import json
import unittest
from pathlib import Path


class AnalysisInputsTests(unittest.TestCase):
    def load_data(self):
        return json.loads((Path(__file__).resolve().parents[1] / "fixtures/analysis/input.json").read_text())

    def test_reference_switch(self):
        data = self.load_data()
        self.assertTrue(len({row['reference_id'] for row in data['pairings']}) == 2)

    def test_future_quality(self):
        data = self.load_data()
        self.assertTrue(data['quality_revisions'][0]['available_at'] > data['as_of'])

    def test_reference_uncertainty(self):
        data = self.load_data()
        self.assertTrue(max(row['reference_error'] for row in data['observations']) > 1)

    def test_source_versions_and_cutoff(self):
        data = self.load_data()
        self.assertEqual(data["schema_version"], 1)
        self.assertTrue(data["source_versions"])
        self.assertIn("+00:00", data["as_of"])
