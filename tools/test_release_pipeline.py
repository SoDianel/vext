"""Exercise release packaging with disposable builds; never use installed VEXT data."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parent.parent
POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")


@unittest.skipUnless(POWERSHELL, "PowerShell is required for release tooling")
class ReleasePipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="vext-release-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = dict(os.environ, VEXT_ROOT=str(self.root / "data"),
                        LOCALAPPDATA=str(self.root / "local"), APPDATA=str(self.root / "roaming"))
        for name in ("tools/publish_release.ps1", "tools/check_version.ps1", "tools/bump_version.ps1",
                     "desktop/src-tauri/tauri.conf.json", "desktop/src-tauri/Cargo.toml",
                     "desktop/src-tauri/Cargo.lock", "desktop/package.json", "desktop/package-lock.json",
                     "CHANGELOG.md"):
            dest = self.root / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / name, dest)
        self.git("init", "-q")
        self.git("-c", "user.name=VEXT test", "-c", "user.email=vext-test@example.invalid",
                 "commit", "--allow-empty", "-qm", "Release fixture")
        self.commit = self.git("rev-parse", "HEAD").strip()
        self.version = json.loads((self.root / "desktop/package.json").read_text())["version"]
        self.release = self.root / "release"
        self.portable = self.release / f"VEXT-{self.version}-portable"
        self.portable.mkdir(parents=True)
        for name in (f"VEXT-{self.version}-setup.exe", "LICENSE", "THIRD_PARTY_NOTICES.md",
                     f"VEXT-{self.version}-portable/VEXT.exe"):
            (self.release / name).write_bytes(b"disposable build fixture\n")
        self.provenance = self.release / "BUILD_PROVENANCE.json"
        self.provenance.write_text(json.dumps(dict(schema_version=1, version=self.version,
                                                   commit=self.commit, source_dirty=False)))
        self.write_checksums()

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.root), *args], text=True, env=self.env)

    def write_checksums(self):
        lines = [f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.relative_to(self.release).as_posix()}"
                 for p in sorted(self.release.rglob("*")) if p.is_file() and p.name != "SHA256SUMS.txt"]
        (self.release / "SHA256SUMS.txt").write_text("\n".join(lines) + "\n")

    def package(self):
        return subprocess.run([POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                               str(self.root / "tools/publish_release.ps1"), "-SkipGitHub"],
                              cwd=self.root, env=self.env, capture_output=True, text=True)

    def test_package_is_repeatable_and_accepts_downloaded_zip(self):
        result = self.package()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        archive = self.release / f"VEXT-{self.version}-portable.zip"
        before = hashlib.sha256(archive.read_bytes()).hexdigest()
        # A downloaded CI artifact contains the ZIP rather than a duplicate portable folder.
        shutil.rmtree(self.portable)
        result = self.package()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(hashlib.sha256(archive.read_bytes()).hexdigest(), before)

    def test_changed_installer_is_rejected_before_packaging(self):
        (self.release / f"VEXT-{self.version}-setup.exe").write_bytes(b"changed")
        result = self.package()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("do not match", result.stdout + result.stderr)
        self.assertFalse((self.release / f"VEXT-{self.version}-portable.zip").exists())

    def test_missing_provenance_is_rejected(self):
        self.provenance.unlink()
        self.assertNotEqual(self.package().returncode, 0)

    def test_dirty_source_is_rejected(self):
        data = json.loads(self.provenance.read_text())
        data["source_dirty"] = True
        self.provenance.write_text(json.dumps(data))
        result = self.package()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("source was dirty", result.stdout + result.stderr)

    def test_unlisted_portable_file_is_rejected(self):
        (self.portable / "unexpected.dll").write_bytes(b"not checksummed")
        result = self.package()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Unlisted portable file", result.stdout + result.stderr)

    def test_npm_lock_version_mismatch_is_rejected(self):
        path = self.root / "desktop/package-lock.json"
        lock = json.loads(path.read_text())
        lock["packages"][""]["version"] = "0.0.0"
        path.write_text(json.dumps(lock))
        result = subprocess.run([POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                                 str(self.root / "tools/check_version.ps1")],
                                cwd=self.root, env=self.env, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Version mismatch", result.stdout + result.stderr)

    def test_version_bump_updates_all_locks_and_crlf_changelog(self):
        changelog = self.root / "CHANGELOG.md"
        changelog.write_bytes(changelog.read_text().replace("\n", "\r\n").encode())
        command = [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                   str(self.root / "tools/bump_version.ps1"), "-Version", "1.0.1", "-Date", "2026-10-26"]
        before = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file() and ".git" not in p.parts}
        preview = subprocess.run([*command, "-DryRun"], cwd=self.root, env=self.env, capture_output=True, text=True)
        self.assertEqual(preview.returncode, 0, preview.stdout + preview.stderr)
        self.assertTrue(all(p.read_bytes() == value for p, value in before.items()))
        result = subprocess.run(command, cwd=self.root, env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        check = subprocess.run([POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                                str(self.root / "tools/check_version.ps1"), "-Tag", "v1.0.1"],
                               cwd=self.root, env=self.env, capture_output=True, text=True)
        self.assertEqual(check.returncode, 0, check.stdout + check.stderr)
        # Dating an existing release must not add a duplicate section on Windows.
        result = subprocess.run(command, cwd=self.root, env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(changelog.read_text().count("## [1.0.1]"), 1)


if __name__ == "__main__":
    unittest.main()
