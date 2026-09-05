"""wf-231 — salem empty-feed mill-stop pin."""
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONTRACT = os.path.join(ROOT, "workers", "salem", "CONTRACT.md")
PROMPT = os.path.join(ROOT, "workers", "salem", "prompt.md")

pytestmark = pytest.mark.skipif(
    not os.path.isfile(CONTRACT) or not os.path.isfile(PROMPT),
    reason="salem papers are not shipped on the public export tree",
)


def test_salem_contract_empty_stops_without_mill_child():
    with open(CONTRACT, encoding="utf-8") as fh:
        body = fh.read()
    assert "pc-1419 mill-stop" in body
    assert "Never invent work" in body
    assert "file **one** bounded hygiene" not in body


def test_salem_prompt_empty_stops_without_mill():
    with open(PROMPT, encoding="utf-8") as fh:
        body = fh.read()
    assert "Do not file a mill" in body
    assert "standing chew first" not in body
