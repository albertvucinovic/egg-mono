"""
Basic streaming example using egg_llm.

Usage:
  export OPENAI_API_KEY=...
  python examples/basic_stream.py

Assumes a models.json is available next to this script or pass a path.
"""
from pathlib import Path
import os
from egg_llm import LLMClient

HERE = Path(__file__).resolve().parent
MODELS = HERE.parent / "models.json.example"
ALL = HERE.parent / "all-models.json.example"

def main():
    llm = LLMClient(models_path=MODELS, all_models_path=ALL)
    # Choose a model by display name, alias, or provider:name
    # llm.set_model("openai:OpenAI GPT-4o")

    messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Name three fruits and one vegetable."},
    ]

    print("Streaming response:\n")
    for event in llm.stream_chat(messages):
        t = event.get("type")
        if t == "content_delta":
            print(event["text"], end="", flush=True)
        elif t == "done":
            print("\n\n---\nFinal message:\n", event["message"])

if __name__ == "__main__":
    main()
