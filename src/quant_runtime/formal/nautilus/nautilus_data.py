from __future__ import annotations

from datetime import datetime, time
from decimal import Decimal
from zoneinfo import ZoneInfo

from nautilus_trader.core.datetime import dt_to_unix_nanos
from nautilus_trader.model.data import Bar, BarType, QuoteTick
from nautilus_trader.model.instruments import Equity
from nautilus_trader.model.objects import Quantity

from quant_runtime.contracts.canonical_hash import normalize_decimal
from quant_runtime.market_data.markethub.daily_data import CanonicalDataset

SHANGHAI = ZoneInfo("Asia/Shanghai")


def bar_types(instruments: dict[str, Equity]) -> tuple[BarType, ...]:
    return tuple(
        BarType.from_str(f"{instrument.id}-1-DAY-LAST-EXTERNAL")
        for instrument in instruments.values()
    )


def native_bars(
    dataset: CanonicalDataset,
    instruments: dict[str, Equity],
) -> dict[str, list[Bar]]:
    result: dict[str, list[Bar]] = {key: [] for key in instruments}
    types = {
        key: BarType.from_str(f"{instrument.id}-1-DAY-LAST-EXTERNAL")
        for key, instrument in instruments.items()
    }
    for item in dataset.bars:
        instrument = instruments[item.instrument]
        timestamp = dt_to_unix_nanos(datetime.combine(item.trading_day, time(15), tzinfo=SHANGHAI))
        result[item.instrument].append(
            Bar(
                bar_type=types[item.instrument],
                open=instrument.make_price(item.open),
                high=instrument.make_price(item.high),
                low=instrument.make_price(item.low),
                close=instrument.make_price(item.close),
                volume=Quantity.from_str(normalize_decimal(item.volume)),
                ts_event=timestamp,
                ts_init=timestamp,
            )
        )
    return result


def native_quotes(
    dataset: CanonicalDataset,
    instruments: dict[str, Equity],
    slippage_bps: Decimal,
) -> dict[str, list[QuoteTick]]:
    result: dict[str, list[QuoteTick]] = {key: [] for key in instruments}
    size = Quantity.from_int(1_000_000_000)
    for item in dataset.bars:
        instrument = instruments[item.instrument]
        for at, value in ((time(9, 30), item.open), (time(14, 59, 59, 999999), item.close)):
            timestamp = dt_to_unix_nanos(datetime.combine(item.trading_day, at, tzinfo=SHANGHAI))
            bid_price = instrument.make_price(value * (Decimal(1) - slippage_bps / Decimal(10_000)))
            ask_price = instrument.make_price(value * (Decimal(1) + slippage_bps / Decimal(10_000)))
            result[item.instrument].append(
                QuoteTick(
                    instrument_id=instrument.id,
                    bid_price=bid_price,
                    ask_price=ask_price,
                    bid_size=size,
                    ask_size=size,
                    ts_event=timestamp,
                    ts_init=timestamp,
                )
            )
    return result
