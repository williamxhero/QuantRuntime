from __future__ import annotations

import inspect
import json
from decimal import Decimal
from typing import Any, get_type_hints

import nautilus_trader
import pandas as pd
import pytest
from nautilus_trader.accounting.accounts.base import Account as NautilusAccount
from nautilus_trader.analysis.analyzer import PortfolioAnalyzer
from nautilus_trader.backtest.results import BacktestResult
from nautilus_trader.model.currencies import CNY
from nautilus_trader.model.objects import Money

from quant_runtime.adapters.formal.nautilus.reporting_input import (
    REPORTING_INPUT_SCHEMA,
    SUPPORTED_NAUTILUS_VERSION,
    extract_reporting_input,
)


class Analyzer:
    def __init__(
        self,
        returns: pd.Series,
        *,
        stats_general: dict[str, object] | None = None,
    ) -> None:
        self._returns = returns
        self._stats_general = stats_general if stats_general is not None else {"Win Rate": 0.5}

    def portfolio_returns(self) -> pd.Series:
        return self._returns

    def get_performance_stats_general(self) -> dict[str, object]:
        return self._stats_general


class Account:
    def starting_balances(self) -> dict:
        return {CNY: Money.from_str("1000000 CNY")}

    def currencies(self) -> list:
        return [CNY]

    def balance_total(self, currency) -> Money:
        assert currency == CNY
        return Money.from_str("1001000 CNY")


def result(
    *,
    elapsed_time: float = 0.25,
    stats_returns: dict[str, float] | None = None,
) -> BacktestResult:
    return BacktestResult(
        trader_id="TRADER-001",
        machine_id="machine",
        run_config_id=None,
        instance_id="instance",
        run_id="run",
        run_started=1_735_776_000_000_000_000,
        run_finished=1_735_776_001_000_000_000,
        backtest_start=1_735_776_000_000_000_000,
        backtest_end=1_735_776_001_000_000_000,
        elapsed_time=elapsed_time,
        iterations=1,
        total_events=2,
        total_orders=0,
        total_positions=0,
        summary={"status": "completed"},
        stats_pnls={"CNY": {"PnL": 1000.0}},
        stats_returns=stats_returns or {"Sharpe Ratio (252 days)": 1.25},
    )


def test_pinned_nautilus_public_reporting_interfaces() -> None:
    assert nautilus_trader.__version__ == SUPPORTED_NAUTILUS_VERSION == "1.231.0"
    assert "stats_general" not in BacktestResult.__annotations__
    assert (
        get_type_hints(PortfolioAnalyzer.get_performance_stats_general)["return"] == dict[str, Any]
    )
    assert "general" in PortfolioAnalyzer.get_performance_stats_general.__doc__.lower()
    assert get_type_hints(PortfolioAnalyzer.portfolio_returns)["return"] is pd.Series
    assert list(inspect.signature(PortfolioAnalyzer.portfolio_returns).parameters) == ["self"]
    assert list(inspect.signature(NautilusAccount.starting_balances).parameters) == ["self"]
    assert list(inspect.signature(NautilusAccount.currencies).parameters) == ["self"]
    assert list(inspect.signature(NautilusAccount.balance_total).parameters) == [
        "self",
        "currency",
    ]


def test_reporting_input_is_sorted_finite_versioned_and_uses_public_sources() -> None:
    returns = pd.Series(
        [Decimal("0.02"), Decimal("-0.01")],
        index=[pd.Timestamp("2025-01-03"), pd.Timestamp("2025-01-02", tz="Asia/Shanghai")],
        dtype="object",
    )

    value = extract_reporting_input(result=result(), analyzer=Analyzer(returns), account=Account())

    assert value["schema"] == REPORTING_INPUT_SCHEMA
    assert value["portfolio_returns"] == [
        {"timestamp": "2025-01-01T16:00:00Z", "value": "-0.01"},
        {"timestamp": "2025-01-03T00:00:00Z", "value": "0.02"},
    ]
    assert value["stats_general"] == {"Win Rate": "0.5"}
    assert value["run_info"]["run_started"] == "2025-01-02T00:00:00.000000000Z"
    assert value["account_info"] == {
        "Ending balance (CNY)": "1001000 CNY",
        "Starting balance (CNY)": "1000000 CNY",
    }
    assert value["extraction"]["interfaces"]["stats_general"] == (
        "PortfolioAnalyzer.get_performance_stats_general()"
    )
    assert value["extraction"]["interfaces"]["portfolio_returns"] == (
        "PortfolioAnalyzer.portfolio_returns()"
    )
    json.dumps(value, allow_nan=False)


def test_public_result_stats_general_is_preferred_when_available() -> None:
    native_result = result()
    native_result.stats_general = {"Source": "result"}

    value = extract_reporting_input(
        result=native_result,
        analyzer=Analyzer(pd.Series(dtype="float64"), stats_general={"Source": "analyzer"}),
        account=Account(),
    )

    assert value["stats_general"] == {"Source": "result"}
    assert value["extraction"]["interfaces"]["stats_general"] == ("BacktestResult.stats_general")


def test_empty_returns_and_optional_nonfinite_statistics_are_explicitly_unavailable() -> None:
    value = extract_reporting_input(
        result=result(stats_returns={"Sharpe Ratio (252 days)": float("nan")}),
        analyzer=Analyzer(pd.Series(dtype="float64"), stats_general={}),
        account=Account(),
    )

    assert value["portfolio_returns"] == []
    assert value["availability"]["portfolio_returns"] == {
        "status": "unavailable",
        "reason": "native_series_empty",
    }
    assert value["stats_returns"]["Sharpe Ratio (252 days)"] is None
    assert {
        "path": "stats_returns.Sharpe Ratio (252 days)",
        "reason": "native_non_finite",
    } in value["unavailable"]
    assert value["availability"]["stats_general"] == {
        "status": "unavailable",
        "reason": "native_mapping_empty",
    }


@pytest.mark.parametrize("bad_value", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_portfolio_returns_fail_closed(bad_value: float) -> None:
    returns = pd.Series([bad_value], index=[pd.Timestamp("2025-01-02", tz="UTC")])

    with pytest.raises(ValueError, match="portfolio_returns.*must be finite"):
        extract_reporting_input(result=result(), analyzer=Analyzer(returns), account=Account())


def test_duplicate_portfolio_return_timestamps_fail_closed() -> None:
    timestamp = pd.Timestamp("2025-01-02", tz="UTC")
    returns = pd.Series([0.01, 0.02], index=[timestamp, timestamp])

    with pytest.raises(ValueError, match="duplicate timestamp"):
        extract_reporting_input(result=result(), analyzer=Analyzer(returns), account=Account())


def test_nonfinite_required_run_info_fails_closed() -> None:
    with pytest.raises(ValueError, match="elapsed_time_seconds.*must be finite"):
        extract_reporting_input(
            result=result(elapsed_time=float("inf")),
            analyzer=Analyzer(pd.Series(dtype="float64")),
            account=Account(),
        )
