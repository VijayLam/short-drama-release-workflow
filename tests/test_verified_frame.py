from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from extract_verified_frame import extract_frame
import extract_verified_frame
from index_source_frames import build_index, load_nearest
import index_source_frames


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "需要 ffmpeg/ffprobe")
class VerifiedFrameTests(unittest.TestCase):
    def make_vfr_source(self, source: Path) -> tuple[list[dict[str, str]], str, str]:
        subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                "-i", "testsrc2=size=640x360:rate=30:duration=1",
                "-vf", r"setpts=PTS+if(gte(N\,10)\,1/TB\,0)", "-fps_mode", "passthrough",
                "-c:v", "libx264", "-g", "30", "-bf", "3", "-pix_fmt", "yuv420p",
                "-y", str(source),
            ],
            check=True,
        )
        probe = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "v:0", "-show_streams",
                "-show_frames", "-show_entries",
                "stream=time_base,r_frame_rate,avg_frame_rate:frame=best_effort_timestamp,best_effort_timestamp_time",
                "-of", "json", str(source),
            ],
            text=True,
            capture_output=True,
            check=True,
        )
        payload = json.loads(probe.stdout)
        return payload["frames"], payload["streams"][0]["time_base"], payload["streams"][0]["r_frame_rate"]

    def test_extracts_vfr_frame_by_exact_source_pts(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source_root = Path(temp) / "source"
            source_root.mkdir()
            source = source_root / "episode.mp4"
            frames, time_base, nominal_rate = self.make_vfr_source(source)
            target = frames[17]
            target_pts = int(target["best_effort_timestamp"])
            target_seconds = float(target["best_effort_timestamp_time"])
            nominal_fps = float(nominal_rate.split("/")[0]) / float(nominal_rate.split("/")[1])
            self.assertGreater(abs(target_seconds - 17 / nominal_fps), 0.5)

            output = source_root / "stage" / "frame.png"
            with patch.object(extract_verified_frame.subprocess, "run", wraps=subprocess.run) as run_spy:
                result = extract_frame(
                    source, target_pts, time_base, output, source_root, "ffmpeg", "ffprobe"
                )

            decode_commands = [
                call.args[0]
                for call in run_spy.call_args_list
                if any(source.name in str(arg) for arg in call.args[0]) and "-vf" in call.args[0]
            ]
            self.assertEqual(len(decode_commands), 1)
            self.assertNotIn("-ss", decode_commands[0])
            self.assertIn(rf"select=eq(pts\,{target_pts}),showinfo", decode_commands[0])
            self.assertEqual(result["expected_source_pts_ticks"], target_pts)
            self.assertEqual(result["actual_source_pts_ticks"], target_pts)
            self.assertEqual(result["source_time_base"], time_base)
            self.assertAlmostEqual(result["actual_source_pts_seconds"], target_seconds, places=3)
            self.assertEqual(result["visual_review_status"], "pending")
            self.assertTrue(output.is_file())
            self.assertTrue(Path(result["preview_thumbnail"]).is_file())
            self.assertTrue(Path(result["evidence_file"]).is_file())
            self.assertLessEqual(result["preview_dimensions"][0], result["image_dimensions"][0])
            self.assertLessEqual(result["preview_dimensions"][1], result["image_dimensions"][1])

    def test_missing_pts_does_not_publish_an_image(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source_root = Path(temp) / "source"
            source_root.mkdir()
            source = source_root / "episode.mp4"
            frames, time_base, _ = self.make_vfr_source(source)
            missing_pts = int(frames[17]["best_effort_timestamp"]) + 1
            output = source_root / "stage" / "missing.png"
            with self.assertRaisesRegex(RuntimeError, "requested_source_pts_not_found"):
                extract_frame(source, missing_pts, time_base, output, source_root, "ffmpeg", "ffprobe")
            self.assertFalse(output.exists())

    def test_streamed_index_preserves_vfr_pts_and_cache_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source_root = Path(temp) / "source"
            source_root.mkdir()
            source = source_root / "episode.mp4"
            frames, time_base, nominal_rate = self.make_vfr_source(source)
            index_result = build_index(source, source_root, "ffprobe", timeout=60)
            index_path = Path(index_result["index_file"])
            with index_path.open("r", encoding="utf-8") as index_file:
                rows = [json.loads(line) for line in index_file]
            target_seconds = float(frames[17]["best_effort_timestamp_time"])
            candidate = load_nearest(index_path, target_seconds)
            nominal_fps = float(nominal_rate.split("/")[0]) / float(nominal_rate.split("/")[1])
            self.assertEqual(index_result["status"], "ready")
            self.assertEqual(len(rows), len(frames))
            self.assertEqual(candidate["pts_ticks"], int(frames[17]["best_effort_timestamp"]))
            self.assertEqual(index_result["time_base"], time_base)
            self.assertGreater(abs(candidate["pts_seconds"] - 17 / nominal_fps), 0.5)
            cached = build_index(source, source_root, "ffprobe", timeout=60)
            self.assertTrue(cached["cache_hit"])
            self.assertEqual(cached["source_sha256"], index_result["source_sha256"])


if __name__ == "__main__":
    unittest.main()
