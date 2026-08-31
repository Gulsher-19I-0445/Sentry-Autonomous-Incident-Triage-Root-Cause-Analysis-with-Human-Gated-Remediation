"""Bedrock client — Converse API with tool use.
 
Deliberately not a framework. The agent loop here is ~60 lines and fully
visible, which matters because the trace view and the eval harness both need to
inspect every tool call the model makes. A framework would hide exactly the
thing this project is measuring.
 
Encodes the failures already hit in this account:
  ValidationException + "inference profile" -> bare model id used
  AccessDeniedException                     -> model access not granted
  ThrottlingException                       -> quota; back off and retry
"""


from ..common.logging import get_logger, log_event
import boto3
import time
import random
import json
from .config import Config
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError
from dataclasses import dataclass, field
from typing import Any, Callable

logger = get_logger("bedrock")
# config = Config()

_client = boto3.client("bedrock-runtime", region_name=Config.REGION, config=BotoConfig(retries={"max_attempts":0}, read_timeout= 120 ))

class ModelAccessError(Exception):
    "Not retryable-model access to or id is wrong"


# class MaxStepsExceeded(Exception):
#     "Agent stuck in loop without conclusion"

class MaxStepsExceeded(Exception):
    def __init__(self, message: str, run: "AgentRun | None" = None):
        super().__init__(message)
        self.run = run

@dataclass
class ToolCall:
    name: str
    input: dict
    result: Any = None
    error: str | None = None
    duration_ms: int = 0

@dataclass
class AgentRun:
    """Everything one investigation did. This IS the trace the dashboard renders."""
    steps: list[ToolCall] = field(default_factory=list)
    final_text: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    stop_reason: str = ""
    duration_ms: int = 0
    model_id: str = ""
 
    @property
    def cost_usd(self) -> float:
        p = Config.price_for(self.model_id)
        return (self.input_tokens / 1e6) * p["input"] + (self.output_tokens / 1e6) * p["output"]
 
    def to_dict(self) -> dict:
        return {
            "steps": [
                {"tool": s.name, "input": s.input, "error": s.error,
                 "duration_ms": s.duration_ms,
                 "result_preview": str(s.result)[:500] if s.result else None}
                for s in self.steps
            ],
            "final_text": self.final_text,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost_usd": round(self.cost_usd, 6),
            "duration_ms": self.duration_ms,
            "stop_reason": self.stop_reason,
            "model_id": self.model_id,
            "step_count": len(self.steps),
        }


def _converse(model_id: str, messages: list[dict], system: str,
              tools: list[dict] | None, max_attempts: int = 5) -> dict:
    """One Converse call, with backoff on throttling."""
    inference_config: dict[str, Any] = {"maxTokens": Config.MAX_TOKENS}
    if Config.TEMPERATURE is not None and "sonnet-5" not in model_id:
        inference_config["temperature"] = Config.TEMPERATURE
    kwargs: dict[str, Any] = {
        "modelId": model_id,
        "messages": messages,
        "system": [{"text": system}],
        "inferenceConfig": inference_config,
    }
    if tools:
        kwargs["toolConfig"] = {"tools": tools}
 
    for attempt in range(max_attempts):
        try:
            return _client.converse(**kwargs)
        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            msg = exc.response["Error"]["Message"]
 
            if code == "ThrottlingException":
                if attempt == max_attempts - 1:
                    raise
                delay = (2 ** attempt) + random.random()
                log_event(logger, "warning", "throttled, backing off",
                          attempt=attempt + 1, delay_s=round(delay, 2))
                time.sleep(delay)
                continue
 
            if code == "ValidationException" and "inference profile" in msg.lower():
                raise ModelAccessError(
                    f"{model_id} requires an inference profile — use the 'us.' prefixed id"
                ) from exc
 
            if code in ("AccessDeniedException", "ResourceNotFoundException"):
                raise ModelAccessError(
                    f"{model_id} unavailable: {msg} — check Bedrock model access "
                    f"and that the Anthropic use-case form is submitted"
                ) from exc
 
            raise
 
    raise RuntimeError("unreachable")


def run_agent(
    system_prompt: str,
    user_prompt: str,
    tools: list[dict],
    executor: Callable[[str, dict], Any],
    model_id: str | None = None,
    max_steps: int | None = None,
) -> AgentRun:
    """Run the tool-use loop until the model stops asking for tools.
 
    `tools`    — Converse toolSpec definitions
    `executor` — called as executor(tool_name, tool_input) -> result
    """
    model_id = model_id or Config.AGENT_MODEL_ID
    max_steps = max_steps or Config.MAX_AGENT_STEPS
 
    run = AgentRun(model_id=model_id)
    started = time.time()
    messages: list[dict] = [{"role": "user", "content": [{"text": user_prompt}]}]
 
    for step in range(max_steps):
        response = _converse(model_id, messages, system_prompt, tools)
 
        usage = response.get("usage", {})
        run.input_tokens += usage.get("inputTokens", 0)
        run.output_tokens += usage.get("outputTokens", 0)
        run.stop_reason = response.get("stopReason", "")
        content = response["output"]["message"]["content"]
        messages.append({"role": "assistant", "content": content})
 
        tool_uses = [c["toolUse"] for c in content if "toolUse" in c]
 
        if not tool_uses:
            run.final_text = "".join(c.get("text", "") for c in content)
            run.duration_ms = int((time.time() - started) * 1000)
            log_event(logger, "info", "agent finished",
                      steps=len(run.steps), stop_reason=run.stop_reason,
                      input_tokens=run.input_tokens, output_tokens=run.output_tokens,
                      cost_usd=round(run.cost_usd, 6))
            if run.stop_reason == "max_tokens":
                      raise MaxStepsExceeded(
                          "model output truncated before producing a complete RCA — raise MAX_TOKENS"
                      )
            return run
 
        results = []
        for use in tool_uses:
            call = ToolCall(name=use["name"], input=use.get("input", {}))
            t0 = time.time()
            try:
                call.result = executor(call.name, call.input)
                block = {"json": {"result": call.result}} if isinstance(call.result, (dict, list)) \
                    else {"text": str(call.result)}
                status = "success"
            except Exception as exc:  # tool errors are evidence, not crashes
                call.error = f"{type(exc).__name__}: {exc}"
                block = {"text": call.error}
                status = "error"
                log_event(logger, "warning", "tool call failed",
                          tool=call.name, error=call.error)
 
            call.duration_ms = int((time.time() - t0) * 1000)
            run.steps.append(call)
            results.append({
                "toolResult": {
                    "toolUseId": use["toolUseId"],
                    "content": [block],
                    "status": status,
                }
            })
 
        messages.append({"role": "user", "content": results})
 
    run.duration_ms = int((time.time() - started) * 1000)
    raise MaxStepsExceeded(f"no conclusion after {max_steps} steps")
 
 
def extract_json(text: str) -> dict:
    """Pull a JSON object out of a model response, fenced or bare."""
    cleaned = text.strip()
    if "```" in cleaned:
        parts = cleaned.split("```")
        for part in parts:
            candidate = part.removeprefix("json").strip()
            if candidate.startswith("{"):
                cleaned = candidate
                break
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"no JSON object found in response: {text[:200]}")
    return json.loads(cleaned[start:end + 1])