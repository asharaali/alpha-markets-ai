"""Guards against committing credentials.

The Kalshi private key lives in backend/.env, and the credential installer also writes
timestamped backups of that file — one of which contains the key. The original gitignore
listed only `.env`, so those backups were one `git add -A` away from being published to a
public repository. These tests make that class of mistake fail loudly in CI rather than
quietly in a push.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


# A PEM header, then a long unbroken base64 run within the next few lines. That is a key;
# a header on its own is just prose.
_PEM_HEADER = re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |)PRIVATE KEY-----")
_B64_RUN = re.compile(r"^[A-Za-z0-9+/=]{60,}$")


def _contains_key_material(text: str) -> bool:
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if _PEM_HEADER.search(line):
            for candidate in lines[i + 1: i + 6]:
                if _B64_RUN.match(candidate.strip()):
                    return True
    # A key folded onto one line with literal \n, which is how it lands in a .env.
    return bool(re.search(r"PRIVATE KEY-----(?:\\n)[A-Za-z0-9+/=]{60,}", text))


def ignored(relative_path: str) -> bool:
    result = subprocess.run(["git", "check-ignore", "-q", relative_path],
                            cwd=REPO, capture_output=True)
    return result.returncode == 0


class TestGitignoreCoversSecrets:
    @pytest.mark.parametrize("path", [
        "backend/.env",
        "backend/.env.local",
        "backend/.env.backup.20990101120000",
        "backend/.env.production",
        "backend/kalshi.pem",
        "backend/some-private.key",
    ])
    def test_secret_shaped_paths_are_ignored(self, path):
        assert ignored(path), f"{path} would be committed"

    def test_the_example_env_is_still_committable(self):
        """The template has to stay tracked — it is the setup documentation."""
        assert not ignored("backend/.env.example")


class TestNoSecretsTracked:
    def test_no_env_file_other_than_the_example_is_tracked(self):
        tracked = subprocess.run(["git", "ls-files"], cwd=REPO,
                                 capture_output=True, text=True).stdout.split()
        offenders = [f for f in tracked
                     if Path(f).name.startswith(".env") and not f.endswith(".env.example")]
        assert not offenders, f"tracked env files: {offenders}"

    def test_no_private_key_material_is_tracked(self):
        tracked = subprocess.run(["git", "ls-files"], cwd=REPO,
                                 capture_output=True, text=True).stdout.split()
        keyish = [f for f in tracked if f.endswith((".pem", ".key"))]
        assert not keyish, f"tracked key files: {keyish}"

    def test_no_committed_file_contains_a_pem_private_key(self):
        """A belt-and-braces scan of everything git is actually tracking.

        Matches key MATERIAL, not the marker string: the installer legitimately mentions
        the PEM header in a validation message, and a scanner that cannot tell the
        difference between talking about a key and containing one gets muted within a week.
        """
        tracked = subprocess.run(["git", "ls-files"], cwd=REPO,
                                 capture_output=True, text=True).stdout.split()
        offenders = []
        for name in tracked:
            path = REPO / name
            if not path.is_file() or path.stat().st_size > 2_000_000:
                continue
            try:
                text = path.read_text(errors="ignore")
            except OSError:
                continue
            if _contains_key_material(text):
                offenders.append(name)
        assert not offenders, f"files containing private key material: {offenders}"


class TestExampleEnvHasNoRealValues:
    def test_the_template_ships_empty_credentials(self):
        text = (REPO / "backend/.env.example").read_text()
        for line in text.splitlines():
            if line.startswith(("KALSHI_KEY_ID=", "KALSHI_PRIVATE_KEY=", "ODDS_API_KEY=")):
                _, _, value = line.partition("=")
                assert not value.strip(), f"template carries a real value: {line.split('=')[0]}"
