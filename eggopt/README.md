# eggopt

`eggopt` is the domain-neutral optimization core for the Egg monorepo. It contains
immutable candidate/evidence/decision values, a composable `Producer[Input, Output]`
contract, deterministic function producers, and pure GEPA and physics-strategy
transitions.

The package deliberately has no runtime dependencies. It does not perform model calls,
repair, persistence, or domain evaluation. Those effects belong in later Eggthreads,
Eggflow, and application adapters.

```python
from eggopt import Candidate, GEPAState, GEPAStrategy, Observation

strategy = GEPAStrategy()
decision = strategy.advance(
    (Observation(Candidate("draft"), score=0.5, feedback="be more precise"),),
    GEPAState(max_generations=2),
)
proposal = decision.proposals[0]
```

Run the focused suite with `pytest eggopt/tests -q`.
