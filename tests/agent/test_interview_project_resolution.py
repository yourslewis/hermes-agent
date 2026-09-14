"""Project discovery is bounded metadata lookup, never a read capability."""
import os
from pathlib import Path

import pytest

from agent.interview_policy import dispatch_tool, tool_schemas
from gateway import interview_permissions as permissions

pytestmark = pytest.mark.skipif(os.name != 'posix', reason='Secure descriptor walk requires POSIX')


@pytest.fixture
def home(tmp_path, monkeypatch):
    path = tmp_path.resolve() / 'owner'
    path.mkdir()
    monkeypatch.setenv('HOME', str(path))
    return path


def test_home_relative_request_resolves_before_permission_outcome(home):
    project = home / '.hermes/repos/msan_algo'
    project.mkdir(parents=True)
    roots = []
    result, outcome = dispatch_tool('request_read_access',
        {'path': '.hermes/repos/msan_algo', 'reason': 'Inspect project'}, read_roots=roots)
    assert outcome == {'kind': 'permission', 'path': str(project), 'reason': 'Inspect project'}
    assert result == {'status': 'pending'}
    assert permissions.validate_read_root(outcome['path']) == str(project)
    assert roots == []


@pytest.mark.parametrize('reference', ['absolute', '~/.hermes/repos/msan_algo', 'msan_algo'])
def test_project_reference_lookup(home, reference):
    project = home / '.hermes/repos/msan_algo'
    project.mkdir(parents=True)
    if reference == 'absolute':
        reference = str(project)
    assert permissions.resolve_project_candidates(reference) == [str(project)]


def test_find_project_reports_ambiguity_without_grant_or_content_reads(home, monkeypatch):
    projects = [home / '.hermes' / container / 'demo' for container in ('repos', 'worktrees')]
    for project in projects:
        project.mkdir(parents=True)
        (project / '.env').write_text('private')
    monkeypatch.setattr(os, 'read', lambda *args: pytest.fail('Discovery read file contents'))
    monkeypatch.setattr(os, 'scandir', lambda *args: pytest.fail('Discovery enumerated a directory'))
    roots = []
    result, outcome = dispatch_tool('find_project', {'name': 'demo'}, read_roots=roots)
    assert result == {'candidates': sorted(map(str, projects))}
    assert outcome is None
    result, outcome = dispatch_tool('request_read_access', {'path': 'demo', 'reason': 'Inspect'}, read_roots=roots)
    assert 'error' in result and result['candidates'] == sorted(map(str, projects))
    assert outcome is None and roots == []
    assert 'find_project' in {tool['function']['name'] for tool in tool_schemas()}


@pytest.mark.parametrize('reference', ['demo', 'absolute', 'child'])
def test_approved_project_requests_are_nonterminal(home, reference):
    project = home / 'custom/demo'
    (project / 'src').mkdir(parents=True)
    roots = [str(project)]
    raw = {'demo': 'demo', 'absolute': str(project), 'child': str(project / 'src')}[reference]
    result, outcome = dispatch_tool('request_read_access', {'path': raw, 'reason': 'Inspect'}, read_roots=roots)
    assert result == {'status': 'already_approved', 'path': str(project / 'src') if reference == 'child' else str(project)}
    assert outcome is None
    assert roots == [str(project)]


@pytest.mark.parametrize('container', ['repos', 'worktrees'])
def test_exact_home_relative_file_alias_requires_approval(home, container):
    project = home / '.hermes' / container / 'demo'
    project.mkdir(parents=True)
    (project / 'README.md').write_text('Project facts')
    reference = f'.hermes/{container}/demo/README.md'
    result, outcome = dispatch_tool('read_file', {'path': reference}, read_roots=[str(project)])
    assert result['content'] == 'Project facts' and outcome is None
    result, outcome = dispatch_tool('read_file', {'path': reference})
    assert 'error' in result and outcome is None


@pytest.mark.parametrize('target', ['missing', 'traversal', 'profile', 'sensitive', 'symlink',
    'ancestor_symlink', 'file', 'home', 'container', 'arbitrary_relative', 'nested_name', 'cwd_name'])
def test_unsafe_or_unresolved_projects_never_produce_permission(home, monkeypatch, target):
    project = home / '.hermes/repos/demo'
    project.mkdir(parents=True)
    (project / 'src').mkdir()
    (project / '.env').mkdir()
    (project.parent / 'alias').symlink_to(project, target_is_directory=True)
    (project / 'README.md').write_text('Safe')
    (home / '.hermes/profiles/work').mkdir(parents=True)
    (home / 'custom/nested').mkdir(parents=True)
    (project / 'nested').mkdir()
    monkeypatch.chdir(home / 'custom')
    raw = {
        'missing': 'absent', 'traversal': '.hermes/repos/demo/../demo',
        'profile': '~/.hermes/profiles/work', 'sensitive': str(project / '.env'),
        'symlink': 'alias', 'ancestor_symlink': str(project.parent / 'alias/src'),
        'file': str(project / 'README.md'), 'home': str(home),
        'container': '.hermes/repos', 'arbitrary_relative': 'custom/nested',
        'nested_name': 'nested', 'cwd_name': 'custom',
    }[target]
    assert permissions.resolve_project_candidates(raw) == []
    result, outcome = dispatch_tool('request_read_access', {'path': raw, 'reason': 'Inspect'})
    assert 'error' in result and result['candidates'] == [] and outcome is None
    with pytest.raises(ValueError):
        permissions.validate_read_root(raw)


def test_existing_approval_does_not_hide_ambiguity(home):
    projects = [home / '.hermes/repos/demo', home / 'custom/demo']
    for project in projects:
        project.mkdir(parents=True)
    roots = [str(projects[1])]
    assert permissions.resolve_project_candidates('demo', roots) == sorted(map(str, projects))
    result, outcome = dispatch_tool('request_read_access', {'path': 'demo', 'reason': 'Inspect'}, read_roots=roots)
    assert 'error' in result and outcome is None
    assert permissions.resolve_project_candidates('demo', list(map(str, projects))) == sorted(map(str, projects))


@pytest.mark.parametrize('suffix', ['.env', '../other/README.md', 'link', 'dirlink/README.md'])
def test_home_relative_alias_does_not_bypass_file_policy(home, suffix):
    project = home / '.hermes/repos/demo'
    project.mkdir(parents=True)
    other = project.parent / 'other'
    other.mkdir()
    (other / 'README.md').write_text('Other project')
    (project / '.env').write_text('Private')
    (project / 'link').symlink_to(other / 'README.md')
    (project / 'dirlink').symlink_to(other, target_is_directory=True)
    result, outcome = dispatch_tool('read_file', {'path': '.hermes/repos/demo/' + suffix}, read_roots=[str(project)])
    assert 'error' in result and outcome is None


def test_arbitrary_relative_file_stays_project_relative(home):
    project = home / '.hermes/repos/demo'
    (project / 'custom').mkdir(parents=True)
    (project / 'custom/README.md').write_text('Project copy')
    (home / 'custom').mkdir()
    (home / 'custom/README.md').write_text('Home copy')
    result, outcome = dispatch_tool('read_file', {'path': 'custom/README.md'}, read_roots=[str(project)])
    assert result['content'] == 'Project copy' and outcome is None


@pytest.mark.parametrize('args', [{}, {'name': ''}, {'name': 42}, {'name': 'demo', 'path': '/'}])
def test_find_project_schema_rejects_invalid_arguments(args):
    result, outcome = dispatch_tool('find_project', args)
    assert 'error' in result and outcome is None
