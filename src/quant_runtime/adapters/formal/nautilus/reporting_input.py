from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from numbers import Integral, Real
from typing import Any

import nautilus_trader
import pandas as pd
from nautilus_trader.core.datetime import unix_nanos_to_iso8601

from quant_runtime.artifacts import normalize_decimal

REPORTING_INPUT_SCHEMA = "quant-runtime.nautilus-reporting-input.v1"
REPORTING_INPUT_EXTRACTION_VERSION = "1"
SUPPORTED_NAUTILUS_VERSION = "1.231.0"


def extract_reporting_input(*, result: Any, analyzer: Any, account: Any | None) -> dict[str, Any]:
    """Capture the public Nautilus inputs needed for offline report rendering."""
    if nautilus_trader.__version__ != SUPPORTED_NAUTILUS_VERSION:
        raise ValueError(
            "Nautilus reporting input extraction requires exact version "
            f"{SUPPORTED_NAUTILUS_VERSION}, got {nautilus_trader.__version__}"
        )

    unavailable: list[dict[str, str]] = []
    stats_general, stats_general_source = _stats_general(result, analyzer)
    stats_pnls = _statistics_mapping(result.stats_pnls, "stats_pnls", unavailable)
    stats_returns = _statistics_mapping(result.stats_returns, "stats_returns", unavailable)
    normalized_stats_general = _statistics_mapping(
        stats_general,
        "stats_general",
        unavailable,
    )
    summary = _statistics_mapping(result.summary, "summary", unavailable)
    portfolio_returns = _portfolio_returns(analyzer)
    run_info = _run_info(result, unavailable)
    account_info = _account_info(account, unavailable)
    statistics_by_field = {
        "stats_general": normalized_stats_general,
        "stats_pnls": stats_pnls,
        "stats_returns": stats_returns,
    }

    availability = {
        "account_info": _field_availability("account_info", account_info, unavailable),
        "portfolio_returns": (
            {"status": "available"}
            if portfolio_returns
            else {"status": "unavailable", "reason": "native_series_empty"}
        ),
        "run_info": _field_availability("run_info", run_info, unavailable),
        "stats_general": _field_availability(
            "stats_general",
            normalized_stats_general,
            unavailable,
        ),
        "stats_pnls": _field_availability("stats_pnls", stats_pnls, unavailable),
        "stats_returns": _field_availability("stats_returns", stats_returns, unavailable),
    }
    if not account_info:
        _append_unavailable(unavailable, "account_info", "native_account_balances_empty")
        availability["account_info"] = {
            "status": "unavailable",
            "reason": "native_account_balances_empty",
        }
    for field in ("stats_general", "stats_pnls", "stats_returns"):
        if not statistics_by_field[field]:
            reason = "native_mapping_empty"
            _append_unavailable(unavailable, field, reason)
            availability[field] = {"status": "unavailable", "reason": reason}

    return {
        "schema": REPORTING_INPUT_SCHEMA,
        "stats_pnls": stats_pnls,
        "stats_returns": stats_returns,
        "summary": summary,
        "total_events": _nonnegative_integer(result.total_events, "total_events"),
        "total_orders": _nonnegative_integer(result.total_orders, "total_orders"),
        "total_positions": _nonnegative_integer(result.total_positions, "total_positions"),
        "stats_general": normalized_stats_general,
        "portfolio_returns": portfolio_returns,
        "run_info": run_info,
        "account_info": account_info,
        "extraction": {
            "version": REPORTING_INPUT_EXTRACTION_VERSION,
            "source": "nautilus-public-api",
            "engine_version": nautilus_trader.__version__,
            "interfaces": {
                "account_info": "Account.starting_balances()/balance_total()",
                "portfolio_returns": "PortfolioAnalyzer.portfolio_returns()",
                "run_info": "BacktestResult",
                "stats_general": stats_general_source,
                "stats_pnls": "BacktestResult.stats_pnls",
                "stats_returns": "BacktestResult.stats_returns",
            },
            "portfolio_returns_order": "timestamp_ascending_unique",
        },
        "availability": availability,
        "unavailable": sorted(unavailable, key=lambda item: (item["path"], item["reason"])),
    }


def _stats_general(result: Any, analyzer: Any) -> tuple[Mapping[Any, Any], str]:
    if hasattr(result, "stats_general"):
        value = result.stats_general
        source = "BacktestResult.stats_general"
    else:
        get_stats = getattr(analyzer, "get_performance_stats_general", None)
        if not callable(get_stats):
            raise ValueError("pinned Nautilus lacks a public general-statistics interface")
        value = get_stats()
        source = "PortfolioAnalyzer.get_performance_stats_general()"
    if not isinstance(value, Mapping):
        raise ValueError("Nautilus stats_general must be a mapping")
    return value, source


def _portfolio_returns(analyzer: Any) -> list[dict[str, str]]:
    get_returns = getattr(analyzer, "portfolio_returns", None)
    if not callable(get_returns):
        raise ValueError("pinned Nautilus lacks public PortfolioAnalyzer.portfolio_returns()")
    series = get_returns()
    if not isinstance(series, pd.Series):
        raise ValueError("Nautilus portfolio_returns() must return a pandas Series")
    if series.empty:
        return []

    records: list[dict[str, str]] = []
    observed: set[str] = set()
    for raw_timestamp, raw_value in series.items():
        timestamp = _utc_timestamp(raw_timestamp)
        if timestamp in observed:
            raise ValueError(f"Nautilus portfolio returns contain duplicate timestamp {timestamp}")
        observed.add(timestamp)
        value = _required_decimal(raw_value, f"portfolio_returns[{timestamp}]")
        records.append({"timestamp": timestamp, "value": normalize_decimal(value)})
    return sorted(records, key=lambda item: item["timestamp"])


def _run_info(result: Any, unavailable: list[dict[str, str]]) -> dict[str, Any]:
    value: dict[str, Any] = {
        "trader_id": str(result.trader_id),
        "machine_id": str(result.machine_id),
        "run_config_id": _optional_string(result.run_config_id),
        "instance_id": str(result.instance_id),
        "run_id": str(result.run_id),
        "run_started": _optional_nanos(result.run_started, "run_info.run_started", unavailable),
        "run_finished": _optional_nanos(
            result.run_finished,
            "run_info.run_finished",
            unavailable,
        ),
        "backtest_start": _optional_nanos(
            result.backtest_start,
            "run_info.backtest_start",
            unavailable,
        ),
        "backtest_end": _optional_nanos(
            result.backtest_end,
            "run_info.backtest_end",
            unavailable,
        ),
        "elapsed_time_seconds": normalize_decimal(
            _required_decimal(result.elapsed_time, "run_info.elapsed_time_seconds")
        ),
        "iterations": _nonnegative_integer(result.iterations, "run_info.iterations"),
        "total_events": _nonnegative_integer(result.total_events, "run_info.total_events"),
        "total_orders": _nonnegative_integer(result.total_orders, "run_info.total_orders"),
        "total_positions": _nonnegative_integer(result.total_positions, "run_info.total_positions"),
    }
    for key in ("run_config_id",):
        if value[key] is None:
            _append_unavailable(unavailable, f"run_info.{key}", "native_value_unavailable")
    return value


def _account_info(account: Any | None, unavailable: list[dict[str, str]]) -> dict[str, str | None]:
    if account is None:
        return {}
    get_starting = getattr(account, "starting_balances", None)
    get_currencies = getattr(account, "currencies", None)
    get_ending = getattr(account, "balance_total", None)
    if not callable(get_starting) or not callable(get_currencies) or not callable(get_ending):
        raise ValueError("Nautilus account lacks public balance interfaces")
    starting = get_starting()
    currencies = get_currencies()
    if not isinstance(starting, Mapping) or not isinstance(currencies, list | tuple):
        raise ValueError("Nautilus account balance interfaces returned invalid data")

    result: dict[str, str | None] = {}
    starting_by_code = {str(currency): money for currency, money in starting.items()}
    currencies_by_code = {str(currency): currency for currency in currencies}
    for currency in sorted(set(starting_by_code) | set(currencies_by_code)):
        ending_money = (
            get_ending(currencies_by_code[currency]) if currency in currencies_by_code else None
        )
        balances = (
            ("Starting balance", starting_by_code.get(currency)),
            ("Ending balance", ending_money),
        )
        for label, money in balances:
            path = f"account_info.{label} ({currency})"
            if money is None:
                result[f"{label} ({currency})"] = None
                _append_unavailable(unavailable, path, "native_value_unavailable")
                continue
            as_decimal = getattr(money, "as_decimal", None)
            if not callable(as_decimal):
                raise ValueError(f"Nautilus {path} lacks a public decimal representation")
            amount = _required_decimal(as_decimal(), path)
            result[f"{label} ({currency})"] = f"{normalize_decimal(amount)} {currency}"
    return result


def _statistics_mapping(
    value: Any,
    path: str,
    unavailable: list[dict[str, str]],
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"Nautilus {path} must be a mapping")
    return {
        str(key): _optional_native_value(item, f"{path}.{key}", unavailable)
        for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
    }


def _optional_native_value(
    value: Any,
    path: str,
    unavailable: list[dict[str, str]],
) -> Any:
    if value is None:
        _append_unavailable(unavailable, path, "native_value_unavailable")
        return None
    if isinstance(value, Mapping):
        return {
            str(key): _optional_native_value(item, f"{path}.{key}", unavailable)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, list | tuple):
        return [
            _optional_native_value(item, f"{path}[{index}]", unavailable)
            for index, item in enumerate(value)
        ]
    if isinstance(value, bool | str):
        return value
    if isinstance(value, Integral):
        return int(value)
    if isinstance(value, Decimal | Real):
        decimal = Decimal(str(value))
        if not decimal.is_finite():
            _append_unavailable(unavailable, path, "native_non_finite")
            return None
        return normalize_decimal(decimal)
    raise ValueError(f"Nautilus {path} has unsupported value type {type(value).__name__}")


def _field_availability(
    prefix: str,
    value: Mapping[str, Any],
    unavailable: list[dict[str, str]],
) -> dict[str, str]:
    if not value:
        return {"status": "unavailable", "reason": "native_mapping_empty"}
    if any(item["path"] == prefix or item["path"].startswith(f"{prefix}.") for item in unavailable):
        return {"status": "partial", "reason": "native_values_unavailable"}
    return {"status": "available"}


def _required_decimal(value: Any, path: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"Nautilus {path} must be numeric")
    try:
        decimal = Decimal(str(value))
    except Exception as exc:
        raise ValueError(f"Nautilus {path} must be numeric") from exc
    if not decimal.is_finite():
        raise ValueError(f"Nautilus {path} must be finite")
    return decimal


def _nonnegative_integer(value: Any, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, Integral) or int(value) < 0:
        raise ValueError(f"Nautilus {path} must be a non-negative integer")
    return int(value)


def _utc_timestamp(value: Any) -> str:
    try:
        timestamp = pd.Timestamp(value)
    except Exception as exc:
        raise ValueError("Nautilus portfolio returns contain an invalid timestamp") from exc
    if pd.isna(timestamp):
        raise ValueError("Nautilus portfolio returns contain an invalid timestamp")
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize("UTC")
    else:
        timestamp = timestamp.tz_convert("UTC")
    return timestamp.isoformat().replace("+00:00", "Z")


def _optional_nanos(
    value: Any,
    path: str,
    unavailable: list[dict[str, str]],
) -> str | None:
    if value is None:
        _append_unavailable(unavailable, path, "native_value_unavailable")
        return None
    if isinstance(value, bool) or not isinstance(value, Integral) or int(value) < 0:
        raise ValueError(f"Nautilus {path} must be a non-negative nanosecond timestamp")
    return unix_nanos_to_iso8601(int(value))


def _optional_string(value: Any) -> str | None:
    return None if value is None else str(value)


def _append_unavailable(values: list[dict[str, str]], path: str, reason: str) -> None:
    item = {"path": path, "reason": reason}
    if item not in values:
        values.append(item)
