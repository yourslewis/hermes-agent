"""Filesystem capability boundary tests for the isolated interview policy."""
import os
import sys

import pytest

from agent.interview_policy import dispatch_tool


def dispatch(name, args, roots=()):
    result, outcome = dispatch_tool(name, args, read_roots=roots)
    assert outcome is None
    return result


def test_read_and_literal_search_require_explicit_roots(tmp_path):
    root = tmp_path.resolve()
    (root / 'notes.txt').write_text('Target users: staff\nBudget: TBD\n')
    args = {'path': str(root / 'notes.txt')}
    assert 'error' in dispatch('read_file', args)
    assert dispatch('read_file', args, [str(root)])['content'].startswith('Target users')
    found = dispatch('search_files', {'path': str(root), 'pattern': 'staff'}, [str(root)])
    assert found['matches'] == [{'path': str(root / 'notes.txt'), 'line': 1, 'text': 'Target users: staff'}]
    assert 'error' in dispatch('read_file', {'path': str(root.parent / 'other.txt')}, [str(root)])


@pytest.mark.parametrize('name', ['.env', '.env.local', 'auth.json', 'credentials.json',
                                  'id_rsa', 'private.pem', '.ssh/config', '.aws/config',
                                  '.git/config', '.hermes/memories/USER.md'])
def test_sensitive_paths_are_denied_even_with_approved_root(tmp_path, name):
    path = tmp_path.resolve() / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('private material')
    assert 'error' in dispatch('read_file', {'path': str(path)}, [str(tmp_path.resolve())])


@pytest.mark.skipif(sys.platform == 'win32', reason='POSIX symlink and special file boundary')
def test_symlinks_and_special_files_never_read_or_searched(tmp_path):
    root = tmp_path.resolve()
    (root / 'normal.txt').write_text('safe')
    (root / 'alias').symlink_to(root / 'normal.txt')
    (root / 'dirlink').symlink_to(root, target_is_directory=True)
    os.mkfifo(root / 'pipe')
    for path in [root / 'alias', root / 'dirlink' / 'normal.txt', root / 'pipe']:
        assert 'error' in dispatch('read_file', {'path': str(path)}, [str(root)])
    assert 'error' in dispatch('read_file', {'path': str(root / 'dirlink' / 'normal.txt')}, [str(root / 'dirlink')])
    found = dispatch('search_files', {'path': str(root), 'pattern': 'safe'}, [str(root)])
    assert [m['path'] for m in found['matches']] == [str(root / 'normal.txt')]


def test_content_screening_and_hardlinks_and_output_bounds(tmp_path):
    from agent.interview_policy import MAX_FILE_BYTES
    root = tmp_path.resolve()
    sensitive = root / 'ordinary.txt'
    sensitive.write_text('-----BEGIN PRIVATE KEY-----\nprivate key data')
    assert 'error' in dispatch('read_file', {'path': str(sensitive)}, [str(root)])
    sensitive.write_text('OPENAI_API_KEY=sk-test-sensitive-value')
    assert 'error' in dispatch('read_file', {'path': str(sensitive)}, [str(root)])
    found = dispatch('search_files', {'path': str(root), 'pattern': 'sk-test'}, [str(root)])
    assert found['matches'] == []
    normal = root / 'normal.txt'
    normal.write_text('x' * (MAX_FILE_BYTES + 100))
    data = dispatch('read_file', {'path': str(normal)}, [str(root)])
    assert len(data['content']) == MAX_FILE_BYTES
    assert data['truncated'] is True
    os.link(normal, root / 'hardlink')
    assert 'error' in dispatch('read_file', {'path': str(root / 'hardlink')}, [str(root)])


def test_explicit_project_below_hermes_readable_but_profile_data_denied(tmp_path):
    home = tmp_path.resolve() / '.hermes'
    project = home / 'repos' / 'example'
    project.mkdir(parents=True)
    (project / 'README.md').write_text('Project requirements')
    private = home / 'profiles' / 'user' / 'memory.md'
    private.parent.mkdir(parents=True)
    private.write_text('private')
    assert dispatch('read_file', {'path': str(project/'README.md')}, [str(project)])['content'] == 'Project requirements'
    assert 'error' in dispatch('read_file', {'path': str(private)}, [str(private.parent)])
    assert 'error' in dispatch('read_file', {'path': str(project/'README.md')}, [str(home)])
