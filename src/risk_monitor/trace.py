"""Console tracer: prints each stage of an agent run to stderr, optionally pausing between stages.

Hooks into Strands' lifecycle events, so the agent code itself is unchanged.
"""

import json
import sys
import time

from strands.hooks import (
    AfterInvocationEvent,
    AfterModelCallEvent,
    AfterToolCallEvent,
    BeforeInvocationEvent,
    BeforeModelCallEvent,
    BeforeToolCallEvent,
    HookProvider,
    HookRegistry,
)

_COLOR = sys.stderr.isatty()


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOR else text


def _short(value, limit: int = 600) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return text if len(text) <= limit else text[:limit] + _c("2", f" … (+{len(text) - limit} chars)")


class ConsoleTracer(HookProvider):
    def __init__(self, step: bool = False):
        self.step = step
        self.n = 0
        self.model_calls = 0
        self.start = 0.0

    def register_hooks(self, registry: HookRegistry, **kwargs) -> None:
        registry.add_callback(BeforeInvocationEvent, self.on_start)
        registry.add_callback(BeforeModelCallEvent, self.before_model)
        registry.add_callback(AfterModelCallEvent, self.after_model)
        registry.add_callback(BeforeToolCallEvent, self.before_tool)
        registry.add_callback(AfterToolCallEvent, self.after_tool)
        registry.add_callback(AfterInvocationEvent, self.on_end)

    def _header(self, title: str) -> None:
        self.n += 1
        print(_c("1;36", f"\n── Step {self.n}: {title} " + "─" * max(0, 50 - len(title))), file=sys.stderr)

    def _line(self, text: str = "") -> None:
        print(f"   {text}", file=sys.stderr)

    def _pause(self) -> None:
        if self.step:
            print(_c("2", "   ⏎ press Enter to continue…"), end="", file=sys.stderr, flush=True)
            try:
                input()
            except EOFError:
                self.step = False

    def on_start(self, event: BeforeInvocationEvent) -> None:
        self.start = time.monotonic()
        self._header("Agent started")
        txns = event.invocation_state.get("transactions", {})
        customers = {t.customer_id for t in txns.values()}
        self._line(f"input validated: {len(txns)} transactions from {len(customers)} customers")
        self._line(f"model: {event.agent.model.get_config().get('model_id', '?')}")
        self._line(f"tools available to the LLM: {', '.join(event.agent.tool_names)}")
        self._pause()

    def before_model(self, event: BeforeModelCallEvent) -> None:
        self.model_calls += 1
        self._header(f"Asking the LLM (call #{self.model_calls})")
        self._line(f"sending {len(event.agent.messages)} message(s) of conversation history")
        self._pause()

    def after_model(self, event: AfterModelCallEvent) -> None:
        if event.exception:
            self._line(_c("31", f"✗ LLM call failed: {event.exception}"))
            return
        resp = event.stop_response
        self._line(_c("33", f"LLM replied (stop_reason={resp.stop_reason}):"))
        for block in resp.message["content"]:
            if "text" in block and block["text"].strip():
                self._line(_c("37", "💬 " + _short(block["text"].strip())))
            elif "toolUse" in block:
                tu = block["toolUse"]
                self._line(_c("35", f"🔧 wants to call {tu['name']}({_short(tu['input'], 300)})"))

    def before_tool(self, event: BeforeToolCallEvent) -> None:
        tu = event.tool_use
        self._header(f"Running tool {tu['name']}")
        self._line(f"input: {_short(tu['input'], 400)}")
        self._pause()

    def after_tool(self, event: AfterToolCallEvent) -> None:
        result = event.result
        if isinstance(result, Exception):
            self._line(_c("31", f"✗ tool raised {type(result).__name__}: {result}"))
            return
        text = " ".join(c.get("text", "") for c in result.get("content", []))
        try:
            failed = bool(json.loads(text).get("error"))
        except (ValueError, AttributeError):
            failed = False
        ok = result.get("status") == "success" and not failed
        mark = _c("32", "✓") if ok else _c("31", "✗")
        took = f" in {event.duration:.2f}s" if event.duration is not None else ""
        self._line(f"{mark} result{took}: {_short(text)}")

    def on_end(self, event: AfterInvocationEvent) -> None:
        self._header("Agent finished")
        self._line(f"{self.model_calls} LLM call(s), {time.monotonic() - self.start:.1f}s total")
