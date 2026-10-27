import base64
import io
import json
import logging
import os
import re
import uuid
from datetime import date as date_type
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import jwt
from dotenv import load_dotenv
from fastapi import (
    Body,
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    UploadFile,
    status,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from fastapi.staticfiles import StaticFiles
from openai import OpenAI
from passlib.context import CryptContext
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, EmailStr, Field
from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    inspect,
    text,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, declarative_base, relationship, sessionmaker

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("nutriai")

# ---------------------------------------------------------------------------
# Настройки приложения
# ---------------------------------------------------------------------------

APP_TIMEZONE = os.getenv("APP_TIMEZONE", "Asia/Yekaterinburg").strip()
try:
    APP_TZ = ZoneInfo(APP_TIMEZONE)
except Exception:
    logger.warning("Неизвестный часовой пояс %r, используется UTC", APP_TIMEZONE)
    APP_TZ = timezone.utc
    APP_TIMEZONE = "UTC"


def now_utc() -> datetime:
    """Наивное UTC-время: SQLite не хранит tzinfo, так проще и предсказуемее."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def today_local() -> date_type:
    """«Сегодня» по часовому поясу пользователя, а не по UTC."""
    return datetime.now(APP_TZ).date()


def local_midnight(value: date_type) -> datetime:
    return datetime.combine(value, time.min)


DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./data/nutriai.db").strip()

if DATABASE_URL.startswith("sqlite"):
    db_path = DATABASE_URL.split("///", 1)[-1]
    if db_path and db_path != ":memory:":
        Path(db_path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
else:
    engine = create_engine(DATABASE_URL, pool_pre_ping=True)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

MEDIA_ROOT = Path(os.getenv("MEDIA_ROOT", "./media")).expanduser()
MEDIA_ROOT.mkdir(parents=True, exist_ok=True)
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "10"))

SECRET_KEY = os.getenv("SECRET_KEY", "super-secret-key-for-demo-change-me")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = int(
    os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", str(60 * 24 * 7))
)

AI_API_KEY = os.getenv("AI_API_KEY", "").strip()
AI_BASE_URL = os.getenv("AI_BASE_URL", "https://openrouter.ai/api/v1").strip().rstrip("/")
AI_MODEL = os.getenv("AI_MODEL", "").strip()
AI_TIMEOUT_SECONDS = float(os.getenv("AI_TIMEOUT_SECONDS", "60"))
AI_MAX_TOKENS = int(os.getenv("AI_MAX_TOKENS", "3000"))
AI_RECOMMENDATION_MAX_TOKENS = int(
    os.getenv("AI_RECOMMENDATION_MAX_TOKENS", "4000")
)
AI_SITE_URL = os.getenv("AI_SITE_URL", "http://localhost:8001").strip()
AI_APP_NAME = os.getenv("AI_APP_NAME", "NutriAI Tracker").strip()
AI_ENABLED = bool(AI_API_KEY and AI_MODEL)

ai_client: Optional[OpenAI] = None
if AI_ENABLED:
    ai_client = OpenAI(
        api_key=AI_API_KEY,
        base_url=AI_BASE_URL,
        timeout=AI_TIMEOUT_SECONDS,
        default_headers={"HTTP-Referer": AI_SITE_URL, "X-Title": AI_APP_NAME},
    )
    logger.info("ИИ включён: %s / %s", AI_BASE_URL, AI_MODEL)
else:
    logger.warning("ИИ выключен: задайте AI_API_KEY и AI_MODEL в .env")

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")


class AIServiceError(Exception):
    def __init__(self, message: str, status_code: int = 502):
        super().__init__(message)
        self.status_code = status_code


# ---------------------------------------------------------------------------
# Модели БД
# ---------------------------------------------------------------------------


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, index=True, nullable=False)
    hashed_password = Column(String, nullable=True)
    external_id = Column(String, unique=True, nullable=True)
    created_at = Column(DateTime, default=now_utc)

    profile = relationship("Profile", back_populates="user", uselist=False)
    daily_records = relationship("DailyRecord", back_populates="user")
    goals = relationship("Goal", back_populates="user")


class Profile(Base):
    __tablename__ = "profiles"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), unique=True)
    age = Column(Integer, nullable=True)
    height_cm = Column(Float, nullable=True)
    weight_kg = Column(Float, nullable=True)
    lifestyle = Column(String, nullable=True)
    updated_at = Column(DateTime, default=now_utc, onupdate=now_utc)

    user = relationship("User", back_populates="profile")


class Goal(Base):
    __tablename__ = "goals"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    goal_type = Column(String, nullable=False)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=now_utc)

    user = relationship("User", back_populates="goals")


class DailyRecord(Base):
    __tablename__ = "daily_records"
    __table_args__ = (
        UniqueConstraint("user_id", "date", name="uq_daily_records_user_date"),
    )

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    date = Column(DateTime, nullable=False, index=True)
    weight_kg = Column(Float, nullable=True)
    steps = Column(Integer, nullable=True)
    pulse_avg = Column(Integer, nullable=True)
    pulse_min = Column(Integer, nullable=True)
    pulse_max = Column(Integer, nullable=True)
    wellbeing = Column(Text, nullable=True)
    created_at = Column(DateTime, default=now_utc)
    updated_at = Column(DateTime, default=now_utc, onupdate=now_utc)

    user = relationship("User", back_populates="daily_records")
    meals = relationship(
        "Meal", back_populates="daily_record", cascade="all, delete-orphan"
    )


class Meal(Base):
    __tablename__ = "meals"

    id = Column(Integer, primary_key=True, index=True)
    daily_record_id = Column(Integer, ForeignKey("daily_records.id"))
    dish_name = Column(String, nullable=False)
    meal_type = Column(String, nullable=True)
    weight_g = Column(Float, default=0)
    calories = Column(Float, default=0)
    protein = Column(Float, default=0)
    fat = Column(Float, default=0)
    carbs = Column(Float, default=0)
    is_manual = Column(Boolean, default=False)
    photo_url = Column(String, nullable=True)
    ai_raw_response = Column(Text, nullable=True)
    created_at = Column(DateTime, default=now_utc)

    daily_record = relationship("DailyRecord", back_populates="meals")


class Recommendation(Base):
    __tablename__ = "recommendations"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    daily_record_id = Column(
        Integer, ForeignKey("daily_records.id"), nullable=True, index=True
    )
    date = Column(DateTime, nullable=False, index=True)
    content = Column(Text, nullable=False)
    model_name = Column(String, nullable=True)
    is_fallback = Column(Boolean, default=False)
    raw_response = Column(Text, nullable=True)
    created_at = Column(DateTime, default=now_utc)


class Snack(Base):
    """Позиция в автомате выдачи перекусов."""

    __tablename__ = "snacks"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    weight_g = Column(Float, default=0)
    calories = Column(Float, default=0)
    protein = Column(Float, default=0)
    fat = Column(Float, default=0)
    carbs = Column(Float, default=0)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=now_utc)


class SnackIssue(Base):
    """Журнал выдач: и разрешённые, и отклонённые запросы."""

    __tablename__ = "snack_issues"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    daily_record_id = Column(
        Integer, ForeignKey("daily_records.id"), nullable=True, index=True
    )
    snack_id = Column(Integer, ForeignKey("snacks.id"), nullable=True)
    meal_id = Column(Integer, ForeignKey("meals.id"), nullable=True)
    device_id = Column(String, nullable=True)
    allowed = Column(Boolean, default=False)
    reason = Column(String, nullable=True)
    calories = Column(Float, default=0)
    protein = Column(Float, default=0)
    fat = Column(Float, default=0)
    carbs = Column(Float, default=0)
    consumed_before = Column(Float, default=0)
    consumed_after = Column(Float, default=0)
    daily_norm = Column(Float, nullable=True)
    created_at = Column(DateTime, default=now_utc)


Base.metadata.create_all(bind=engine)


def ensure_schema() -> None:
    """Простейшая миграция для уже существующей SQLite-базы.

    create_all() создаёт только отсутствующие таблицы и не добавляет
    новые колонки в существующие. Поэтому meal_type добавляем вручную.
    """
    inspector = inspect(engine)
    if "meals" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("meals")}
    if "meal_type" not in columns:
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE meals ADD COLUMN meal_type VARCHAR"))
        logger.info("Миграция: добавлена колонка meals.meal_type")


ensure_schema()

DEFAULT_SNACKS = [
    {
        "name": "Протеиновый батончик",
        "weight_g": 60,
        "calories": 180,
        "protein": 20,
        "fat": 6,
        "carbs": 12,
    },
    {
        "name": "Орехи (миндаль)",
        "weight_g": 35,
        "calories": 200,
        "protein": 7,
        "fat": 17,
        "carbs": 7,
    },
    {
        "name": "Яблоко",
        "weight_g": 180,
        "calories": 95,
        "protein": 0.5,
        "fat": 0.3,
        "carbs": 25,
    },
]


def seed_snacks() -> None:
    """Заполняет каталог снеков при первом запуске."""
    with SessionLocal() as db:
        if db.query(Snack).count() > 0:
            return
        for item in DEFAULT_SNACKS:
            db.add(Snack(**item))
        db.commit()
        logger.info("Каталог снеков заполнен: %s позиций", len(DEFAULT_SNACKS))


seed_snacks()


# ---------------------------------------------------------------------------
# Схемы запросов
# ---------------------------------------------------------------------------


class UserCreate(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)


class UserResponse(BaseModel):
    id: int
    email: str
    model_config = ConfigDict(from_attributes=True)


class ProfileUpdate(BaseModel):
    age: Optional[int] = Field(None, ge=1, le=120)
    height_cm: Optional[float] = Field(None, ge=50, le=250)
    weight_kg: Optional[float] = Field(None, ge=20, le=300)
    lifestyle: Optional[str] = Field(
        None, pattern="^(sedentary|light|moderate|active|very_active)$"
    )


class GoalUpdate(BaseModel):
    goal_type: str = Field(
        ..., pattern="^(lose_weight|gain_muscle|maintain|cardio_health)$"
    )


class DailyRecordCreate(BaseModel):
    date: str
    weight_kg: Optional[float] = Field(None, gt=0)
    steps: Optional[int] = Field(None, ge=0)
    pulse_avg: Optional[int] = Field(None, gt=0)
    pulse_min: Optional[int] = Field(None, gt=0)
    pulse_max: Optional[int] = Field(None, gt=0)
    wellbeing: Optional[str] = Field(None, max_length=5000)


class DailyRecordUpdate(BaseModel):
    weight_kg: Optional[float] = Field(None, gt=0)
    steps: Optional[int] = Field(None, ge=0)
    pulse_avg: Optional[int] = Field(None, gt=0)
    pulse_min: Optional[int] = Field(None, gt=0)
    pulse_max: Optional[int] = Field(None, gt=0)
    wellbeing: Optional[str] = Field(None, max_length=5000)


MEAL_TYPE_PATTERN = "^(breakfast|lunch|dinner|snack)$"


class MealCreate(BaseModel):
    dish_name: str = Field(..., min_length=1, max_length=255)
    meal_type: Optional[str] = Field(None, pattern=MEAL_TYPE_PATTERN)
    weight_g: float = Field(..., gt=0, le=10000)
    calories: float = Field(0, ge=0, le=10000)
    protein: float = Field(0, ge=0, le=1000)
    fat: float = Field(0, ge=0, le=1000)
    carbs: float = Field(0, ge=0, le=1000)
    photo_url: Optional[str] = Field(None, max_length=500)


class MealManualUpdate(BaseModel):
    dish_name: Optional[str] = Field(None, min_length=1, max_length=255)
    meal_type: Optional[str] = Field(None, pattern=MEAL_TYPE_PATTERN)
    weight_g: Optional[float] = Field(None, gt=0, le=10000)
    calories: Optional[float] = Field(None, ge=0, le=10000)
    protein: Optional[float] = Field(None, ge=0, le=1000)
    fat: Optional[float] = Field(None, ge=0, le=1000)
    carbs: Optional[float] = Field(None, ge=0, le=1000)


class SnackCheckRequest(BaseModel):
    device_id: Optional[str] = Field(None, max_length=100)
    snack_id: Optional[int] = Field(None, ge=1)


class SnackCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    weight_g: float = Field(..., gt=0, le=5000)
    calories: float = Field(..., gt=0, le=5000)
    protein: float = Field(0, ge=0, le=500)
    fat: float = Field(0, ge=0, le=500)
    carbs: float = Field(0, ge=0, le=500)


# ---------------------------------------------------------------------------
# Общие помощники
# ---------------------------------------------------------------------------


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_current_user(
    token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)
):
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Не удалось подтвердить учётные данные",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id_raw = payload.get("sub")
        if user_id_raw is None:
            raise credentials_exception
        user_id = int(user_id_raw)
    except (jwt.PyJWTError, TypeError, ValueError):
        raise credentials_exception

    user = db.query(User).filter(User.id == user_id).first()
    if user is None:
        raise credentials_exception
    return user


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    expire = datetime.now(timezone.utc) + (
        expires_delta or timedelta(minutes=15)
    )
    payload = {**data, "exp": expire}
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


ACTIVITY_FACTORS = {
    "sedentary": 1.2,
    "light": 1.375,
    "moderate": 1.55,
    "active": 1.725,
    "very_active": 1.9,
}


def calculate_daily_norm(
    profile: Optional[Profile], goal: Optional[Goal]
) -> Optional[dict]:
    """Mifflin-St Jeor + коэффициент активности + поправка на цель.

    Возвращает None, если профиль заполнен не полностью.
    """
    if (
        not profile
        or not profile.age
        or not profile.height_cm
        or not profile.weight_kg
        or not profile.lifestyle
    ):
        return None

    activity = ACTIVITY_FACTORS.get(profile.lifestyle, 1.2)
    bmr = 10 * profile.weight_kg + 6.25 * profile.height_cm - 5 * profile.age + 5
    tdee = bmr * activity

    if goal and goal.goal_type == "lose_weight":
        calories = tdee * 0.8
    elif goal and goal.goal_type == "gain_muscle":
        calories = tdee * 1.15
    else:
        calories = tdee

    protein = profile.weight_kg * 2.0
    fat = calories * 0.25 / 9
    carbs = (calories - protein * 4 - fat * 9) / 4

    return {
        "calories": round(calories, 1),
        "protein": round(protein, 1),
        "fat": round(fat, 1),
        "carbs": round(max(carbs, 0), 1),
    }


def get_active_goal(db: Session, user_id: int) -> Optional[Goal]:
    return (
        db.query(Goal)
        .filter(Goal.user_id == user_id, Goal.is_active == True)  # noqa: E712
        .first()
    )


def parse_date(value: str) -> date_type:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(
            status_code=400, detail="Неверный формат даты. Используйте YYYY-MM-DD"
        )


def ensure_not_future(value: date_type) -> None:
    if value > today_local():
        raise HTTPException(
            status_code=400, detail="Нельзя вносить данные за будущую дату"
        )


def get_record(
    db: Session, user_id: int, entry_date: date_type
) -> Optional[DailyRecord]:
    return (
        db.query(DailyRecord)
        .filter(
            DailyRecord.user_id == user_id,
            DailyRecord.date == local_midnight(entry_date),
        )
        .first()
    )


def get_or_create_record(
    db: Session, user_id: int, entry_date: date_type
) -> DailyRecord:
    record = get_record(db, user_id, entry_date)
    if record is not None:
        return record

    record = DailyRecord(user_id=user_id, date=local_midnight(entry_date))
    db.add(record)
    try:
        db.commit()
    except IntegrityError:
        # Параллельный запрос успел создать запись — используем её.
        db.rollback()
        record = get_record(db, user_id, entry_date)
        if record is None:
            raise
    else:
        db.refresh(record)
    return record


def totals_for_record(record: Optional[DailyRecord]) -> dict:
    meals = record.meals if record else []
    return {
        "calories": round(sum(m.calories or 0 for m in meals), 1),
        "protein": round(sum(m.protein or 0 for m in meals), 1),
        "fat": round(sum(m.fat or 0 for m in meals), 1),
        "carbs": round(sum(m.carbs or 0 for m in meals), 1),
    }


def balance_for(norm: Optional[dict], totals: dict) -> Optional[dict]:
    if norm is None:
        return None
    return {
        key: round(norm[key] - totals[key], 1)
        for key in ("calories", "protein", "fat", "carbs")
    }


MEAL_TYPES = {"breakfast", "lunch", "dinner", "snack"}


def default_meal_type() -> str:
    hour = datetime.now(APP_TZ).hour
    if 5 <= hour < 11:
        return "breakfast"
    if 11 <= hour < 16:
        return "lunch"
    if 16 <= hour < 20:
        return "dinner"
    return "snack"


def meal_to_dict(meal: Meal) -> dict:
    return {
        "id": meal.id,
        "dish_name": meal.dish_name,
        "meal_type": meal.meal_type,
        "weight_g": meal.weight_g,
        "calories": meal.calories,
        "protein": meal.protein,
        "fat": meal.fat,
        "carbs": meal.carbs,
        "is_manual": meal.is_manual,
        "photo_url": media_url(meal.photo_url),
    }


ALLOWED_IMAGE_FORMATS = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp"}
ALLOWED_CONTENT_TYPES = {
    "image/jpeg",
    "image/png",
    "image/webp",
    "application/octet-stream",
}


def read_image_upload(file: UploadFile) -> tuple[bytes, str]:
    if file.content_type and file.content_type not in ALLOWED_CONTENT_TYPES:
        raise HTTPException(
            status_code=415, detail="Поддерживаются только JPEG, PNG и WEBP"
        )

    limit = MAX_UPLOAD_MB * 1024 * 1024
    content = file.file.read(limit + 1)
    if not content:
        raise HTTPException(status_code=400, detail="Файл пустой")
    if len(content) > limit:
        raise HTTPException(
            status_code=413, detail=f"Файл больше {MAX_UPLOAD_MB} МБ"
        )

    try:
        with Image.open(io.BytesIO(content)) as image:
            image_format = image.format
            image.verify()
    except (
        UnidentifiedImageError,
        OSError,
        ValueError,
        SyntaxError,
        Image.DecompressionBombError,
    ) as exc:
        raise HTTPException(
            status_code=400, detail="Не удалось открыть изображение"
        ) from exc

    if image_format not in ALLOWED_IMAGE_FORMATS:
        raise HTTPException(
            status_code=415, detail="Поддерживаются только JPEG, PNG и WEBP"
        )

    return content, ALLOWED_IMAGE_FORMATS[image_format]


def store_photo(content: bytes, extension: str, entry_date: date_type) -> str:
    relative_dir = (
        Path("meals")
        / f"{entry_date.year:04d}"
        / f"{entry_date.month:02d}"
        / f"{entry_date.day:02d}"
    )
    absolute_dir = MEDIA_ROOT / relative_dir
    absolute_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{uuid.uuid4().hex}.{extension}"
    (absolute_dir / filename).write_bytes(content)
    return (relative_dir / filename).as_posix()


def media_url(relative_path: Optional[str]) -> Optional[str]:
    if not relative_path:
        return None
    return f"/media/{relative_path}"


def normalize_photo_path(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    path = value.strip()
    if path.startswith("/media/"):
        path = path[len("/media/") :]
    elif path.startswith("media/"):
        path = path[len("media/") :]
    if not path or path.startswith("/") or ".." in Path(path).parts:
        raise HTTPException(status_code=400, detail="Некорректный photo_url")
    return path


def prepare_image_for_ai(content: bytes) -> Image.Image:
    image = Image.open(io.BytesIO(content))
    image.load()
    image = image.convert("RGB")
    image.thumbnail((1024, 1024))
    return image


# ---------------------------------------------------------------------------
# ИИ
# ---------------------------------------------------------------------------


def _to_float(value, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number:  # NaN
        return default
    return number


def _parse_json_response(raw: str) -> dict:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.S)
        if not match:
            raise AIServiceError("ИИ вернул ответ не в формате JSON", 502)
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            raise AIServiceError("ИИ вернул ответ не в формате JSON", 502) from exc

    if not isinstance(parsed, dict):
        raise AIServiceError("ИИ вернул неожиданный формат ответа", 502)
    return parsed


def _normalize_ai_meal(data: dict) -> dict:
    if data.get("error"):
        raise AIServiceError(str(data["error"]), 422)

    dishes = data.get("dishes")
    if isinstance(dishes, list) and dishes:
        items = []
        for item in dishes:
            if not isinstance(item, dict):
                continue
            name = (
                str(item.get("name") or item.get("dish") or "Блюдо").strip()[:255]
                or "Блюдо"
            )
            items.append(
                {
                    "dish": name,
                    "weight_g": round(
                        max(_to_float(item.get("portion_grams", item.get("weight_g"))), 0.0), 1
                    ),
                    "calories": round(max(_to_float(item.get("calories")), 0.0), 1),
                    "protein": round(
                        max(
                            _to_float(item.get("protein_g", item.get("protein"))),
                            0.0,
                        ),
                        1,
                    ),
                    "fat": round(
                        max(_to_float(item.get("fat_g", item.get("fat"))), 0.0), 1
                    ),
                    "carbs": round(
                        max(_to_float(item.get("carbs_g", item.get("carbs"))), 0.0),
                        1,
                    ),
                }
            )

        if items and sum(item["weight_g"] for item in items) > 0:
            return {
                "dish": ", ".join(item["dish"] for item in items)[:255],
                "weight_g": round(sum(item["weight_g"] for item in items), 1),
                "calories": round(sum(item["calories"] for item in items), 1),
                "protein": round(sum(item["protein"] for item in items), 1),
                "fat": round(sum(item["fat"] for item in items), 1),
                "carbs": round(sum(item["carbs"] for item in items), 1),
                "dishes": items,
            }

    name = str(data.get("dish") or data.get("name") or "Блюдо").strip()[:255] or "Блюдо"
    weight = _to_float(data.get("weight_g", data.get("portion_grams")))
    if weight <= 0:
        raise AIServiceError("ИИ не определил массу блюда", 422)

    return {
        "dish": name,
        "weight_g": round(weight, 1),
        "calories": round(max(_to_float(data.get("calories")), 0.0), 1),
        "protein": round(
            max(_to_float(data.get("protein", data.get("protein_g"))), 0.0), 1
        ),
        "fat": round(max(_to_float(data.get("fat", data.get("fat_g"))), 0.0), 1),
        "carbs": round(
            max(_to_float(data.get("carbs", data.get("carbs_g"))), 0.0), 1
        ),
    }


def analyze_photo_with_ai(image: Image.Image) -> dict:
    if not AI_ENABLED or ai_client is None:
        raise AIServiceError(
            "ИИ не настроен. Задайте AI_API_KEY и AI_MODEL в .env", 503
        )

    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=85)
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")

    prompt = (
        "Ты — нутрициолог. Проанализируй фотографию еды. "
        "Определи название блюда, массу порции в граммах, калорийность, белки, жиры и углеводы. "
        "Верни ответ СТРОГО в формате JSON без markdown и пояснений, например:\n"
        '{"dish":"Салат Цезарь","weight_g":250,"calories":350,"protein":20,"fat":15,"carbs":30}\n'
        "Все числа положительные. Если на фото нет еды, верни "
        '{"error":"На фото не обнаружена еда"}.'
    )

    try:
        response = ai_client.chat.completions.create(
            model=AI_MODEL,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{encoded}"},
                        },
                    ],
                }
            ],
            temperature=0.1,
            max_tokens=AI_MAX_TOKENS,
            timeout=AI_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        logger.warning("Ошибка запроса к ИИ: %s", exc)
        raise AIServiceError(
            f"Не удалось получить ответ от ИИ: {str(exc)[:300]}", 502
        ) from exc

    raw = (response.choices[0].message.content or "").strip()
    logger.info("Ответ ИИ (первые 500 символов): %s", raw[:500])

    result = _normalize_ai_meal(_parse_json_response(raw))
    result["model_name"] = getattr(response, "model", None) or AI_MODEL
    result["raw_response"] = raw
    return result


def _as_string_list(value, limit: int = 10) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        text_item = str(item).strip()
        if text_item:
            result.append(text_item[:500])
        if len(result) >= limit:
            break
    return result


def _normalize_recommendation(data: dict, raw: str, model_name: str) -> dict:
    recipes: list[dict] = []
    raw_recipes = data.get("recipes")
    if isinstance(raw_recipes, list):
        for item in raw_recipes[:5]:
            if not isinstance(item, dict):
                continue
            recipes.append(
                {
                    "title": (
                        str(item.get("title") or "Рецепт").strip()[:120] or "Рецепт"
                    ),
                    "description": (
                        str(item.get("description") or "").strip()[:500] or None
                    ),
                    "calories": round(max(_to_float(item.get("calories")), 0.0), 1),
                    "protein": round(max(_to_float(item.get("protein")), 0.0), 1),
                    "fat": round(max(_to_float(item.get("fat")), 0.0), 1),
                    "carbs": round(max(_to_float(item.get("carbs")), 0.0), 1),
                    "ingredients": _as_string_list(item.get("ingredients"), 15),
                    "steps": _as_string_list(item.get("steps"), 10),
                }
            )

    content = {
        "summary": str(data.get("summary") or "").strip()[:1000],
        "nutrition_advice": _as_string_list(data.get("nutrition_advice"), 8),
        "activity_advice": _as_string_list(data.get("activity_advice"), 8),
        "recipes": recipes,
    }
    if not any(
        (
            content["summary"],
            content["nutrition_advice"],
            content["activity_advice"],
            content["recipes"],
        )
    ):
        raise AIServiceError("ИИ вернул пустые рекомендации", 502)
    return {"content": content, "model_name": model_name, "raw_response": raw}


def generate_recommendation_with_ai(context: dict) -> dict:
    if not AI_ENABLED or ai_client is None:
        raise AIServiceError(
            "ИИ не настроен. Задайте AI_API_KEY и AI_MODEL в .env", 503
        )

    system_prompt = (
        "Ты — персональный нутрициолог. На основе цели, профиля, суточной нормы, "
        "фактического рациона и баланса КБЖУ дай короткие практичные рекомендации "
        "на русском языке. Не ставь медицинских диагнозов. "
        "Верни строго JSON без markdown и пояснений."
    )
    user_prompt = (
        f"Данные пользователя за день: {json.dumps(context, ensure_ascii=False)}.\n\n"
        "Формат ответа:\n"
        '{"summary":"краткий вывод",'
        '"nutrition_advice":["совет 1","совет 2"],'
        '"activity_advice":["совет 1"],'
        '"recipes":[{"title":"название","description":"краткое описание",'
        '"calories":320,"protein":25,"fat":10,"carbs":35,'
        '"ingredients":["..."],"steps":["..."]}]}'
    )

    try:
        response = ai_client.chat.completions.create(
            model=AI_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.3,
            max_tokens=AI_RECOMMENDATION_MAX_TOKENS,
            timeout=AI_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        logger.warning("Ошибка запроса рекомендаций к ИИ: %s", exc)
        raise AIServiceError(
            f"Не удалось получить рекомендации от ИИ: {str(exc)[:300]}", 502
        ) from exc

    message = response.choices[0].message
    raw = (message.content or "").strip()
    if not raw:
        raw = (getattr(message, "reasoning", None) or "").strip()
    logger.info("Ответ ИИ по рекомендациям (первые 500 символов): %s", raw[:500])

    return _normalize_recommendation(
        _parse_json_response(raw),
        raw,
        getattr(response, "model", None) or AI_MODEL,
    )


def _fallback_recommendation(context: dict) -> dict:
    """Детерминированные рекомендации, если внешний ИИ недоступен."""
    balance = context.get("balance") or {}
    calorie_delta = _to_float(balance.get("calories"))
    protein_delta = _to_float(balance.get("protein"))
    steps = context.get("steps")

    if calorie_delta < -200:
        summary = (
            f"Сегодня перебор около {abs(calorie_delta):.0f} ккал. "
            "Следующий приём пищи сделайте легче и добавьте активности."
        )
    elif calorie_delta > 200:
        summary = (
            f"Сегодня дефицит около {calorie_delta:.0f} ккал. "
            "Следите, чтобы недобор не был слишком большим."
        )
    else:
        summary = "Калорийность близка к суточной норме. Продолжайте в том же режиме."

    nutrition_advice = [
        "Следите за достаточным количеством овощей и клетчатки.",
        "Распределяйте белок равномерно между приёмами пищи.",
    ]
    if protein_delta > 20:
        nutrition_advice.insert(
            0, "Добавьте белковый продукт: творог, курицу, рыбу или яйца."
        )
    if calorie_delta < -200:
        nutrition_advice.insert(
            0, "Уменьшите калорийность следующего приёма пищи на 150–200 ккал."
        )

    activity_advice = []
    if steps is None:
        activity_advice.append(
            "Внесите количество шагов за день, чтобы оценка активности была точнее."
        )
    elif steps < 6000:
        activity_advice.append(
            "Пройдите ещё 2000–3000 шагов или сделайте 10-минутную разминку."
        )
    else:
        activity_advice.append("Активность в норме. Добавьте лёгкую растяжку после дня.")

    recipes = [
        {
            "title": "Творог с ягодами",
            "description": "Быстрый белковый перекус",
            "calories": 220,
            "protein": 25,
            "fat": 6,
            "carbs": 18,
            "ingredients": ["творог 200 г", "ягоды 80 г"],
            "steps": ["Смешать творог с ягодами."],
        },
        {
            "title": "Омлет с овощами",
            "description": "Сбалансированный завтрак",
            "calories": 280,
            "protein": 20,
            "fat": 18,
            "carbs": 9,
            "ingredients": ["2 яйца", "помидор", "перец", "зелень"],
            "steps": ["Взбить яйца.", "Обжарить овощи.", "Залить яйцами и готовить 5 минут."],
        },
    ]
    return {
        "content": {
            "summary": summary,
            "nutrition_advice": nutrition_advice,
            "activity_advice": activity_advice,
            "recipes": recipes,
        },
        "model_name": "rule-based",
        "raw_response": None,
    }


def build_recommendation_context(
    entry_date, record, totals, norm, balance, profile, goal
) -> dict:
    return {
        "date": entry_date.isoformat(),
        "goal": goal.goal_type if goal else None,
        "profile": {
            "age": profile.age if profile else None,
            "height_cm": profile.height_cm if profile else None,
            "weight_kg": profile.weight_kg if profile else None,
            "lifestyle": profile.lifestyle if profile else None,
        },
        "norm": norm,
        "consumed": totals,
        "balance": balance,
        "steps": record.steps if record else None,
        "pulse": {
            "avg": record.pulse_avg if record else None,
            "min": record.pulse_min if record else None,
            "max": record.pulse_max if record else None,
        },
        "wellbeing": record.wellbeing if record else None,
        "meals": [
            {
                "dish_name": meal.dish_name,
                "meal_type": meal.meal_type,
                "weight_g": meal.weight_g,
                "calories": meal.calories,
                "protein": meal.protein,
                "fat": meal.fat,
                "carbs": meal.carbs,
            }
            for meal in (record.meals if record else [])
        ][:50],
    }


# ---------------------------------------------------------------------------
# Приложение
# ---------------------------------------------------------------------------

app = FastAPI(
    title="NutriAI Tracker API",
    version="0.3.0",
    description=(
        "Backend системы персонального контроля питания и физической активности. "
        "Этапы 0-1: стабильная база, ИИ-рекомендации, типы приёмов пищи и CRUD дневных записей."
    ),
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/media", StaticFiles(directory=str(MEDIA_ROOT)), name="media")


@app.get("/", tags=["System"])
def root():
    return {
        "service": "NutriAI Tracker",
        "status": "alive",
        "docs": "/docs",
        "ai_enabled": AI_ENABLED,
    }


@app.get("/health", tags=["System"])
def health_check():
    return {
        "status": "healthy",
        "timestamp": now_utc().isoformat() + "Z",
        "today": today_local().isoformat(),
        "timezone": APP_TIMEZONE,
        "ai_enabled": AI_ENABLED,
        "ai_model": AI_MODEL if AI_ENABLED else None,
        "database": DATABASE_URL.split(":", 1)[0],
    }


@app.get("/ai/status", tags=["AI"])
def ai_status():
    return {
        "configured": AI_ENABLED,
        "base_url": AI_BASE_URL,
        "model": AI_MODEL or None,
        "timeout_seconds": AI_TIMEOUT_SECONDS,
        "max_tokens": AI_MAX_TOKENS,
        "recommendation_max_tokens": AI_RECOMMENDATION_MAX_TOKENS,
        "hint": (
            "Ключ и модель задаются в .env: AI_API_KEY, AI_BASE_URL, AI_MODEL"
            if not AI_ENABLED
            else "Настройки загружены. Проверка: GET /ai/check"
        ),
    }


@app.get("/ai/check", tags=["AI"])
def ai_check():
    if not AI_ENABLED or ai_client is None:
        raise HTTPException(
            status_code=503,
            detail="ИИ не настроен: задайте AI_API_KEY и AI_MODEL в .env",
        )
    try:
        response = ai_client.chat.completions.create(
            model=AI_MODEL,
            messages=[{"role": "user", "content": "Ответь одним словом: работает"}],
            temperature=0,
            max_tokens=AI_MAX_TOKENS,
            timeout=AI_TIMEOUT_SECONDS,
        )
        answer = (response.choices[0].message.content or "").strip()
    except Exception as exc:
        logger.warning("Проверка ИИ не удалась: %s", exc)
        raise HTTPException(
            status_code=502, detail=f"Ошибка ИИ: {str(exc)[:300]}"
        ) from exc
    return {"ok": True, "model": AI_MODEL, "answer": answer}


@app.post("/auth/register", response_model=UserResponse, tags=["Auth"])
def register(user_data: UserCreate, db: Session = Depends(get_db)):
    email = user_data.email.lower()
    existing = db.query(User).filter(User.email == email).first()
    if existing:
        raise HTTPException(
            status_code=400, detail="Пользователь с таким email уже существует"
        )

    user = User(
        email=email,
        hashed_password=pwd_context.hash(user_data.password),
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    db.add(Profile(user_id=user.id))
    db.commit()
    return user


@app.post("/auth/login", tags=["Auth"])
def login(
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(get_db),
):
    email = form_data.username.strip().lower()
    user = db.query(User).filter(User.email == email).first()
    if not user or not user.hashed_password:
        raise HTTPException(status_code=401, detail="Неверный email или пароль")
    if not pwd_context.verify(form_data.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Неверный email или пароль")

    token = create_access_token(
        {"sub": str(user.id)}, timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    return {
        "access_token": token,
        "token_type": "bearer",
        "user_id": user.id,
        "email": user.email,
    }


@app.post("/auth/external", tags=["Auth"])
def external_login(
    provider: str,
    external_token: str,
    db: Session = Depends(get_db),
):
    """Заглушка внешней авторизации: реальный OAuth будет на следующем этапе."""
    fake_external_id = f"{provider}_{external_token}"[:255]
    user = db.query(User).filter(User.external_id == fake_external_id).first()
    if not user:
        user = User(
            email=f"{uuid.uuid4().hex}@external.local",
            external_id=fake_external_id,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        db.add(Profile(user_id=user.id))
        db.commit()

    token = create_access_token(
        {"sub": str(user.id)}, timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    return {
        "access_token": token,
        "token_type": "bearer",
        "user_id": user.id,
        "note": "Заглушка внешней авторизации",
    }


@app.get("/profile", tags=["Profile"])
def get_profile(
    current_user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    profile = db.query(Profile).filter(Profile.user_id == current_user.id).first()
    if not profile:
        raise HTTPException(status_code=404, detail="Профиль не найден")

    goal = get_active_goal(db, current_user.id)
    return {
        "user_id": current_user.id,
        "email": current_user.email,
        "age": profile.age,
        "height_cm": profile.height_cm,
        "weight_kg": profile.weight_kg,
        "lifestyle": profile.lifestyle,
        "goal": goal.goal_type if goal else None,
        "norm": calculate_daily_norm(profile, goal),
    }


@app.put("/profile", tags=["Profile"])
def update_profile(
    data: ProfileUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    profile = db.query(Profile).filter(Profile.user_id == current_user.id).first()
    if not profile:
        raise HTTPException(status_code=404, detail="Профиль не найден")

    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(profile, field, value)
    db.commit()
    db.refresh(profile)

    goal = get_active_goal(db, current_user.id)
    return {
        "status": "updated",
        "profile": {
            "age": profile.age,
            "height_cm": profile.height_cm,
            "weight_kg": profile.weight_kg,
            "lifestyle": profile.lifestyle,
        },
        "norm": calculate_daily_norm(profile, goal),
    }


@app.get("/goals", tags=["Goals"])
def get_goals(
    current_user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    goals = (
        db.query(Goal)
        .filter(Goal.user_id == current_user.id)
        .order_by(Goal.created_at.desc())
        .all()
    )
    return {
        "goals": [
            {
                "id": goal.id,
                "goal_type": goal.goal_type,
                "is_active": goal.is_active,
                "created_at": goal.created_at.isoformat() if goal.created_at else None,
            }
            for goal in goals
        ]
    }


@app.post("/goals", tags=["Goals"])
def set_goal(
    data: GoalUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    db.query(Goal).filter(Goal.user_id == current_user.id).update(
        {"is_active": False}
    )
    goal = Goal(user_id=current_user.id, goal_type=data.goal_type, is_active=True)
    db.add(goal)
    db.commit()
    db.refresh(goal)

    profile = db.query(Profile).filter(Profile.user_id == current_user.id).first()
    return {
        "status": "created",
        "goal": {
            "id": goal.id,
            "goal_type": goal.goal_type,
            "is_active": goal.is_active,
        },
        "norm": calculate_daily_norm(profile, goal),
    }


@app.post("/daily-records", tags=["Daily Records"])
def create_daily_record(
    data: DailyRecordCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    record_date = parse_date(data.date)
    ensure_not_future(record_date)

    if data.pulse_min and data.pulse_avg and data.pulse_max:
        if not (data.pulse_min <= data.pulse_avg <= data.pulse_max):
            raise HTTPException(
                status_code=400,
                detail="Пульс: минимальный <= средний <= максимальный",
            )

    existing = get_record(db, current_user.id, record_date)
    if existing:
        for field in (
            "weight_kg",
            "steps",
            "pulse_avg",
            "pulse_min",
            "pulse_max",
            "wellbeing",
        ):
            value = getattr(data, field)
            if value is not None:
                setattr(existing, field, value)
        db.commit()
        db.refresh(existing)
        return {
            "status": "updated",
            "record_id": existing.id,
            "date": existing.date.strftime("%Y-%m-%d"),
        }

    record = DailyRecord(
        user_id=current_user.id,
        date=local_midnight(record_date),
        weight_kg=data.weight_kg,
        steps=data.steps,
        pulse_avg=data.pulse_avg,
        pulse_min=data.pulse_min,
        pulse_max=data.pulse_max,
        wellbeing=data.wellbeing,
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return {
        "status": "created",
        "record_id": record.id,
        "date": record.date.strftime("%Y-%m-%d"),
    }


@app.put("/daily-records/{date}", tags=["Daily Records"])
def update_daily_record(
    date: str,
    data: DailyRecordUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    record_date = parse_date(date)
    ensure_not_future(record_date)
    record = get_record(db, current_user.id, record_date)
    if not record:
        raise HTTPException(
            status_code=404, detail="Запись за эту дату не найдена"
        )

    pulse_min = (
        data.pulse_min if data.pulse_min is not None else record.pulse_min
    )
    pulse_avg = (
        data.pulse_avg if data.pulse_avg is not None else record.pulse_avg
    )
    pulse_max = (
        data.pulse_max if data.pulse_max is not None else record.pulse_max
    )
    if None not in (pulse_min, pulse_avg, pulse_max):
        if not (pulse_min <= pulse_avg <= pulse_max):
            raise HTTPException(
                status_code=400,
                detail="Пульс: минимальный <= средний <= максимальный",
            )

    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(record, field, value)
    db.commit()
    db.refresh(record)
    return {
        "status": "updated",
        "record_id": record.id,
        "date": record.date.strftime("%Y-%m-%d"),
    }


@app.delete("/daily-records/{date}", tags=["Daily Records"])
def delete_daily_record(
    date: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    record_date = parse_date(date)
    record = get_record(db, current_user.id, record_date)
    if not record:
        raise HTTPException(
            status_code=404, detail="Запись за эту дату не найдена"
        )

    db.query(Recommendation).filter(
        Recommendation.user_id == current_user.id,
        Recommendation.date == local_midnight(record_date),
    ).delete(synchronize_session=False)
    record_id = record.id
    db.delete(record)
    db.commit()
    return {
        "status": "deleted",
        "record_id": record_id,
        "date": record_date.isoformat(),
    }


@app.get("/daily-records", tags=["Daily Records"])
def list_daily_records(
    current_user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    records = (
        db.query(DailyRecord)
        .filter(DailyRecord.user_id == current_user.id)
        .order_by(DailyRecord.date.desc())
        .all()
    )
    return {
        "records": [
            {
                "id": record.id,
                "date": record.date.strftime("%Y-%m-%d"),
                "weight_kg": record.weight_kg,
                "steps": record.steps,
                "pulse_avg": record.pulse_avg,
                "pulse_min": record.pulse_min,
                "pulse_max": record.pulse_max,
                "wellbeing": record.wellbeing,
                "meals_count": len(record.meals),
                "total_calories": round(
                    sum(meal.calories or 0 for meal in record.meals), 1
                ),
            }
            for record in records
        ]
    }


@app.get("/daily-records/{date}", tags=["Daily Records"])
def get_daily_record(
    date: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    record_date = parse_date(date)
    record = get_record(db, current_user.id, record_date)
    if not record:
        raise HTTPException(
            status_code=404, detail="Запись за эту дату не найдена"
        )

    totals = totals_for_record(record)
    profile = db.query(Profile).filter(Profile.user_id == current_user.id).first()
    goal = get_active_goal(db, current_user.id)
    norm = calculate_daily_norm(profile, goal)

    return {
        "date": record.date.strftime("%Y-%m-%d"),
        "weight_kg": record.weight_kg,
        "steps": record.steps,
        "pulse_avg": record.pulse_avg,
        "pulse_min": record.pulse_min,
        "pulse_max": record.pulse_max,
        "wellbeing": record.wellbeing,
        "meals": [meal_to_dict(meal) for meal in record.meals],
        "statistics": {
            "consumed": totals,
            "norm": norm,
            "balance": balance_for(norm, totals),
        },
    }


@app.post("/daily-records/{date}/meals", tags=["Meals"])
def add_meal(
    date: str,
    data: MealCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Ручное добавление блюда — сценарий A2, когда ИИ не распознал фото."""
    record_date = parse_date(date)
    ensure_not_future(record_date)
    record = get_or_create_record(db, current_user.id, record_date)

    meal = Meal(
        daily_record_id=record.id,
        dish_name=data.dish_name.strip(),
        meal_type=data.meal_type or default_meal_type(),
        weight_g=data.weight_g,
        calories=data.calories,
        protein=data.protein,
        fat=data.fat,
        carbs=data.carbs,
        is_manual=True,
        photo_url=normalize_photo_path(data.photo_url),
    )
    db.add(meal)
    record.updated_at = now_utc()
    db.commit()
    db.refresh(meal)

    totals = totals_for_record(record)
    return {
        "status": "created",
        "meal": meal_to_dict(meal),
        "statistics": {"consumed": totals},
    }


@app.post("/daily-records/{date}/analyze-photo", tags=["AI Analysis"])
def analyze_photo_for_day(
    date: str,
    file: UploadFile = File(...),
    meal_type: Optional[str] = Form(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Синхронный эндпоинт: FastAPI выполняет его в пуле потоков.

    Поэтому долгий запрос к ИИ не блокирует остальные запросы приложения.
    """
    record_date = parse_date(date)
    ensure_not_future(record_date)
    if meal_type and meal_type not in MEAL_TYPES:
        raise HTTPException(
            status_code=400,
            detail="meal_type должен быть breakfast, lunch, dinner или snack",
        )
    meal_type = meal_type or default_meal_type()
    record = get_or_create_record(db, current_user.id, record_date)

    content, extension = read_image_upload(file)
    relative_path = store_photo(content, extension, record_date)
    photo_url = media_url(relative_path)

    try:
        ai_result = analyze_photo_with_ai(prepare_image_for_ai(content))
    except AIServiceError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={
                "message": str(exc),
                "photo_url": photo_url,
                "can_enter_manually": True,
            },
        )
    except Exception:
        logger.exception("Неожиданная ошибка при анализе фото")
        raise HTTPException(
            status_code=500,
            detail={
                "message": "Внутренняя ошибка при анализе фото",
                "photo_url": photo_url,
                "can_enter_manually": True,
            },
        )

    meal = Meal(
        daily_record_id=record.id,
        dish_name=ai_result["dish"],
        meal_type=meal_type,
        weight_g=ai_result["weight_g"],
        calories=ai_result["calories"],
        protein=ai_result["protein"],
        fat=ai_result["fat"],
        carbs=ai_result["carbs"],
        is_manual=False,
        photo_url=relative_path,
        ai_raw_response=ai_result.get("raw_response"),
    )
    db.add(meal)
    record.updated_at = now_utc()
    db.commit()
    db.refresh(meal)

    return {
        "success": True,
        "record_id": record.id,
        "meal": meal_to_dict(meal),
        "ai": {
            "model": ai_result.get("model_name"),
            "dishes": ai_result.get("dishes"),
        },
    }


def _get_owned_meal(db: Session, meal_id: int, user_id: int) -> Meal:
    meal = (
        db.query(Meal)
        .join(DailyRecord)
        .filter(Meal.id == meal_id, DailyRecord.user_id == user_id)
        .first()
    )
    if not meal:
        raise HTTPException(status_code=404, detail="Блюдо не найдено")
    return meal


@app.put("/meals/{meal_id}", tags=["Meals"])
def update_meal(
    meal_id: int,
    data: MealManualUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    meal = _get_owned_meal(db, meal_id, current_user.id)

    for field, value in data.model_dump(exclude_unset=True).items():
        if field == "dish_name" and value is not None:
            value = value.strip()
        setattr(meal, field, value)
    meal.is_manual = True
    meal.daily_record.updated_at = now_utc()
    db.commit()
    db.refresh(meal)

    return {
        "status": "updated",
        "meal": meal_to_dict(meal),
        "statistics": {"consumed": totals_for_record(meal.daily_record)},
    }


@app.delete("/meals/{meal_id}", tags=["Meals"])
def delete_meal(
    meal_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    meal = _get_owned_meal(db, meal_id, current_user.id)
    record = meal.daily_record
    db.delete(meal)
    record.updated_at = now_utc()
    db.commit()
    return {"status": "deleted", "meal_id": meal_id}


def snack_to_dict(snack: Snack) -> dict:
    return {
        "id": snack.id,
        "name": snack.name,
        "weight_g": snack.weight_g,
        "calories": snack.calories,
        "protein": snack.protein,
        "fat": snack.fat,
        "carbs": snack.carbs,
    }


def resolve_snack(db: Session, snack_id: Optional[int]) -> Snack:
    if snack_id is not None:
        snack = db.get(Snack, snack_id)
        if snack is None or not snack.is_active:
            raise HTTPException(status_code=404, detail="Снек не найден")
        return snack

    snack = (
        db.query(Snack)
        .filter(Snack.is_active == True)  # noqa: E712
        .order_by(Snack.id)
        .first()
    )
    if snack is None:
        raise HTTPException(status_code=503, detail="Каталог снеков пуст")
    return snack


def _snack_base_response(
    entry_date: date_type,
    snack: Snack,
    device_id: Optional[str],
) -> dict:
    return {
        "date": entry_date.isoformat(),
        "device_id": device_id,
        "snack": snack_to_dict(snack),
        "issue_id": None,
        "meal_id": None,
        "meal": None,
        "added_to_diary": False,
        "consumed_before": None,
        "consumed": None,
        "limit": None,
        "remaining": None,
        "allowed": False,
        "reason": None,
        "message": "",
        "indicator": "RED",
    }


def _limit_for(db: Session, user: User) -> Optional[float]:
    profile = db.query(Profile).filter(Profile.user_id == user.id).first()
    goal = get_active_goal(db, user.id)
    norm = calculate_daily_norm(profile, goal)
    return norm["calories"] if norm else None


def build_snack_status(
    db: Session,
    user: User,
    snack: Snack,
    device_id: Optional[str] = None,
) -> dict:
    """Статус без побочных эффектов: ничего не пишет в БД."""
    entry_date = today_local()
    record = get_record(db, user.id, entry_date)
    totals = totals_for_record(record)
    consumed = totals["calories"]
    limit = _limit_for(db, user)

    response = _snack_base_response(entry_date, snack, device_id)
    response["consumed_before"] = consumed
    response["consumed"] = consumed

    if limit is None:
        response.update(
            reason="PROFILE_INCOMPLETE",
            message="Сначала заполните профиль: возраст, рост, вес и образ жизни.",
        )
        return response

    remaining = round(limit - consumed, 1)
    response["limit"] = round(limit, 1)
    response["remaining"] = remaining

    if snack.calories <= remaining:
        response.update(
            allowed=True,
            reason="WITHIN_LIMIT",
            message=f"Готов к выдаче: «{snack.name}», {snack.calories:.0f} ккал.",
            indicator="GREEN",
        )
    elif remaining > 0:
        response.update(
            reason="NOT_ENOUGH_CALORIES",
            message=(
                f"Недостаточно лимита: осталось {remaining:.0f} ккал, "
                f"а снек содержит {snack.calories:.0f} ккал."
            ),
        )
    else:
        response.update(
            reason="LIMIT_EXCEEDED",
            message="Лимит калорий исчерпан.",
        )
    return response


def check_and_issue_snack(
    db: Session,
    user: User,
    snack: Snack,
    device_id: Optional[str] = None,
) -> dict:
    """Проверяет лимит и при разрешении записывает снек в дневник."""
    entry_date = today_local()
    record = get_record(db, user.id, entry_date)
    totals = totals_for_record(record)
    consumed_before = totals["calories"]
    limit = _limit_for(db, user)

    response = _snack_base_response(entry_date, snack, device_id)
    response["consumed_before"] = consumed_before
    response["consumed"] = consumed_before

    issue = SnackIssue(
        user_id=user.id,
        daily_record_id=record.id if record else None,
        snack_id=snack.id,
        device_id=device_id,
        calories=snack.calories,
        protein=snack.protein,
        fat=snack.fat,
        carbs=snack.carbs,
        consumed_before=consumed_before,
        daily_norm=limit,
    )

    if limit is None:
        issue.reason = "PROFILE_INCOMPLETE"
        issue.consumed_after = consumed_before
        db.add(issue)
        db.commit()
        response.update(
            reason="PROFILE_INCOMPLETE",
            message="Сначала заполните профиль: возраст, рост, вес и образ жизни.",
            issue_id=issue.id,
        )
        return response

    remaining = round(limit - consumed_before, 1)
    response["limit"] = round(limit, 1)
    response["remaining"] = remaining

    if snack.calories > remaining:
        reason = "NOT_ENOUGH_CALORIES" if remaining > 0 else "LIMIT_EXCEEDED"
        if remaining > 0:
            message = (
                f"Недостаточно лимита: осталось {remaining:.0f} ккал, "
                f"а снек содержит {snack.calories:.0f} ккал."
            )
        else:
            message = "Лимит калорий исчерпан."

        issue.allowed = False
        issue.reason = reason
        issue.consumed_after = consumed_before
        db.add(issue)
        db.commit()
        response.update(reason=reason, message=message, issue_id=issue.id)
        return response

    record = get_or_create_record(db, user.id, entry_date)
    meal = Meal(
        daily_record_id=record.id,
        dish_name=snack.name,
        meal_type="snack",
        weight_g=snack.weight_g,
        calories=snack.calories,
        protein=snack.protein,
        fat=snack.fat,
        carbs=snack.carbs,
        is_manual=False,
    )
    db.add(meal)
    db.flush()

    consumed_after = round(consumed_before + snack.calories, 1)
    record.updated_at = now_utc()

    issue.daily_record_id = record.id
    issue.meal_id = meal.id
    issue.allowed = True
    issue.reason = "WITHIN_LIMIT"
    issue.consumed_after = consumed_after
    db.add(issue)
    db.commit()
    db.refresh(meal)
    db.refresh(issue)

    response.update(
        allowed=True,
        reason="WITHIN_LIMIT",
        message=(
            f"Перекус разрешён. «{snack.name}» добавлен в дневник: "
            f"+{snack.calories:.0f} ккал."
        ),
        indicator="GREEN",
        issue_id=issue.id,
        meal_id=meal.id,
        meal=meal_to_dict(meal),
        added_to_diary=True,
        consumed=consumed_after,
        remaining=round(limit - consumed_after, 1),
    )
    return response


@app.get("/snacks", tags=["Snack Machine"])
def list_snacks(db: Session = Depends(get_db)):
    """Каталог снеков автомата."""
    snacks = (
        db.query(Snack)
        .filter(Snack.is_active == True)  # noqa: E712
        .order_by(Snack.id)
        .all()
    )
    return {"snacks": [snack_to_dict(snack) for snack in snacks]}


@app.post("/snack-check", tags=["Snack Machine"])
def snack_check(
    data: Optional[SnackCheckRequest] = Body(default=None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Проверка лимита и выдача.

    При разрешении снек записывается в дневник как обычное блюдо
    с типом `snack`, поэтому лимит нельзя обойти повторными нажатиями.
    Требует JWT — запросы эмулятора авторизованы.
    """
    snack = resolve_snack(db, data.snack_id if data else None)
    return check_and_issue_snack(
        db,
        current_user,
        snack,
        device_id=data.device_id if data else None,
    )


@app.get("/snack-status", tags=["Snack Machine"])
def snack_status(
    snack_id: Optional[int] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Статус готовности автомата. Ничего не записывает в БД."""
    snack = resolve_snack(db, snack_id)
    return build_snack_status(db, current_user, snack)


@app.get("/snack-issues", tags=["Snack Machine"])
def list_snack_issues(
    date: Optional[str] = None,
    limit: int = 50,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Журнал выдач: разрешённые и отклонённые запросы."""
    query = db.query(SnackIssue).filter(SnackIssue.user_id == current_user.id)
    if date:
        entry_date = parse_date(date)
        query = query.filter(
            SnackIssue.daily_record_id.in_(
                db.query(DailyRecord.id).filter(
                    DailyRecord.user_id == current_user.id,
                    DailyRecord.date == local_midnight(entry_date),
                )
            )
        )

    issues = (
        query.order_by(SnackIssue.created_at.desc())
        .limit(max(1, min(limit, 200)))
        .all()
    )
    snacks = {
        snack.id: snack
        for snack in db.query(Snack).filter(
            Snack.id.in_([issue.snack_id for issue in issues if issue.snack_id])
        )
    }

    return {
        "issues": [
            {
                "id": issue.id,
                "created_at": (
                    issue.created_at.isoformat() if issue.created_at else None
                ),
                "allowed": issue.allowed,
                "reason": issue.reason,
                "snack_id": issue.snack_id,
                "snack_name": (
                    snacks[issue.snack_id].name
                    if issue.snack_id in snacks
                    else None
                ),
                "meal_id": issue.meal_id,
                "device_id": issue.device_id,
                "calories": issue.calories,
                "consumed_before": issue.consumed_before,
                "consumed_after": issue.consumed_after,
                "daily_norm": issue.daily_norm,
            }
            for issue in issues
        ]
    }


def _recommendation_response(
    entry_date: date_type,
    totals: dict,
    norm: Optional[dict],
    balance: Optional[dict],
    recommendation: Optional[Recommendation] = None,
    generated: bool = False,
    insufficient_message: Optional[str] = None,
    error: Optional[str] = None,
) -> dict:
    if insufficient_message:
        return {
            "date": entry_date.isoformat(),
            "ai_generated": False,
            "insufficient_data": True,
            "message": insufficient_message,
            "summary": "",
            "nutrition_advice": [],
            "activity_advice": [],
            "recommendations": [],
            "recipes": [],
            "norm": norm,
            "consumed": totals,
            "balance": balance,
            "model": None,
            "created_at": None,
            "cached": False,
            "fallback": False,
            "error": None,
        }

    content: dict = {}
    if recommendation is not None and recommendation.content:
        try:
            content = json.loads(recommendation.content)
        except (TypeError, json.JSONDecodeError):
            logger.warning(
                "Не удалось разобрать сохранённую рекомендацию id=%s",
                recommendation.id,
            )
            content = {}

    nutrition = content.get("nutrition_advice")
    nutrition = nutrition if isinstance(nutrition, list) else []
    activity = content.get("activity_advice")
    activity = activity if isinstance(activity, list) else []
    recipes = content.get("recipes")
    recipes = recipes if isinstance(recipes, list) else []

    return {
        "date": entry_date.isoformat(),
        "ai_generated": bool(recommendation and not recommendation.is_fallback),
        "insufficient_data": False,
        "summary": content.get("summary", ""),
        "nutrition_advice": nutrition,
        "activity_advice": activity,
        "recommendations": list(nutrition) + list(activity),
        "recipes": recipes,
        "norm": norm,
        "consumed": totals,
        "balance": balance,
        "model": recommendation.model_name if recommendation else None,
        "created_at": (
            recommendation.created_at.isoformat()
            if recommendation and recommendation.created_at
            else None
        ),
        "cached": bool(recommendation and not generated),
        "fallback": bool(recommendation and recommendation.is_fallback),
        "error": error,
    }


def recommendations_for(
    db: Session, user: User, entry_date: date_type, force: bool
) -> dict:
    record = get_record(db, user.id, entry_date)
    totals = totals_for_record(record)
    profile = db.query(Profile).filter(Profile.user_id == user.id).first()
    goal = get_active_goal(db, user.id)
    norm = calculate_daily_norm(profile, goal)
    balance = balance_for(norm, totals)

    if norm is None:
        return _recommendation_response(
            entry_date,
            totals,
            norm,
            balance,
            insufficient_message=(
                "Заполните профиль (возраст, рост, вес, образ жизни) "
                "и выберите цель, чтобы получить персональные рекомендации."
            ),
        )

    if not record or not record.meals:
        return _recommendation_response(
            entry_date,
            totals,
            norm,
            balance,
            insufficient_message=(
                "Добавьте приёмы пищи или активность, чтобы ИИ-агент смог "
                "проанализировать ваш день и дать рекомендации."
            ),
        )

    latest = (
        db.query(Recommendation)
        .filter(
            Recommendation.user_id == user.id,
            Recommendation.date == local_midnight(entry_date),
        )
        .order_by(Recommendation.created_at.desc())
        .first()
    )

    # Если после генерации пользователь изменил запись, рекомендация устарела.
    record_changed = (
        latest is not None
        and record.updated_at is not None
        and latest.created_at is not None
        and record.updated_at > latest.created_at
    )
    if latest is not None and not force and not record_changed:
        return _recommendation_response(
            entry_date, totals, norm, balance, latest, generated=False
        )

    context = build_recommendation_context(
        entry_date, record, totals, norm, balance, profile, goal
    )
    error = None
    try:
        ai_result = generate_recommendation_with_ai(context)
        is_fallback = False
    except AIServiceError as exc:
        logger.warning("ИИ-рекомендации недоступны, используется fallback: %s", exc)
        ai_result = _fallback_recommendation(context)
        is_fallback = True
        error = str(exc)

    recommendation = Recommendation(
        user_id=user.id,
        daily_record_id=record.id,
        date=local_midnight(entry_date),
        content=json.dumps(ai_result["content"], ensure_ascii=False),
        model_name=ai_result.get("model_name"),
        is_fallback=is_fallback,
        raw_response=ai_result.get("raw_response"),
    )
    db.add(recommendation)
    db.commit()
    db.refresh(recommendation)
    return _recommendation_response(
        entry_date,
        totals,
        norm,
        balance,
        recommendation,
        generated=True,
        error=error,
    )


@app.get("/recommendations", tags=["Recommendations"])
def get_recommendations(
    date: Optional[str] = None,
    force: bool = False,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Возвращает рекомендации за день. При отсутствии генерирует через ИИ."""
    entry_date = parse_date(date) if date else today_local()
    ensure_not_future(entry_date)
    return recommendations_for(db, current_user, entry_date, force=force)


@app.post("/recommendations", tags=["Recommendations"])
def regenerate_recommendations(
    date: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Принудительно перегенерирует рекомендации за день."""
    entry_date = parse_date(date) if date else today_local()
    ensure_not_future(entry_date)
    return recommendations_for(db, current_user, entry_date, force=True)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8001")),
        reload=True,
    )
