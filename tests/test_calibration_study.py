from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from station_mesh_guard.calibration_study import main as cli_main
from station_mesh_guard.calibration.metrics import sign_test
from station_mesh_guard.calibration.study import CalibrationStudy

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "fixtures/analysis/input.json"


def load_fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def run(payload: dict, database: str | None = None) -> dict:
    return CalibrationStudy(payload, database).execute()


def entry_for(study: dict, device_id: str, method: str) -> dict:
    return next(item for item in study["validation"]
                if item["device_id"] == device_id and item["method"] == method)


class FixtureStudyTests(unittest.TestCase):
    def test_required_top_level_blocks(self):
        result = run(load_fixture())
        for key in ("study_id", "pairings", "candidates", "validation", "uncertainty", "applicability"):
            self.assertIn(key, result)
        self.assertTrue(result["study_id"])

    def test_fixed_offset_batch_is_supported(self):
        result = run(load_fixture())
        for device in ("device-0", "device-1"):
            entry = entry_for(result, device, "mean_offset")
            self.assertEqual(entry["verdict"], "supported")
            self.assertGreaterEqual(entry["improvement"]["interval"][0], 0.5)
            self.assertEqual(entry["corrected"]["bias"], 0.0)
            self.assertEqual(entry["corrected"]["max_abs"], 0.0)
            self.assertEqual(result["candidates"][0]["training_events"], 7)
        offset_batch = next(item for item in result["applicability"]["batches"]
                            if item["batch_id"] == "offset")
        self.assertEqual(offset_batch["decision"], "continue_calibration")
        self.assertEqual(offset_batch["supported_devices"], ["device-0", "device-1"])

    def test_unchanged_batch_not_credited(self):
        result = run(load_fixture())
        for device in ("device-2", "device-3"):
            for method in ("mean_offset", "median_offset", "gain_ratio"):
                entry = entry_for(result, device, method)
                self.assertNotEqual(entry["verdict"], "supported")
                self.assertEqual(entry["improvement"]["estimate"], 0.0)
        normal_batch = next(item for item in result["applicability"]["batches"]
                            if item["batch_id"] == "normal")
        self.assertEqual(normal_batch["decision"], "do_not_apply_calibration")
        self.assertNotIn("device-2", result["applicability"]["supported_devices"])

    def test_naive_before_after_confounded_by_intensity(self):
        result = run(load_fixture())
        # Observed means look better after maintenance on every device, but the paired
        # residual does not change: the fixture's later events are lighter rain.
        for entry in result["validation"]:
            if entry["method"] == "none":
                naive = entry["naive_before_after"]
                self.assertEqual(naive["attribution"], "event_intensity")
                self.assertLess(naive["observed_mean_post"], naive["observed_mean_pre"])
                self.assertEqual(naive["residual_delta"], 0.0)
                self.assertEqual(entry["event_intensity"]["label"], "intensity_confound")

    def test_reference_outage_has_no_evidence(self):
        result = run(load_fixture())
        gaps = result["applicability"]["reference_outage_gaps"]
        self.assertEqual(len(gaps), 1)
        gap = gaps[0]
        self.assertEqual(gap["evidence"], "none")
        self.assertEqual(gap["usable_observations"], 0)
        self.assertEqual(gap["excluded_observations"],
                         ["obs-0-50", "obs-2-50", "obs-3-50"])

    def test_future_quality_revision_not_used(self):
        result = run(load_fixture())
        by_id = {item["observation_id"]: item for item in result["exclusions"]["rows"]}
        self.assertIn("obs-1-45", by_id)
        self.assertIn("quality_pending", by_id["obs-1-45"]["reasons"])
        for device in ("device-0", "device-1", "device-2", "device-3"):
            self.assertNotIn("rain-9", {item["event_id"] for item in
                                        [event for entry in result["validation"]
                                         if entry["device_id"] == device
                                         for event in entry.get("per_event", [])]})

    def test_gain_candidate_rejected_with_reason(self):
        result = run(load_fixture())
        entry = entry_for(result, "device-0", "gain_ratio")
        self.assertEqual(entry["verdict"], "rejected")
        self.assertTrue(any("training fit" in reason for reason in entry["reasons"]))

    def test_evidence_links_calibration_and_maintenance_versions(self):
        result = run(load_fixture())
        evidence = result["evidence"]
        self.assertEqual(evidence["calibration"]["source_versions"]["reference"], "r1")
        self.assertEqual(evidence["maintenance"]["source_versions"]["operations"], "r1")
        self.assertTrue(evidence["calibration"]["version"].startswith("r1:"))
        self.assertTrue(evidence["maintenance"]["version"].startswith("r1:"))
        self.assertIn("offset", evidence["maintenance"]["batches"])

    def test_reference_error_limits_are_stated(self):
        result = run(load_fixture())
        limits = result["applicability"]["limits"]
        self.assertTrue(any("resolution floor" in item for item in limits))
        view = next(item for item in result["uncertainty"] if item["candidate_id"] == "device-0:mean_offset")
        self.assertGreater(view["resolution_floor"], 0)
        self.assertFalse(view["reference_limited"])
        self.assertEqual(view["reference_error_sd_assumption"], "symmetric uniform ±bound")

    def test_maintenance_concentration_states_control_gap(self):
        result = run(load_fixture())
        concentration = result["applicability"]["maintenance_concentration"]
        self.assertTrue(concentration["concentrated"])
        self.assertFalse(concentration["control_available"])
        self.assertEqual(concentration["batch_device_counts"], {"normal": 2, "offset": 2})
        self.assertTrue(any("control" in item for item in result["applicability"]["limits"]))

    def test_effective_sample_is_event_clustered(self):
        result = run(load_fixture())
        view = next(item for item in result["uncertainty"] if item["device_id"] == "device-0"
                    and item["method"] == "mean_offset")
        self.assertEqual(view["post_events"], 5)
        self.assertEqual(view["post_events"], view["effective_sample_units"])
        self.assertEqual(view["cluster_unit"], "rain event")


class ConstrainedStudyTests(unittest.TestCase):
    def test_unknown_reference_error_blocks_evidence(self):
        payload = load_fixture()
        for observation in payload["observations"]:
            observation["reference_error"] = None
        result = run(payload)
        self.assertTrue(all(entry["verdict"] == "insufficient_evidence"
                            for entry in result["validation"]))
        self.assertTrue(result["applicability"]["no_evidence_cases"])

    def test_no_pairing_is_not_forced(self):
        payload = load_fixture()
        payload["pairings"] = [pairing for pairing in payload["pairings"]
                               if pairing["device_id"] != "device-0"]
        result = run(payload)
        by_id = {item["observation_id"]: item for item in result["exclusions"]["rows"]}
        for identifier in ("obs-0-5", "obs-0-40"):
            self.assertIn("no_active_pairing", by_id[identifier]["reasons"])
        self.assertNotIn("device-0", result["applicability"]["supported_devices"])

    def test_revised_quality_respected_when_known(self):
        payload = load_fixture()
        payload["as_of"] = "2026-11-05T00:00:00+00:00"
        result = run(payload)
        by_id = {item["observation_id"]: item for item in result["exclusions"]["rows"]}
        self.assertIn("obs-1-45", by_id)
        self.assertIn("quality_invalid", by_id["obs-1-45"]["reasons"])

    def test_pending_quality_is_never_silently_used(self):
        result = run(load_fixture())
        used = {item["event_id"] for entry in result["validation"] for item in entry["per_event"]}
        self.assertNotIn("rain-9", used)

    def test_deterministic_replay(self):
        payload = load_fixture()
        first = run(payload)
        second = run(copy.deepcopy(payload))
        self.assertEqual(first["study_id"], second["study_id"])
        self.assertEqual(first["validation"], second["validation"])
        self.assertEqual(first["uncertainty"], second["uncertainty"])

    def test_cli_writes_output_and_replays(self):
        with tempfile.TemporaryDirectory() as workspace:
            database = str(Path(workspace) / "study.sqlite3")
            first_path = Path(workspace) / "first.json"
            replay_path = Path(workspace) / "replay.json"
            self.assertEqual(cli_main(["--input", str(FIXTURE), "--database", database,
                                       "--output", str(first_path)]), 0)
            first = json.loads(first_path.read_text(encoding="utf-8"))
            self.assertEqual(cli_main(["--input", str(FIXTURE), "--database", database,
                                       "--output", str(replay_path)]), 0)
            replay = json.loads(replay_path.read_text(encoding="utf-8"))
            self.assertEqual(first, replay)
            self.assertEqual(first["study_id"], replay["study_id"])

    def test_cli_rejects_invalid_input_without_guessing(self):
        with tempfile.TemporaryDirectory() as workspace:
            bad = Path(workspace) / "bad.json"
            bad.write_text(json.dumps({"as_of": "2026-10-30T00:00:00+00:00",
                                       "observations": [], "pairings": []}), encoding="utf-8")
            code = cli_main(["--input", str(bad), "--database", str(Path(workspace) / "db.sqlite3"),
                             "--output", str(Path(workspace) / "out.json")])
            self.assertEqual(code, 2)
            self.assertFalse((Path(workspace) / "out.json").exists())


class SignTestTests(unittest.TestCase):
    def test_exact_one_and_two_sided_values(self):
        result = sign_test(5, 0, 0)
        self.assertAlmostEqual(result["p_value"], 0.03125)
        self.assertAlmostEqual(result["p_value_two_sided"], 0.0625)
        self.assertEqual(result["direction"], "improves")
        self.assertEqual(sign_test(0, 0, 5)["p_value"], 1.0)


if __name__ == "__main__":
    unittest.main()
