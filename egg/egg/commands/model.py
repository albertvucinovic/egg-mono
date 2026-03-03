"""Model-related command mixins for the egg application."""
from __future__ import annotations

import os
from typing import Any, Dict, List

from eggthreads import create_snapshot, current_thread_model_info

from ..utils import MODELS_PATH, ALL_MODELS_PATH


class ModelCommandsMixin:
    """Mixin providing model-related commands: /model, /updateAllModels."""

    def cmd_model(self, arg: str) -> None:
        """Handle /model command - set or display model configuration."""
        arg2 = (arg or '').strip()
        if arg2:
            # Record a model.switch event as the authoritative source
            # of model selection for this thread and append a
            # user-level notification that is excluded from LLM
            # context (no_api=True) but visible in the transcript.
            from eggthreads import set_thread_model  # type: ignore
            set_thread_model(self.db, self.current_thread, arg2, reason='ui /model', models_path=str(MODELS_PATH))
            self.db.append_event(
                event_id=os.urandom(10).hex(),
                thread_id=self.current_thread,
                type_='msg.create',
                msg_id=os.urandom(10).hex(),
                payload={
                    'role': 'user',
                    'content': f"/model {arg2}",
                    'no_api': True,
                },
            )
            create_snapshot(self.db, self.current_thread)
            # Get concrete configuration and display a static view box
            concrete = current_thread_model_info(self.db, self.current_thread)
            formatted = self.format_model_info(concrete, arg2)
            self.log_system('Model set (see console for full).')
            self.console_print_block('Model', formatted, border_style='blue')
        else:
            # Show current model configuration
            cur_model = self.current_model_for_thread(self.current_thread)
            if cur_model:
                concrete = current_thread_model_info(self.db, self.current_thread)
                formatted = self.format_model_info(concrete, cur_model)
                self.log_system("Current model configuration (see console).")
                self.console_print_block("Model", formatted, border_style="blue")
            else:
                self.log_system("No model selected for this thread.")

            try:
                llm = self.llm_client
                if not llm:
                    self.log_system('Models not available (llm client not initialized).')
                else:
                    by_provider: Dict[str, List[str]] = {}
                    for name, cfg in (llm.registry.models_config or {}).items():
                        prov = cfg.get('provider', 'unknown')
                        by_provider.setdefault(prov, []).append(name)
                    lines = []
                    for prov in sorted(by_provider.keys()):
                        lines.append(f"{prov}:")
                        for m in sorted(by_provider[prov]):
                            lines.append(f"  - {m}")
                    lines.append("\nTip: use 'all:provider:model' to pick catalog models.")
                    self.log_system("Available models:\n" + "\n".join(lines))
            except Exception as e:
                self.log_system(f"Error listing models: {e}")

    def cmd_updateAllModels(self, arg: str) -> None:
        """Handle /updateAllModels command - refresh model catalog for a provider."""
        provider = (arg or '').strip()
        if not provider:
            self.log_system('Usage: /updateAllModels <provider>')
        else:
            try:
                # Prefer to use the long-lived LLM client instance
                # so that its in-memory AllModelsCatalog is updated
                # and autocomplete (/model all:...<tab>) immediately
                # sees the new models. If no client is available in
                # this UI, fall back to a temporary one.
                if self.llm_client is not None:
                    res = self.llm_client.update_all_models(provider)
                else:
                    from eggllm import LLMClient
                    if not LLMClient:
                        raise RuntimeError('eggllm not available')
                    llm_tmp = LLMClient(models_path=MODELS_PATH, all_models_path=ALL_MODELS_PATH)
                    res = llm_tmp.update_all_models(provider)
                self.log_system("Update All Models:\n" + res)
            except Exception as e:
                self.log_system(f"Update All Models error: {e}")
