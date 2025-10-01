"""
Update all-models.json for a provider.

Usage:
  export OPENROUTER_API_KEY=...
  python examples/update_catalog.py openrouter
"""
import sys
from pathlib import Path
from egg_llm import LLMClient

HERE = Path(__file__).resolve().parent
MODELS = HERE.parent / "models.json"
ALL = HERE.parent / "all-models.json"

def main():
    if len(sys.argv) < 2:
        print("Usage: update_catalog.py <provider>")
        sys.exit(1)
    provider = sys.argv[1]
    llm = LLMClient(models_path=MODELS, all_models_path=ALL)
    print(llm.update_all_models(provider))

if __name__ == "__main__":
    main()
