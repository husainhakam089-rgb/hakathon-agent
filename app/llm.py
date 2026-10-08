"""Swappable model layer.

The agent talks to the model only through `LLM.complete()` using a small,
provider-neutral message format. To switch provider (OpenAI, Gemini, a
hackathon platform...), write another class with the same `complete()` and
change `make_llm()`. Nothing else needs to change.

Neutral message format (what `agent.py` stores and sends):
    {"role": "user", "text": "..."}
    {"role": "assistant", "text": "...", "tool_calls": [ToolCall, ...], "raw": <provider data or None>}
    {"role": "tool", "results": [{"id": "...", "content": "<json string>", "is_error": bool}]}

`raw` lets a provider replay its own native content (e.g. Claude thinking
blocks) inside the current turn. The agent drops it when a turn is saved to
history, so providers must work without it too.
"""

import json
import os
import time
from dataclasses import dataclass, field

# Models that accept the server-side refusal fallback (`fallbacks: "default"`).
_FALLBACK_MODELS = {"claude-sonnet-5-5", "claude-opus-5-5", "claude-opus-5", "claude-fable-5-1"}


class LLMError(Exception):
    """Model call failed. `user_message` is safe to show to the shop owner (Arabic)."""

    def __init__(self, user_message: str, detail: str = ""):
        super().__init__(detail or user_message)
        self.user_message = user_message
        self.detail = detail


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict


@dataclass
class LLMResponse:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    raw: object = None  # provider-native assistant content, replayed within the turn
    refused: bool = False


class AnthropicLLM:
    def __init__(self, model: str | None = None, api_key: str | None = None,
                 effort: str | None = None, max_tokens: int = 16000):
        import anthropic

        self._anthropic = anthropic
        self.model = model or os.getenv("MODEL_NAME") or "claude-sonnet-5-5"
        self.effort = effort or os.getenv("EFFORT") or "medium"
        self.max_tokens = max_tokens
        # The SDK also resolves credentials itself when no key is passed.
        self.client = anthropic.Anthropic(api_key=api_key or os.getenv("ANTHROPIC_API_KEY") or None)

    # ----- format conversion -----

    @staticmethod
    def _to_api_messages(messages: list[dict]) -> list[dict]:
        out = []
        for m in messages:
            if m["role"] == "user":
                out.append({"role": "user", "content": m["text"]})
            elif m["role"] == "assistant":
                if m.get("raw") is not None:
                    content = m["raw"]  # native blocks incl. thinking, unchanged
                else:
                    content = []
                    if m.get("text"):
                        content.append({"type": "text", "text": m["text"]})
                    for tc in m.get("tool_calls", []):
                        content.append({"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.input})
                out.append({"role": "assistant", "content": content})
            elif m["role"] == "tool":
                out.append({"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": r["id"], "content": r["content"],
                     **({"is_error": True} if r.get("is_error") else {})}
                    for r in m["results"]
                ]})
        return out

    # ----- main call -----

    def complete(self, system: str, messages: list[dict], tools: list[dict]) -> LLMResponse:
        a = self._anthropic
        params = dict(
            model=self.model,
            max_tokens=self.max_tokens,
            system=system,
            tools=tools,
            messages=self._to_api_messages(messages),
            output_config={"effort": self.effort},
            cache_control={"type": "ephemeral"},  # system + tools are stable: cache them
        )
        try:
            if self.model in _FALLBACK_MODELS:
                response = self.client.beta.messages.create(
                    betas=["server-side-fallback-2026-07-01"], fallbacks="default", **params)
            else:
                response = self.client.messages.create(**params)
        except a.AuthenticationError as e:
            raise LLMError("مفتاح الـ API غلط أو مو موجود. تأكد من ANTHROPIC_API_KEY بملف .env", str(e))
        except a.PermissionDeniedError as e:
            raise LLMError("المفتاح ما عنده صلاحية على هذا النموذج.", str(e))
        except a.NotFoundError as e:
            raise LLMError(f"النموذج {self.model} مو موجود. تأكد من MODEL_NAME بملف .env", str(e))
        except a.RateLimitError as e:
            raise LLMError("صار ضغط على الخدمة، انتظر شوية وعيد.", str(e))
        except a.BadRequestError as e:
            raise LLMError("صار خطأ بالطلب للنموذج، عيد المحاولة.", str(e))
        except a.APIStatusError as e:
            raise LLMError("خدمة الذكاء الاصطناعي بيها مشكلة هسه، جرب بعد شوية.", str(e))
        except a.APIConnectionError as e:
            raise LLMError("ما گدرت أتصل بالإنترنت. تأكد من النت وعيد.", str(e))

        if response.stop_reason == "refusal":
            return LLMResponse(text="", refused=True)
        if response.stop_reason == "max_tokens":
            raise LLMError("الرد طلع طويل وانقطع، عيد الطلب بطريقة أبسط.", "max_tokens")

        text = "".join(b.text for b in response.content if b.type == "text").strip()
        calls = [ToolCall(b.id, b.name, dict(b.input)) for b in response.content if b.type == "tool_use"]
        return LLMResponse(text=text, tool_calls=calls, raw=list(response.content))


_GENERATED_ID = "gen_"


def _real_id(call_id: str):
    """Ids we made up locally are never sent back to Gemini."""
    return None if call_id.startswith(_GENERATED_ID) else call_id


class GeminiLLM:
    """Google Gemini via the official `google-genai` SDK."""

    def __init__(self, model: str | None = None, api_key: str | None = None,
                 max_tokens: int = 8192):
        import httpx
        from google import genai
        from google.genai import errors, types

        self._httpx, self._errors, self.types = httpx, errors, types
        self.model = model or os.getenv("GEMINI_MODEL") or "gemini-3.8-flash"
        self.max_tokens = max_tokens
        key = api_key or os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        if not key:
            raise LLMError("ما كو مفتاح Gemini. حط GEMINI_API_KEY بملف .env", "missing GEMINI_API_KEY")
        # Gemini often answers 503 "high demand" for a moment: retry those.
        # 429 is not retried: on the free tier it is usually the daily quota.
        self.http_options = types.HttpOptions(
            timeout=60_000,  # ms, per attempt
            retry_options=types.HttpRetryOptions(
                attempts=4, initial_delay=1.0, max_delay=8.0,
                http_status_codes=[408, 500, 502, 503, 504]))
        self.client = genai.Client(api_key=key, http_options=self.http_options)

    # ----- format conversion -----

    def _tools(self, tools: list[dict]):
        t = self.types
        decls = []
        for tool in tools:
            schema = tool.get("input_schema") or {}
            kwargs = {"name": tool["name"], "description": tool.get("description", "")}
            if schema.get("properties"):  # tools without inputs get no schema
                kwargs["parameters_json_schema"] = schema
            decls.append(t.FunctionDeclaration(**kwargs))
        return [t.Tool(function_declarations=decls)]

    def _to_contents(self, messages: list[dict]):
        t = self.types
        out = []
        for m in messages:
            if m["role"] == "user":
                out.append(t.Content(role="user", parts=[t.Part(text=m["text"])]))
            elif m["role"] == "assistant":
                if m.get("raw") is not None:
                    out.append(m["raw"])  # native content incl. thought signatures, unchanged
                    continue
                parts = [t.Part(text=m["text"])] if m.get("text") else []
                parts += [t.Part(function_call=t.FunctionCall(id=_real_id(tc.id), name=tc.name, args=tc.input))
                          for tc in m.get("tool_calls", [])]
                out.append(t.Content(role="model", parts=parts))
            elif m["role"] == "tool":
                out.append(t.Content(role="user", parts=[
                    t.Part(function_response=t.FunctionResponse(
                        id=_real_id(r["id"]), name=r["name"], response=json.loads(r["content"])))
                    for r in m["results"]
                ]))
        return out

    # ----- main call -----

    MAX_RATE_LIMIT_WAIT = 60  # seconds: a per-minute window never needs more

    @staticmethod
    def _retry_delay(e) -> float | None:
        """Seconds Gemini asks us to wait (RetryInfo in the error details), if any."""
        error = e.details.get("error", e.details) if isinstance(e.details, dict) else {}
        for d in error.get("details") or []:
            if str(d.get("@type", "")).endswith("RetryInfo"):
                try:
                    return float(str(d.get("retryDelay", "")).rstrip("s"))
                except ValueError:
                    return None
        return None

    def _generate(self, contents, config):
        """Free tier allows only a few requests per minute. When Gemini says how
        long to wait, wait and retry instead of failing, up to MAX_RATE_LIMIT_WAIT in total."""
        waited = 0.0
        while True:
            try:
                return self.client.models.generate_content(model=self.model, contents=contents, config=config)
            except self._errors.ClientError as e:
                delay = self._retry_delay(e) if e.code == 429 and "PerDay" not in str(e) else None
                if delay is None or waited + delay > self.MAX_RATE_LIMIT_WAIT:
                    raise
                time.sleep(delay + 1)
                waited += delay + 1

    def complete(self, system: str, messages: list[dict], tools: list[dict]) -> LLMResponse:
        t, errors = self.types, self._errors
        config = t.GenerateContentConfig(
            system_instruction=system,
            tools=self._tools(tools),
            # We run the tools ourselves in agent.py
            automatic_function_calling=t.AutomaticFunctionCallingConfig(disable=True),
            max_output_tokens=self.max_tokens,
        )
        try:
            response = self._generate(self._to_contents(messages), config)
        except errors.ClientError as e:
            if e.code == 429:
                if "PerDay" in str(e):
                    raise LLMError(f"خلصت حصة اليوم المجانية للنموذج {self.model}. جرب باچر، "
                                   "أو بدّل GEMINI_MODEL بملف .env لنموذج ثاني.", str(e))
                raise LLMError("خلصت حصة Gemini المجانية هسه، انتظر دقيقة وعيد.", str(e))
            if e.code in (401, 403) or "API_KEY" in str(e):
                raise LLMError("مفتاح Gemini غلط أو ما عنده صلاحية. تأكد من GEMINI_API_KEY بملف .env", str(e))
            if e.code == 404:
                raise LLMError(f"النموذج {self.model} مو موجود. تأكد من GEMINI_MODEL بملف .env", str(e))
            raise LLMError("صار خطأ بالطلب للنموذج، عيد المحاولة.", str(e))
        except errors.APIError as e:
            raise LLMError("خدمة الذكاء الاصطناعي بيها مشكلة هسه، جرب بعد شوية.", str(e))
        except self._httpx.TransportError as e:
            raise LLMError("ما گدرت أتصل بالإنترنت. تأكد من النت وعيد.", str(e))

        if not response.candidates:
            return LLMResponse(text="", refused=True)  # prompt blocked
        candidate = response.candidates[0]
        reason = candidate.finish_reason
        if reason == t.FinishReason.MAX_TOKENS:
            raise LLMError("الرد طلع طويل وانقطع، عيد الطلب بطريقة أبسط.", "max_tokens")
        parts = (candidate.content.parts if candidate.content else None) or []
        if not parts and reason in (t.FinishReason.SAFETY, t.FinishReason.PROHIBITED_CONTENT,
                                    t.FinishReason.BLOCKLIST, t.FinishReason.SPII):
            return LLMResponse(text="", refused=True)

        text = "".join(p.text for p in parts if p.text and not p.thought).strip()
        calls = []
        for i, p in enumerate(parts):
            if p.function_call:
                fc = p.function_call
                # Some models give no id; then tool results are matched by name only.
                call_id = fc.id or f"{_GENERATED_ID}{len(messages)}_{i}"
                calls.append(ToolCall(call_id, fc.name, dict(fc.args or {})))
        return LLMResponse(text=text, tool_calls=calls, raw=candidate.content)


def make_llm():
    """Factory: the one place to change when switching provider (LLM_PROVIDER in .env)."""
    provider = (os.getenv("LLM_PROVIDER") or "anthropic").strip().lower()
    if provider == "gemini":
        return GeminiLLM()
    return AnthropicLLM()
