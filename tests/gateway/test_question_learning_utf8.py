"""Configuration creation and controls must not depend on the host locale."""
import json
import os
from pathlib import Path
import runpy
import sys
from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize('boundary', ['configure', 'control'])
@pytest.mark.skipif(sys.platform == 'win32', reason='POSIX nofollow/flock filesystem API')
async def test_question_learning_config_uses_utf8(tmp_path, monkeypatch, boundary):
    from gateway.interview_learning import learning_control
    from tests.agent.test_interview_notes_bank import bank_files

    home = tmp_path / 'rex'
    home.mkdir()
    root = bank_files(tmp_path)
    config = home / 'question-learning.json'
    owner = '用户'
    real_fdopen = os.fdopen
    encodings = []

    def locale_fdopen(fd, mode='r', *args, **kwargs):
        # Emulate a Western Windows locale without mocking the filesystem.
        kwargs.setdefault('encoding', 'cp1252')
        stream = real_fdopen(fd, mode, *args, **kwargs)
        encodings.append(stream.encoding)
        return stream

    monkeypatch.setattr(os, 'fdopen', locale_fdopen)
    if boundary == 'configure':
        script = Path(__file__).resolve().parents[2] / 'scripts/configure_question_learning.py'
        monkeypatch.setattr(sys, 'argv', [str(script), '--home', str(home),
                            '--notes-root', str(root), '--owner', owner,
                            '--team', 'T', '--channel', 'C'])
        # Cache creation is binary and must retain its existing semantics.
        def locale_text_fdopen(fd, mode='r', *args, **kwargs):
            if 'b' in mode:
                return real_fdopen(fd, mode, *args, **kwargs)
            return locale_fdopen(fd, mode, *args, **kwargs)
        monkeypatch.setattr(os, 'fdopen', locale_text_fdopen)
        runpy.run_path(str(script), run_name='__main__')
        assert json.loads(config.read_bytes())['owner'] == owner
        assert config.stat().st_mode & 0o777 == 0o600
    else:
        data = dict(enabled=True, profile='rex', owner=owner, team='T', channels=['C'])
        config.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        source = SimpleNamespace(user_id=owner, scope_id='T', chat_id='C')
        assert 'paused' in await learning_control(home, source, 'pause')
        assert json.loads(config.read_bytes())['enabled'] is False
    assert encodings == ['utf-8']
