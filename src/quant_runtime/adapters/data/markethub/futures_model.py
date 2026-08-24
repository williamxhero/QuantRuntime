from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from hashlib import sha256
from heapq import merge
from typing import Any
from zoneinfo import ZoneInfo

import pyarrow as pa

from quant_runtime.artifacts import canonical_json, normalize_decimal


@dataclass(frozen=True, slots=True)
class CanonicalFuturesInstrument:
    instrument: str
    product_code: str
    exchange: str
    series_type: str

    def hash_record(self) -> dict[str, Any]:
        return {
            "exchange": self.exchange,
            "instrument": self.instrument,
            "product_code": self.product_code,
            "series_type": self.series_type,
        }


@dataclass(frozen=True, slots=True)
class CanonicalFuturesBar:
    bar_time: datetime
    instrument: str
    signal_open: Decimal
    signal_high: Decimal
    signal_low: Decimal
    signal_close: Decimal
    volume: Decimal
    open_interest: Decimal | None
    adjustment_offset: Decimal

    @classmethod
    def from_markethub(
        cls,
        row: dict[str, Any],
        instrument_by_product: dict[str, CanonicalFuturesInstrument],
        *,
        parse_time,
    ) -> CanonicalFuturesBar:
        product_code = str(row.get("product_code", "")).casefold()
        try:
            instrument = instrument_by_product[product_code]
        except KeyError as exc:
            raise ValueError(f"futures bar has unknown product_code {product_code!r}") from exc
        if str(row.get("exchange", "")) != instrument.exchange:
            raise ValueError(f"futures exchange drifted for {instrument.instrument!r}")
        if str(row.get("series_type", "")) != instrument.series_type:
            raise ValueError(f"futures series_type drifted for {instrument.instrument!r}")
        result = cls(
            bar_time=parse_time(row.get("bar_time")),
            instrument=instrument.instrument,
            signal_open=_decimal(row.get("open")),
            signal_high=_decimal(row.get("high")),
            signal_low=_decimal(row.get("low")),
            signal_close=_decimal(row.get("close")),
            volume=_decimal(row.get("volume")),
            open_interest=_optional_decimal(row.get("open_interest")),
            adjustment_offset=_decimal(row.get("adjustment_offset")),
        )
        result.validate()
        return result

    @property
    def identity(self) -> tuple[datetime, str]:
        return self.bar_time, self.instrument

    @property
    def economic_open(self) -> Decimal:
        return self.signal_open + self.adjustment_offset

    @property
    def economic_high(self) -> Decimal:
        return self.signal_high + self.adjustment_offset

    @property
    def economic_low(self) -> Decimal:
        return self.signal_low + self.adjustment_offset

    @property
    def economic_close(self) -> Decimal:
        return self.signal_close + self.adjustment_offset

    def validate(self) -> None:
        values = (
            self.signal_open,
            self.signal_high,
            self.signal_low,
            self.signal_close,
            self.volume,
            self.adjustment_offset,
            self.economic_open,
            self.economic_high,
            self.economic_low,
            self.economic_close,
        )
        if self.bar_time.tzinfo is None or any(not value.is_finite() for value in values):
            raise ValueError(f"invalid futures 1m row: {self.identity}")
        if self.open_interest is not None and (
            not self.open_interest.is_finite() or self.open_interest < 0
        ):
            raise ValueError(f"invalid futures open interest: {self.identity}")
        if self.volume < 0:
            raise ValueError(f"negative futures volume or open interest: {self.identity}")
        if min(self.signal_open, self.signal_high, self.signal_low, self.signal_close) <= 0:
            raise ValueError(f"non-positive signal price: {self.identity}")
        if min(self.economic_open, self.economic_high, self.economic_low, self.economic_close) <= 0:
            raise ValueError(f"non-positive economic price: {self.identity}")
        if self.signal_high < max(self.signal_open, self.signal_low, self.signal_close):
            raise ValueError(f"signal high below OHLC member: {self.identity}")
        if self.signal_low > min(self.signal_open, self.signal_high, self.signal_close):
            raise ValueError(f"signal low above OHLC member: {self.identity}")

    def hash_record(self) -> dict[str, Any]:
        return {
            "adjustment_offset": normalize_decimal(self.adjustment_offset),
            "bar_time": self.bar_time.isoformat(),
            "instrument": self.instrument,
            "open_interest": (
                normalize_decimal(self.open_interest) if self.open_interest is not None else None
            ),
            "signal_close": normalize_decimal(self.signal_close),
            "signal_high": normalize_decimal(self.signal_high),
            "signal_low": normalize_decimal(self.signal_low),
            "signal_open": normalize_decimal(self.signal_open),
            "volume": normalize_decimal(self.volume),
        }


@dataclass(frozen=True, slots=True)
class CanonicalFuturesBarChunk:
    instrument: str
    timestamps_ns: pa.Int64Array
    signal_open: pa.StringArray
    signal_high: pa.StringArray
    signal_low: pa.StringArray
    signal_close: pa.StringArray
    volume: pa.StringArray
    open_interest: pa.StringArray
    adjustment_offset: pa.StringArray
    trading_dates: tuple[date, ...]

    @classmethod
    def from_rows(
        cls,
        rows: Iterable[dict[str, Any]],
        instrument_by_product: dict[str, CanonicalFuturesInstrument],
        *,
        parse_time,
    ) -> CanonicalFuturesBarChunk:
        bars = (
            CanonicalFuturesBar.from_markethub(
                row,
                instrument_by_product,
                parse_time=parse_time,
            )
            for row in rows
        )
        timestamps_ns: list[int] = []
        signal_open: list[str] = []
        signal_high: list[str] = []
        signal_low: list[str] = []
        signal_close: list[str] = []
        volume: list[str] = []
        open_interest: list[str | None] = []
        adjustment_offset: list[str] = []
        trading_dates: set[date] = set()
        instrument = ""
        for bar in bars:
            if instrument and instrument != bar.instrument:
                raise ValueError("compact futures chunk contains multiple instruments")
            instrument = bar.instrument
            timestamps_ns.append(_datetime_to_ns(bar.bar_time))
            signal_open.append(normalize_decimal(bar.signal_open))
            signal_high.append(normalize_decimal(bar.signal_high))
            signal_low.append(normalize_decimal(bar.signal_low))
            signal_close.append(normalize_decimal(bar.signal_close))
            volume.append(normalize_decimal(bar.volume))
            open_interest.append(
                normalize_decimal(bar.open_interest) if bar.open_interest is not None else None
            )
            adjustment_offset.append(normalize_decimal(bar.adjustment_offset))
            trading_dates.add(bar.bar_time.date())
        if not timestamps_ns or not instrument:
            raise ValueError("compact futures chunk cannot be empty")
        return cls(
            instrument=instrument,
            timestamps_ns=pa.array(timestamps_ns, type=pa.int64()),
            signal_open=pa.array(signal_open, type=pa.string()),
            signal_high=pa.array(signal_high, type=pa.string()),
            signal_low=pa.array(signal_low, type=pa.string()),
            signal_close=pa.array(signal_close, type=pa.string()),
            volume=pa.array(volume, type=pa.string()),
            open_interest=pa.array(open_interest, type=pa.string()),
            adjustment_offset=pa.array(adjustment_offset, type=pa.string()),
            trading_dates=tuple(sorted(trading_dates)),
        )

    def __len__(self) -> int:
        return len(self.timestamps_ns)

    def __iter__(self):
        for index in range(len(self)):
            open_interest = self.open_interest[index].as_py()
            yield CanonicalFuturesBar(
                bar_time=_ns_to_datetime(self.timestamps_ns[index].as_py()),
                instrument=self.instrument,
                signal_open=Decimal(self.signal_open[index].as_py()),
                signal_high=Decimal(self.signal_high[index].as_py()),
                signal_low=Decimal(self.signal_low[index].as_py()),
                signal_close=Decimal(self.signal_close[index].as_py()),
                volume=Decimal(self.volume[index].as_py()),
                open_interest=Decimal(open_interest) if open_interest is not None else None,
                adjustment_offset=Decimal(self.adjustment_offset[index].as_py()),
            )

    @property
    def first_timestamp_ns(self) -> int:
        return int(self.timestamps_ns[0].as_py())

    @property
    def last_timestamp_ns(self) -> int:
        return int(self.timestamps_ns[-1].as_py())


@dataclass(frozen=True, slots=True)
class CanonicalFuturesBars:
    chunks_by_instrument: tuple[tuple[CanonicalFuturesBarChunk, ...], ...]

    def __post_init__(self) -> None:
        instruments: list[str] = []
        for chunks in self.chunks_by_instrument:
            if not chunks:
                raise ValueError("compact futures instrument cannot have no chunks")
            instrument = chunks[0].instrument
            if any(chunk.instrument != instrument for chunk in chunks):
                raise ValueError("compact futures chunks drifted across instruments")
            if any(
                current.first_timestamp_ns <= previous.last_timestamp_ns
                for previous, current in zip(chunks, chunks[1:], strict=False)
            ):
                raise ValueError(f"compact futures chunks overlap for {instrument!r}")
            instruments.append(instrument)
        if instruments != sorted(set(instruments)):
            raise ValueError("compact futures instruments must be unique and ordered")

    def __len__(self) -> int:
        return sum(len(chunk) for chunks in self.chunks_by_instrument for chunk in chunks)

    def __iter__(self):
        streams = (
            (bar for chunk in chunks for bar in chunk) for chunks in self.chunks_by_instrument
        )
        return merge(*streams, key=lambda item: item.identity)

    @property
    def instruments(self) -> tuple[str, ...]:
        return tuple(chunks[0].instrument for chunks in self.chunks_by_instrument)

    @property
    def bar_counts(self) -> dict[str, int]:
        return {
            chunks[0].instrument: sum(len(chunk) for chunk in chunks)
            for chunks in self.chunks_by_instrument
        }

    @property
    def trading_dates(self) -> tuple[date, ...]:
        return tuple(
            sorted(
                {
                    trading_date
                    for chunks in self.chunks_by_instrument
                    for chunk in chunks
                    for trading_date in chunk.trading_dates
                }
            )
        )

    @property
    def instrument_bounds(self) -> dict[str, tuple[datetime, datetime]]:
        return {
            chunks[0].instrument: (
                _ns_to_datetime(chunks[0].first_timestamp_ns),
                _ns_to_datetime(chunks[-1].last_timestamp_ns),
            )
            for chunks in self.chunks_by_instrument
        }


@dataclass(frozen=True, slots=True)
class CanonicalFuturesDataset:
    data_version: str
    dataset_version: str
    timezone: str
    series_type: str
    instruments: tuple[CanonicalFuturesInstrument, ...]
    bars: tuple[CanonicalFuturesBar, ...] | CanonicalFuturesBars
    _validated: bool = field(default=False, init=False, repr=False, compare=False)
    _input_hash: str | None = field(default=None, init=False, repr=False, compare=False)

    def validate(self) -> None:
        if self._validated:
            return
        if not self.data_version or not self.dataset_version:
            raise ValueError("canonical futures dataset requires both version identities")
        if self.timezone != "Asia/Shanghai" or self.series_type not in {
            "back_adjusted_continuous",
            "main_continuous",
        }:
            raise ValueError("invalid futures timezone or series type")
        if not _strictly_increasing(item.instrument for item in self.instruments):
            raise ValueError("futures instruments are duplicated or out of canonical order")
        instruments = {item.instrument for item in self.instruments}
        if isinstance(self.bars, CanonicalFuturesBars):
            if set(self.bars.instruments) != instruments:
                raise ValueError("compact futures bars do not match the instrument catalog")
        else:
            if not _strictly_increasing(item.identity for item in self.bars):
                raise ValueError("futures bars are duplicated or out of canonical order")
            if any(item.instrument not in instruments for item in self.bars):
                raise ValueError("futures bar references an unknown instrument")
            if {item.instrument for item in self.bars} != instruments:
                raise ValueError("futures dataset has no bars for one or more instruments")
        object.__setattr__(self, "_validated", True)

    @property
    def input_hash(self) -> str:
        if self._input_hash is not None:
            return self._input_hash
        self.validate()
        metadata = {
            "data_version": self.data_version,
            "dataset_version": self.dataset_version,
            "instruments": [item.hash_record() for item in self.instruments],
            "schema": "quant-runtime.canonical-futures-1m.v1",
            "series_type": self.series_type,
            "timezone": self.timezone,
        }
        digest = sha256()
        digest.update(b'{"bars":[')
        for index, item in enumerate(self.bars):
            if index:
                digest.update(b",")
            digest.update(canonical_json(item.hash_record()))
        digest.update(b"],")
        digest.update(canonical_json(metadata)[1:])
        result = digest.hexdigest()
        object.__setattr__(self, "_input_hash", result)
        return result

    @property
    def bar_counts(self) -> dict[str, int]:
        if isinstance(self.bars, CanonicalFuturesBars):
            return self.bars.bar_counts
        return {
            instrument.instrument: sum(
                item.instrument == instrument.instrument for item in self.bars
            )
            for instrument in self.instruments
        }

    @property
    def trading_dates(self) -> tuple[date, ...]:
        if isinstance(self.bars, CanonicalFuturesBars):
            return self.bars.trading_dates
        return tuple(sorted({item.bar_time.date() for item in self.bars}))

    @property
    def instrument_bounds(self) -> dict[str, tuple[datetime, datetime]]:
        if isinstance(self.bars, CanonicalFuturesBars):
            return self.bars.instrument_bounds
        return {
            instrument.instrument: (
                min(
                    item.bar_time for item in self.bars if item.instrument == instrument.instrument
                ),
                max(
                    item.bar_time for item in self.bars if item.instrument == instrument.instrument
                ),
            )
            for instrument in self.instruments
        }


def product_code_from_instrument(instrument: str) -> str:
    value = instrument.removesuffix("L0")
    if not value or value == instrument:
        raise ValueError(
            f"continuous futures instrument must use the MarketHub <product>L0 form: {instrument!r}"
        )
    return value


def _decimal(value: Any) -> Decimal:
    if value is None:
        raise ValueError("required futures numeric value is null")
    result = value if isinstance(value, Decimal) else Decimal(str(value))
    if not result.is_finite():
        raise ValueError(f"non-finite futures numeric value {value!r}")
    return result


def _optional_decimal(value: Any) -> Decimal | None:
    return None if value is None else _decimal(value)


def _strictly_increasing(values: Iterable[Any]) -> bool:
    iterator = iter(values)
    try:
        previous = next(iterator)
    except StopIteration:
        return True
    for current in iterator:
        if current <= previous:
            return False
        previous = current
    return True


_SHANGHAI = ZoneInfo("Asia/Shanghai")


def _datetime_to_ns(value: datetime) -> int:
    return int(value.timestamp()) * 1_000_000_000 + value.microsecond * 1_000


def _ns_to_datetime(value: int) -> datetime:
    seconds, nanoseconds = divmod(value, 1_000_000_000)
    return datetime.fromtimestamp(seconds, tz=_SHANGHAI).replace(microsecond=nanoseconds // 1_000)
