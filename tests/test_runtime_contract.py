from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "scripts" / "build_input_manifest.py"
VALIDATOR = ROOT / "scripts" / "validate_delivery.py"


class RuntimeContractTests(unittest.TestCase):
    def run_script(self, script: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(script), *args],
            text=True,
            capture_output=True,
            check=False,
        )

    def test_empty_source_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source"
            source.mkdir()
            result = self.run_script(MANIFEST, str(source))
            self.assertEqual(result.returncode, 2)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["status"], "blocked")
            self.assertEqual(payload["reason"], "没有发现正式视频")

    @unittest.skipUnless(shutil.which("ffmpeg"), "需要ffmpeg生成最小视频")
    def test_duplicate_episode_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source"
            source.mkdir()
            for name in ("剧集-第1集.mp4", "测试-1.mp4"):
                output = source / name
                subprocess.run(
                    [
                        "ffmpeg",
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-f",
                        "lavfi",
                        "-i",
                        "color=c=black:s=128x256:d=0.2",
                        "-pix_fmt",
                        "yuv420p",
                        "-y",
                        str(output),
                    ],
                    check=True,
                )
            result = self.run_script(MANIFEST, str(source), "--include-subfolders")
            self.assertEqual(result.returncode, 2)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["status"], "blocked")
            self.assertEqual(payload["duplicates"], ["e001"])

    def test_validator_is_fail_closed_without_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            result = self.run_script(VALIDATOR, temp, "--json")
            self.assertEqual(result.returncode, 2)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["status"], "blocked")
            self.assertGreater(payload["issue_count"], 0)


if __name__ == "__main__":
    unittest.main()

