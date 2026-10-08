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

import os
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
        self.model = model or os.getenv("MODEL_NAME", "claude-sonnet-5-5")
        self.effort = effort or os.getenv("EFFORT", "medium")
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


def make_llm():
    """Factory: the one place to change when switching provider."""
    return AnthropicLLM()
