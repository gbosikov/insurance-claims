# Требования к серверу: Gemma 4 31B-it для слоя Extraction

**Модель:** `google/gemma-4-31B-it`  
**GGUF-источник:** `unsloth/gemma-4-31B-it-GGUF` (HuggingFace, Unsloth — авторитетная организация)  
**Задача:** замена Claude API на шаге Extraction (Слой 4)  
**Целевое время обработки:** 20–25 мин (p90 по CLAUDE.md: ≤ 15 мин)  
**Запуск:** Ollama в Docker Compose, CPU-only inference

---

## Доступные квантизации (unsloth/gemma-4-31B-it-GGUF)

| Файл | Размер | Скорость | Качество | Назначение |
|------|--------|----------|----------|------------|
| `gemma-4-31B-it-Q4_K_M.gguf` | ~20 ГБ | быстро | хорошо | **CPU, рекомендуется** |
| `gemma-4-31B-it-Q5_K_M.gguf` | ~24 ГБ | средне | лучше | CPU с запасом RAM |
| `gemma-4-31B-it-Q6_K.gguf` | ~27 ГБ | медленнее | отлично | CPU 64+ ГБ RAM |
| `gemma-4-31B-it-Q8_0.gguf` | ~32 ГБ | медленно | максимум | только с GPU |
| `gemma-4-31B-it-UD-Q4_K_XL.gguf` | ~22 ГБ | быстро | хорошо | CPU альтернатива Q4_K_M |
| `mmproj-F16.gguf` | ~1 ГБ | — | — | **Vision: подача изображений** |

> **Vision-возможность:** файл `mmproj-F16.gguf` — multimodal projector.
> При загрузке вместе с основной моделью можно подавать изображения документов напрямую,
> минуя OCR-текст. Потенциально убирает ошибки OCR (ложные коды, обрезанные значения).

Q4_K_M — оптимальный выбор для CPU-inference.

---

## Расчёт RAM

| Компонент | Объём |
|-----------|-------|
| Модель Gemma 4 31B (Q4_K_M) | ~20 ГБ |
| KV-кэш (контекст 4096 токенов) | ~3 ГБ |
| PostgreSQL + Redis + FastAPI + Celery workers | ~8 ГБ |
| ОС + Docker + буфер | ~5 ГБ |
| **Итого минимум** | **~36 ГБ → брать 48 ГБ** |

---

## Скорость генерации vs RAM-канальность

Узкое место CPU-inference — пропускная способность RAM, не количество ядер.

| RAM конфигурация | Пропускная способность | Скорость (Q4 31B) | 500 токенов |
|-----------------|------------------------|-------------------|-------------|
| DDR4 2-канала (десктоп) | ~50 ГБ/с | ~1.5–2 tok/s | ~4–6 мин |
| DDR4 4-канала (сервер) | ~100 ГБ/с | ~3–5 tok/s | ~2–3 мин |
| DDR5 2-канала | ~76 ГБ/с | ~2.5–3.5 tok/s | ~2.5–3.5 мин |

---

## Конфигурации сервера

### Минимальная (~18–22 мин итого)
> Укладывается в SLA 20–25 мин

| Компонент | Характеристика |
|-----------|----------------|
| CPU | 16 ядер / 32 потока, AVX2 (Intel Xeon E-2300 / AMD Ryzen 9) |
| RAM | 48 ГБ DDR4 dual-channel |
| SSD | 200 ГБ (20 ГБ модель + ОС + данные) |
| Сеть | 1 Гбит |

### Рекомендуемая (~12–16 мин итого)
> Укладывается в целевой p90 ≤ 15 мин

| Компонент | Характеристика |
|-----------|----------------|
| CPU | 32 ядра / 64 потока, AVX2 (AMD EPYC 7003 / Intel Xeon Silver) |
| RAM | 64 ГБ DDR4 quad-channel ECC |
| SSD | 500 ГБ NVMe |
| Сеть | 1 Гбит |

### Комфортная (~8–10 мин итого)
> Запас на рост нагрузки (300+ заявок/день)

| Компонент | Характеристика |
|-----------|----------------|
| CPU | 64 ядра, AVX512 (AMD EPYC 7004 / Intel Xeon Gold) |
| RAM | 128 ГБ DDR4 8-канальный ECC |
| SSD | 1 ТБ NVMe |
| Сеть | 10 Гбит |

---

## Время по шагам пайплайна (рекомендуемая конфигурация)

| Шаг | Время |
|-----|-------|
| Download + Preprocessing | ~1 мин |
| OCR (Vision API, параллельно) | ~1.5 мин |
| **Extraction (Gemma 4 31B CPU)** | **~2–3 мин** |
| Core fetch + Decision (Claude Sonnet) | ~3 мин |
| Submit + Routing | ~10 сек |
| **Итого** | **~8–9 мин** |

---

## Интеграция в docker-compose.yml

```yaml
ollama:
  image: ollama/ollama
  volumes:
    - ollama_data:/root/.ollama
  ports:
    - "11434:11434"

volumes:
  ollama_data:    # ~20 ГБ, модель скачивается один раз
```

Первый запуск — скачать модель напрямую с HuggingFace (20 ГБ, одноразово):
```bash
# Ollama 0.5+ поддерживает прямой импорт из HuggingFace
docker compose exec ollama ollama run hf.co/unsloth/gemma-4-31B-it-GGUF:Q4_K_M

# Или вручную через Modelfile
docker compose exec ollama bash -c "
  wget https://huggingface.co/unsloth/gemma-4-31B-it-GGUF/resolve/main/gemma-4-31B-it-Q4_K_M.gguf -O /root/.ollama/gemma4-31b.gguf
  echo 'FROM /root/.ollama/gemma4-31b.gguf' > /tmp/Modelfile
  ollama create gemma4-31b-it -f /tmp/Modelfile
"
```

С vision-поддержкой (добавить mmproj):
```bash
# Скачать оба файла и создать модель с vision
docker compose exec ollama bash -c "
  wget https://huggingface.co/unsloth/gemma-4-31B-it-GGUF/resolve/main/gemma-4-31B-it-Q4_K_M.gguf
  wget https://huggingface.co/unsloth/gemma-4-31B-it-GGUF/resolve/main/mmproj-F16.gguf
  printf 'FROM gemma-4-31B-it-Q4_K_M.gguf\nMMPROJ mmproj-F16.gguf\n' > Modelfile
  ollama create gemma4-31b-vision -f Modelfile
"
```

---

## Переключение в .env

```bash
# Extraction: gemma (локальный) | claude (API) | rules (regex)
EXTRACTION_BACKEND=gemma
OLLAMA_BASE_URL=http://ollama:11434
OLLAMA_EXTRACTION_MODEL=gemma4:31b-it
```

Decision (Слой 7) остаётся на Claude Sonnet — переключается независимо.

---

## Стоимость

| Вариант | Стоимость extraction | При 300 заявках/день |
|---------|----------------------|----------------------|
| Claude Sonnet (текущий) | ~$0.03/заявку | ~$270/месяц |
| Claude Haiku | ~$0.006/заявку | ~$54/месяц |
| **Gemma 4 31B локально** | **$0/заявку** | **$0/месяц** |

Окупаемость сервера (рекомендуемая конфигурация ~$300–500/мес аренда):
при 300 заявок/день vs Sonnet — окупается в первый же месяц.
