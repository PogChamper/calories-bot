# Telegram Fitness Bot

Бот для расчета дневных норм воды и калорий с учетом погоды, трекингом питания и тренировок.

**Telegram:** [@my_HSE_calories_tracker_bot](https://t.me/my_HSE_calories_tracker_bot)

## Возможности

- Расчет нормы воды (вес + активность + температура)
- Расчет нормы калорий (формула Миффлина-Сан Жеора)
- Логирование воды, еды и тренировок
- Поиск калорийности продуктов (локальная база + USDA API + перевод)
- Графики прогресса
- Рекомендации по питанию и активности

## Команды

| Команда | Описание |
|---------|----------|
| `/start` | Приветствие |
| `/set_profile` | Настройка профиля |
| `/log_water <мл>` | Записать воду |
| `/log_food <продукт>` | Записать еду |
| `/log_workout <тип> <мин>` | Записать тренировку |
| `/check_progress` | Текущий прогресс |
| `/chart` | График прогресса |
| `/recommend` | Рекомендации |

## Типы тренировок

бег, ходьба, плавание, велосипед, йога, силовая, кардио, танцы, футбол, баскетбол, теннис

## Установка

```bash
git clone https://github.com/PogChamper/calories-bot.git
cd calories-bot
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Настройка

Создать `.env`:

```
TELEGRAM_TOKEN=токен_от_botfather
OPENWEATHER_API_KEY=ключ_openweathermap
USDA_API_KEY=ключ_usda_fooddata_central
```

## Запуск

```bash
python bot.py
```

## Docker

```bash
docker build -t calories-bot .
docker run --env-file .env calories-bot
```

## API

- [OpenWeatherMap](https://openweathermap.org/api) — температура для расчета нормы воды
- [USDA FoodData Central](https://fdc.nal.usda.gov/) — калорийность продуктов
- [translatepy](https://github.com/Animenosekai/translate) — перевод названий продуктов

## Структура

```
bot.py           — основной код бота
food_data.json   — локальная база продуктов (~100 шт)
Dockerfile       — сборка образа
requirements.txt — зависимости
```
