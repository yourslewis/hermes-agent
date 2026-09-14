"""Synthetic live-model canary for project-name discovery; no Slack/live state writes."""
import asyncio
import json
import sys
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent.interview_runtime import run_interview_turn

async def main():
    # Explicit temporary directory is not a grant: model must request owner approval.
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory).resolve()
        record = {'task': 'Clarify synthetic event schema', 'owner': 'synthetic', 'messages': [], 'read_roots': []}
        result = await run_interview_turn(record,
            f'The project directory is {root}. Use request_read_access to ask me for permission before reading.')
        assert result['kind'] == 'permission', result['kind']
        assert result['path'] == str(root)
        assert record['read_roots'] == []
        print(json.dumps({'kind': result['kind'], 'permission_granted': False}))
        record.update(messages=result['messages'], read_roots=[str(root)])
        (root / 'schema.txt').write_text('Synthetic fields: event_id, user_id, timestamp')
        next_turn = await run_interview_turn(record,
            'The owner approved the project. Read schema.txt, then ask one question about latency; do not execute.')
        assert next_turn['kind'] == 'question', next_turn['kind']
        calls = [c['function']['name'] for m in next_turn['messages'][len(result['messages']):] for c in m.get('tool_calls', [])]
        assert 'read_file' in calls, calls
        assert next_turn['messages'][:len(result['messages'])] == result['messages']
        print(json.dumps({'kind': next_turn['kind'], 'tools': calls, 'prefix_preserved': True}))

if __name__ == '__main__':
    asyncio.run(main())
