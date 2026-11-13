"""Exercise the real scanner: fdopen arguments belong to the outer call."""
import importlib.util
from pathlib import Path
import sys

import pytest


@pytest.mark.parametrize(('source', 'expected_lines'), [
    ('os.fdopen(fd, "r+")\n', [1]),
    ('os.fdopen(fd, "w")\n', [1]),
    ('fdopen(fd)\n', [1]),
    ('import os as _os\n_os.fdopen(fd, "w")\n', [2]),
    ('module.fdopen(fd, "w")\n', [1]),
    ('package.module.fdopen(fd, "w")\n', [1]),
    ('get_module().fdopen(fd, "w")\n', [1]),
    ('modules[0].fdopen(fd, "w")\n', [1]),
    ('from os import fdopen as open_fd\nopen_fd(fd, "w")\n', [2]),
    ('from os import fdopen as open_fd\nopen_fd(\n    get_fd(encoding="utf-8"),\n    mode="w",\n)\n', [2]),
    ('os.fdopen(os.open(name, flags, dir_fd=directory), "rb")\n', []),
    ('os.fdopen(os.open(name, flags, 0o600,\n    dir_fd=directory), "wb")\n', []),
    ('os.fdopen(os.open(name, flags,\n    dir_fd=directory), "w", encoding="utf-8")\n', []),
    ('os.fdopen(os.open(name, flags, dir_fd=directory), "w")\n', [1]),
    ('os.fdopen(\n    fd, mode="rb")\n', []),
    ('os.fdopen(\n    fd, mode="w")\n', [1]),
    ('os.fdopen(fd, "w",\n    encoding = "utf-8")\n', []),
    ('os.fdopen(fd, "wb"); os.fdopen(fd, "w")\n', [1]),
    ('os.fdopen(fd, "w"); other(encoding="utf-8")\n', [1]),
    ('os.fdopen(get_fd(encoding="utf-8"), "w")\n', [1]),
    ('os.fdopen(fd, "w", **options)\n', []),
    ('os.fdopen(fd, "w")  # windows-footgun: ok\n', []),
    ('if is_windows: os.fdopen(fd, "w")\n', []),
    ('# os.fdopen(fd, "w")\n', []),
    ('"""os.fdopen(fd, "w")"""\n', []),
    ('os.fdopen(fd, "w")\nnot valid python !\n', [1]),
    ('import os as _os\n_os.fdopen(fd)\n', [2]),
    ('module.fdopen(get_fd(encoding="utf-8"), mode="w")\n', [1]),
    ('module.fdopen(\n    get_fd(),\n    "w",\n)\n', [1]),
    ('module.fdopen(fd, selected_mode)\n', [1]),
    ('module.fdopen(fd, "rb+")\n', []),
    ('import os as _os\n_os.fdopen(\n    get_fd(encoding="utf-8"), "wb")\n', []),
    ('from os import fdopen as open_fd\nopen_fd(\n    get_fd(), mode="rb+"\n)\n', []),
    ('from os import fdopen as open_fd\nopen_fd(fd, "w",\n    encoding="utf-8")\n', []),
    ('module.fdopen(fd, "w", **options)\n', []),
    ('module.fdopen(fd, "w")  # windows-footgun: ok\n', []),
    ('module.fdopen(fd, "w")\nnot valid python !\n', [1]),
    ('module.fdopen(fd, "wb")\nnot valid python !\n', []),
    ('def fdopen(fd, mode="w"):\n    pass\n', []),
    ('"module.fdopen(fd)"\n', []),
])
def test_fdopen_scanner_uses_outer_call_arguments(tmp_path, source, expected_lines):
    script = Path(__file__).resolve().parents[2] / 'scripts/check-windows-footguns.py'
    spec = importlib.util.spec_from_file_location('fdopen_footguns', script)
    assert spec is not None and spec.loader is not None
    linter = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = linter
    spec.loader.exec_module(linter)
    rule = next(fg for fg in linter.FOOTGUNS if fg.name.startswith('os.fdopen()'))
    path = tmp_path / 'sample.py'
    path.write_text(source, encoding='utf-8')
    assert [line for line, _, _ in linter.scan_file(path, [rule])] == expected_lines
