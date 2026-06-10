"""The limen boundary — the one module that imports limen's ``Cohort`` /
``HistoricalData`` / ``Trainer``.

The serving path (``furnace.prediction_cache``) imports these symbols from
here rather than from ``limen`` directly, so the stub-less ``from limen …``
statements the frozen pyright budget absorbs live in exactly one place.
"""

from __future__ import annotations

from limen.cohort import Cohort
from limen.data import HistoricalData
from limen.experiment import Trainer

__all__ = ["Cohort", "HistoricalData", "Trainer"]
