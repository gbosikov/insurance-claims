"""
Тест: поддерживает ли текущая модель Gemini extended thinking при tool-use.

Использует реальный кейс из БД:
  - Пациент: გოლიაძე ნათია
  - Диагнозы: I10, I49.3, E78.5, D64.9
  - Сумма: 260 GEL
  - Статус: MANUAL_REVIEW (медицинская несогласованность)

Запуск:
  docker compose exec api python -m scripts.test_gemini_thinking
"""

import asyncio
import sys
import traceback

from core.config import get_settings

settings = get_settings()

SYSTEM_PROMPT = """Ты — система анализа страховых случаев ДМС.
Проанализируй кейс и определи покрывается ли он страховым договором."""

USER_PROMPT = """
## Кейс
Пациент: გოლიაძე ნათია, 1976-11-06
Клиника: შპს თბილისის გულის ცენტრი
Дата: 2026-06-02
Сумма: 260 GEL

## Диагнозы
- I10 — ესენციური (პირველადი) ჰიპერტენზია
- I49.3 — პარკუჭების ადრეული დეპოლარიზაცია
- E78.5 — ჰიპერლიპიდემია, დაუზუსტებელი
- D64.9 — ანემია, დაუზუსტებელი

## Услуги
- კარდიოლოგის კონსულტაცია — 90 GEL
- მუხლ-სახსრის რენტგენოგრაფია — 40 GEL
- ორთოპედ-ტრავმატოლოგის კონსულტაცია — 130 GEL

## Проблема
Рентген сустава и консультация ортопеда не соответствуют кардиологическим диагнозам.
Проанализируй есть ли медицинская логика и должен ли кейс быть покрыт.
"""

DECISION_TOOL = {
    "name": "make_decision",
    "description": "Вынести решение по страховому кейсу",
    "input_schema": {
        "type": "object",
        "properties": {
            "overall_confidence": {
                "type": "number",
                "description": "Уверенность 0.0–1.0"
            },
            "is_covered": {
                "type": "boolean",
                "description": "Покрыт ли кейс"
            },
            "reasoning": {
                "type": "string",
                "description": "Обоснование решения"
            },
            "recommended_route": {
                "type": "string",
                "enum": ["auto_approve", "manual_review", "reject"],
                "description": "Рекомендуемый маршрут"
            }
        },
        "required": ["overall_confidence", "is_covered", "reasoning", "recommended_route"]
    }
}


async def test_with_thinking_budget(budget: int) -> dict:
    """Вызов Gemini с конкретным thinking_budget."""
    try:
        import google.genai as genai
        import google.genai.types as types

        client = genai.Client(api_key=settings.gemini_api_key)
        model = settings.gemini_model

        fn_decl = types.FunctionDeclaration(
            name=DECISION_TOOL["name"],
            description=DECISION_TOOL["description"],
            parameters={
                "type": "OBJECT",
                "properties": {
                    "overall_confidence": {"type": "NUMBER"},
                    "is_covered": {"type": "BOOLEAN"},
                    "reasoning": {"type": "STRING"},
                    "recommended_route": {"type": "STRING"}
                },
                "required": ["overall_confidence", "is_covered", "reasoning", "recommended_route"]
            }
        )

        config_kwargs = dict(
            system_instruction=SYSTEM_PROMPT,
            temperature=0.1,
            max_output_tokens=2000,
            tools=[types.Tool(function_declarations=[fn_decl])],
            tool_config=types.ToolConfig(
                function_calling_config=types.FunctionCallingConfig(
                    mode="ANY",
                    allowed_function_names=["make_decision"],
                )
            ),
        )

        if budget > 0:
            config_kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=budget)
        else:
            config_kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=0)

        response = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: client.models.generate_content(
                model=model,
                contents=[{"role": "user", "parts": [{"text": USER_PROMPT}]}],
                config=types.GenerateContentConfig(**config_kwargs),
            )
        )

        # Разбираем ответ
        thinking_text = None
        tool_result = None

        for part in response.candidates[0].content.parts:
            part_type = type(part).__name__
            if hasattr(part, "thought") and part.thought:
                thinking_text = getattr(part, "text", None) or str(part)
            elif hasattr(part, "function_call") and part.function_call:
                tool_result = dict(part.function_call.args)

        usage = response.usage_metadata
        return {
            "success": True,
            "budget": budget,
            "thinking_returned": thinking_text is not None,
            "thinking_preview": (thinking_text or "")[:200] if thinking_text else None,
            "tool_called": tool_result is not None,
            "tool_result": tool_result,
            "input_tokens": getattr(usage, "prompt_token_count", "?"),
            "output_tokens": getattr(usage, "candidates_token_count", "?"),
            "thinking_tokens": getattr(usage, "thoughts_token_count", None),
        }

    except Exception as e:
        return {
            "success": False,
            "budget": budget,
            "error": str(e),
            "error_type": type(e).__name__,
            "traceback": traceback.format_exc()[-500:],
        }


async def main():
    print(f"\n{'='*60}")
    print(f"Модель: {settings.gemini_model}")
    print(f"Провайдер: {settings.llm_provider}")
    print(f"{'='*60}\n")

    if settings.llm_provider != "gemini":
        print("⚠  LLM_PROVIDER != gemini. Установите LLM_PROVIDER=gemini в .env")
        sys.exit(1)

    if not settings.gemini_api_key:
        print("⚠  GEMINI_API_KEY не задан")
        sys.exit(1)

    budgets = [0, 1024, 8192]

    for budget in budgets:
        label = "ОТКЛЮЧЁН (budget=0)" if budget == 0 else f"budget={budget}"
        print(f"\n--- Тест: thinking {label} ---")
        result = await test_with_thinking_budget(budget)

        if result["success"]:
            print(f"  ✅ Вызов успешен")
            print(f"  Thinking вернулся: {'✅ ДА' if result['thinking_returned'] else '❌ НЕТ'}")
            if result["thinking_preview"]:
                print(f"  Thinking preview: {result['thinking_preview'][:150]}...")
            print(f"  Tool вызван: {'✅' if result['tool_called'] else '❌'}")
            if result["tool_result"]:
                r = result["tool_result"]
                print(f"  Решение: {r.get('recommended_route')} | confidence={r.get('overall_confidence')}")
                print(f"  Обоснование: {str(r.get('reasoning', ''))[:150]}")
            print(f"  Токены: input={result['input_tokens']} output={result['output_tokens']}", end="")
            if result["thinking_tokens"]:
                print(f" thinking={result['thinking_tokens']}", end="")
            print()
        else:
            print(f"  ❌ Ошибка: {result['error_type']}")
            print(f"  {result['error'][:300]}")
            if "thinking_budget" in result.get("error", ""):
                print("  → Модель не поддерживает ThinkingConfig с этим budget")
            elif "function_call" in result.get("error", "").lower():
                print("  → Конфликт thinking + function_calling")

    print(f"\n{'='*60}")
    print("Готово.")


if __name__ == "__main__":
    asyncio.run(main())
