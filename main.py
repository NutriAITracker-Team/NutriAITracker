import os
import io
import json
import uuid
import base64
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import FastAPI, UploadFile, File, Form, Depends, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from pydantic import BaseModel, Field, ConfigDict
from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, ForeignKey, Text, Boolean
from sqlalchemy.orm import declarative_base, sessionmaker, Session, relationship
from passlib.context import CryptContext
from jose import JWTError, jwt
from PIL import Image
from openai import OpenAI

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")

AI_ENABLED = False
groq_client = None

if GROQ_API_KEY and GROQ_API_KEY.startswith("gsk_"):
    try:
        groq_client = OpenAI(
            api_key=GROQ_API_KEY,
            base_url="https://api.groq.com/openai/v1"
        )
        AI_ENABLED = True
        print("DEBUG: Клиент Groq успешно создан!")
    except Exception as e:
        print(f"DEBUG: Ошибка инициализации Groq: {e}")
else:
    print("DEBUG: Ключ GROQ_API_KEY не установлен или неверного формата.")

SQLALCHEMY_DATABASE_URL = "sqlite:///./nutriai.db"
engine = create_engine(SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

SECRET_KEY = os.getenv("SECRET_KEY", "super-secret-key-for-demo-change-me")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, index=True, nullable=False)
    hashed_password = Column(String, nullable=True)
    external_id = Column(String, unique=True, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

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
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = relationship("User", back_populates="profile")

class Goal(Base):
    __tablename__ = "goals"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    goal_type = Column(String, nullable=False)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="goals")

class DailyRecord(Base):
    __tablename__ = "daily_records"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    date = Column(DateTime, nullable=False, index=True)
    weight_kg = Column(Float, nullable=True)
    steps = Column(Integer, nullable=True)
    pulse_avg = Column(Integer, nullable=True)
    pulse_min = Column(Integer, nullable=True)
    pulse_max = Column(Integer, nullable=True)
    wellbeing = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = relationship("User", back_populates="daily_records")
    meals = relationship("Meal", back_populates="daily_record", cascade="all, delete-orphan")

class Meal(Base):
    __tablename__ = "meals"
    id = Column(Integer, primary_key=True, index=True)
    daily_record_id = Column(Integer, ForeignKey("daily_records.id"))
    dish_name = Column(String, nullable=False)
    weight_g = Column(Float, default=0)
    calories = Column(Float, default=0)
    protein = Column(Float, default=0)
    fat = Column(Float, default=0)
    carbs = Column(Float, default=0)
    is_manual = Column(Boolean, default=False)
    photo_url = Column(String, nullable=True)
    ai_raw_response = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    daily_record = relationship("DailyRecord", back_populates="meals")

Base.metadata.create_all(bind=engine)

class UserCreate(BaseModel):
    email: str
    password: str

class UserResponse(BaseModel):
    id: int
    email: str
    model_config = ConfigDict(from_attributes=True)

class ProfileUpdate(BaseModel):
    age: Optional[int] = Field(None, ge=1, le=120)
    height_cm: Optional[float] = Field(None, ge=50, le=250)
    weight_kg: Optional[float] = Field(None, ge=20, le=300)
    lifestyle: Optional[str] = Field(None, pattern="^(sedentary|light|moderate|active|very_active)$")

class GoalUpdate(BaseModel):
    goal_type: str = Field(..., pattern="^(lose_weight|gain_muscle|maintain|cardio_health)$")

class DailyRecordCreate(BaseModel):
    date: str
    weight_kg: Optional[float] = Field(None, gt=0)
    steps: Optional[int] = Field(None, ge=0)
    pulse_avg: Optional[int] = Field(None, gt=0)
    pulse_min: Optional[int] = Field(None, gt=0)
    pulse_max: Optional[int] = Field(None, gt=0)
    wellbeing: Optional[str] = None

class MealManualUpdate(BaseModel):
    dish_name: Optional[str] = None
    weight_g: Optional[float] = Field(None, ge=0)
    calories: Optional[float] = Field(None, ge=0)
    protein: Optional[float] = Field(None, ge=0)
    fat: Optional[float] = Field(None, ge=0)
    carbs: Optional[float] = Field(None, ge=0)

class SnackCheckRequest(BaseModel):
    user_id: int

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)):
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Не удалось подтвердить учётные данные",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id_str = payload.get("sub")
        if user_id_str is None:
            raise credentials_exception
        user_id = int(user_id_str)
    except (JWTError, ValueError):
        raise credentials_exception
    user = db.query(User).filter(User.id == user_id).first()
    if user is None:
        raise credentials_exception
    return user

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None):
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=15)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

def calculate_daily_norm(profile: Profile, goal: Goal) -> dict:
    if not profile or not profile.age or not profile.height_cm or not profile.weight_kg:
        return {"calories": 2000, "protein": 100, "fat": 60, "carbs": 250}

    if profile.lifestyle == "sedentary":
        activity = 1.2
    elif profile.lifestyle == "light":
        activity = 1.375
    elif profile.lifestyle == "moderate":
        activity = 1.55
    elif profile.lifestyle == "active":
        activity = 1.725
    else:
        activity = 1.9

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
    carbs = (calories - (protein * 4) - (fat * 9)) / 4

    return {
        "calories": round(calories, 1),
        "protein": round(protein, 1),
        "fat": round(fat, 1),
        "carbs": round(carbs, 1)
    }

def analyze_photo_with_ai(image: Image.Image) -> dict:
    if not AI_ENABLED or groq_client is None:
        return {
            "dish": "Тестовое блюдо (ИИ не подключён)",
            "weight_g": 200, "calories": 300,
            "protein": 15, "fat": 10, "carbs": 40,
            "is_fallback": True
        }

    img_byte_arr = io.BytesIO()
    image.save(img_byte_arr, format='JPEG')
    img_bytes = img_byte_arr.getvalue()
    img_b64 = base64.b64encode(img_bytes).decode('utf-8')

    prompt = """
    Ты — нутрициолог. Проанализируй это изображение еды.
    Определи: название блюда, массу порции (г), калорийность (ккал), белки (г), жиры (г), углеводы (г).
    Верни ответ СТРОГО в формате JSON (без markdown), например:
    {"dish": "Салат Цезарь", "weight_g": 250, "calories": 350, "protein": 20, "fat": 15, "carbs": 30}
    Если на фото не еда, верни {"error": "На фото не обнаружена еда"}.
    """

    try:
        response = groq_client.chat.completions.create(
            model="qwen/qwen3.8-27b",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}}
                    ]
                }
            ],
            timeout=30
        )
        raw_text = response.choices[0].message.content
        raw_text = raw_text.strip().replace("```json", "").replace("```", "")
        result = json.loads(raw_text)

        if "error" in result:
            return {"error": result["error"]}
        return result

    except Exception as e:
        print(f"Ошибка Groq ИИ: {e}")
        return {
            "dish": "Тестовое блюдо (Ошибка Groq)",
            "weight_g": 200, "calories": 300,
            "protein": 15, "fat": 10, "carbs": 40,
            "is_fallback": True,
            "error_detail": str(e)
        }

app = FastAPI(title="NutriAI Tracker API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
def root():
    return {"service": "NutriAI Tracker", "status": "alive", "ai_enabled": AI_ENABLED}

@app.get("/health", tags=["System"])
def health_check():
    return {
        "status": "healthy",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "ai_enabled": AI_ENABLED
    }

@app.post("/auth/register", response_model=UserResponse, tags=["Auth"])
def register(user_data: UserCreate, db: Session = Depends(get_db)):
    existing = db.query(User).filter(User.email == user_data.email).first()
    if existing:
        raise HTTPException(status_code=400, detail="Пользователь с таким email уже существует")

    hashed = pwd_context.hash(user_data.password)
    user = User(email=user_data.email, hashed_password=hashed)
    db.add(user)
    db.commit()
    db.refresh(user)

    profile = Profile(user_id=user.id)
    db.add(profile)
    db.commit()

    return user

@app.post("/auth/login", tags=["Auth"])
def login(form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == form_data.username).first()
    if not user or not user.hashed_password:
        raise HTTPException(status_code=401, detail="Неверный email или пароль")
    if not pwd_context.verify(form_data.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Неверный email или пароль")

    token = create_access_token({"sub": str(user.id)}, timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    return {"access_token": token, "token_type": "bearer", "user_id": user.id}

@app.post("/auth/external", tags=["Auth"])
def external_login(provider: str, external_token: str, db: Session = Depends(get_db)):
    fake_external_id = f"{provider}_{external_token}"
    user = db.query(User).filter(User.external_id == fake_external_id).first()
    if not user:
        user = User(
            email=f"{fake_external_id}@example.com",
            external_id=fake_external_id
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        profile = Profile(user_id=user.id)
        db.add(profile)
        db.commit()

    token = create_access_token({"sub": str(user.id)}, timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    return {"access_token": token, "token_type": "bearer", "user_id": user.id, "note": "Заглушка внешней авторизации"}

@app.get("/profile", tags=["Profile"])
def get_profile(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    profile = db.query(Profile).filter(Profile.user_id == current_user.id).first()
    if not profile:
        raise HTTPException(status_code=404, detail="Профиль не найден")
    return {
        "user_id": current_user.id,
        "email": current_user.email,
        "age": profile.age,
        "height_cm": profile.height_cm,
        "weight_kg": profile.weight_kg,
        "lifestyle": profile.lifestyle
    }

@app.put("/profile", tags=["Profile"])
def update_profile(data: ProfileUpdate, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    profile = db.query(Profile).filter(Profile.user_id == current_user.id).first()
    if not profile:
        raise HTTPException(status_code=404, detail="Профиль не найден")

    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(profile, field, value)
    db.commit()
    db.refresh(profile)
    return {"status": "updated", "profile": {"age": profile.age, "height_cm": profile.height_cm, "weight_kg": profile.weight_kg, "lifestyle": profile.lifestyle}}

@app.get("/goals", tags=["Goals"])
def get_goals(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    goals = db.query(Goal).filter(Goal.user_id == current_user.id).all()
    return {"goals": [{"id": g.id, "goal_type": g.goal_type, "is_active": g.is_active, "created_at": g.created_at} for g in goals]}

@app.post("/goals", tags=["Goals"])
def set_goal(data: GoalUpdate, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    db.query(Goal).filter(Goal.user_id == current_user.id).update({"is_active": False})
    goal = Goal(user_id=current_user.id, goal_type=data.goal_type, is_active=True)
    db.add(goal)
    db.commit()
    db.refresh(goal)
    return {"status": "created", "goal": {"id": goal.id, "goal_type": goal.goal_type, "is_active": goal.is_active}}

@app.post("/daily-records", tags=["Daily Records"])
def create_daily_record(data: DailyRecordCreate, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    try:
        record_date = datetime.strptime(data.date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=400, detail="Неверный формат даты. Используйте YYYY-MM-DD")

    if record_date.date() > datetime.utcnow().date():
        raise HTTPException(status_code=400, detail="Дата не может быть в будущем")

    if data.pulse_min and data.pulse_avg and data.pulse_max:
        if not (data.pulse_min <= data.pulse_avg <= data.pulse_max):
            raise HTTPException(status_code=400, detail="Пульс: минимальный <= средний <= максимальный")

    existing = db.query(DailyRecord).filter(
        DailyRecord.user_id == current_user.id,
        DailyRecord.date == record_date
    ).first()

    if existing:
        existing.weight_kg = data.weight_kg if data.weight_kg is not None else existing.weight_kg
        existing.steps = data.steps if data.steps is not None else existing.steps
        existing.pulse_avg = data.pulse_avg if data.pulse_avg is not None else existing.pulse_avg
        existing.pulse_min = data.pulse_min if data.pulse_min is not None else existing.pulse_min
        existing.pulse_max = data.pulse_max if data.pulse_max is not None else existing.pulse_max
        existing.wellbeing = data.wellbeing if data.wellbeing is not None else existing.wellbeing
        db.commit()
        db.refresh(existing)
        return {"status": "updated", "record_id": existing.id, "date": existing.date.strftime("%Y-%m-%d")}
    else:
        record = DailyRecord(
            user_id=current_user.id,
            date=record_date,
            weight_kg=data.weight_kg,
            steps=data.steps,
            pulse_avg=data.pulse_avg,
            pulse_min=data.pulse_min,
            pulse_max=data.pulse_max,
            wellbeing=data.wellbeing
        )
        db.add(record)
        db.commit()
        db.refresh(record)
        return {"status": "created", "record_id": record.id, "date": record.date.strftime("%Y-%m-%d")}

@app.get("/daily-records", tags=["Daily Records"])
def list_daily_records(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    records = db.query(DailyRecord).filter(DailyRecord.user_id == current_user.id).order_by(DailyRecord.date.desc()).all()
    return {"records": [
        {
            "id": r.id,
            "date": r.date.strftime("%Y-%m-%d"),
            "weight_kg": r.weight_kg,
            "steps": r.steps,
            "pulse_avg": r.pulse_avg,
            "wellbeing": r.wellbeing,
            "meals_count": len(r.meals)
        } for r in records
    ]}

@app.get("/daily-records/{date}", tags=["Daily Records"])
def get_daily_record(date: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    try:
        record_date = datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=400, detail="Неверный формат даты")

    record = db.query(DailyRecord).filter(
        DailyRecord.user_id == current_user.id,
        DailyRecord.date == record_date
    ).first()

    if not record:
        raise HTTPException(status_code=404, detail="Запись за эту дату не найдена")

    total_cal = sum(m.calories for m in record.meals)
    total_protein = sum(m.protein for m in record.meals)
    total_fat = sum(m.fat for m in record.meals)
    total_carbs = sum(m.carbs for m in record.meals)

    profile = db.query(Profile).filter(Profile.user_id == current_user.id).first()
    goal = db.query(Goal).filter(Goal.user_id == current_user.id, Goal.is_active == True).first()
    norm = calculate_daily_norm(profile, goal)

    return {
        "date": record.date.strftime("%Y-%m-%d"),
        "weight_kg": record.weight_kg,
        "steps": record.steps,
        "pulse_avg": record.pulse_avg,
        "pulse_min": record.pulse_min,
        "pulse_max": record.pulse_max,
        "wellbeing": record.wellbeing,
        "meals": [
            {
                "id": m.id,
                "dish_name": m.dish_name,
                "weight_g": m.weight_g,
                "calories": m.calories,
                "protein": m.protein,
                "fat": m.fat,
                "carbs": m.carbs,
                "is_manual": m.is_manual
            } for m in record.meals
        ],
        "statistics": {
            "consumed": {"calories": round(total_cal, 1), "protein": round(total_protein, 1), "fat": round(total_fat, 1), "carbs": round(total_carbs, 1)},
            "norm": norm,
            "balance": {
                "calories": round(norm["calories"] - total_cal, 1),
                "protein": round(norm["protein"] - total_protein, 1),
                "fat": round(norm["fat"] - total_fat, 1),
                "carbs": round(norm["carbs"] - total_carbs, 1)
            }
        }
    }

@app.post("/daily-records/{date}/analyze-photo", tags=["AI Analysis"])
async def analyze_photo_for_day(
    date: str,
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    try:
        record_date = datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=400, detail="Неверный формат даты")

    if record_date.date() > datetime.utcnow().date():
        raise HTTPException(status_code=400, detail="Дата не может быть в будущем")

    record = db.query(DailyRecord).filter(
        DailyRecord.user_id == current_user.id,
        DailyRecord.date == record_date
    ).first()
    if not record:
        record = DailyRecord(user_id=current_user.id, date=record_date)
        db.add(record)
        db.commit()
        db.refresh(record)

    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="Файл должен быть изображением")

    if file.content_type not in ["image/jpeg", "image/png", "image/webp"]:
        raise HTTPException(status_code=400, detail="Поддерживаются только JPEG, PNG, WEBP")

    contents = await file.read()
    try:
        image = Image.open(io.BytesIO(contents))
    except Exception:
        raise HTTPException(status_code=400, detail="Не удалось открыть изображение")

    ai_result = analyze_photo_with_ai(image)

    if "error" in ai_result:
        return {"success": False, "message": ai_result["error"]}

    meal = Meal(
        daily_record_id=record.id,
        dish_name=ai_result.get("dish", "Неизвестное блюдо"),
        weight_g=ai_result.get("weight_g", 0),
        calories=ai_result.get("calories", 0),
        protein=ai_result.get("protein", 0),
        fat=ai_result.get("fat", 0),
        carbs=ai_result.get("carbs", 0),
        is_manual=False,
        photo_url=f"/uploads/{uuid.uuid4()}.jpg",
        ai_raw_response=json.dumps(ai_result, ensure_ascii=False)
    )
    db.add(meal)
    db.commit()
    db.refresh(meal)

    return {
        "success": True,
        "record_id": record.id,
        "meal": {
            "id": meal.id,
            "dish_name": meal.dish_name,
            "weight_g": meal.weight_g,
            "calories": meal.calories,
            "protein": meal.protein,
            "fat": meal.fat,
            "carbs": meal.carbs,
            "is_manual": meal.is_manual
        },
        "ai_meta": {"fallback": ai_result.get("is_fallback", False)}
    }

@app.put("/meals/{meal_id}", tags=["Meals"])
def update_meal(
    meal_id: int,
    data: MealManualUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    meal = db.query(Meal).join(DailyRecord).filter(
        Meal.id == meal_id,
        DailyRecord.user_id == current_user.id
    ).first()

    if not meal:
        raise HTTPException(status_code=404, detail="Блюдо не найдено")

    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(meal, field, value)
    meal.is_manual = True
    db.commit()
    db.refresh(meal)

    return {"status": "updated", "meal": {
        "id": meal.id,
        "dish_name": meal.dish_name,
        "weight_g": meal.weight_g,
        "calories": meal.calories,
        "protein": meal.protein,
        "fat": meal.fat,
        "carbs": meal.carbs,
        "is_manual": meal.is_manual
    }}

@app.delete("/meals/{meal_id}", tags=["Meals"])
def delete_meal(meal_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    meal = db.query(Meal).join(DailyRecord).filter(
        Meal.id == meal_id,
        DailyRecord.user_id == current_user.id
    ).first()
    if not meal:
        raise HTTPException(status_code=404, detail="Блюдо не найдено")
    db.delete(meal)
    db.commit()
    return {"status": "deleted", "meal_id": meal_id}

@app.post("/snack-check", tags=["Snack Machine"])
def snack_check(request: SnackCheckRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.id == request.user_id).first()
    if not user:
        return {"allowed": False, "reason": "Пользователь не найден", "indicator": "RED"}

    today = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    record = db.query(DailyRecord).filter(
        DailyRecord.user_id == user.id,
        DailyRecord.date >= today
    ).first()

    if not record:
        consumed = 0
    else:
        consumed = sum(m.calories for m in record.meals)

    profile = db.query(Profile).filter(Profile.user_id == user.id).first()
    goal = db.query(Goal).filter(Goal.user_id == user.id, Goal.is_active == True).first()
    norm = calculate_daily_norm(profile, goal)
    limit = norm["calories"]
    remaining = limit - consumed

    if remaining > 0:
        return {
            "allowed": True,
            "message": "Перекус разрешён",
            "consumed": round(consumed, 1),
            "limit": limit,
            "remaining": round(remaining, 1),
            "indicator": "GREEN"
        }
    else:
        return {
            "allowed": False,
            "message": "Лимит калорий исчерпан",
            "consumed": round(consumed, 1),
            "limit": limit,
            "remaining": round(remaining, 1),
            "indicator": "RED"
        }

@app.get("/recommendations", tags=["Recommendations"])
def get_recommendations(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    profile = db.query(Profile).filter(Profile.user_id == current_user.id).first()
    goal = db.query(Goal).filter(Goal.user_id == current_user.id, Goal.is_active == True).first()

    return {
        "note": "Заглушка рекомендаций. В реальной версии — генерация через ИИ.",
        "daily_norm": calculate_daily_norm(profile, goal),
        "recommendations": [
            "Добавьте больше овощей в рацион — это снизит калорийность и повысит содержание клетчатки.",
            "Пейте достаточное количество воды в течение дня.",
            "Старайтесь делать не менее 8000 шагов в день для поддержания активности."
        ],
        "recipes": [
            {"title": "Греческий салат", "calories": 250, "time_min": 15},
            {"title": "Куриная грудка на гриле", "calories": 180, "time_min": 25},
            {"title": "Овсянка с ягодами", "calories": 320, "time_min": 10}
        ]
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)