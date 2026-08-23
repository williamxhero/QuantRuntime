from __future__ import annotations

from nautilus_trader.model.currencies import CNY
from nautilus_trader.model.identifiers import InstrumentId, Symbol, Venue
from nautilus_trader.model.instruments import Equity
from nautilus_trader.model.objects import Price, Quantity

from quant_runtime.contracts.canonical_hash import normalize_decimal
from quant_runtime.markethub.catalog import CanonicalInstrument

VENUE = Venue("XCN")


def native_instrument(item: CanonicalInstrument) -> Equity:
    symbol = Symbol(item.instrument.replace(".", "-"))
    return Equity(
        instrument_id=InstrumentId(symbol=symbol, venue=VENUE),
        raw_symbol=symbol,
        currency=CNY,
        price_precision=item.price_precision,
        price_increment=Price.from_str(normalize_decimal(item.tick_size)),
        lot_size=Quantity.from_int(item.lot_size),
        isin=None,
        ts_event=0,
        ts_init=0,
        info={"canonical_instrument": item.instrument, "exchange": item.exchange},
    )
