from __future__ import annotations

import pytest

from unilabos.legacy_support import (
    LEGACY_REMOVAL_DATE,
    LegacySupportDeprecationWarning,
    configure_legacy_support,
)


def test_legacy_mode_announces_december_2026_removal() -> None:
    assert LEGACY_REMOVAL_DATE == "2026-12-01"
    with pytest.warns(LegacySupportDeprecationWarning, match="2026-12-01"):
        configure_legacy_support(True)
    configure_legacy_support(False)
