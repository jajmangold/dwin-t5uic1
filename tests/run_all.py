# SPDX-License-Identifier: GPL-3.0-or-later
"""Run every test module. No hardware required."""
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))

MODULES = ('test_parity', 'test_driver', 'test_portability')


def main():
    import importlib
    passed = 0
    failed = []
    for name in MODULES:
        module = importlib.import_module(name)
        tests = sorted((attr, getattr(module, attr)) for attr in dir(module)
                       if attr.startswith('test_'))
        print('\n--- %s (%d tests) ---' % (name, len(tests)))
        for test_name, function in tests:
            try:
                function()
                passed += 1
            except Exception:
                failed.append('%s.%s' % (name, test_name))
                print('FAIL %s' % test_name)
                traceback.print_exc()
    print('\n%d passed, %d failed' % (passed, len(failed)))
    for name in failed:
        print('  failed: %s' % name)
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
