"""Explicit owner-scoped activation; never reads or rewrites interview records."""
import argparse
import json
import os
from pathlib import Path
import sys
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from agent.interview_notes_bank import load_notes_bank
p=argparse.ArgumentParser()
p.add_argument('--home',type=Path,required=True)
p.add_argument('--notes-root',type=Path,required=True)
p.add_argument('--owner',required=True)
p.add_argument('--team',required=True)
p.add_argument('--channel',action='append',required=True)
a=p.parse_args()
assert a.home.name=='rex' and a.home.is_dir()
assert a.notes_root.is_absolute()
config=a.home/'question-learning.json'
assert not config.exists(), 'Existing configuration requires explicit review, not overwrite'
cache=a.home/'question-bank-cache.json'
bank=load_notes_bank(a.notes_root,cache_path=cache)
data=dict(enabled=True,profile='rex',owner=a.owner,team=a.team,channels=a.channel,
    since=f'{time.time():.6f}',notes_root=str(a.notes_root),cache_path=str(cache))
fd=os.open(config,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
with os.fdopen(fd,'w') as stream:
    json.dump(data,stream,indent=2);stream.flush();os.fsync(stream.fileno())
print('Configured new-message learning; validated questions:',len(bank['questions']))
