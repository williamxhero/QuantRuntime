from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from .schema import validate_instance


@dataclass(frozen=True, slots=True)
class SnapshotRequest:
    adapter: str
    snapshot_mode: str
    trust_policy: str
    local_cache: str
    endpoint_contract: str
    base_url: str
    instruments: tuple[str, ...]
    start: date
    end: date
    frequency: str
    adjustment: str
    calendar: str
    contract_mapping: str | None

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> SnapshotRequest:
        query = value.get("query")
        if not isinstance(query, dict):
            raise ValueError("data.query must be an object")
        request = cls(
            adapter=str(value.get("adapter", "markethub")),
            snapshot_mode=str(value.get("snapshot_mode", "reference")),
            trust_policy=str(value.get("trust_policy", "assumed_immutable")),
            local_cache=str(value.get("local_cache", "none")),
            endpoint_contract=str(value.get("endpoint_contract", "v2")),
            base_url=str(value.get("base_url", "http://yosef-server:8803")).rstrip("/"),
            instruments=tuple(str(item) for item in query.get("instruments", [])),
            start=date.fromisoformat(str(query.get("start", ""))),
            end=date.fromisoformat(str(query.get("end", ""))),
            frequency=str(query.get("frequency", "1d")),
            adjustment=str(query.get("adjustment", "none")),
            calendar=str(query.get("calendar", "cn-equity-v1")),
            contract_mapping=(
                str(query["contract_mapping"]) if query.get("contract_mapping") else None
            ),
        )
        request.validate()
        return request

    def validate(self) -> None:
        if self.adapter != "markethub":
            raise ValueError("MarketHub is the only production data adapter")
        if self.snapshot_mode not in {"reference", "materialized"}:
            raise ValueError("snapshot_mode must be reference or materialized")
        if self.trust_policy not in {"assumed_immutable", "verified_immutable", "mutable"}:
            raise ValueError("invalid trust_policy")
        if self.local_cache not in {"none", "ephemeral", "persistent"}:
            raise ValueError("invalid local_cache")
        if self.start > self.end or not self.instruments:
            raise ValueError("snapshot query requires instruments and an ordered date range")
        if self.instruments != tuple(sorted(set(self.instruments))):
            raise ValueError("snapshot instruments must be unique and canonical-order sorted")
        if self.snapshot_mode == "materialized" and self.trust_policy == "mutable":
            raise ValueError("materialized snapshots cannot be mutable")

    def identity_payload(self) -> dict[str, Any]:
        return {
            "schema": "quant-research.market-snapshot-request.v1",
            "source": {
                "adapter": self.adapter,
                "endpoint_contract": self.endpoint_contract,
                "base_url": self.base_url,
            },
            "query": {
                "instruments": list(self.instruments),
                "start": self.start.isoformat(),
                "end": self.end.isoformat(),
                "frequency": self.frequency,
                "adjustment": self.adjustment,
            },
            "calendar": self.calendar,
            "contract_mapping": self.contract_mapping,
        }


def validate_snapshot_manifest(value: dict[str, Any]) -> None:
    mode = value.get("mode")
    name = "market-snapshot-ref.v1" if mode == "reference" else "market-snapshot.v1"
    validate_instance(name, value)
