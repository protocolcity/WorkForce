"""Provider adapters — no bypass flag, explicit allow list, pin honored (wf-259)."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from workforce.adapters import ADAPTERS, PROVIDERS, AdapterError, SeatContext, get_adapter


def _ctx(**overrides):
    base = dict(
        slug="demo",
        identity="demo",
        model="a-pin",
        repository="/tmp/demo-repo",
        remote="https://example.invalid/demo.git",
        project="recipes",
        mcp_config_path="/tmp/demo-repo/mcp.json",
        worklane_python="/usr/bin/python3",
        worklane_runtime_dir="/tmp/wl-runtime",
    )
    base.update(overrides)
    return SeatContext(**base)


def test_providers_are_the_four_adopted_vendors():
    assert set(PROVIDERS) == {"claude", "cursor", "grok", "codex"}


def test_get_adapter_rejects_unknown_provider():
    with pytest.raises(AdapterError, match="unknown provider"):
        get_adapter("chatgpt")


@pytest.mark.parametrize("provider", ["claude", "cursor", "grok", "codex"])
def test_adapter_command_carries_no_bypass_flag(provider):
    adapter = ADAPTERS[provider]
    cmd = adapter.command(_ctx())
    for flag in adapter.bypass_flags:
        assert flag not in cmd


@pytest.mark.parametrize("provider", ["claude", "cursor", "grok", "codex"])
def test_adapter_command_carries_the_model_pin(provider):
    adapter = ADAPTERS[provider]
    cmd = adapter.command(_ctx(model="pinned-model-x"))
    assert any("pinned-model-x" in part for part in cmd)


@pytest.mark.parametrize("provider", ["claude", "cursor", "grok", "codex"])
def test_adapter_allow_list_is_the_five_worklane_hand_tools(provider):
    adapter = ADAPTERS[provider]
    allow_list = adapter.allow_list(_ctx())
    assert allow_list == ["wl_show", "wl_ready", "wl_claim", "wl_comment", "wl_park"]


@pytest.mark.parametrize("provider", ["claude", "cursor", "grok", "codex"])
def test_adapter_has_a_nonempty_auth_check_and_doc(provider):
    adapter = ADAPTERS[provider]
    assert adapter.auth_check()
    assert adapter.doc.strip()


def test_cursor_trust_always_carries_sandbox_enabled():
    """--trust only dismisses the workspace-trust prompt; --sandbox enabled
    is the actual safety boundary and must always accompany it."""
    adapter = ADAPTERS["cursor"]
    cmd = adapter.command(_ctx())
    trust_idx = cmd.index("--trust")
    assert cmd[trust_idx + 1 : trust_idx + 3] == ["--sandbox", "enabled"]


def test_grok_uses_auto_permission_mode_not_dontask():
    """wf-262: dontAsk denies the whole turn (stopReason=cancelled) on any
    run_terminal_command a bare tool-name grant can't match per-segment, not
    only genuinely dangerous ones (reproduced live: dontAsk cancelled a
    harmless `mkdir && echo && cat` chain). auto instead reports a blocked
    call back to the model so the turn can still finish."""
    adapter = ADAPTERS["grok"]
    cmd = adapter.command(_ctx())
    mode_idx = cmd.index("--permission-mode")
    assert cmd[mode_idx + 1] == "auto"
    assert "dontAsk" not in cmd


def test_grok_hard_denies_its_own_dangerous_command_bucket():
    """auto mode's classifier alone lets rm/chmod/kill/git-push through
    unconfirmed in a headless session (reproduced live) — explicit --deny
    rules keep grok's own documented dangerous-command bucket hard-blocked
    regardless of mode, since deny always wins over allow/classifier."""
    adapter = ADAPTERS["grok"]
    cmd = adapter.command(_ctx())
    denied = [cmd[i + 1] for i, part in enumerate(cmd) if part == "--deny"]
    for pattern in (
        "Bash(rm *)", "Bash(chmod *)", "Bash(chown *)", "Bash(chgrp *)",
        "Bash(chattr *)", "Bash(pkill *)", "Bash(kill *)", "Bash(killall *)",
        "Bash(git push*)", "Bash(sudo *)",
    ):
        assert pattern in denied


@pytest.mark.parametrize("provider", ["claude", "cursor", "grok", "codex"])
def test_adapter_fails_closed_when_bypass_flag_is_injected(provider, monkeypatch):
    """command() must refuse even if a future edit slips a bypass flag in."""
    adapter = ADAPTERS[provider]
    bad_flag = sorted(adapter.bypass_flags)[0]
    monkeypatch.setattr(
        adapter, "build_command",
        lambda ctx, binary: [binary, bad_flag],
    )
    with pytest.raises(AdapterError, match="forbidden bypass flag"):
        adapter.command(_ctx())
