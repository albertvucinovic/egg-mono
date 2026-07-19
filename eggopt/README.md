# eggopt

`eggopt` is a dependency-free, domain-neutral core for composing optimization
work in the Egg ecosystem. It represents arbitrary text candidates, typed
producers, structured evidence, and one-step strategy transitions without
assigning meaning to candidate text, metrics, feedback, or cases.

The pure core does not execute evaluations or effects. Domain adapters express
strategy, mutation, solving, evaluation, aggregation, and judging as
`Producer` roles. Eggflow and Eggthreads integrations live outside this core.

See [`API.md`](API.md) for the stable concepts and optional module boundaries.

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

## Durable Eggflow adapter

Install the optional Eggflow integration with `eggopt[eggflow]`, then wrap any
synchronous deterministic Producer with an explicit stable identity:

```python
from eggflow import FlowExecutor, TaskStore
from eggopt import Candidate, FunctionProducer
from eggopt.eggflow import EggflowProducer

producer = FunctionProducer(lambda candidate: candidate.text.upper())
durable = EggflowProducer(producer, producer_identity="uppercase-candidate:v1")
task = durable.produce(Candidate("policy"))
# result = await FlowExecutor(TaskStore("flow.db")).run(task)
```

The identity is caller-owned semantic code-and-configuration identity; change
it whenever behavior or configuration changes. The task key also covers the
pickled input. This adapter is for deterministic, process-local Producers: do
not store live clients or schedulers in the wrapper, and do not expect them in
cached results. Runtime resources must be reconstructed outside cached values.
Import this optional adapter from `eggopt.eggflow`; importing `eggopt` itself
never imports Eggflow.

## Inspectable fake Eggthreads leaf

Install `eggopt[eggflow,eggthreads]` and import the optional substrate from
`eggopt.eggthreads`. `CreateRunRoots` durably creates a study root and strategy
child, while `ThreadProducer` creates a configured leaf and records a typed
fake drive's user/assistant transcript. Eggflow's committed `RunRoots` and
`ThreadOutput` values are authoritative; adapters never scan for threads by
name.

The injected drive is deterministic and process-local. Its explicit
`drive_identity` must change with behavior or configuration. Do not put live
clients, schedulers, or database connections in specs, inputs, or cached
results; reconstruct runtime resources outside these values. This P3 substrate
does not call a model or scheduler.

## Same-conversation repair

Pure repair values are available directly from `eggopt`; durable composition is
optional:

```python
from eggopt.eggflow_repair import RepairingProducer

repairing = RepairingProducer(
    inner=inner_producer,
    inner_identity="candidate-writer:v1",
    inspect=inspection_producer,
    inspect_identity="compile-and-test:v1",
    max_repairs=2,
)
task = repairing.produce(original_input)
```

Each expected invalid output becomes concrete cumulative `RepairFeedback` for
the same process-local inner Producer instance. Inner attempts and inspections
are independently cached. `Accepted` may carry a normalized value; exhaustion
or terminal context failure becomes an `ItemFailure` so a containing batch can
continue. Nonterminal infrastructure errors remain Eggflow failures. There is
no Check/Constraint hierarchy. Identities are caller-owned and must change with
behavior or configuration; live Producers are excluded from cache identity and
cached results.

## Evaluation composition

`EvaluationRequest` and `CaseRequest` are dependency-free ordered request
values. Optional `EvaluationProducer` from `eggopt.eggflow_evaluation` maps
candidate/case requests through one Producer and aggregates the resulting
`Observation` through another Producer. Per-case `CaseEvidence` is preserved
in full and in order; aggregation may only add aggregate metrics/feedback.

A domain case can represent one case or an indivisible batch. Deterministic,
sandboxed, soft, or composite evaluators remain ordinary Producers, including
Producers that return Eggflow Tasks. Explicit identities own behavior/config
cache identity; process-local Producer objects and live resources do not.
Objectives, archives, feasibility policy, and Pareto selection remain optional
and outside this composition.

## Generic hierarchical runtime

The optional `eggopt.eggthreads_runtime.HierarchicalRuntime` is the single
Eggopt-provided `Producer[StrategyRunInput, Task]` runtime. It materializes the
replay-safe `StudyRoot/StrategyRunRoot/RunSetup/Step S000/Proposal P000` seed
and subsequent serial steps/proposals, with bounded ordered case execution.
Every configured role receives a dependency-free `OperationInput` containing
its semantic value and authoritative physical thread context, then returns a
cached value paired with that thread ID. Operation threads retain digest-only,
model-hidden start/outcome audit messages. Candidate/case `ItemFailure` values
remain ordered item outcomes and do not abort sibling proposals or later
steps; infrastructure exceptions still fail. No registry, validation stage,
selector subthreads, raw-name scan, model call, or general descendant-context
REPL capability is included. Domains may implement the same top-level
structural Producer contract instead.
