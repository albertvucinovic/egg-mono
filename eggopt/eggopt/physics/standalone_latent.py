"""Trusted evaluator for latent Physics world models.

This file is executed inside the Critic's isolated tool sandbox. It deliberately
uses only the Python standard library and communicates through durable JSON
request/report files.
"""

import copy
import hashlib
import importlib.util
import json
from pathlib import Path

EXPECTED_MODEL_ERRORS = (
    TypeError,
    ValueError,
    RuntimeError,
    KeyError,
    AttributeError,
)


def _json_value(value, *, name):
    try:
        encoded = json.dumps(value, allow_nan=False, sort_keys=True)
        decoded = json.loads(encoded)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be finite JSON") from exc
    return decoded


def _pure(function, *values, name):
    first_arguments = copy.deepcopy(values)
    first_before = copy.deepcopy(first_arguments)
    first = function(*first_arguments)
    if first_arguments != first_before:
        raise ValueError(f"{name} must not mutate its arguments")

    second_arguments = copy.deepcopy(values)
    second_before = copy.deepcopy(second_arguments)
    second = function(*second_arguments)
    if second_arguments != second_before:
        raise ValueError(f"{name} must not mutate its arguments")

    first = _json_value(first, name=f"{name} result")
    second = _json_value(second, name=f"{name} result")
    if first != second:
        raise ValueError(f"{name} must be deterministic")
    return first


def _module(source, work_dir):
    work = Path(work_dir)
    work.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(source.encode()).hexdigest()[:12]
    path = work / f"world_model_{digest}.py"
    path.write_text(source)
    spec = importlib.util.spec_from_file_location(f"physics_latent_model_{digest}", path)
    if spec is None or spec.loader is None:
        raise ValueError("world model could not be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _canonical_evidence(timeline):
    if not isinstance(timeline, list) or not timeline:
        raise ValueError("canonical evidence must contain a non-empty timeline")
    return _json_value({"timeline": timeline}, name="canonical evidence")


def _current_public_state(evidence):
    final = evidence["timeline"][-1]
    if not isinstance(final, dict):
        raise TypeError("the latest canonical timeline item must be an object")
    return final.get("next_state", final)


def _canonical_plan(value):
    if not isinstance(value, dict) or set(value) != {"model", "actions"}:
        raise ValueError("latent plan must contain exactly model and actions")
    model = value["model"]
    actions = value["actions"]
    if not isinstance(model, str) or not model or not model.isidentifier():
        raise ValueError("latent plan model must be a non-empty identifier suffix")
    if not isinstance(actions, list) or not actions:
        raise ValueError("latent plan actions must be a non-empty list")
    return {
        "model": model,
        "actions": [_json_value(action, name="plan action") for action in actions],
    }


def _interface(module, model, *, verified):
    encode = getattr(module, f"encode_{model}", None)
    step = getattr(module, f"step_{model}", None)
    observe = getattr(module, f"observe_{model}", None)
    if not callable(encode) or not callable(step):
        raise TypeError(
            f"selected model {model!r} must define encode_{model} and step_{model}"
        )
    if verified and not callable(observe):
        raise TypeError(f"verified latent model must define observe_{model}")
    return encode, step, observe


def _available_models(module, *, verified):
    names = {
        name.removeprefix("encode_")
        for name in dir(module)
        if name.startswith("encode_") and callable(getattr(module, name))
    }
    return sorted(
        name
        for name in names
        if callable(getattr(module, f"step_{name}", None))
        and (not verified or callable(getattr(module, f"observe_{name}", None)))
    )


def evaluate_proposal(request):
    verified = bool(request.get("verified"))
    evidence = _canonical_evidence(request["timeline"])
    plan = _canonical_plan(request["plan"])
    module = _module(request["source"], request.get("work_dir", ".physics-latent"))
    models = _available_models(module, verified=verified)
    if plan["model"] not in models:
        qualifier = "encode/step/observe" if verified else "encode/step"
        raise ValueError(
            f"selected model {plan['model']!r} has no complete {qualifier} interface"
        )
    encode, step, observe = _interface(module, plan["model"], verified=verified)

    latent = _pure(encode, evidence, name=f"encode_{plan['model']}")
    latent_states = [latent]
    public_states = []
    if verified:
        public = _pure(observe, latent, name=f"observe_{plan['model']}")
        current = _json_value(_current_public_state(evidence), name="current public state")
        if public != current:
            raise ValueError("observe(initial latent state) must equal current public state")
        public_states.append(public)

    for action in plan["actions"]:
        latent = _pure(step, latent, action, name=f"step_{plan['model']}")
        latent_states.append(latent)
        if verified:
            public_states.append(
                _pure(observe, latent, name=f"observe_{plan['model']}")
            )

    return {
        "valid": True,
        "verified": verified,
        "available_models": models,
        "model": plan["model"],
        "actions": plan["actions"],
        "latent_states": latent_states,
        "public_states": public_states,
    }


def evaluate_observation(request):
    verified = bool(request.get("verified"))
    evidence = _canonical_evidence(request["timeline"])
    model = request["model"]
    if not isinstance(model, str) or not model:
        raise ValueError("observation request model must be non-empty")
    module = _module(request["source"], request.get("work_dir", ".physics-latent"))
    encode, _step, observe = _interface(module, model, verified=verified)
    latent = _pure(encode, evidence, name=f"encode_{model}")
    expected_latent = _json_value(request["expected_latent"], name="expected latent")
    document = {
        "valid": True,
        "latent": latent,
        "expected_latent": expected_latent,
        "latent_matches": latent == expected_latent,
    }
    if verified:
        public = _pure(observe, latent, name=f"observe_{model}")
        expected_public = _json_value(
            request["expected_public"], name="expected public state"
        )
        document.update(
            {
                "public": public,
                "expected_public": expected_public,
                "public_matches": public == expected_public,
            }
        )
    return document


def evaluate_request(request):
    kind = request.get("kind", "proposal")
    if kind == "proposal":
        return evaluate_proposal(request)
    if kind == "observation":
        return evaluate_observation(request)
    raise ValueError(f"unknown latent evaluator request kind: {kind}")


def _write_json(path, value):
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def emit_evaluator_result(request):
    try:
        document = evaluate_request(request)
    except EXPECTED_MODEL_ERRORS as exc:
        document = {"valid": False, "error": str(exc)}
    output = request["output_path"]
    _write_json(output, document)
    print(
        "__EGG_PHYSICS_LATENT_REPORT__"
        + json.dumps({"path": output}, sort_keys=True)
    )


if __name__ == "__main__":
    trusted_request = globals().get("_EGG_PHYSICS_REQUEST")
    if trusted_request is None:
        raise SystemExit("standalone_latent.py requires a trusted request")
    emit_evaluator_result(trusted_request)
