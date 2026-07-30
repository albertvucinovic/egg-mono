from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from eggflow import Task
from eggthreads import create_child_thread, create_root_thread

from ..identity import digest_payload
from ..runtime import Runtime as BaseRuntime
from ..runtime import sync


@dataclass
class Runtime(BaseRuntime):
    study_id: str
    validation_id: str
    reflection_id: str

    @classmethod
    def open(cls, root: str | Path) -> Runtime:
        base = BaseRuntime.open(root)
        try:
            study_id, validation_id, reflection_id = sync(
                base.flow.run(_CreateStudy(base.threads)), operation="GEPA"
            )
        except BaseException:
            base.close()
            raise
        return cls(
            base.root,
            base.store,
            base.flow,
            base.threads,
            base.runtime_key,
            study_id,
            validation_id,
            reflection_id,
        )


@dataclass
class _CreateStudy(Task):
    threads: object

    def get_cache_key(self) -> str:
        return digest_payload("eggopt.gepa.create-study.v2", {})

    def run(self) -> tuple[str, str, str]:
        study_id = create_root_thread(self.threads, name="GEPA")
        validation_id = create_child_thread(
            self.threads,
            study_id,
            name="Validation",
            inherit_tools_config=False,
        )
        mutation_review_id = create_child_thread(
            self.threads,
            study_id,
            name="Mutation Review",
            inherit_tools_config=False,
        )
        reflection_id = create_child_thread(
            self.threads,
            mutation_review_id,
            name="Reflection",
            inherit_tools_config=False,
        )
        return study_id, validation_id, reflection_id


__all__ = ["Runtime"]
