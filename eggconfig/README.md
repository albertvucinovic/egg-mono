# eggconfig

`eggconfig` packages the shared JSON configuration used by Egg clients and
libraries. It has no runtime dependencies and exposes stable filesystem paths
instead of loading or interpreting the files itself.

## Bundled data

| File | Purpose |
| --- | --- |
| `models.json` | Providers, model display keys, API model ids, capabilities, request parameters, context limits, and costs |
| `all-models.json` | Cached provider catalogs used for broader model selection and autocomplete |
| `image-generation-models.json` | Image-generation providers, models, API types, sizes, quality, and output options |

Secrets are never stored in these files. Provider entries name environment
variables such as `OPENAI_API_KEY`; credentials remain in the environment or an
auth store.

## Install

```bash
pip install -e ./eggconfig
```

Python 3.10+ is required.

## Public API

```python
from eggconfig import (
    get_all_models_path,
    get_image_generation_models_path,
    get_models_path,
)

models_path = get_models_path()
catalog_path = get_all_models_path()
image_models_path = get_image_generation_models_path()
```

Each function returns a `pathlib.Path` to package data. Pass those paths to
`eggllm`, `eggthreads`, or a client; use the owning library's loader rather than
duplicating its schema interpretation.

## Overrides

The bundled files are defaults. EggW resolves project-local files and explicit
`EGG_MODELS_PATH`, `EGG_ALL_MODELS_PATH`, and
`EGG_IMAGE_GENERATION_MODELS_PATH` overrides. The terminal client currently
uses the packaged chat/catalog paths and lets `eggllm` resolve the image-config
path. Model credentials and the initial model are configured separately; see
[`dot.env.example`](../dot.env.example) and the
[root configuration guide](../README.md#configuration).

## Updating data

Keep model kinds, API types, capability metadata, endpoint parameters, and
credential variable names consistent with `eggllm`. Do not add real keys or
local secrets. Validate consumers after changing configuration:

```bash
pytest -q eggllm/tests eggthreads/tests egg/tests eggw/tests
```

Related documentation:

- [eggllm provider router](../eggllm/README.md)
- [Egg terminal client](../egg/README.md)
- [EggW web client](../eggw/README.md)
