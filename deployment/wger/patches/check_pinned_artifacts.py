#!/usr/bin/env python3
"""Explicit network regression: python3 deployment/wger/patches/check_pinned_artifacts.py.

Requires curl and network access; intentionally excluded from unit-test
discovery. Works only in temporary storage, without Docker or release.
"""
import hashlib
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

PATCH_DIR = Path(__file__).resolve().parent


class PinnedArtifactsTest(unittest.TestCase):
    def test_real_pinned_artifacts_pass_and_corrupt_recorded_digests_fail(self):
        pins = dict(re.findall(
            r"^(WGER_REPO|WGER_COMMIT)=(\S+)$",
            (PATCH_DIR / "prepare-react.sh").read_text(), re.MULTILINE,
        ))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            template = root / "template.html"
            subprocess.run([
                "curl", "--fail", "--location", "--silent", "--show-error",
                f"{pins['WGER_REPO']}/raw/{pins['WGER_COMMIT']}/wger/core/templates/template.html",
                "--output", str(template),
            ], check=True)
            self.check_gate(template, "patch_footer.py", "Pinned wger template changed")

            history = root / "history-overview.html"
            subprocess.run([
                "curl", "--fail", "--location", "--silent", "--show-error",
                f"{pins['WGER_REPO']}/raw/{pins['WGER_COMMIT']}/wger/exercises/templates/history/overview.html",
                "--output", str(history),
            ], check=True)
            self.check_gate(
                history, "patch_australian_template_dates.py",
                "Pinned wger history-overview template changed", ("history-overview",),
            )

            api_key = root / "api-key.html"
            subprocess.run([
                "curl", "--fail", "--location", "--silent", "--show-error",
                f"{pins['WGER_REPO']}/raw/{pins['WGER_COMMIT']}/wger/core/templates/user/api_key.html",
                "--output", str(api_key),
            ], check=True)
            self.check_gate(
                api_key, "patch_australian_template_dates.py",
                "Pinned wger api-key template changed", ("api-key",),
            )
            pdf = root / "pdf.py"
            subprocess.run([
                "curl", "--fail", "--location", "--silent", "--show-error",
                f"{pins['WGER_REPO']}/raw/{pins['WGER_COMMIT']}/wger/utils/pdf.py",
                "--output", str(pdf),
            ], check=True)
            self.check_gate(
                pdf, "patch_australian_pdf.py",
                "Pinned wger PDF utility changed",
            )


    def check_gate(self, source, patch_name, refusal, patch_args=()):
        patch = PATCH_DIR / patch_name
        target = source.with_name(source.name + ".next")
        subprocess.run([sys.executable, str(patch), *patch_args, str(source), str(target)], check=True)
        self.assertTrue(target.is_file(), "Accepted artifact must produce a deployable override")

        # Mutate only the recorded digest, never the actual built/pinned artifact.
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        code = patch.read_text()
        self.assertEqual(code.count(digest), 1, "Expected one recorded digest to corrupt")
        corrupted = source.with_name(patch_name)
        corrupted.write_text(code.replace(digest, "0" * 64))
        rejected_target = source.with_name(source.name + ".rejected")
        result = subprocess.run([
            sys.executable, str(corrupted), *patch_args, str(source), str(rejected_target),
        ], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0, "Corrupt recorded digest must reject the real artifact")
        self.assertIn(refusal, result.stderr)
        self.assertFalse(rejected_target.exists(), "Rejection must not stage an override")
        print(f"{patch_name}: real sha256={digest} accepted; corrupted digest rejected")


if __name__ == "__main__":
    unittest.main()
