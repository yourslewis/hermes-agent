"""Real Notes HTTP API -> canonical Python loader using temporary synthetic files."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent.interview_notes_bank import initialize_notes_bank, load_notes_bank
from agent.interview_questions import load_question_bank

app = Path(sys.argv[1]).resolve()
with tempfile.TemporaryDirectory() as temp:
    root = Path(temp).resolve()
    bank = root / '03-Question-Bank'
    initialize_notes_bank(bank, load_question_bank(root / 'unconfigured'))
    with socket.socket() as s:
        s.bind(('127.0.0.1',0)); port = s.getsockname()[1]
    env = dict(os.environ, MOBILE_NOTES_VAULT=str(root), MOBILE_NOTES_GIT_PUSH='0',
               MOBILE_NOTES_REQUIRE_TOKEN='0', HOST='127.0.0.1', PORT=str(port))
    proc = subprocess.Popen(['node',str(app / 'src/server.js')],env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    try:
        base = f'http://127.0.0.1:{port}'
        for _ in range(100):
            try:
                urllib.request.urlopen(base+'/api/health',timeout=1).close(); break
            except OSError:
                if proc.poll() is not None: raise RuntimeError('Server failed')
                time.sleep(.03)
        else: raise RuntimeError('Server readiness failed')
        def request(path,data=None,method='GET'):
            req=urllib.request.Request(base+path,data=json.dumps(data).encode() if data is not None else None,
                headers={'Content-Type':'application/json'},method=method)
            with urllib.request.urlopen(req,timeout=10) as r: return json.load(r)
        path='03-Question-Bank/common-outcome.md'
        note=request('/api/note?path='+path)['note']
        content=note['content'].replace('What should be different when this task is finished?',
            'What measurable outcome should this task achieve?')
        request('/api/note',{'path':path,'content':content,'expectedHash':note['hash']},'PUT')
        loaded=load_notes_bank(bank)
        assert any(q['question']=='What measurable outcome should this task achieve?' for q in loaded['questions'])
        entries=request('/api/notes')['notes']
        assert len([x for x in entries if x['path'].startswith('03-Question-Bank/')])==16
        print(json.dumps({'http_manual_edit_loaded':True,'questions':len(loaded['questions']),
                          'prior_version_recoverable':bool(list((root/'.mobile-notes-trash').rglob('*.md')))}))
    finally:
        proc.terminate(); proc.wait(timeout=5)
