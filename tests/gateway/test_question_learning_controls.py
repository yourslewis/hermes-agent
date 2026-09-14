"""Controls are code-owned and scoped independently of interview task state."""
import json
from types import SimpleNamespace
import pytest
from gateway.config import Platform
from gateway.session import SessionSource
from gateway.platforms.base import MessageEvent

@pytest.mark.asyncio
async def test_learning_pause_retains_bank_and_resume_does_not_backfill(tmp_path):
    from gateway.interview_learning import learning_control
    config = {'enabled': True, 'profile':'rex', 'owner':'U', 'team':'T', 'channels':['C'],
              'since':'1.0', 'notes_root': str(tmp_path / 'notes')}
    p = tmp_path / 'question-learning.json'
    p.write_text(json.dumps(config))
    source = SessionSource(platform=Platform.SLACK, chat_id='C', user_id='U', scope_id='T')
    response = await learning_control(tmp_path, source, 'pause')
    assert 'paused' in response.lower()
    assert json.loads(p.read_text())['enabled'] is False
    response = await learning_control(tmp_path, source, 'enable')
    cfg = json.loads(p.read_text())
    assert cfg['enabled'] is True and float(cfg['since']) > 1
    assert cfg['notes_root'] == config['notes_root']

@pytest.mark.asyncio
async def test_controls_reject_other_owner_without_changes(tmp_path):
    from gateway.interview_learning import learning_control
    p = tmp_path / 'question-learning.json'
    data = json.dumps({'owner':'U','team':'T','channels':['C'],'profile':'rex','enabled':True})
    p.write_text(data)
    source = SessionSource(platform=Platform.SLACK, chat_id='C', user_id='OTHER', scope_id='T')
    assert 'denied' in (await learning_control(tmp_path,source,'pause')).lower()
    assert p.read_text() == data

@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['symlink','malformed'])
async def test_controls_refuse_unsafe_config(tmp_path, kind):
    from gateway.interview_learning import learning_control
    p = tmp_path / 'question-learning.json'
    target = tmp_path / 'target'
    data = json.dumps({'owner':'U','team':'T','channels':['C'],'profile':'rex','enabled':True})
    target.write_text(data)
    if kind == 'symlink':
        p.symlink_to(target)
    else:
        p.write_text('[]')
    source = SessionSource(platform=Platform.SLACK, chat_id='C', user_id='U', scope_id='T')
    assert 'invalid' in (await learning_control(tmp_path,source,'pause')).lower()
    assert target.read_text() == data
