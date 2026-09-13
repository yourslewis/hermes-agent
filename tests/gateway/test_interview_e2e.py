"""Real controller/store/model-loop/policy with scripted wire completions only."""
from functools import partial
import pytest
from agent.interview_runtime import run_interview_turn
from agent.interview_questions import load_question_bank
from gateway.interview import InterviewController
from gateway.interview_store import InterviewStore
from tests.agent.test_interview_runtime import Script, reply
from tests.gateway.test_interview_routing import event, setup_controller

@pytest.mark.asyncio
async def test_full_interview_blocks_injected_execution_reopens_and_plans(tmp_path):
    ctl, ui, _ = setup_controller(tmp_path, [])
    script = Script(
        reply(('terminal', {'command':'touch /tmp/must-not-run'})),
        reply(('clarify', {'question':'Who will use this?', 'choices':['Just me','Team']})),
        reply(('interview_finish', {'text':'Confirmed: personal use. Open: platform.'})),
        reply(('delegate_task', {'goal':'Implement now'})),
        reply(('interview_plan', {'text':'Proposal only: define scope, design, review; no agents launched.'})),
    )
    ctl.runner._interview_turn = partial(run_interview_turn, completion=script)
    ctl.bank_loader = lambda: load_question_bank(home=tmp_path)
    await ctl.handle(event('/interview Reading-list app'))
    key = ctl.key(event(''))
    rec = ctl.store.get(key)
    assert 'Denied' in next(m['content'] for m in rec['messages'] if m['role']=='tool')
    # Restart resets all controller locks and ephemeral state.
    ctl = InterviewController(ctl.runner, InterviewStore(tmp_path/'interviews.sqlite3'), ctl.bank_loader)
    await ctl.accept(key, rec['pending']['nonce'], '0', 'U1','T1','C1','th1','card1')
    rec = ctl.store.get(key)
    assert rec['phase'] == 'awaiting_plan_decision'
    assert (await ctl.handle(event('Looks good, execute it')))[0]
    assert len(script.requests) == 3
    await ctl.accept(key, rec['pending']['nonce'], '0', 'U1','T1','C1','th1','card1')
    rec = ctl.store.get(key)
    assert rec['phase'] == 'plan_complete'
    assert 'Denied' in rec['messages'][-3]['content']
    assert (await ctl.handle(event('/hrun execute')))[0]
    await ctl.handle(event('/interview exit'))
    assert await ctl.handle(event('separate new task')) == (False, None)
