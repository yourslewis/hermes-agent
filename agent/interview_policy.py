"""Closed interview-only tool policy; intentionally unrelated to the registry."""


def tool_schemas():
    definitions = [
        ('clarify', 'Ask one question and stop for the user.',
         {'question': {'type': 'string'}, 'choices': {'type': 'array', 'items': {'type': 'string'}}}, ['question']),
        ('interview_finish', 'Finish with requirements, assumptions and unresolved items.',
         {'text': {'type': 'string'}}, ['text']),
        ('interview_plan', 'Return a proposal only; permitted only for intent=plan. Never execute.',
         {'text': {'type': 'string'}}, ['text']),
        ('read_file', 'Read text under explicitly approved read_roots only.',
         {'path': {'type': 'string'}}, ['path']),
        ('search_files', 'Literal text search under approved read_roots only.',
         {'path': {'type': 'string'}, 'pattern': {'type': 'string'}}, ['path', 'pattern']),
    ]
    return [{'type': 'function', 'function': {'name': name, 'description': description,
             'parameters': {'type': 'object', 'properties': props, 'required': required,
                            'additionalProperties': False}}}
            for name, description, props, required in definitions]


def dispatch_tool(name, arguments, *, intent='collect', read_roots=()):
    """Return (tool result, optional terminal turn result); default deny."""
    schemas = {tool['function']['name']: tool['function']['parameters'] for tool in tool_schemas()}
    schema = schemas.get(name)
    if schema is not None:
        if (not isinstance(arguments, dict) or set(arguments) - set(schema['properties'])
                or not set(schema['required']).issubset(arguments)):
            return {'error': 'Denied: invalid tool arguments.'}, None
        for key, value in arguments.items():
            expected = schema['properties'][key]['type']
            if expected == 'string' and (not isinstance(value, str) or not value.strip() or len(value) > 32_768):
                return {'error': 'Denied: expected a nonempty bounded string.'}, None
            if expected == 'array' and (not isinstance(value, list) or len(value) > 10
                    or any(not isinstance(item, str) or not item.strip() or len(item) > 200 for item in value)):
                return {'error': 'Denied: choices must be up to ten short strings.'}, None
    if name == 'clarify':
        return {'status': 'pending'}, {'kind': 'question', 'question': arguments['question'],
                                     'choices': arguments.get('choices', []), 'text': ''}
    if name == 'interview_finish' or (name == 'interview_plan' and intent == 'plan'):
        kind = 'plan' if name == 'interview_plan' else 'summary'
        return {'status': 'complete'}, {'kind': kind, 'question': '', 'choices': [],
                                      'text': arguments['text']}
    if name in {'read_file', 'search_files'}:
        try:
            return _filesystem(name, arguments, read_roots), None
        except (OSError, ValueError, TypeError):
            return {'error': 'Denied: path unavailable, unsafe, or outside approved read_roots.'}, None
    return {'error': 'Denied: tool is not authorized in interview mode (web/history disabled in v1).'}, None


# POSIX descriptor-relative walking avoids check-then-open symlink races.
# Platforms without these primitives fail closed rather than weaken the boundary.
import os
import re
from pathlib import Path
import stat
import time

MAX_FILE_BYTES = 32_768
MAX_SEARCH_ENTRIES = 512
MAX_MATCHES = 50
FILE_TIMEOUT_SECONDS = 2.0


def _sensitive(name):
    name = name.lower()
    return (name.startswith(('.env', 'id_rsa', 'id_ed25519', 'id_dsa', 'id_ecdsa'))
            or name in {'.ssh', '.aws', '.azure', '.gcloud', '.gnupg', '.git', '.hermes',
                        '.netrc', '.npmrc', '.pypirc', 'auth.json', 'keychain', 'keychains'}
            or any(word in name for word in ('credential', 'secret', 'token', 'password'))
            or name.endswith(('.pem', '.key', '.p12', '.pfx', '.kdbx')))


def _absolute(path):
    if not isinstance(path, str) or not path or not Path(path).is_absolute():
        raise ValueError('Absolute paths required')
    if '..' in Path(path).parts:
        raise ValueError('Traversal denied')
    result = Path(path)
    for i, part in enumerate(result.parts[1:], 1):
        # Agent-owned project checkouts are not profile state. Only a named
        # project below these containers is eligible; approving .hermes itself
        # or a profile directory still fails closed.
        project_container = (part == '.hermes' and len(result.parts) > i + 2
            and result.parts[i + 1] in {'repos', 'worktrees'})
        if _sensitive(part) and not project_container:
            raise ValueError('Sensitive path')
    return result


def _open_path(path):
    if os.name != 'posix' or not hasattr(os, 'O_NOFOLLOW'):
        raise ValueError('Secure reads unsupported')
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for i, part in enumerate(path.parts[1:]):
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            if i < len(path.parts) - 2:
                flags |= os.O_DIRECTORY
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def _read_fd(fd):
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError('Only ordinary non-hardlinked files may be read')
    data = os.read(fd, MAX_FILE_BYTES + 1)
    if b'\x00' in data:
        raise ValueError('Binary file')
    # Defense in depth, not a claim that arbitrary secrets are detectable:
    # root approval must itself exclude private data. Never return recognizable
    # credentials even when they have been copied into an innocuous filename.
    text = data.decode('utf-8')
    if re.search(r'(?i)-----BEGIN [^\n]*PRIVATE KEY-----|(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|client[_-]?secret)[\"\s]*[:=]|\b(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9]{12,}|xox[baprs]-[A-Za-z0-9-]+)', text):
        raise ValueError('Recognizable secret material')
    return {'content': data[:MAX_FILE_BYTES].decode('utf-8', errors='ignore'),
            'truncated': len(data) > MAX_FILE_BYTES}


def _filesystem(name, arguments, roots):
    path = _absolute(arguments['path'])
    if not isinstance(roots, (list, tuple)) or not roots:
        raise ValueError('No approved roots')
    allowed = False
    for raw in roots:
        root = _absolute(raw)
        if path.is_relative_to(root):
            root_fd = _open_path(root)
            try:
                if not stat.S_ISDIR(os.fstat(root_fd).st_mode):
                    raise ValueError('Read root must be a directory')
            finally:
                os.close(root_fd)
            allowed = True
            break
    if not allowed:
        raise ValueError('Outside approved roots')
    fd = _open_path(path)
    try:
        if name == 'read_file':
            return _read_fd(fd)
        pattern = arguments['pattern']
        if not isinstance(pattern, str) or not pattern or len(pattern) > 512:
            raise ValueError('A short nonempty literal pattern is required')
        matches = []
        budget = [MAX_SEARCH_ENTRIES]
        deadline = time.monotonic() + FILE_TIMEOUT_SECONDS

        def walk(directory, display, depth=0):
            if depth > 20:
                return
            with os.scandir(directory) as entries:
                for entry in entries:
                    budget[0] -= 1
                    if budget[0] < 0 or time.monotonic() >= deadline or len(matches) >= MAX_MATCHES:
                        return
                    if _sensitive(entry.name) or entry.is_symlink():
                        continue
                    child = None
                    try:
                        child = os.open(entry.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                        dir_fd=directory)
                        child_path = display / entry.name
                        if stat.S_ISDIR(os.fstat(child).st_mode):
                            walk(child, child_path, depth + 1)
                        else:
                            text = _read_fd(child)['content']
                            for number, line in enumerate(text.splitlines(), 1):
                                if pattern in line:
                                    matches.append({'path': str(child_path), 'line': number,
                                                    'text': line[:512]})
                                    if len(matches) >= MAX_MATCHES:
                                        return
                    except (OSError, ValueError):
                        continue
                    finally:
                        if child is not None:
                            os.close(child)
        walk(fd, path)
        return {'matches': matches, 'truncated': budget[0] <= 0 or len(matches) >= MAX_MATCHES
                or time.monotonic() >= deadline}
    finally:
        os.close(fd)
