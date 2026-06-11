# Infinite Turn Skill

Use this skill when an assistant should keep an interaction open instead of ending with a normal final assistant message.

## Core rule

Do not give the final answer in the normal way.

When you think the turn is over, call:

```text
get_user_message_while_preserving_llm_turn(assistant_note=<your would-be final answer>)
```

The `assistant_note` should contain the complete message you would otherwise have sent as the final answer: status, commits, tests, blockers, and the next recommended action.

Then wait for the next user message returned by the tool and continue from that message inside the preserved LLM/tool turn.

## Mid-turn user messages

If the user sends any message while you are still working, answer it with:

```text
answer_user_while_preserving_llm_turn(message=<your interim answer>)
```

Then continue the current work.

## Practical expectations

- Treat every natural handoff/status report as an `assistant_note` passed to `get_user_message_while_preserving_llm_turn`.
- Use normal tool calls for work, tests, inspection, and implementation.
- Use `answer_user_while_preserving_llm_turn` only for interim communication while work continues.
- If the user asks you to exit infinite-turn behavior, follow that explicit instruction.
