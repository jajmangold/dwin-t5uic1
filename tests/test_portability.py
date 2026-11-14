# SPDX-License-Identifier: GPL-3.0-or-later
"""Guards that keep the core importable on MicroPython.

The whole point of this library is that the core runs on a microcontroller,
which is easy to break by adding one convenient import. These tests fail if
that happens, rather than leaving it to be discovered on a Pico.
"""
import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PACKAGE = ROOT / 'dwin_t5uic1'

# Modules that must import on a bare MicroPython port, and what each may use.
CORE_MODULES = {
    'protocol.py': set(),                       # must stay dependency-free
    '_compat.py': {'time', '_thread', 'threading', 'unicodedata'},
    'driver.py': {'math'},
    '__init__.py': set(),
    'transport/__init__.py': {'importlib'},
    'transport/_loopback.py': set(),
    'input/__init__.py': {'importlib'},
    'input/quadrature.py': {'machine', 'time'},
}

# Never acceptable anywhere in the core: these do not exist on MicroPython.
FORBIDDEN = {'decimal', 'concurrent', 'concurrent.futures', 'collections',
             'logging', 'serial', 'typing', 'dataclasses', 'enum',
             'functools', 'itertools', 'abc', 'struct'}


def _collect(nodes):
    names = set()
    for node in nodes:
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split('.')[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level:                      # relative import, in-package
                continue
            if node.module:
                names.add(node.module.split('.')[0])
    return names


def module_imports(path, toplevel_only=False):
    """Absolute module names imported by a source file.

    ``toplevel_only`` restricts the result to imports that run at import
    time. Imports nested in a function are deferred, which is how the
    hardware backends stay optional, so they are not import-time cost.
    """
    tree = ast.parse(path.read_text(encoding='utf-8'))
    if toplevel_only:
        # Module body, plus try/except wrappers used for the compat probes.
        nodes = []
        for node in tree.body:
            nodes.append(node)
            if isinstance(node, ast.Try):
                for inner in ast.walk(node):
                    nodes.append(inner)
        return _collect(nodes)
    return _collect(ast.walk(tree))


def test_core_modules_declare_only_allowed_imports():
    for relative, allowed in sorted(CORE_MODULES.items()):
        path = PACKAGE / relative
        assert path.exists(), 'missing core module %s' % relative
        used = module_imports(path, toplevel_only=True)
        unexpected = used - allowed
        assert not unexpected, (
            '%s imports %s at import time, which is not allowed on MicroPython'
            % (relative, sorted(unexpected)))
    print('imports: %d core modules within the MicroPython allowlist'
          % len(CORE_MODULES))


def test_hardware_backends_are_lazy():
    """pyserial and machine must only be imported inside the helpers."""
    path = PACKAGE / 'transport' / '__init__.py'
    toplevel = module_imports(path, toplevel_only=True)
    for hardware in ('serial', 'machine'):
        assert hardware not in toplevel, (
            '%s must be imported lazily so the other backend still works'
            % hardware)
        assert hardware in module_imports(path), (
            'expected a lazy import of %s' % hardware)
    print('imports: pyserial and machine backends are lazily imported')


def test_protocol_has_no_imports():
    used = module_imports(PACKAGE / 'protocol.py')
    assert used == set(), 'protocol.py must stay dependency-free, found %s' % used
    print('imports: protocol.py is dependency-free')


def test_no_forbidden_imports_in_core():
    """The protocol/driver/compat trio must not touch these at any level."""
    offenders = []
    for relative in ('protocol.py', 'driver.py', '_compat.py'):
        used = module_imports(PACKAGE / relative)
        for name in sorted(used & FORBIDDEN):
            offenders.append('%s -> %s' % (relative, name))
    # Everywhere else, only import-time use is disqualifying.
    for relative in CORE_MODULES:
        used = module_imports(PACKAGE / relative, toplevel_only=True)
        for name in sorted(used & FORBIDDEN):
            offenders.append('%s (import time) -> %s' % (relative, name))
    assert not offenders, offenders
    print('imports: no CPython-only modules in the core')


def test_no_upstream_petrel_coupling():
    """The driver must not reach back into Petrel's UI, theme or asset layers."""
    offenders = []
    for path in PACKAGE.rglob('*.py'):
        text = path.read_text(encoding='utf-8')
        for line in text.splitlines():
            stripped = line.strip()
            if not (stripped.startswith('import ') or stripped.startswith('from ')):
                continue
            if 'petrel' in stripped.lower():
                offenders.append('%s: %s' % (path.relative_to(ROOT), stripped))
    assert not offenders, offenders
    print('coupling: no imports of petrel.* anywhere in the package')


def test_driver_avoids_cpython_only_builtins():
    """Catch the easy regressions: f-string '=' specs, int.to_bytes(signed=...)."""
    source = (PACKAGE / 'driver.py').read_text(encoding='utf-8')
    for pattern in ('.to_bytes(', 'concurrent', 'Decimal', 'unicodedata',
                    'deque(', 'isfinite(') :
        if pattern == '.to_bytes(':
            assert pattern not in source, (
                'use _compat.to_bytes_be: MicroPython int.to_bytes lacks signed=')
        elif pattern == 'isfinite(':
            assert 'math.isfinite' not in source, (
                'use _compat.isfinite: some ports lack math.isfinite')
        else:
            assert pattern not in source, (
                '%s is CPython-only and must stay behind a _compat shim' % pattern)
    print('driver: no CPython-only facilities used directly')


def test_package_compiles_without_hardware():
    """Importing the package must not pull in pyserial, gpiozero or machine."""
    import importlib
    for name in ('dwin_t5uic1', 'dwin_t5uic1.protocol', 'dwin_t5uic1.driver',
                 'dwin_t5uic1._compat', 'dwin_t5uic1.transport',
                 'dwin_t5uic1.input'):
        importlib.import_module(name)
    for hardware in ('serial', 'gpiozero', 'machine', 'evdev'):
        assert hardware not in sys.modules, (
            'importing the package must not require %s' % hardware)
    print('imports: package imports cleanly with no hardware libraries present')


if __name__ == '__main__':
    tests = sorted((name, function) for name, function in globals().items()
                   if name.startswith('test_'))
    for name, function in tests:
        function()
    print('\nALL %d PORTABILITY TESTS PASSED' % len(tests))
