from __future__ import annotations

import nautilus_trader
import qlib

from quant_runtime.adapters.discovery.qlib import QlibDiscoveryAdapter
from quant_runtime.adapters.formal.nautilus import NautilusWorkspaceAdapter
from quant_runtime.sdk.capability_contract import AdapterRegistry, CapabilityProfile


def production_registry() -> AdapterRegistry:
    registry = AdapterRegistry()
    registry.register(
        CapabilityProfile.from_dict(
            {
                "schema": "quant-research.runtime-capability.v1",
                "backend_id": "qlib",
                "role": "discovery",
                "adapter_version": "1.0.0",
                "engine_version": qlib.__version__,
                "provides": [
                    "data.bar.1d",
                    "decision.target_weight",
                    "discovery.cross_sectional",
                    "evidence.qlib_native",
                    "market.cn.equity",
                ],
            }
        ),
        QlibDiscoveryAdapter,
    )
    registry.register(
        CapabilityProfile.from_dict(
            {
                "schema": "quant-research.runtime-capability.v1",
                "backend_id": "nautilus",
                "role": "formal",
                "adapter_version": "1.0.0",
                "engine_version": nautilus_trader.__version__,
                "provides": [
                    "data.bar.1d",
                    "decision.target_weight",
                    "evidence.engine_native",
                    "market.cn.equity",
                    "market.cn.equity.board_lot",
                    "market.cn.equity.price_limit",
                    "market.cn.equity.suspension",
                    "market.cn.equity.t_plus_one",
                    "order.market",
                    "portfolio.multi_instrument",
                    "replay.deterministic",
                    "run.backtest",
                    "strategy.stateful",
                ],
            }
        ),
        NautilusWorkspaceAdapter,
    )
    return registry
