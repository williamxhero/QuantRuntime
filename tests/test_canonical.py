from decimal import Decimal

import pandas as pd

from markethub_qlib.canonical import hash_frame, sha256_value


def test_canonical_hash_ignores_mapping_order() -> None:
    assert sha256_value({"b": 2, "a": Decimal("1.20")}) == sha256_value(
        {"a": Decimal("1.20"), "b": 2}
    )


def test_frame_hash_changes_with_version() -> None:
    index = pd.MultiIndex.from_tuples(
        [(pd.Timestamp("2025-01-02"), "SH.600000")],
        names=["datetime", "instrument"],
    )
    frame = pd.DataFrame({"close": [10.0]}, index=index)
    first = hash_frame(frame, data_version="v1", dataset_version="d1")
    second = hash_frame(frame, data_version="v2", dataset_version="d1")
    assert first != second
