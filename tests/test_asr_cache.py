from __future__ import annotations

import sys
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from run_asr_batch import cache_matches, normalize_transcription, transcribe_local, validate_loopback_host


class ASRCacheTests(unittest.TestCase):
    def test_cache_requires_matching_source_and_fingerprint(self) -> None:
        cached = {
            "status": "ready",
            "source_sha256": "source-hash",
            "fingerprint": "source-model-language-fingerprint",
            "segments": [],
        }
        self.assertTrue(cache_matches(cached, "source-hash", "source-model-language-fingerprint"))
        self.assertFalse(cache_matches(cached, "changed-source", "source-model-language-fingerprint"))
        self.assertFalse(cache_matches(cached, "source-hash", "changed-model-digest"))
        self.assertFalse(cache_matches({**cached, "status": "failed"}, "source-hash", "source-model-language-fingerprint"))

    def test_only_loopback_ollama_hosts_are_allowed(self) -> None:
        self.assertEqual(validate_loopback_host("http://127.0.0.1:11434"), "http://127.0.0.1:11434")
        self.assertEqual(validate_loopback_host("http://localhost:11434/"), "http://localhost:11434")
        for host in ("https://example.com", "http://192.168.1.2:11434", "http://user:pass@localhost:11434"):
            with self.subTest(host=host), self.assertRaises(ValueError):
                validate_loopback_host(host)

    def test_verbose_json_segments_are_normalized(self) -> None:
        text, segments = normalize_transcription(
            {
                "text": "  你怎么还不回家？  ",
                "segments": [{"start": 0.125, "end": 1.78, "text": " 你怎么还不回家？ "}],
            }
        )
        self.assertEqual(text, "你怎么还不回家？")
        self.assertEqual(segments, [{"start": 0.12, "end": 1.78, "text": "你怎么还不回家？"}])

    def test_asr_timeout_is_a_recordable_failure(self) -> None:
        timed_out = subprocess.CompletedProcess([], 28, "", "Operation timed out")
        with patch("run_asr_batch.subprocess.run", return_value=timed_out):
            with self.assertRaisesRegex(TimeoutError, "硬时限"):
                transcribe_local("/usr/bin/curl", "http://127.0.0.1:11434", Path("sample.wav"), "gemma4:12b", "zh", 60)


if __name__ == "__main__":
    unittest.main()
