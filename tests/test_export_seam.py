"""Public package metadata must identify the canonical standalone product."""
from pathlib import Path
import re


def test_package_urls_point_to_the_public_product():
    text = (Path(__file__).resolve().parents[1] / 'pyproject.toml').read_text()
    expected = 'https://github.com/protocolcity/WorkForce'
    for field, value in [('Homepage', expected), ('Source', expected), ('Issues', expected + '/issues')]:
        match = re.search(r'^' + field + r' = "([^"\n]+)"$', text, re.M)
        assert match and match.group(1) == value
