"""Independent MarketHub-backed Qlib discovery workspace."""

from .client import MarketHubClient, MarketHubContractError

__all__ = ["MarketHubClient", "MarketHubContractError"]
__version__ = "0.1.0"
