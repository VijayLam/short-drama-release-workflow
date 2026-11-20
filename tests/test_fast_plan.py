from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from path_guard import OutputPathError, resolve_output_path  # noqa: E402
from plan_run import DEFAULTS, build_plan  # noqa: E402
from delivery_fingerprint import compute  # noqa: E402


class FastPlanTests(unittest.TestCase):
    def test_formal_output_must_stay_under_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source"
            source.mkdir()
            self.assertEqual(resolve_output_path(source), (source / "二次创作发行").resolve())
            with self.assertRaises(OutputPathError):
                resolve_output_path(source, Path(temp) / "outside")

    def write_state(self, root: Path, hashes: dict[str, str], output_status: str = "current") -> None:
        cache = root / ".short-drama-cache"
        output = root / "二次创作发行"
        cache.mkdir(parents=True)
        output.mkdir(parents=True)
        manifest = {
            "schema": 2,
            "status": "ready",
            "episodes": [
                {"episode_key": key, "sha256": value}
                for key, value in sorted(hashes.items())
            ],
        }
        (cache / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        (output / "source_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        (output / "生成清单.json").write_text(
            json.dumps(
                {
                    "status": output_status,
                    "skill_version": DEFAULTS["skill_version"],
                    "image_rule_version": DEFAULTS["image_rule_version"],
                    "script_template_version": DEFAULTS["script_template_version"],
                    "analysis_model_version": DEFAULTS["analysis_model_version"],
                    "delivery_fingerprint": "test-fingerprint",
                }
            ),
            encoding="utf-8",
        )
        (output / "run_manifest.json").write_text(
            json.dumps({"status": output_status}),
            encoding="utf-8",
        )

    def test_unchanged_output_is_noop(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.write_state(root, {"e001": "a", "e002": "b"})
            plan = build_plan(root, root / ".short-drama-cache", root / "二次创作发行", DEFAULTS)
            self.assertEqual(plan["mode"], "noop")
            self.assertEqual(plan["changed_episodes"], [])
            self.assertFalse(plan["model_calls_allowed"])

    def test_changed_episode_is_incremental(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.write_state(root, {"e001": "a", "e002": "b"})
            manifest = json.loads(
                (root / ".short-drama-cache" / "manifest.json").read_text()
            )
            manifest["episodes"][1]["sha256"] = "changed"
            (root / ".short-drama-cache" / "manifest.json").write_text(
                json.dumps(manifest), encoding="utf-8"
            )
            plan = build_plan(root, root / ".short-drama-cache", root / "二次创作发行", DEFAULTS)
            self.assertEqual(plan["mode"], "incremental")
            self.assertEqual(plan["changed_episodes"], ["e002"])
            self.assertTrue(plan["model_calls_allowed"])

    def test_missing_delivery_fingerprint_requires_verify(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.write_state(root, {"e001": "a", "e002": "b"})
            generation = json.loads(
                (root / "二次创作发行" / "生成清单.json").read_text()
            )
            generation.pop("delivery_fingerprint")
            (root / "二次创作发行" / "生成清单.json").write_text(
                json.dumps(generation), encoding="utf-8"
            )
            plan = build_plan(root, root / ".short-drama-cache", root / "二次创作发行", DEFAULTS)
            self.assertEqual(plan["mode"], "verify_required")
            self.assertFalse(plan["model_calls_allowed"])

    def test_delivery_fingerprint_ignores_macos_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "release"
            package = root / "01_引流款_第01集" / "03_脚本"
            package.mkdir(parents=True)
            (package / "Seedance_一键复制.md").write_text("script", encoding="utf-8")
            first = compute(root)["fingerprint"]
            (root / "01_引流款_第01集" / ".DS_Store").write_bytes(b"finder noise")
            second = compute(root)["fingerprint"]
            self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()

