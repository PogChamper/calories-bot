import json
import logging
import os
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from enum import Enum, auto
from io import BytesIO
from pathlib import Path
from typing import Optional

import matplotlib
import matplotlib.pyplot as plt
import requests
from dotenv import load_dotenv
from telegram import ReplyKeyboardMarkup, ReplyKeyboardRemove, Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

matplotlib.use("Agg")
load_dotenv()

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)


class ProfileState(Enum):
    WEIGHT = auto()
    HEIGHT = auto()
    AGE = auto()
    GENDER = auto()
    ACTIVITY = auto()
    CITY = auto()
    CALORIE_GOAL = auto()


class FoodState(Enum):
    GRAMS = auto()


@dataclass
class Config:
    telegram_token: str = field(default_factory=lambda: os.getenv("TELEGRAM_TOKEN", ""))
    weather_key: str = field(default_factory=lambda: os.getenv("OPENWEATHER_API_KEY", ""))
    usda_key: str = field(default_factory=lambda: os.getenv("USDA_API_KEY", ""))


@dataclass
class UserData:
    weight: Optional[float] = None
    height: Optional[float] = None
    age: Optional[int] = None
    gender: Optional[str] = None
    activity: Optional[int] = None
    city: Optional[str] = None
    water_goal: int = 2000
    calorie_goal: int = 2000
    logged_water: int = 0
    logged_calories: float = 0
    burned_calories: int = 0
    last_reset: date = field(default_factory=date.today)
    food_history: list = field(default_factory=list)
    workout_history: list = field(default_factory=list)

    def reset_daily(self):
        today = date.today()
        if self.last_reset != today:
            self.logged_water = 0
            self.logged_calories = 0
            self.burned_calories = 0
            self.food_history = []
            self.workout_history = []
            self.last_reset = today


class FoodService:
    WORKOUT_BURN = {
        "бег": 10, "running": 10, "ходьба": 4, "walking": 4,
        "плавание": 8, "swimming": 8, "велосипед": 7, "cycling": 7,
        "йога": 3, "yoga": 3, "силовая": 6, "strength": 6,
        "кардио": 8, "cardio": 8, "танцы": 6, "dancing": 6,
        "футбол": 9, "football": 9, "баскетбол": 8, "basketball": 8,
        "теннис": 7, "tennis": 7,
    }

    def __init__(self, config: Config):
        self.config = config
        self._translator = None
        self._food_db = self._load_food_db()

    def _load_food_db(self) -> dict:
        path = Path(__file__).parent / "food_data.json"
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        return {}

    @property
    def translator(self):
        if self._translator is None:
            try:
                from translatepy import Translator
                self._translator = Translator()
            except ImportError:
                pass
        return self._translator

    def translate(self, text: str) -> str:
        if text.isascii() or not self.translator:
            return text
        try:
            return self.translator.translate(text, "English").result
        except Exception:
            return text

    def get_weather(self, city: str) -> Optional[float]:
        if not self.config.weather_key:
            return None
        url = f"https://api.openweathermap.org/data/2.5/weather?q={city}&appid={self.config.weather_key}&units=metric"
        resp = requests.get(url, timeout=10)
        if resp.ok:
            return resp.json()["main"]["temp"]
        return None

    def search_usda(self, product: str) -> Optional[dict]:
        if not self.config.usda_key:
            return None

        product_en = self.translate(product)
        log.info(f"USDA search: '{product}' -> '{product_en}'")

        url = (
            f"https://api.nal.usda.gov/fdc/v1/foods/search"
            f"?api_key={self.config.usda_key}&query={product_en}&dataType=SR%20Legacy&pageSize=5"
        )
        resp = requests.get(url, timeout=10)
        if not resp.ok:
            return None

        foods = resp.json().get("foods", [])
        if not foods:
            return None

        best = next(
            (f for f in foods if "raw" in f.get("description", "").lower()),
            foods[0]
        )

        for n in best.get("foodNutrients", []):
            if "energy" in n.get("nutrientName", "").lower() and "kcal" in n.get("unitName", "").lower():
                return {"name": best["description"], "calories": n["value"]}
        return None

    def search_openfoodfacts(self, product: str) -> Optional[dict]:
        url = f"https://world.openfoodfacts.org/cgi/search.pl?action=process&search_terms={product}&json=true"
        resp = requests.get(url, timeout=10)
        if not resp.ok:
            return None

        products = resp.json().get("products", [])
        if products:
            p = products[0]
            cal = p.get("nutriments", {}).get("energy-kcal_100g", 0)
            if cal:
                return {"name": p.get("product_name", product), "calories": cal}
        return None

    def get_food(self, product: str) -> Optional[dict]:
        key = product.lower().strip()

        if key in self._food_db:
            return {"name": product.capitalize(), "calories": self._food_db[key]}

        for food, cal in self._food_db.items():
            if food in key or key in food:
                return {"name": food.capitalize(), "calories": cal}

        result = self.search_usda(product)
        if result:
            return result

        return self.search_openfoodfacts(product)


class Calculator:
    @staticmethod
    def water_goal(weight: float, activity: int, temp: Optional[float]) -> int:
        base = weight * 30
        activity_bonus = (activity // 30) * 500
        weather_bonus = 0
        if temp:
            if temp > 30:
                weather_bonus = 1000
            elif temp > 25:
                weather_bonus = 500
        return int(base + activity_bonus + weather_bonus)

    @staticmethod
    def calorie_goal(weight: float, height: float, age: int, gender: str, activity: int) -> int:
        is_male = gender.lower() in ("м", "m", "male", "мужской")
        bmr = 10 * weight + 6.25 * height - 5 * age + (5 if is_male else -161)

        if activity < 15:
            mult = 1.2
        elif activity < 30:
            mult = 1.375
        elif activity < 60:
            mult = 1.55
        elif activity < 90:
            mult = 1.725
        else:
            mult = 1.9

        return int(bmr * mult)


class FitnessBot:
    def __init__(self, config: Config):
        self.config = config
        self.food_service = FoodService(config)
        self.users: dict[int, UserData] = defaultdict(UserData)

    def get_user(self, user_id: int) -> UserData:
        user = self.users[user_id]
        user.reset_daily()
        return user

    async def cmd_start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        log.info("Received: /start")
        await update.message.reply_text(
            "Привет! Бот для расчета нормы воды и калорий.\n\n"
            "Команды:\n"
            "/set_profile - Настройка профиля\n"
            "/log_water <мл> - Записать воду\n"
            "/log_food <продукт> - Записать еду\n"
            "/log_workout <тип> <мин> - Записать тренировку\n"
            "/check_progress - Прогресс\n"
            "/chart - График\n"
            "/recommend - Рекомендации\n"
            "/help - Справка"
        )

    async def cmd_help(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        log.info("Received: /help")
        await update.message.reply_text(
            "Справка:\n\n"
            "/set_profile - вес, рост, возраст, активность, город\n"
            "/log_water 500 - записать 500 мл воды\n"
            "/log_food банан - записать еду\n"
            "/log_workout бег 30 - записать тренировку\n\n"
            "Типы тренировок: бег, ходьба, плавание, велосипед, йога, "
            "силовая, кардио, танцы, футбол, баскетбол, теннис\n\n"
            "Расчет норм:\n"
            "Вода: вес*30мл + 500мл/30мин активности + до 1000мл в жару\n"
            "Калории: формула Миффлина-Сан Жеора"
        )

    async def profile_start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> ProfileState:
        log.info("Received: /set_profile")
        await update.message.reply_text(
            "Настройка профиля.\n\nВведите вес (кг):",
            reply_markup=ReplyKeyboardRemove()
        )
        return ProfileState.WEIGHT

    async def profile_weight(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> ProfileState:
        try:
            weight = float(update.message.text.replace(",", "."))
            if not 20 <= weight <= 300:
                raise ValueError
            ctx.user_data["weight"] = weight
            await update.message.reply_text("Введите рост (см):")
            return ProfileState.HEIGHT
        except ValueError:
            await update.message.reply_text("Некорректный вес. Введите число от 20 до 300:")
            return ProfileState.WEIGHT

    async def profile_height(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> ProfileState:
        try:
            height = float(update.message.text.replace(",", "."))
            if not 100 <= height <= 250:
                raise ValueError
            ctx.user_data["height"] = height
            await update.message.reply_text("Введите возраст:")
            return ProfileState.AGE
        except ValueError:
            await update.message.reply_text("Некорректный рост. Введите число от 100 до 250:")
            return ProfileState.HEIGHT

    async def profile_age(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> ProfileState:
        try:
            age = int(update.message.text)
            if not 10 <= age <= 120:
                raise ValueError
            ctx.user_data["age"] = age
            keyboard = [["М", "Ж"]]
            await update.message.reply_text(
                "Укажите пол:",
                reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True)
            )
            return ProfileState.GENDER
        except ValueError:
            await update.message.reply_text("Некорректный возраст. Введите число от 10 до 120:")
            return ProfileState.AGE

    async def profile_gender(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> ProfileState:
        gender = update.message.text.strip().lower()
        if gender not in ("м", "ж", "m", "f"):
            await update.message.reply_text("Выберите М или Ж:")
            return ProfileState.GENDER
        ctx.user_data["gender"] = gender
        await update.message.reply_text(
            "Минут активности в день:",
            reply_markup=ReplyKeyboardRemove()
        )
        return ProfileState.ACTIVITY

    async def profile_activity(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> ProfileState:
        try:
            activity = int(update.message.text)
            if not 0 <= activity <= 480:
                raise ValueError
            ctx.user_data["activity"] = activity
            await update.message.reply_text("Город (для учета погоды):")
            return ProfileState.CITY
        except ValueError:
            await update.message.reply_text("Некорректное значение. Введите от 0 до 480:")
            return ProfileState.ACTIVITY

    async def profile_city(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> ProfileState:
        city = update.message.text.strip()
        ctx.user_data["city"] = city

        temp = self.food_service.get_weather(city)
        ctx.user_data["temp"] = temp
        temp_msg = f"Температура в {city}: {temp:.1f}C\n\n" if temp else f"Не удалось получить погоду для {city}\n\n"

        default_cal = Calculator.calorie_goal(
            ctx.user_data["weight"],
            ctx.user_data["height"],
            ctx.user_data["age"],
            ctx.user_data["gender"],
            ctx.user_data["activity"]
        )
        ctx.user_data["default_cal"] = default_cal

        keyboard = [["Использовать расчетную"]]
        await update.message.reply_text(
            f"{temp_msg}Расчетная норма калорий: {default_cal} ккал/день\n\n"
            "Введите свою цель или используйте расчетную:",
            reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True)
        )
        return ProfileState.CALORIE_GOAL

    async def profile_calorie_goal(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        user = self.get_user(update.effective_user.id)
        text = update.message.text.strip()

        if text == "Использовать расчетную":
            cal_goal = ctx.user_data["default_cal"]
        else:
            try:
                cal_goal = int(text)
                if not 1000 <= cal_goal <= 5000:
                    raise ValueError
            except ValueError:
                await update.message.reply_text("Введите значение от 1000 до 5000:")
                return ProfileState.CALORIE_GOAL

        user.weight = ctx.user_data["weight"]
        user.height = ctx.user_data["height"]
        user.age = ctx.user_data["age"]
        user.gender = ctx.user_data["gender"]
        user.activity = ctx.user_data["activity"]
        user.city = ctx.user_data["city"]
        user.calorie_goal = cal_goal
        user.water_goal = Calculator.water_goal(user.weight, user.activity, ctx.user_data.get("temp"))

        gender_text = "Мужской" if user.gender in ("м", "m") else "Женский"
        await update.message.reply_text(
            f"Профиль сохранен.\n\n"
            f"Вес: {user.weight} кг\n"
            f"Рост: {user.height} см\n"
            f"Возраст: {user.age} лет\n"
            f"Пол: {gender_text}\n"
            f"Активность: {user.activity} мин/день\n"
            f"Город: {user.city}\n\n"
            f"Цели:\n"
            f"Вода: {user.water_goal} мл\n"
            f"Калории: {user.calorie_goal} ккал",
            reply_markup=ReplyKeyboardRemove()
        )
        return ConversationHandler.END

    async def profile_cancel(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        await update.message.reply_text("Настройка отменена.", reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END

    async def cmd_log_water(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        log.info(f"Received: {update.message.text}")
        user = self.get_user(update.effective_user.id)

        if not ctx.args:
            await update.message.reply_text("Использование: /log_water <мл>\nПример: /log_water 250")
            return

        try:
            amount = int(ctx.args[0])
            if not 1 <= amount <= 5000:
                raise ValueError
        except ValueError:
            await update.message.reply_text("Введите корректное количество (1-5000 мл)")
            return

        user.logged_water += amount
        remaining = max(0, user.water_goal - user.logged_water)
        status = "Цель выполнена!" if remaining == 0 else f"Осталось: {remaining} мл"

        await update.message.reply_text(
            f"Записано: {amount} мл\n\n"
            f"Выпито: {user.logged_water} мл из {user.water_goal} мл\n"
            f"{status}"
        )

    async def food_start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        log.info(f"Received: {update.message.text}")
        self.get_user(update.effective_user.id)

        if not ctx.args:
            await update.message.reply_text("Использование: /log_food <продукт>\nПример: /log_food банан")
            return ConversationHandler.END

        product = " ".join(ctx.args)
        food = self.food_service.get_food(product)

        if not food or food["calories"] == 0:
            await update.message.reply_text(
                f"Не найден продукт '{product}'.\n"
                "Введите калорийность на 100г вручную или /cancel:"
            )
            ctx.user_data["pending_food"] = {"name": product, "calories": None, "manual": True}
            return FoodState.GRAMS

        ctx.user_data["pending_food"] = {"name": food["name"], "calories": food["calories"]}
        await update.message.reply_text(
            f"{food['name']} - {food['calories']:.0f} ккал на 100г.\n"
            "Сколько грамм? (или /cancel)"
        )
        return FoodState.GRAMS

    async def food_grams(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        user = self.get_user(update.effective_user.id)
        pending = ctx.user_data.get("pending_food")

        if not pending:
            return ConversationHandler.END

        text = update.message.text.strip()

        if pending.get("manual") and pending.get("calories") is None:
            try:
                cal = float(text.replace(",", "."))
                pending["calories"] = cal
                pending["manual"] = False
                await update.message.reply_text(f"Калорийность: {cal:.0f} ккал/100г\nСколько грамм?")
                return FoodState.GRAMS
            except ValueError:
                await update.message.reply_text("Введите число (калорийность на 100г):")
                return FoodState.GRAMS

        try:
            grams = float(text.replace(",", "."))
            if not 1 <= grams <= 5000:
                raise ValueError
        except ValueError:
            await update.message.reply_text("Введите корректное количество (1-5000 г):")
            return FoodState.GRAMS

        calories = (pending["calories"] * grams) / 100
        user.logged_calories += calories
        user.food_history.append({"name": pending["name"], "grams": grams, "calories": calories})

        remaining = max(0, user.calorie_goal - user.logged_calories + user.burned_calories)
        await update.message.reply_text(
            f"Записано: {pending['name']} - {calories:.1f} ккал\n\n"
            f"Потреблено: {user.logged_calories:.0f} ккал из {user.calorie_goal} ккал\n"
            f"Осталось: {remaining:.0f} ккал"
        )

        del ctx.user_data["pending_food"]
        return ConversationHandler.END

    async def food_cancel(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        ctx.user_data.pop("pending_food", None)
        await update.message.reply_text("Запись отменена.")
        return ConversationHandler.END

    async def cmd_log_workout(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        log.info(f"Received: {update.message.text}")
        user = self.get_user(update.effective_user.id)

        if len(ctx.args) < 2:
            types = ", ".join(FoodService.WORKOUT_BURN.keys())
            await update.message.reply_text(
                f"Использование: /log_workout <тип> <минуты>\n"
                f"Пример: /log_workout бег 30\n\nТипы: {types}"
            )
            return

        workout_type = ctx.args[0].lower()
        try:
            minutes = int(ctx.args[1])
            if not 1 <= minutes <= 480:
                raise ValueError
        except ValueError:
            await update.message.reply_text("Время должно быть от 1 до 480 минут")
            return

        cal_per_min = FoodService.WORKOUT_BURN.get(workout_type, 5)
        weight_mult = (user.weight or 70) / 70
        burned = int(cal_per_min * minutes * weight_mult)
        extra_water = (minutes // 30) * 200

        user.burned_calories += burned
        user.water_goal += extra_water
        user.workout_history.append({"type": workout_type, "minutes": minutes, "calories": burned})

        await update.message.reply_text(
            f"{workout_type.capitalize()} {minutes} мин - {burned} ккал\n\n"
            f"Сожжено сегодня: {user.burned_calories} ккал\n"
            f"Дополнительно выпейте: {extra_water} мл воды\n"
            f"Норма воды увеличена до {user.water_goal} мл"
        )

    async def cmd_progress(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        log.info("Received: /check_progress")
        user = self.get_user(update.effective_user.id)

        if user.weight is None:
            await update.message.reply_text("Сначала настройте профиль: /set_profile")
            return

        water_pct = min(100, (user.logged_water / user.water_goal) * 100)
        water_bar = "=" * int(water_pct / 10) + "-" * (10 - int(water_pct / 10))
        water_left = max(0, user.water_goal - user.logged_water)

        cal_balance = user.logged_calories - user.burned_calories
        cal_pct = min(100, (cal_balance / user.calorie_goal) * 100) if user.calorie_goal else 0
        cal_bar = "=" * int(cal_pct / 10) + "-" * (10 - int(cal_pct / 10))
        cal_left = max(0, user.calorie_goal - cal_balance)

        temp_info = ""
        if user.city:
            temp = self.food_service.get_weather(user.city)
            if temp:
                temp_info = f"Температура в {user.city}: {temp:.1f}C\n\n"

        await update.message.reply_text(
            f"Прогресс:\n\n{temp_info}"
            f"Вода:\n[{water_bar}] {water_pct:.0f}%\n"
            f"Выпито: {user.logged_water} мл из {user.water_goal} мл\n"
            f"Осталось: {water_left} мл\n\n"
            f"Калории:\n[{cal_bar}] {cal_pct:.0f}%\n"
            f"Потреблено: {user.logged_calories:.0f} ккал из {user.calorie_goal} ккал\n"
            f"Сожжено: {user.burned_calories} ккал\n"
            f"Баланс: {cal_balance:.0f} ккал\n"
            f"Осталось: {cal_left:.0f} ккал"
        )

    async def cmd_chart(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        log.info("Received: /chart")
        user = self.get_user(update.effective_user.id)

        if user.weight is None:
            await update.message.reply_text("Сначала настройте профиль: /set_profile")
            return

        fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12, 5))
        fig.patch.set_facecolor("#1a1a2e")

        water_left = max(0, user.water_goal - user.logged_water)
        ax1.pie(
            [user.logged_water, water_left],
            colors=["#4fc3f7", "#263238"],
            startangle=90,
            autopct=lambda p: f"{p:.1f}%" if p > 0 else ""
        )
        ax1.set_title(f"Вода: {user.logged_water}/{user.water_goal} мл", color="white", fontsize=14)
        ax1.set_facecolor("#1a1a2e")
        ax1.legend(["Выпито", "Осталось"], loc="lower center", facecolor="#1a1a2e", labelcolor="white")

        cal_balance = max(0, user.logged_calories - user.burned_calories)
        cal_left = max(0, user.calorie_goal - cal_balance)
        sizes = [user.logged_calories, user.burned_calories, cal_left]
        labels = ["Потреблено", "Сожжено", "Осталось"]
        colors = ["#ff7043", "#66bb6a", "#263238"]

        filtered = [(s, l, c) for s, l, c in zip(sizes, labels, colors) if s > 0]
        if filtered:
            s, l, c = zip(*filtered)
            ax2.pie(s, labels=l, colors=c, startangle=90, autopct=lambda p: f"{p:.1f}%" if p > 0 else "")
            ax2.legend(l, loc="lower center", facecolor="#1a1a2e", labelcolor="white")

        ax2.set_title(f"Калории: баланс {cal_balance:.0f}/{user.calorie_goal} ккал", color="white", fontsize=14)
        ax2.set_facecolor("#1a1a2e")

        plt.tight_layout()

        buf = BytesIO()
        plt.savefig(buf, format="png", facecolor="#1a1a2e", dpi=100)
        buf.seek(0)
        plt.close()

        await update.message.reply_photo(photo=buf, caption="График прогресса")

    async def cmd_recommend(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        log.info("Received: /recommend")
        user = self.get_user(update.effective_user.id)

        if user.weight is None:
            await update.message.reply_text("Сначала настройте профиль: /set_profile")
            return

        recs = []

        water_pct = (user.logged_water / user.water_goal) * 100 if user.water_goal else 0
        if water_pct < 30:
            recs.append("Вода: выпито мало, рекомендую выпить стакан воды сейчас.")
        elif water_pct < 60:
            recs.append("Вода: неплохо, но пейте регулярно.")
        else:
            recs.append("Вода: отличный результат.")

        cal_balance = user.logged_calories - user.burned_calories
        cal_left = user.calorie_goal - cal_balance

        if cal_balance > user.calorie_goal * 1.2:
            recs.append(
                "Калории: превышена норма. Рекомендую:\n"
                "- 30 мин ходьбы (~120 ккал)\n"
                "- Легкий ужин: салат или овощи"
            )
        elif cal_left > user.calorie_goal * 0.4:
            recs.append(
                "Калории: много в запасе. Рекомендую:\n"
                "- Творог 150г (~150 ккал)\n"
                "- Яйца 2 шт (~140 ккал)\n"
                "- Куриная грудка 150г (~165 ккал)"
            )
        elif cal_left > 0:
            recs.append(
                "Калории: осталось немного. Легкие перекусы:\n"
                "- Яблоко (~50 ккал)\n"
                "- Огурец (~15 ккал)\n"
                "- Йогурт 100г (~60 ккал)"
            )
        else:
            recs.append("Калории: дневная норма достигнута.")

        if user.burned_calories == 0:
            recs.append(
                "Тренировка: сегодня не было. Предлагаю:\n"
                "- Ходьба 20 мин (~80 ккал)\n"
                "- Йога 15 мин (~45 ккал)\n"
                "- Зарядка 10 мин (~50 ккал)"
            )
        elif user.burned_calories < 200:
            recs.append(
                "Тренировка: хорошее начало. Для большего эффекта:\n"
                "- 15 мин кардио (~120 ккал)\n"
                "- Вечерняя прогулка 30 мин (~120 ккал)"
            )
        else:
            recs.append("Тренировка: отличная активность сегодня.")

        await update.message.reply_text("Рекомендации:\n\n" + "\n\n".join(recs))

    def build_app(self) -> Application:
        app = Application.builder().token(self.config.telegram_token).build()

        profile_handler = ConversationHandler(
            entry_points=[CommandHandler("set_profile", self.profile_start)],
            states={
                ProfileState.WEIGHT: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.profile_weight)],
                ProfileState.HEIGHT: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.profile_height)],
                ProfileState.AGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.profile_age)],
                ProfileState.GENDER: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.profile_gender)],
                ProfileState.ACTIVITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.profile_activity)],
                ProfileState.CITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.profile_city)],
                ProfileState.CALORIE_GOAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.profile_calorie_goal)],
            },
            fallbacks=[CommandHandler("cancel", self.profile_cancel)],
        )

        food_handler = ConversationHandler(
            entry_points=[CommandHandler("log_food", self.food_start)],
            states={
                FoodState.GRAMS: [
                    CommandHandler("cancel", self.food_cancel),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self.food_grams),
                ],
            },
            fallbacks=[CommandHandler("cancel", self.food_cancel)],
        )

        app.add_handler(CommandHandler("start", self.cmd_start))
        app.add_handler(CommandHandler("help", self.cmd_help))
        app.add_handler(profile_handler)
        app.add_handler(food_handler)
        app.add_handler(CommandHandler("log_water", self.cmd_log_water))
        app.add_handler(CommandHandler("log_workout", self.cmd_log_workout))
        app.add_handler(CommandHandler("check_progress", self.cmd_progress))
        app.add_handler(CommandHandler("chart", self.cmd_chart))
        app.add_handler(CommandHandler("recommend", self.cmd_recommend))

        return app

    def run(self):
        if not self.config.telegram_token:
            log.error("TELEGRAM_TOKEN not set")
            return
        log.info("Bot started")
        self.build_app().run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    bot = FitnessBot(Config())
    bot.run()
