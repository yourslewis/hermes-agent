"""Entry guard precedes external harness hooks, including after gateway restart."""
from unittest.mock import AsyncMock
import pytest
from gateway.run import GatewayRunner
from gateway.config import Platform
from tests.gateway.test_interview_routing import event, setup_controller


@pytest.mark.asyncio
async def test_interview_entry_preempts_harness_hook(monkeypatch, tmp_path):
    ctl, ui, model = setup_controller(tmp_path, [dict(kind='question', question='Who?', choices=[], messages=[])])
    runner = object.__new__(GatewayRunner)
    runner._interview_controller = ctl
    runner._scale_to_zero_note_real_inbound = lambda: None
    monkeypatch.setattr('hermes_cli.lifecycle.invoke_hook', lambda *a, **k: pytest.fail('external hook reached'))
    assert await runner._handle_message(event('/interview Dashboard')) is None
    assert model.await_count == 1


@pytest.mark.asyncio
async def test_corrupt_interview_lookup_stops_dispatch(monkeypatch, tmp_path):
    ctl, _, _ = setup_controller(tmp_path, [])
    ctl.store.get = lambda key: (_ for _ in ()).throw(ValueError('broken state'))
    runner = object.__new__(GatewayRunner)
    runner._interview_controller = ctl
    runner._scale_to_zero_note_real_inbound = lambda: None
    monkeypatch.setattr('hermes_cli.lifecycle.invoke_hook', lambda *a, **k: pytest.fail('fail open'))
    result = await runner._handle_message(event('/hrun execute now'))
    assert 'unavailable' in result.lower()


def test_command_registered_as_gateway_busy_dispatch():
    from hermes_cli.commands import COMMAND_REGISTRY
    command = next((c for c in COMMAND_REGISTRY if c.name == 'interview'), None)
    assert command is not None
    assert command.gateway_only
    assert command.busy_policy == 'dispatch'


@pytest.mark.asyncio
async def test_busy_path_preempts_approval_or_steering(monkeypatch, tmp_path):
    ctl, ui, model = setup_controller(tmp_path, [dict(kind='question', question='Who?', choices=[], messages=[])])
    await ctl.handle(event('/interview Dashboard'))
    runner = object.__new__(GatewayRunner)
    runner._interview_controller = ctl
    runner._is_user_authorized = lambda source: True
    runner._adapter_for_source = lambda source: ui
    runner._draining = False
    runner._peek_session_state = lambda key: pytest.fail('ordinary busy path reached')
    # Everything after the interview boundary is deliberately absent.
    assert await runner._handle_active_session_busy_message(event('/hrun execute'), 'normal') is True
    assert model.await_count == 1


@pytest.mark.asyncio
async def test_multiplex_entry_refused_before_any_model_or_bank_load(tmp_path):
    ctl, ui, model = setup_controller(tmp_path, [])
    ctl.runner.config = type('Config', (), {'multiplex_profiles': True})()
    handled, response = await ctl.handle(event('/interview Dashboard'))
    assert handled and 'multiplex' in response.lower()
    assert model.await_count == 0


@pytest.mark.asyncio
async def test_command_access_denial_prevents_creation(tmp_path):
    ctl, ui, model = setup_controller(tmp_path, [])
    ctl.runner._check_slash_access = lambda source, cmd: 'Command denied'
    assert await ctl.handle(event('/interview Dashboard')) == (True, 'Command denied')
    assert ctl.store.get(ctl.key(event(''))) is None
