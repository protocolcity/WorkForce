"""wf-236 — export seam rewrites public pyproject GitHub URLs.

The export whitelist copies tests/ into DEST. This file must stay green
on the public tree (no export script, pyproject already rewritten) and
must not contain the private GitHub handle — scrub greps shipped *.py.
"""
import os
import re
import subprocess
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXPORT = os.path.join(ROOT, "scripts", "export_workforce.sh")
PYPROJECT = os.path.join(ROOT, "pyproject.toml")
PUBLIC_REPO = "https://github.com/protocolcity/WorkForce"


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _homepage(text):
    match = re.search(r'^Homepage = "(https://github.com/[^"]+)"$', text, re.M)
    assert match is not None, "pyproject.toml missing GitHub Homepage"
    return match.group(1)


def test_pyproject_homepage_is_github():
    homepage = _homepage(_read(PYPROJECT))
    assert homepage.startswith("https://github.com/")


def test_internal_pyproject_keeps_private_github_remote():
    if not os.path.isfile(EXPORT):
        pytest.skip("export script does not ship — public tree already rewritten")
    assert PUBLIC_REPO not in _read(PYPROJECT)


def test_export_seam_rewrites_project_urls_to_public_repo():
    if not os.path.isfile(EXPORT):
        pytest.skip("export script does not ship")
    script = _read(EXPORT)
    match = re.search(
        r"python3 - \"\$DEST/pyproject.toml\" <<'PY'\n(.*?)\nPY\n",
        script,
        re.S,
    )
    assert match is not None, "pyproject surgery heredoc missing from export"
    surgery = match.group(1)
    assert "protocolcity/WorkForce" in surgery
    assert "[project.urls]" in surgery

    src = _read(PYPROJECT)
    private_homepage = _homepage(src)
    assert private_homepage != PUBLIC_REPO

    with tempfile.TemporaryDirectory() as tmp:
        dest = os.path.join(tmp, "pyproject.toml")
        with open(dest, "w", encoding="utf-8") as fh:
            fh.write(src)
        proc = subprocess.run(
            ["python3", "-", dest],
            input=surgery,
            text=True,
            capture_output=True,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr
        out = _read(dest)
    assert 'Homepage = "%s"' % PUBLIC_REPO in out
    assert 'Issues = "%s/issues"' % PUBLIC_REPO in out
    assert 'Source = "%s"' % PUBLIC_REPO in out
    assert private_homepage not in out
