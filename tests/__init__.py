"""Test package. Run from the repo root: python -m unittest discover -s tests -t . -v

The runtime modules live in app/ and import each other by top-level name
(app/ is sys.path[0] when app/*.py runs as a script), so the tests do the same.
"""
import os
import sys

APP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)
