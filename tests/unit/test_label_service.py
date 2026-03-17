from __future__ import annotations

import pytest

from roar.application.labels import LabelService


def test_reject_reserved_keys_blocks_system_managed_dataset_labels() -> None:
    with pytest.raises(ValueError, match="Reserved label keys cannot be set manually"):
        LabelService._reject_reserved_keys(
            {
                "dataset": {
                    "id": "file:///data/train",
                    "modality": "tabular",
                }
            }
        )
