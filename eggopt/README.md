# eggopt

`eggopt` is a dependency-free, domain-neutral core for composing optimization
work in the Egg ecosystem. It represents arbitrary text candidates, typed
producers, structured evidence, and one-step strategy transitions without
assigning meaning to candidate text, metrics, feedback, or cases.

The pure core does not execute evaluations or effects. Domain adapters express
strategy, mutation, solving, evaluation, aggregation, and judging as
`Producer` roles. Eggflow and Eggthreads integrations live outside this core.

## Install

`eggopt` requires Python 3.10 or newer:

```bash
python -m pip install -e ./eggopt
```

For development and focused tests:

```bash
python -m pip install -e './eggopt[dev]'
python -m pytest eggopt/tests -q
```

## Minimal example

```python
from eggopt import Candidate, FunctionProducer

normalize = FunctionProducer(lambda candidate: Candidate(candidate.text.strip()))
render = FunctionProducer(lambda candidate: candidate.text)
pipeline = normalize.then(render)

assert pipeline.produce(Candidate("  policy  ")) == "policy"
```
