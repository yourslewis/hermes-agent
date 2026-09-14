"""Code-owned interview permissions. Model text is never an authorization."""
from __future__ import annotations

import os
from pathlib import Path
import shlex
import stat

from agent.interview_policy import _absolute, _open_path

ROOT_ERROR = ('Read access denied: select an existing absolute project directory, not a '
              'symlink, home/system directory, profile, or secret location.')


def validate_read_root(raw: str) -> str:
    """Reuse the read policy and no-follow descriptor walk, including ancestors."""
    try:
        path = _absolute(raw)
        home = Path.home()
        system_trees = ('/usr', '/etc', '/bin', '/sbin', '/lib', '/lib64', '/dev',
                        '/proc', '/sys', '/System', '/Library', '/Applications',
                        '/private/etc', '/private/var/db', '/private/var/root',
                        '/var/db', '/var/lib', '/var/log', '/var/run')
        if (path.anchor != '/' or any(path.is_relative_to(root) for root in system_trees)
                or path.is_relative_to(home / 'Library')):
            raise ValueError(ROOT_ERROR)
        # Selection is not permission for a filesystem/user/system container.
        broad = {'/', '/Users', '/home', '/root', '/tmp', '/private', '/private/tmp',
                 '/var', '/private/var', '/opt', '/srv', '/mnt', '/media', '/Volumes',
                 '/usr', '/usr/local', '/etc', '/bin', '/sbin', '/Library', '/System',
                 '/Applications'}
        if (str(path) in broad or path == home or path in home.parents
                or (len(path.parts) == 3 and path.parts[1] in {'Users', 'home'})
                or path in {home / name for name in ('Desktop', 'Documents', 'Downloads', 'Library')}
                or path.name.lower() in {'repos', 'worktrees', 'projects', 'workspace', 'workspaces'}):
            raise ValueError(ROOT_ERROR)
        from hermes_constants import get_hermes_home
        state_home = Path(get_hermes_home())
        # .hermes/{repos,worktrees}/PROJECT is handled by the shared policy.
        project_checkout = any(part == '.hermes' and i + 2 < len(path.parts)
            and path.parts[i + 1] in {'repos', 'worktrees'}
            for i, part in enumerate(path.parts))
        if path.is_relative_to(state_home) and not project_checkout:
            raise ValueError(ROOT_ERROR)
        fd = _open_path(path)
        try:
            if not stat.S_ISDIR(os.fstat(fd).st_mode):
                raise ValueError(ROOT_ERROR)
        finally:
            os.close(fd)
        return str(path)
    except (OSError, ValueError, TypeError, RuntimeError) as exc:
        raise ValueError(ROOT_ERROR) from exc


def single_argument(raw: str) -> str:
    try:
        tokens = shlex.split(raw)
    except ValueError as exc:
        raise ValueError('Supply one quoted absolute project path or session ID.') from exc
    if len(tokens) != 1:
        raise ValueError('Supply one quoted absolute project path or session ID.')
    return tokens[0]
