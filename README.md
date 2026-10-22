# TestRail Case History Analyzer (v5.4.7)

Скрипт для аналізу історії виконання тест-кейсів у TestRail з фокусом на пошук кейсів, які **ніколи не виконувались** (або не мають знайденої історії в заданій політиці сканування).

## Основний функціонал

- Працює з TestRail API (`/api/v2`)
- Інтерактивний вибір:
  - Project
  - Suite
  - Section scope (all / by IDs / hierarchical picker)
- Аналізує кейси в 2 етапи:
  1. **Stage 1**: сканує **NON-CI runs** для всіх кандидатів
  2. **Stage 2**: сканує **CI runs** тільки для automated кейсів, які **ще не знайдені** після Stage 1
- Визначення automated кейса через підтверджене поле:
  - `custom_case_automation_id`
- Експорт результатів:
  - CSV або XLSX
- Додатково:
  - Retry/backoff при 429/5xx
  - Throttling запитів
  - Checkpoint CSV кожні N run-ів (за замовчуванням 50)
  - In-memory cache для `get_tests` (опційно)

---

## Що означають статуси

- `has_history` — знайдено історію виконання (результати)
- `in_open_run_no_results` — кейс присутній у відкритих ранах, але без результатів
- `never_executed` — не знайдено результатів за політикою сканування

> Важливо: для кейсів без `automation_id` CI-етап не виконується (за задумом оптимізації).

---

## Вимоги

- Python 3.9+
- Пакети:
  - `requests`
  - `openpyxl` (опціонально, лише для XLSX)

Встановлення:

```bash
pip install requests openpyxl
```

---

## Файли

- `testrail_report_final_fast_Version5_4_7.py` — основний скрипт
- `README.md` — цей опис

---

## Запуск

```bash
python testrail_report_final_fast_Version5_4_7.py
```

Скрипт запитає:
1. TestRail URL
2. Email
3. API token
4. Project / Suite / Sections
5. Кількість воркерів
6. Налаштування checkpoint
7. Налаштування кешу
8. Формат і назву фінального звіту

---

## Рекомендовані налаштування

- `workers`: 4–8
- `checkpoint`: увімкнено
- `checkpoint every`: 50
- `get_tests cache`: увімкнено

---

## Приклад checkpoint-файлів

Під час виконання можуть створюватися файли типу:

- `testrail_v547_YYYYMMDD_HHMMSS_checkpoint_stage1_non_ci_run50.csv`
- `testrail_v547_YYYYMMDD_HHMMSS_checkpoint_stage2_ci_unresolved_auto_run100.csv`

---

## Безпека / зауваження

- Не коміть API token у репозиторій.
- Якщо поле `custom_case_automation_id` відсутнє в payload кейса, скрипт завершується з помилкою (це захист від хибного пропуску CI-пошуку).
- Значення `never_executed` завжди інтерпретувати у контексті обраного scope (project/suite/sections) і поточної політики сканування.

---

## Що було оптимізовано у v5.4.7

- Прибрано обмеження “лише останні N Nightly”
- Додано пріоритет non-CI
- Додано CI-скан лише для automated кейсів
- Додано Stage 2 тільки для unresolved automated кейсів
- Додано checkpoint export
- Додано in-memory cache `get_tests`
- Зафіксовано ключ automation ID: `custom_case_automation_id`

---

## Швидка перевірка після запуску

У логах зверни увагу на:
- `Candidate cases after section filter`
- `Automation field presence stats`
- `Stage 2 candidate automated cases (unresolved after Stage 1)`
- `Summary`

Ці рядки дають швидкий sanity-check, що оптимізації працюють коректно.