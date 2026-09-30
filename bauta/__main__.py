"""`python -m bauta`: the bauta command, where its script isn't on PATH --
an orchestrator's worker, say, running the interpreter it was installed into.
"""
import sys

from .cli import main

sys.exit(main())
