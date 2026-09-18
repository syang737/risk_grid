"""Test-wide setup.

The only thing here is the `.env` opt-out, and it has to happen at import time
rather than in a fixture: the API suites reload `api.main`, which calls the
loader at module level, so by the time any fixture ran it would be too late.

Without this, a developer's own `.env` would be read into their test run -- a
stray `RISK_GRID_FIRM` or `RISK_GRID_STORE` failing tests for a reason that
appears nowhere in the diff.
"""

import os

os.environ["RISK_GRID_NO_DOTENV"] = "1"
