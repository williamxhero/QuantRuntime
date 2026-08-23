# Working rules

- This repository is an independent Qlib workspace. It must remain runnable without any research control-plane package.
- MarketHub is the only production market-data source. Reads must freeze and verify its published versions and fail closed on incomplete delivery.
- Do not persist a reusable local market-data cache or mirror. Runtime outputs contain research artifacts, not a data lake.
- Prefer upstream Qlib capabilities over reimplementing equivalent discovery, evaluation, recorder, or model functionality.
- Connected tests must report service/data-gate blockers honestly; never replace unavailable live data with a fixture.
- Test fixtures are contract fixtures only and must be labelled as such. They are not claims about current MarketHub truth.
- Keep runtime output out of Git. Review the staged file list before each commit.
