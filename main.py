import os
import re
import asyncio
import random
import string
from datetime import datetime, timedelta
from typing import List, Optional
from urllib.parse import urlencode

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Request, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import create_engine, Column, Integer, String, Float, Boolean, DateTime, ForeignKey, JSON, func
from sqlalchemy.orm import sessionmaker, declarative_base, Session
from itsdangerous import URLSafeSerializer, BadSignature
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from starlette.concurrency import run_in_threadpool

load_dotenv()

STEAM_API_KEY = os.getenv("STEAM_API_KEY", "F642F808762ADDE50DE61C364734A8A7")
OWNER_TRADE_LINK = os.getenv("OWNER_TRADE_LINK", "https://steamcommunity.com/tradeoffer/new/?partner=768156028&token=ORf8WAv2")
OWNER_STEAM_ID = os.getenv("OWNER_STEAM_ID", "76561198728421756")
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://cs2shop:cs2shop@localhost:5432/cs2shop")
SECRET_KEY = os.getenv("SECRET_KEY", "artcs-skins-secret-key")
SHOP_NAME = "ARTCSSkins"
DEFAULT_SUPPORT = "https://t.me/ARTCSSKINSSHOP"
STEAM_OPENID_URL = "https://steamcommunity.com/openid/login"

WEARS = ["FN", "MW", "FT", "WW", "BS"]
WEAR_SUFFIX = {"FN": "Factory New", "MW": "Minimal Wear", "FT": "Field-Tested",
               "WW": "Well-Worn", "BS": "Battle-Scarred"}
WEAR_LABEL = {"FN": "FN", "MW": "MW", "FT": "FT", "WW": "WW", "BS": "BS"}

PISTOLS = ["Glock-18", "P250", "USP-S", "Five-SeveN", "Tec-9", "Dual Berettas",
           "Desert Eagle", "P2000", "R8 Revolver", "CZ75-Auto", "Zeus x27"]
SMGS = ["MP9", "MAC-10", "MP7", "MP5-SD", "UMP-45", "P90", "PP-Bizon"]
HEAVY = ["Nova", "XM1014", "Sawed-Off", "MAG-7", "M249", "Negev"]
RIFLES = ["AK-47", "M4A4", "M4A1-S", "FAMAS", "Galil AR", "AWP", "SSG 08",
          "SCAR-20", "G3SG1", "AUG", "SG 553"]

ALL_PERMS = ["sales", "withdrawals", "preorders", "users", "promocodes", "moderators", "bans", "settings"]

TRADE_LINK_RE = re.compile(r"^https://steamcommunity\.com/tradeoffer/new/\?partner=\d+&token=[A-Za-z0-9_-]+$")
PROMO_CODE_RE = re.compile(r"^[A-Z0-9]+$")

INV_ERROR_MSG = ("Ваш инвентарь закрыт либо в нём нет подходящих скинов. "
                 "Возможно ошибка на стороне сервера. Попробуйте позже.")

def _inv_error(user, public_message, debug_info):
    resp = {
        "ok": False,
        "message": f"{public_message} Возможно ошибка на стороне сервера. Попробуйте позже.",
    }
    if user.steam_id == OWNER_STEAM_ID:
        resp["debug"] = debug_info
    return resp


# Время последнего обновления инвентаря: user_id -> datetime (in-memory)
INVENTORY_LAST_REFRESH: dict = {}

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
Base = declarative_base()
templates = Jinja2Templates(directory="templates")
serializer = URLSafeSerializer(SECRET_KEY, salt="artcs-session")
app = FastAPI(title=SHOP_NAME)
scheduler = AsyncIOScheduler()


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    steam_id = Column(String(32), unique=True, index=True)
    nickname = Column(String(128), default="")
    avatar = Column(String(512), default="")
    balance = Column(Integer, default=0)
    trade_link = Column(String(512), default="")
    trade_link_updated_at = Column(DateTime, nullable=True)
    is_admin = Column(Boolean, default=False)
    is_moderator = Column(Boolean, default=False)
    permissions = Column(JSON, default=list)
    is_banned = Column(Boolean, default=False)
    ban_until = Column(DateTime, nullable=True)
    ban_reason = Column(String(512), default="")
    referral_code = Column(String(16), unique=True)
    referred_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    referral_rewarded = Column(Boolean, default=False)
    active_promo = Column(String(64), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class Skin(Base):
    __tablename__ = "skins"
    id = Column(Integer, primary_key=True)
    market_hash_name = Column(String(256), index=True)
    name = Column(String(256))
    icon_url = Column(String(512), default="")
    weapon_type = Column(String(32), default="other")
    created_at = Column(DateTime, default=datetime.utcnow)


class Price(Base):
    __tablename__ = "prices"
    id = Column(Integer, primary_key=True)
    skin_id = Column(Integer, ForeignKey("skins.id"), index=True)
    stattrak = Column(Boolean, default=False)
    wear = Column(String(4))
    steam_price = Column(Integer)
    buy_price = Column(Integer)
    sell_price = Column(Integer)
    updated_at = Column(DateTime, default=datetime.utcnow)


class Instance(Base):
    __tablename__ = "instances"
    id = Column(Integer, primary_key=True)
    skin_id = Column(Integer, ForeignKey("skins.id"), index=True)
    stattrak = Column(Boolean, default=False)
    wear = Column(String(4))
    float_value = Column(Float, default=0.0)
    status = Column(String(16), default="in_stock", index=True)
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    buy_price = Column(Integer, default=0)
    sell_price = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)


class Transaction(Base):
    __tablename__ = "transactions"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), index=True)
    amount = Column(Integer)
    type = Column(String(16))
    description = Column(String(512), default="")
    created_at = Column(DateTime, default=datetime.utcnow)


class Withdrawal(Base):
    __tablename__ = "withdrawals"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), index=True)
    instance_ids = Column(JSON, default=list)
    items = Column(JSON, default=list)
    total = Column(Integer, default=0)
    status = Column(String(16), default="pending", index=True)
    note = Column(String(512), default="")
    created_at = Column(DateTime, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=True)


class SaleRequest(Base):
    __tablename__ = "sale_requests"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), index=True)
    items = Column(JSON, default=list)
    total = Column(Integer, default=0)
    promo_code = Column(String(64), nullable=True)
    status = Column(String(16), default="pending", index=True)
    reason = Column(String(512), default="")
    created_at = Column(DateTime, default=datetime.utcnow)
    resolved_at = Column(DateTime, nullable=True)


class Preorder(Base):
    __tablename__ = "preorders"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), index=True)
    skin_id = Column(Integer, ForeignKey("skins.id"), index=True)
    stattrak = Column(Boolean, default=False)
    wear = Column(String(4))
    price = Column(Integer)
    status = Column(String(16), default="active", index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    expires_at = Column(DateTime)


class Promocode(Base):
    __tablename__ = "promocodes"
    id = Column(Integer, primary_key=True)
    code = Column(String(64), unique=True, index=True)
    type = Column(String(16))
    value = Column(Integer, default=0)
    max_uses = Column(Integer, default=0)
    used_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)


class Notification(Base):
    __tablename__ = "notifications"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), index=True)
    text = Column(String(512))
    is_read = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class Setting(Base):
    __tablename__ = "settings"
    key = Column(String(64), primary_key=True)
    value = Column(String(1024), default="")


Base.metadata.create_all(engine)


def weapon_type_of(name):
    if "★" in name:
        return "knife"
    for w in PISTOLS:
        if name.startswith(w):
            return "pistol"
    for w in SMGS:
        if name.startswith(w):
            return "smg"
    for w in HEAVY:
        if name.startswith(w):
            return "heavy"
    for w in RIFLES:
        if name.startswith(w):
            return "rifle"
    return "other"


def parse_hash(mhn):
    st = mhn.startswith("StatTrak™ ")
    base = mhn[len("StatTrak™ "):] if st else mhn
    wear = None
    for short, full in WEAR_SUFFIX.items():
        suf = " (" + full + ")"
        if base.endswith(suf):
            wear = short
            base = base[:-len(suf)]
            break
    return base, st, wear


def build_hash(base, st, wear):
    return ("StatTrak™ " if st else "") + base + " (" + WEAR_SUFFIX[wear] + ")"


def gen_code(n=10):
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=n))


def price_to_cents(s):
    try:
        return int(round(float(str(s).replace("$", "").replace(",", "").strip()) * 100))
    except Exception:
        return None


async def fetch_steam_price(client, mhn):
    """Всегда возвращает цену в центах либо None. Никогда не бросает исключения."""
    try:
        r = await client.get("https://steamcommunity.com/market/priceoverview/",
                             params={"appid": 730, "currency": 1, "market_hash_name": mhn})
        if r.status_code != 200:
            return None
        try:
            data = r.json()
        except Exception:
            return None
        if not isinstance(data, dict):
            return None
        raw = data.get("lowest_price") or data.get("median_price")
        if not raw:
            return None
        return price_to_cents(raw)
    except Exception:
        return None


def _notify(session, user_id, text):
    session.add(Notification(user_id=user_id, text=text))


def _notify_admins(session, text):
    for u in session.query(User).filter((User.is_admin == True) | (User.is_moderator == True)).all():
        _notify(session, u.id, text)


def _setting(session, key, default=""):
    row = session.query(Setting).filter(Setting.key == key).first()
    return row.value if row else default


def _money(cents):
    return f"${cents / 100:.2f}"


def user_public(u):
    return {
        "id": u.id, "steam_id": u.steam_id, "nickname": u.nickname, "avatar": u.avatar,
        "balance": u.balance, "trade_link": u.trade_link,
        "trade_link_updated_at": u.trade_link_updated_at.isoformat() if u.trade_link_updated_at else None,
        "is_admin": u.is_admin, "is_moderator": u.is_moderator,
        "is_staff": u.is_admin or u.is_moderator,
        "permissions": u.permissions or [],
        "is_banned": u.is_banned,
        "ban_until": u.ban_until.isoformat() if u.ban_until else None,
        "ban_reason": u.ban_reason,
        "referral_code": u.referral_code,
        "referred_by": u.referred_by,
        "created_at": u.created_at.isoformat() if u.created_at else None,
    }


def get_db():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def get_current_user(request: Request) -> User:
    token = request.cookies.get("session")
    if not token:
        raise HTTPException(401, "Не авторизован")
    try:
        steam_id = serializer.loads(token)
    except BadSignature:
        raise HTTPException(401, "Неверная сессия")
    session = SessionLocal()
    user = session.query(User).filter(User.steam_id == steam_id).first()
    if user and user.steam_id == OWNER_STEAM_ID and not user.is_admin:
        user.is_admin = True
        session.commit()
    session.close()
    if not user:
        raise HTTPException(401, "Пользователь не найден")
    if user.is_banned and (user.ban_until is None or user.ban_until > datetime.utcnow()):
        raise HTTPException(403, "Аккаунт заблокирован. Причина: " + (user.ban_reason or "не указана"))
    if user.is_banned and user.ban_until is not None and user.ban_until <= datetime.utcnow():
        session = SessionLocal()
        u = session.get(User, user.id)
        u.is_banned = False
        u.ban_until = None
        session.commit()
        session.close()
        user.is_banned = False
    return user


def get_staff(request: Request) -> User:
    user = get_current_user(request)
    if not (user.is_admin or user.is_moderator):
        raise HTTPException(403, "Нет доступа")
    return user


def require_perm(perm):
    def dep(request: Request) -> User:
        user = get_staff(request)
        if not user.is_admin and perm not in (user.permissions or []):
            raise HTTPException(403, "Нет права: " + perm)
        return user
    return dep


# ---------- Ошибки валидации -> 400, а не 422/500 ----------

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    errors = exc.errors()
    if errors:
        msg = str(errors[0].get("msg", "Неверные данные"))
        msg = re.sub(r"^Value error,?\s*", "", msg)
        loc = errors[0].get("loc", [])
        field = str(loc[-1]) if loc else ""
        if field and field not in ("body", "query"):
            msg = f"{field}: {msg}"
    else:
        msg = "Неверные данные"
    return JSONResponse({"detail": msg}, status_code=400)


@app.middleware("http")
async def maintenance_middleware(request: Request, call_next):
    path = request.url.path
    if path.startswith("/api") and not path.startswith("/api/admin") and path != "/api/settings" and not path.startswith("/auth"):
        sess = SessionLocal()
        try:
            if _setting(sess, "maintenance", "0") == "1":
                allowed = False
                token = request.cookies.get("session")
                if token:
                    try:
                        steam_id = serializer.loads(token)
                        u = sess.query(User).filter(User.steam_id == steam_id).first()
                        if u and (u.is_admin or u.is_moderator):
                            allowed = True
                    except BadSignature:
                        pass
                if not allowed:
                    return JSONResponse({"detail": "Технические работы. Зайдите позже."}, status_code=503)
        finally:
            sess.close()
    return await call_next(request)


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request, "shop": SHOP_NAME})


@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    try:
        user = get_current_user(request)
    except HTTPException:
        return RedirectResponse("/auth/login")
    if not (user.is_admin or user.is_moderator):
        return PlainTextResponse("403 Forbidden", status_code=403)
    return templates.TemplateResponse("admin.html", {"request": request, "shop": SHOP_NAME})


@app.get("/health")
async def health():
    return {"ok": True, "shop": SHOP_NAME}


@app.get("/api/settings")
async def public_settings():
    sess = SessionLocal()
    try:
        return {"shop": SHOP_NAME,
                "support_link": _setting(sess, "support_link", DEFAULT_SUPPORT),
                "maintenance": _setting(sess, "maintenance", "0")}
    finally:
        sess.close()


@app.get("/auth/login")
async def auth_login(request: Request):
    params = {
        "openid.ns": "http://specs.openid.net/auth/2.0",
        "openid.identity": "http://specs.openid.net/auth/2.0/identifier_select",
        "openid.claimed_id": "http://specs.openid.net/auth/2.0/identifier_select",
        "openid.mode": "checkid_setup",
        "openid.return_to": str(request.base_url) + "auth/callback",
        "openid.realm": str(request.base_url),
    }
    return RedirectResponse(STEAM_OPENID_URL + "?" + urlencode(params))


@app.get("/auth/callback")
async def auth_callback(request: Request):
    params = dict(request.query_params)
    if "openid.claimed_id" not in params:
        return RedirectResponse("/")
    verify = {k: v for k, v in params.items() if k.startswith("openid.")}
    verify["openid.mode"] = "check_authentication"
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(STEAM_OPENID_URL, data=verify)
        if "is_valid:true" not in r.text:
            return PlainTextResponse("Steam OpenID verification failed", status_code=400)
    except Exception:
        return PlainTextResponse("Steam unavailable", status_code=502)
    claimed = params["openid.claimed_id"]
    steam_id = claimed.rstrip("/").split("/")[-1]
    session = SessionLocal()
    user = session.query(User).filter(User.steam_id == steam_id).first()
    nickname, avatar = steam_id, ""
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get("https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v0002/",
                                 params={"key": STEAM_API_KEY, "steamids": steam_id})
        players = r.json().get("response", {}).get("players", [])
        if players:
            nickname = players[0].get("personaname", nickname)
            avatar = players[0].get("avatarfull", "")
    except Exception:
        pass
    if not user:
        user = User(steam_id=steam_id, nickname=nickname, avatar=avatar,
                    referral_code=gen_code(), is_admin=(steam_id == OWNER_STEAM_ID))
        session.add(user)
        session.commit()
    else:
        user.nickname = nickname
        if avatar:
            user.avatar = avatar
        if steam_id == OWNER_STEAM_ID:
            user.is_admin = True
        session.commit()
    session.close()
    resp = RedirectResponse("/")
    resp.set_cookie("session", serializer.dumps(steam_id), httponly=True, samesite="lax", max_age=30 * 86400)
    return resp


@app.post("/api/logout")
async def logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("session")
    return resp


@app.get("/api/me")
async def me(user: User = Depends(get_current_user)):
    return {**user_public(user), "owner_trade_link": OWNER_TRADE_LINK,
            "support_link": DEFAULT_SUPPORT, "shop": SHOP_NAME}


# ---------- Pydantic-модели с валидацией ----------

class TradeLinkIn(BaseModel):
    trade_link: str = Field(min_length=40, max_length=200)

    @field_validator("trade_link")
    @classmethod
    def _check_trade_link(cls, v):
        if not TRADE_LINK_RE.match(v):
            raise ValueError("Неверная трейд-ссылка. Формат: https://steamcommunity.com/tradeoffer/new/?partner=XXXX&token=XXXX")
        return v


@app.put("/api/me/trade-link")
async def set_trade_link(data: TradeLinkIn, user: User = Depends(get_current_user),
                         session: Session = Depends(get_db)):
    def work():
        if user.trade_link_updated_at and user.trade_link_updated_at > datetime.utcnow() - timedelta(minutes=30):
            raise HTTPException(400, "Трейд-ссылку можно менять раз в 30 минут")
        u = session.get(User, user.id)
        u.trade_link = data.trade_link
        u.trade_link_updated_at = datetime.utcnow()
        session.commit()
    await run_in_threadpool(work)
    return {"ok": True}


def _catalog(st, wear, wtype, pmin, pmax, sort):
    session = SessionLocal()
    try:
        q = session.query(Price, Skin).join(Skin, Price.skin_id == Skin.id)
        if st in ("0", "1"):
            q = q.filter(Price.stattrak == (st == "1"))
        if wear in WEARS:
            q = q.filter(Price.wear == wear)
        if wtype != "all":
            q = q.filter(Skin.weapon_type == wtype)
        if pmin is not None:
            q = q.filter(Price.sell_price >= pmin)
        if pmax is not None:
            q = q.filter(Price.sell_price <= pmax)
        wear_order = {"FN": 0, "MW": 1, "FT": 2, "WW": 3, "BS": 4}
        skins = {}
        for price, skin in q.all():
            d = skins.setdefault(skin.id, {
                "id": skin.id, "name": skin.name, "icon_url": skin.icon_url,
                "weapon_type": skin.weapon_type, "min_price": None, "in_stock": 0, "best_wear": 9})
            if price.sell_price is not None and (d["min_price"] is None or price.sell_price < d["min_price"]):
                d["min_price"] = price.sell_price
            d["best_wear"] = min(d["best_wear"], wear_order.get(price.wear, 9))
        for sid, d in skins.items():
            d["in_stock"] = session.query(Instance).filter(
                Instance.skin_id == sid, Instance.status == "in_stock").count()
            d.pop("best_wear") if sort not in ("wear_asc", "wear_desc") else None
        items = list(skins.values())
        if sort == "price_asc":
            items.sort(key=lambda x: (x["min_price"] is None, x["min_price"] or 0))
        elif sort == "price_desc":
            items.sort(key=lambda x: (x["min_price"] is None, -(x["min_price"] or 0)))
        elif sort == "wear_asc":
            items.sort(key=lambda x: (x["best_wear"], x["name"]))
        elif sort == "wear_desc":
            items.sort(key=lambda x: (-x["best_wear"], x["name"]))
        for i in items:
            i.pop("best_wear", None)
        return items
    finally:
        session.close()


CATALOG_ST = ["all", "0", "1"]
CATALOG_WEAR = ["all", "FN", "MW", "FT", "WW", "BS"]
CATALOG_TYPE = ["all", "pistol", "smg", "heavy", "rifle", "knife", "other"]
CATALOG_SORT = ["price_asc", "price_desc", "wear_asc", "wear_desc"]


@app.get("/api/catalog")
async def catalog(st: str = Query("all"), wear: str = Query("all"), wtype: str = Query("all"),
                  pmin: int = Query(None), pmax: int = Query(None),
                  sort: str = Query("price_asc"), user: User = Depends(get_current_user)):
    if st not in CATALOG_ST:
        raise HTTPException(400, "Параметр st: all, 0 или 1")
    if wear not in CATALOG_WEAR:
        raise HTTPException(400, "Параметр wear: all, FN, MW, FT, WW или BS")
    if wtype not in CATALOG_TYPE:
        raise HTTPException(400, "Параметр wtype: all, pistol, smg, heavy, rifle, knife, other")
    if pmin is not None and not (0 <= pmin <= 10_000_000):
        raise HTTPException(400, "Параметр pmin: от 0 до 10 000 000 (центы)")
    if pmax is not None and not (0 <= pmax <= 10_000_000):
        raise HTTPException(400, "Параметр pmax: от 0 до 10 000 000 (центы)")
    if sort not in CATALOG_SORT:
        raise HTTPException(400, "Параметр sort: price_asc, price_desc, wear_asc, wear_desc")
    return await run_in_threadpool(_catalog, st, wear, wtype, pmin, pmax, sort)


def _skin_prices(skin_id):
    session = SessionLocal()
    try:
        rows = session.query(Price).filter(Price.skin_id == skin_id).all()
        out = []
        for p in rows:
            stock = session.query(Instance).filter(
                Instance.skin_id == skin_id, Instance.stattrak == p.stattrak,
                Instance.wear == p.wear, Instance.status == "in_stock").count()
            out.append({"stattrak": p.stattrak, "wear": p.wear, "steam_price": p.steam_price,
                        "buy_price": p.buy_price, "sell_price": p.sell_price, "in_stock": stock})
        return out
    finally:
        session.close()


@app.get("/api/skins/{skin_id}/prices")
async def skin_prices(skin_id: int, user: User = Depends(get_current_user)):
    return await run_in_threadpool(_skin_prices, skin_id)


class BuyIn(BaseModel):
    skin_id: int = Field(gt=0)
    stattrak: bool
    wear: str

    @field_validator("wear")
    @classmethod
    def _check_wear(cls, v):
        if v not in WEARS:
            raise ValueError("Состояние должно быть одним из: FN, MW, FT, WW, BS")
        return v


def _maybe_referral_reward(session, user, amount):
    if user.referred_by and not user.referral_rewarded and amount >= 1000:
        inv = session.get(User, user.referred_by)
        if inv:
            inv.balance += 100
            session.add(Transaction(user_id=inv.id, amount=100, type="referral",
                                    description=f"Реферальная награда: {user.nickname} совершил покупку на $10+"))
            _notify(session, inv.id, "Реферальная награда: +$1.00")
        user.referral_rewarded = True
        session.add(user)


@app.post("/api/buy")
async def buy(data: BuyIn, user: User = Depends(get_current_user), session: Session = Depends(get_db)):
    def work():
        price_row = session.query(Price).filter_by(skin_id=data.skin_id, stattrak=data.stattrak,
                                                   wear=data.wear).first()
        if not price_row or price_row.sell_price is None:
            raise HTTPException(400, "Цена для этой комбинации не найдена")
        inst = session.query(Instance).filter_by(skin_id=data.skin_id, stattrak=data.stattrak,
                                                 wear=data.wear, status="in_stock").first()
        if not inst:
            raise HTTPException(400, "Нет в наличии. Доступен предзаказ.")
        u = session.get(User, user.id)
        if u.balance < price_row.sell_price:
            raise HTTPException(400, "Недостаточно средств на балансе")
        u.balance -= price_row.sell_price
        inst.status = "with_user"
        inst.owner_id = u.id
        inst.sell_price = price_row.sell_price
        session.add(Transaction(user_id=u.id, amount=-price_row.sell_price, type="purchase",
                                description=f"Покупка скина (ID {inst.id})"))
        _maybe_referral_reward(session, u, price_row.sell_price)
        _notify(session, u.id, f"Куплен скин за {_money(price_row.sell_price)}. Он в вашем инвентаре на сайте.")
        _notify_admins(session, f"{u.nickname} купил скин на {_money(price_row.sell_price)}")
        session.commit()
        return {"ok": True, "balance": u.balance}
    return await run_in_threadpool(work)


@app.post("/api/preorder")
async def preorder(data: BuyIn, user: User = Depends(get_current_user), session: Session = Depends(get_db)):
    def work():
        price_row = session.query(Price).filter_by(skin_id=data.skin_id, stattrak=data.stattrak,
                                                   wear=data.wear).first()
        if not price_row or price_row.sell_price is None:
            raise HTTPException(400, "Цена для этой комбинации не найдена")
        has_stock = session.query(Instance).filter_by(skin_id=data.skin_id, stattrak=data.stattrak,
                                                      wear=data.wear, status="in_stock").count() > 0
        if has_stock:
            raise HTTPException(400, "Скин есть в наличии — оформите покупку")
        u = session.get(User, user.id)
        if u.balance < price_row.sell_price:
            raise HTTPException(400, "Недостаточно средств на балансе")
        u.balance -= price_row.sell_price
        po = Preorder(user_id=u.id, skin_id=data.skin_id, stattrak=data.stattrak, wear=data.wear,
                      price=price_row.sell_price, expires_at=datetime.utcnow() + timedelta(days=14))
        session.add(po)
        session.add(Transaction(user_id=u.id, amount=-price_row.sell_price, type="preorder",
                                description=f"Предзаказ скина #{po.id if po.id else ''}".strip()))
        _maybe_referral_reward(session, u, price_row.sell_price)
        _notify(session, u.id, f"Предзаказ создан на сумму {_money(price_row.sell_price)}. Срок выполнения — 14 дней.")
        _notify_admins(session, f"Новый предзаказ от {u.nickname} на {_money(price_row.sell_price)}")
        session.commit()
        return {"ok": True, "balance": u.balance, "preorder_id": po.id}
    return await run_in_threadpool(work)


def _site_inventory(user_id):
    session = SessionLocal()
    try:
        rows = session.query(Instance, Skin).join(Skin, Instance.skin_id == Skin.id).filter(
            Instance.owner_id == user_id, Instance.status == "with_user").all()
        return [{"id": i.id, "name": s.name, "icon_url": s.icon_url, "stattrak": i.stattrak,
                 "wear": i.wear, "float_value": i.float_value, "buy_price": i.buy_price,
                 "sell_price": i.sell_price} for i, s in rows]
    finally:
        session.close()


@app.get("/api/inventory")
async def site_inventory(user: User = Depends(get_current_user)):
    return await run_in_threadpool(_site_inventory, user.id)


# ---------- Steam-инвентарь: без 500, rate limit, защита от ошибок ----------

@app.get("/api/steam/inventory")
async def steam_inventory(user: User = Depends(get_current_user), session: Session = Depends(get_db)):
    debug_info = {"user_id": user.id, "steam_id": user.steam_id}

    # Оборот пользователя (центы): положительные транзакции типа "sale"
    turnover = session.query(func.coalesce(func.sum(Transaction.amount), 0)).filter(
        Transaction.user_id == user.id,
        Transaction.amount > 0,
        Transaction.type == "sale"
    ).scalar() or 0

    cooldown_sec = 300 if turnover >= 30000 else 1800
    limit_min = cooldown_sec // 60

    last = INVENTORY_LAST_REFRESH.get(user.id)
    now = datetime.utcnow()
    if last is not None:
        remaining = cooldown_sec - (now - last).total_seconds()
        if remaining > 0:
            remain_min = int(remaining // 60) + (1 if remaining % 60 else 0)
            return JSONResponse({"ok": False,
                                 "message": f"Обновление инвентаря доступно раз в {limit_min} минут. "
                                            f"Подождите ещё {remain_min} минут."})

    try:
        url = f"https://steamcommunity.com/inventory/{user.steam_id}/730/2"
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(url, params={"l": "english", "count": 5000})
    except Exception as e:
        debug_info["reason"] = "http_error"
        debug_info["error"] = repr(e)
        return JSONResponse(_inv_error(user, "Не удалось связаться со Steam.", debug_info))

    debug_info["http_status"] = r.status_code
    debug_info["response_len"] = len(r.content)
    debug_info["body_preview"] = r.text[:300]

    if r.status_code != 200:
        debug_info["success"] = False
        debug_info["reason"] = "non_200"
        return JSONResponse(_inv_error(user, f"Steam вернул {r.status_code}.", debug_info))

    try:
        data = r.json()
    except Exception as e:
        debug_info["success"] = False
        debug_info["reason"] = "non_json"
        debug_info["error"] = repr(e)
        return JSONResponse(_inv_error(user, "Steam вернул не-JSON.", debug_info))

    if not isinstance(data, dict):
        debug_info["success"] = False
        debug_info["reason"] = "unexpected_json_type"
        debug_info["error"] = f"type={type(data).__name__}"
        return JSONResponse(_inv_error(user, "Инвентарь закрыт или недоступен.", debug_info))

    debug_info["success"] = data.get("success")
    assets_raw = data.get("assets")
    descs_raw = data.get("descriptions")
    debug_info["assets_count"] = len(assets_raw) if isinstance(assets_raw, list) else 0
    debug_info["descriptions_count"] = len(descs_raw) if isinstance(descs_raw, list) else 0

    if data.get("success") is not True:
        debug_info["reason"] = "success_not_true"
        if data.get("error"):
            debug_info["error"] = str(data.get("error"))[:300]
        return JSONResponse(_inv_error(user, "Инвентарь закрыт или недоступен.", debug_info))

    # Steam ответил успешно — фиксируем время обновления
    INVENTORY_LAST_REFRESH[user.id] = datetime.utcnow()

    try:
        descs = {d.get("classid"): d for d in data.get("descriptions", []) if isinstance(d, dict)}
        seen = set()
        items = []
        for a in data.get("assets", []):
            if not isinstance(a, dict):
                continue
            d = descs.get(a.get("classid"))
            if not d or not d.get("marketable") or not d.get("tradable"):
                continue
            mhn = d.get("market_hash_name")
            icon = d.get("icon_url", "")
            if not mhn or not isinstance(icon, str) or mhn in seen:
                continue
            seen.add(mhn)
            items.append({"market_hash_name": mhn,
                          "icon_url": "https://community.cloudflare.steamstatic.com/economy/image/" + icon})
        items = items[:60]
        sem = asyncio.Semaphore(2)

        async def price_one(item):
            # Ошибка цены одного предмета не должна ронять весь запрос
            try:
                async with sem:
                    async with httpx.AsyncClient(timeout=15) as client:
                        p = await fetch_steam_price(client, item["market_hash_name"])
                item["steam_price"] = p
                item["site_price"] = int(p * 0.85) if p else None
            except Exception:
                item["steam_price"] = None
                item["site_price"] = None

        await asyncio.gather(*[price_one(i) for i in items])
        items = [i for i in items if i.get("steam_price") and i["steam_price"] >= 20]
        items.sort(key=lambda x: -(x.get("site_price") or 0))
        debug_info["items_count"] = len(items)
        if not items:
            debug_info["reason"] = "no_suitable_items"
            return JSONResponse(_inv_error(user, "Нет подходящих скинов.", debug_info))
        return items
    except Exception as e:
        debug_info["reason"] = "processing_error"
        debug_info["error"] = repr(e)
        return JSONResponse(_inv_error(user, "Не удалось обработать инвентарь.", debug_info))


class SellItem(BaseModel):
    market_hash_name: str = Field(min_length=1, max_length=256)
    icon_url: str = Field(default="", max_length=512)

    @field_validator("market_hash_name")
    @classmethod
    def _check_mhn(cls, v):
        if "(" not in v or ")" not in v:
            raise ValueError("Некорректное название скина (должно содержать состояние)")
        return v


class SellIn(BaseModel):
    items: List[SellItem] = Field(min_length=1, max_length=5)


@app.post("/api/sale-requests")
async def create_sale_request(data: SellIn, user: User = Depends(get_current_user),
                              session: Session = Depends(get_db)):
    async with httpx.AsyncClient(timeout=25) as client:
        priced = []
        for it in data.items:
            mhn = it.market_hash_name
            sp = await fetch_steam_price(client, mhn)
            if not sp or sp < 20:
                raise HTTPException(400, f"Скин \"{mhn}\" дешевле $0.20 или цена недоступна")
            base, st, wear = parse_hash(mhn)
            skin = session.query(Skin).filter(Skin.market_hash_name == base).first()
            if not skin:
                skin = Skin(market_hash_name=base, name=base, icon_url=it.icon_url,
                            weapon_type=weapon_type_of(base))
                session.add(skin)
                session.commit()
            prow = session.query(Price).filter_by(skin_id=skin.id, stattrak=st, wear=wear).first()
            if prow:
                prow.steam_price = sp
                prow.buy_price = int(sp * 0.85)
                prow.sell_price = int(sp * 0.90)
                prow.updated_at = datetime.utcnow()
            else:
                session.add(Price(skin_id=skin.id, stattrak=st, wear=wear, steam_price=sp,
                                  buy_price=int(sp * 0.85), sell_price=int(sp * 0.90)))
            session.commit()
            priced.append({"market_hash_name": mhn, "icon_url": it.icon_url,
                           "steam_price": sp, "site_price": int(sp * 0.85),
                           "base": base, "stattrak": st, "wear": wear})

    def work():
        u = session.get(User, user.id)
        existing = session.query(SaleRequest).filter_by(user_id=u.id, status="pending").first()
        if existing:
            raise HTTPException(400, "У вас уже есть активная заявка на продажу")
        promo = None
        if u.active_promo:
            pc = session.query(Promocode).filter(Promocode.code == u.active_promo,
                                                 Promocode.type == "percent").first()
            if pc and (pc.max_uses == 0 or pc.used_count < pc.max_uses):
                promo = pc.code
        total = sum(p["site_price"] for p in priced)
        req = SaleRequest(user_id=u.id, items=priced, total=total, promo_code=promo)
        session.add(req)
        session.commit()
        _notify_admins(session, f"Новая заявка на продажу от {u.nickname} на {_money(total)}")
        session.commit()
        return {"ok": True, "id": req.id, "total": total}
    return await run_in_threadpool(work)


@app.get("/api/sale-requests/active")
async def active_sale_request(user: User = Depends(get_current_user), session: Session = Depends(get_db)):
    def work():
        req = session.query(SaleRequest).filter_by(user_id=user.id, status="pending").order_by(
            SaleRequest.id.desc()).first()
        if not req:
            return None
        return {"id": req.id, "items": req.items, "total": req.total,
                "trade_link": OWNER_TRADE_LINK,
                "created_at": req.created_at.isoformat() if req.created_at else None}
    return await run_in_threadpool(work)


class WithdrawIn(BaseModel):
    instance_ids: List[int] = Field(min_length=1, max_length=5)

    @field_validator("instance_ids")
    @classmethod
    def _check_ids(cls, v):
        if any(not isinstance(i, int) or i <= 0 for i in v):
            raise ValueError("ID предметов должны быть положительными числами")
        return v


@app.post("/api/withdraw")
async def create_withdrawal(data: WithdrawIn, user: User = Depends(get_current_user),
                            session: Session = Depends(get_db)):
    def work():
        u = session.get(User, user.id)
        if u.balance < 0:
            raise HTTPException(400, "Баланс в минусе — вывод недоступен")
        active = session.query(Withdrawal).filter(
            Withdrawal.user_id == u.id, Withdrawal.status.in_(["pending", "sending", "sent"])).first()
        if active:
            raise HTTPException(400, "У вас уже есть активная заявка на вывод")
        items = []
        for iid in data.instance_ids:
            inst = session.get(Instance, int(iid))
            if not inst or inst.owner_id != u.id or inst.status != "with_user":
                raise HTTPException(400, "Скин недоступен")
            skin = session.get(Skin, inst.skin_id)
            items.append({"instance_id": inst.id, "name": skin.name, "icon_url": skin.icon_url,
                          "stattrak": inst.stattrak, "wear": inst.wear,
                          "float_value": inst.float_value, "sell_price": inst.sell_price})
        wd = Withdrawal(user_id=u.id, instance_ids=[i["instance_id"] for i in items],
                        items=items, total=sum(i["sell_price"] for i in items))
        session.add(wd)
        session.commit()
        _notify_admins(session, f"Новая заявка на вывод от {u.nickname}: {len(items)} скин(а)")
        session.commit()
        return {"ok": True, "id": wd.id}
    return await run_in_threadpool(work)


@app.get("/api/withdrawals/active")
async def active_withdrawal(user: User = Depends(get_current_user), session: Session = Depends(get_db)):
    def work():
        wd = session.query(Withdrawal).filter(
            Withdrawal.user_id == user.id, Withdrawal.status.in_(["pending", "sending", "sent"])).order_by(
            Withdrawal.id.desc()).first()
        if not wd:
            return None
        return {"id": wd.id, "items": wd.items, "total": wd.total, "status": wd.status,
                "expires_at": wd.expires_at.isoformat() if wd.expires_at else None,
                "created_at": wd.created_at.isoformat() if wd.created_at else None}
    return await run_in_threadpool(work)


@app.post("/api/withdrawals/{wd_id}/confirm")
async def confirm_withdrawal(wd_id: int, user: User = Depends(get_current_user),
                             session: Session = Depends(get_db)):
    def work():
        wd = session.get(Withdrawal, wd_id)
        if not wd or wd.user_id != user.id:
            raise HTTPException(404, "Заявка не найдена")
        if wd.status != "sent":
            raise HTTPException(400, "Трейд ещё не отправлен")
        wd.status = "closed"
        _notify(session, user.id, "Вывод закрыт. Спасибо за покупку!")
        _notify_admins(session, f"Вывод #{wd.id} закрыт пользователем")
        session.commit()
        return {"ok": True}
    return await run_in_threadpool(work)


@app.get("/api/transactions")
async def transactions(user: User = Depends(get_current_user), session: Session = Depends(get_db)):
    def work():
        rows = session.query(Transaction).filter_by(user_id=user.id).order_by(
            Transaction.id.desc()).limit(100).all()
        return [{"id": t.id, "amount": t.amount, "type": t.type, "description": t.description,
                 "created_at": t.created_at.isoformat() if t.created_at else None} for t in rows]
    return await run_in_threadpool(work)


class PromoIn(BaseModel):
    code: str = Field(min_length=1, max_length=64)

    @field_validator("code")
    @classmethod
    def _check_code(cls, v):
        c = v.strip().upper()
        if not PROMO_CODE_RE.match(c):
            raise ValueError("Промокод может содержать только латинские буквы A-Z и цифры 0-9")
        return c


@app.post("/api/promo/apply")
async def apply_promo(data: PromoIn, user: User = Depends(get_current_user), session: Session = Depends(get_db)):
    code = data.code

    def work():
        u = session.get(User, user.id)
        pc = session.query(Promocode).filter(Promocode.code == code).first()
        if pc:
            if pc.type == "percent":
                if pc.max_uses > 0 and pc.used_count >= pc.max_uses:
                    raise HTTPException(400, "Промокод исчерпан")
                u.active_promo = code
                session.commit()
                return {"ok": True, "message": f"Промокод активирован: +{pc.value}% к следующей продаже"}
            inviter = session.query(User).filter(User.referral_code == code).first()
            if inviter:
                return _bind_referral(session, u, inviter)
            raise HTTPException(400, "Промокод не найден")
        inviter = session.query(User).filter(User.referral_code == code).first()
        if inviter:
            return _bind_referral(session, u, inviter)
        raise HTTPException(400, "Промокод не найден")
    return await run_in_threadpool(work)


def _bind_referral(session, user, inviter):
    if user.id == inviter.id:
        raise HTTPException(400, "Нельзя указать собственный код")
    if user.referred_by:
        raise HTTPException(400, "Реферал уже привязан")
    user.referred_by = inviter.id
    session.commit()
    _notify(session, inviter.id, f"По вашему коду зарегистрировался {user.nickname}")
    return {"ok": True, "message": "Реферальный код привязан"}


@app.get("/api/notifications")
async def notifications(user: User = Depends(get_current_user), session: Session = Depends(get_db)):
    def work():
        rows = session.query(Notification).filter_by(user_id=user.id).order_by(
            Notification.id.desc()).limit(50).all()
        unread = session.query(Notification).filter_by(user_id=user.id, is_read=False).count()
        return {"items": [{"id": n.id, "text": n.text, "is_read": n.is_read,
                           "created_at": n.created_at.isoformat() if n.created_at else None} for n in rows],
                "unread": unread}
    return await run_in_threadpool(work)


@app.post("/api/notifications/{nid}/read")
async def notif_read(nid: int, user: User = Depends(get_current_user), session: Session = Depends(get_db)):
    def work():
        n = session.get(Notification, nid)
        if n and n.user_id == user.id:
            n.is_read = True
            session.commit()
        return {"ok": True}
    return await run_in_threadpool(work)


@app.post("/api/notifications/read-all")
async def notif_read_all(user: User = Depends(get_current_user), session: Session = Depends(get_db)):
    def work():
        session.query(Notification).filter_by(user_id=user.id, is_read=False).update({"is_read": True})
        session.commit()
        return {"ok": True}
    return await run_in_threadpool(work)


@app.delete("/api/notifications/{nid}")
async def notif_delete(nid: int, user: User = Depends(get_current_user), session: Session = Depends(get_db)):
    def work():
        n = session.get(Notification, nid)
        if n and n.user_id == user.id:
            session.delete(n)
            session.commit()
        return {"ok": True}
    return await run_in_threadpool(work)


@app.delete("/api/notifications")
async def notif_delete_all(user: User = Depends(get_current_user), session: Session = Depends(get_db)):
    def work():
        session.query(Notification).filter_by(user_id=user.id).delete()
        session.commit()
        return {"ok": True}
    return await run_in_threadpool(work)


# ---------- Админка ----------

def _sale_req_dict(req, user):
    return {"id": req.id, "user_id": user.id, "nickname": user.nickname, "steam_id": user.steam_id,
            "items": req.items, "total": req.total, "promo_code": req.promo_code, "status": req.status,
            "reason": req.reason,
            "created_at": req.created_at.isoformat() if req.created_at else None,
            "resolved_at": req.resolved_at.isoformat() if req.resolved_at else None}


def _check_tab(tab: str):
    if tab not in ("active", "done"):
        raise HTTPException(400, "Параметр tab: active или done")


@app.get("/api/admin/sales")
async def admin_sales(tab: str = Query("active"), user: User = Depends(require_perm("sales")),
                      session: Session = Depends(get_db)):
    _check_tab(tab)

    def work():
        q = session.query(SaleRequest, User).join(User, SaleRequest.user_id == User.id)
        if tab == "done":
            q = q.filter(SaleRequest.status != "pending",
                         SaleRequest.resolved_at > datetime.utcnow() - timedelta(hours=24))
        else:
            q = q.filter(SaleRequest.status == "pending")
        q = q.order_by(SaleRequest.id.desc())
        return [_sale_req_dict(r, u) for r, u in q.all()]
    return await run_in_threadpool(work)


class DeclineIn(BaseModel):
    reason: str = Field(default="", max_length=500)
    penalty: bool = True


@app.post("/api/admin/sales/{req_id}/accept")
async def admin_sale_accept(req_id: int, user: User = Depends(require_perm("sales")),
                            session: Session = Depends(get_db)):
    def work():
        req = session.get(SaleRequest, req_id)
        if not req or req.status != "pending":
            raise HTTPException(400, "Заявка не активна")
        u = session.get(User, req.user_id)
        u.balance += req.total
        session.add(Transaction(user_id=u.id, amount=req.total, type="sale",
                                description=f"Продажа {len(req.items)} скин(а) по заявке #{req.id}"))
        if req.promo_code:
            pc = session.query(Promocode).filter_by(code=req.promo_code, type="percent").first()
            if pc and (pc.max_uses == 0 or pc.used_count < pc.max_uses):
                bonus = int(req.total * pc.value / 100)
                if bonus > 0:
                    u.balance += bonus
                    pc.used_count += 1
                    session.add(Transaction(user_id=u.id, amount=bonus, type="promo",
                                            description=f"Бонус по промокоду {pc.code} ({pc.value}%)"))
                    _notify(session, u.id, f"Промокод {pc.code}: начислено {_money(bonus)}")
                if u.active_promo == pc.code:
                    u.active_promo = None
        for it in req.items:
            base, st, wear = it["base"], it["stattrak"], it["wear"]
            skin = session.query(Skin).filter(Skin.market_hash_name == base).first()
            if skin:
                session.add(Instance(skin_id=skin.id, stattrak=st, wear=wear, float_value=0.0,
                                     status="in_stock", buy_price=it["site_price"],
                                     sell_price=int(it["steam_price"] * 0.90)))
        req.status = "accepted"
        req.resolved_at = datetime.utcnow()
        _notify(session, u.id, f"Заявка на продажу принята. Начислено {_money(req.total)}")
        session.commit()
        return {"ok": True}
    return await run_in_threadpool(work)


@app.post("/api/admin/sales/{req_id}/decline")
async def admin_sale_decline(req_id: int, data: DeclineIn, user: User = Depends(require_perm("sales")),
                             session: Session = Depends(get_db)):
    def work():
        req = session.get(SaleRequest, req_id)
        if not req or req.status != "pending":
            raise HTTPException(400, "Заявка не активна")
        u = session.get(User, req.user_id)
        req.status = "declined"
        req.reason = data.reason
        req.resolved_at = datetime.utcnow()
        if data.penalty:
            u.balance -= 100
            session.add(Transaction(user_id=u.id, amount=-100, type="penalty",
                                    description="Штраф за отклонённую заявку на продажу: " + (data.reason or "без причины")))
            _notify(session, u.id, f"Заявка отклонена. Причина: {data.reason}. Штраф $1.00")
        else:
            _notify(session, u.id, f"Заявка отклонена без штрафа. Комментарий: {data.reason}")
        session.commit()
        return {"ok": True}
    return await run_in_threadpool(work)


def _wd_dict(wd, user):
    return {"id": wd.id, "user_id": user.id, "nickname": user.nickname, "steam_id": user.steam_id,
            "trade_link": user.trade_link, "items": wd.items, "total": wd.total, "status": wd.status,
            "note": wd.note,
            "created_at": wd.created_at.isoformat() if wd.created_at else None,
            "expires_at": wd.expires_at.isoformat() if wd.expires_at else None}


@app.get("/api/admin/withdrawals")
async def admin_withdrawals(tab: str = Query("active"), user: User = Depends(require_perm("withdrawals")),
                            session: Session = Depends(get_db)):
    _check_tab(tab)

    def work():
        q = session.query(Withdrawal, User).join(User, Withdrawal.user_id == User.id)
        if tab == "done":
            q = q.filter(Withdrawal.status.in_(["closed", "cancelled"]),
                         Withdrawal.created_at > datetime.utcnow() - timedelta(hours=24))
        else:
            q = q.filter(Withdrawal.status.in_(["pending", "sending", "sent"]))
        q = q.order_by(Withdrawal.id.desc())
        return [_wd_dict(w, u) for w, u in q.all()]
    return await run_in_threadpool(work)


@app.post("/api/admin/withdrawals/{wd_id}/start")
async def admin_wd_start(wd_id: int, user: User = Depends(require_perm("withdrawals")),
                         session: Session = Depends(get_db)):
    def work():
        wd = session.get(Withdrawal, wd_id)
        if not wd or wd.status != "pending":
            raise HTTPException(400, "Заявка не в очереди")
        wd.status = "sending"
        wd.expires_at = datetime.utcnow() + timedelta(minutes=5)
        _notify(session, wd.user_id, "Админ начал вывод ваших скинов. У вас 5 минут на принятие трейда.")
        session.commit()
        return {"ok": True}
    return await run_in_threadpool(work)


@app.post("/api/admin/withdrawals/{wd_id}/sent")
async def admin_wd_sent(wd_id: int, user: User = Depends(require_perm("withdrawals")),
                        session: Session = Depends(get_db)):
    def work():
        wd = session.get(Withdrawal, wd_id)
        if not wd or wd.status not in ("pending", "sending"):
            raise HTTPException(400, "Неверный статус")
        wd.status = "sent"
        if not wd.expires_at:
            wd.expires_at = datetime.utcnow() + timedelta(minutes=5)
        _notify(session, wd.user_id, "Трейд отправлен. Примите обмен и подтвердите получение на сайте.")
        session.commit()
        return {"ok": True}
    return await run_in_threadpool(work)


@app.post("/api/admin/withdrawals/{wd_id}/close")
async def admin_wd_close(wd_id: int, user: User = Depends(require_perm("withdrawals")),
                         session: Session = Depends(get_db)):
    def work():
        wd = session.get(Withdrawal, wd_id)
        if not wd or wd.status in ("closed", "cancelled"):
            raise HTTPException(400, "Заявка уже закрыта")
        wd.status = "closed"
        _notify(session, wd.user_id, "Вывод закрыт. Спасибо за покупку!")
        session.commit()
        return {"ok": True}
    return await run_in_threadpool(work)


@app.post("/api/admin/withdrawals/{wd_id}/decline")
async def admin_wd_decline(wd_id: int, data: DeclineIn, user: User = Depends(require_perm("withdrawals")),
                           session: Session = Depends(get_db)):
    def work():
        wd = session.get(Withdrawal, wd_id)
        if not wd or wd.status in ("closed", "cancelled"):
            raise HTTPException(400, "Заявка уже закрыта")
        wd.status = "cancelled"
        wd.note = data.reason
        u = session.get(User, wd.user_id)
        if data.penalty:
            u.balance -= 100
            session.add(Transaction(user_id=u.id, amount=-100, type="penalty",
                                    description="Штраф по заявке на вывод: " + (data.reason or "без причины")))
            _notify(session, u.id, f"Вывод отклонён. Причина: {data.reason}. Штраф $1.00")
        else:
            _notify(session, u.id, f"Вывод отклонён без штрафа. Комментарий: {data.reason}")
        session.commit()
        return {"ok": True}
    return await run_in_threadpool(work)


def _po_dict(po, user, skin):
    return {"id": po.id, "user_id": user.id, "nickname": user.nickname, "steam_id": user.steam_id,
            "trade_link": user.trade_link, "skin_id": po.skin_id, "skin_name": skin.name,
            "icon_url": skin.icon_url, "stattrak": po.stattrak, "wear": po.wear, "price": po.price,
            "status": po.status,
            "created_at": po.created_at.isoformat() if po.created_at else None,
            "expires_at": po.expires_at.isoformat() if po.expires_at else None}


@app.get("/api/admin/preorders")
async def admin_preorders(tab: str = Query("active"), user: User = Depends(require_perm("preorders")),
                          session: Session = Depends(get_db)):
    _check_tab(tab)

    def work():
        q = session.query(Preorder, User, Skin).join(User, Preorder.user_id == User.id).join(
            Skin, Preorder.skin_id == Skin.id)
        if tab == "done":
            q = q.filter(Preorder.status != "active",
                         Preorder.created_at > datetime.utcnow() - timedelta(hours=24))
        else:
            q = q.filter(Preorder.status == "active")
        q = q.order_by(Preorder.id.desc())
        return [_po_dict(p, u, s) for p, u, s in q.all()]
    return await run_in_threadpool(work)


@app.post("/api/admin/preorders/{po_id}/fulfill")
async def admin_po_fulfill(po_id: int, user: User = Depends(require_perm("preorders")),
                           session: Session = Depends(get_db)):
    def work():
        po = session.get(Preorder, po_id)
        if not po or po.status != "active":
            raise HTTPException(400, "Предзаказ не активен")
        po.status = "done"
        session.add(Instance(skin_id=po.skin_id, stattrak=po.stattrak, wear=po.wear, float_value=0.0,
                             status="with_user", owner_id=po.user_id, buy_price=po.price, sell_price=po.price))
        _notify(session, po.user_id, "Ваш предзаказ выполнен! Скин в инвентаре на сайте.")
        session.commit()
        return {"ok": True}
    return await run_in_threadpool(work)


@app.post("/api/admin/preorders/{po_id}/cancel")
async def admin_po_cancel(po_id: int, user: User = Depends(require_perm("preorders")),
                          session: Session = Depends(get_db)):
    def work():
        po = session.get(Preorder, po_id)
        if not po or po.status != "active":
            raise HTTPException(400, "Предзаказ не активен")
        po.status = "cancelled"
        u = session.get(User, po.user_id)
        u.balance += po.price
        session.add(Transaction(user_id=u.id, amount=po.price, type="refund",
                                description=f"Возврат по предзаказу #{po.id}"))
        _notify(session, u.id, f"Предзаказ #{po.id} отменён, средства возвращены на баланс.")
        session.commit()
        return {"ok": True}
    return await run_in_threadpool(work)


@app.get("/api/admin/users")
async def admin_users(q: str = Query(""), user: User = Depends(require_perm("users")),
                      session: Session = Depends(get_db)):
    if len(q) > 64:
        raise HTTPException(400, "Строка поиска: максимум 64 символа")

    def work():
        query = session.query(User)
        if q.strip():
            query = query.filter((User.id == q.strip()) | (User.steam_id == q.strip()) |
                                 (User.nickname.ilike(f"%{q.strip()}%")))
        users = query.order_by(User.id.desc()).limit(200).all()
        out = []
        for u in users:
            d = user_public(u)
            d["referrals"] = session.query(User).filter_by(referred_by=u.id).count()
            out.append(d)
        return out
    return await run_in_threadpool(work)


class BalanceIn(BaseModel):
    delta: int = Field(ge=-10_000_000, le=10_000_000)
    description: str = Field(default="", max_length=500)


@app.post("/api/admin/users/{uid}/balance")
async def admin_balance(uid: int, data: BalanceIn, user: User = Depends(require_perm("users")),
                        session: Session = Depends(get_db)):
    def work():
        u = session.get(User, uid)
        if not u:
            raise HTTPException(404, "Пользователь не найден")
        u.balance += data.delta
        session.add(Transaction(user_id=u.id, amount=data.delta, type="promo",
                                description="Ручное начисление/списание: " + (data.description or "без комментария")))
        _notify(session, u.id, f"Баланс изменён на {_money(data.delta)}. {data.description}")
        session.commit()
        return {"ok": True, "balance": u.balance}
    return await run_in_threadpool(work)


class BanIn(BaseModel):
    minutes: Optional[int] = Field(default=None, ge=1, le=525_600)
    reason: str = Field(default="", max_length=500)


@app.post("/api/admin/users/{uid}/ban")
async def admin_ban(uid: int, data: BanIn, user: User = Depends(require_perm("bans")),
                    session: Session = Depends(get_db)):
    def work():
        u = session.get(User, uid)
        if not u:
            raise HTTPException(404, "Пользователь не найден")
        if u.steam_id == OWNER_STEAM_ID:
            raise HTTPException(400, "Нельзя заблокировать главного администратора")
        u.is_banned = True
        u.ban_until = datetime.utcnow() + timedelta(minutes=data.minutes) if data.minutes else None
        u.ban_reason = data.reason
        _notify(session, u.id, f"Аккаунт заблокирован. Причина: {data.reason or 'не указана'}")
        session.commit()
        return {"ok": True}
    return await run_in_threadpool(work)


class UnbanIn(BaseModel):
    comment: str = Field(default="", max_length=500)


@app.post("/api/admin/users/{uid}/unban")
async def admin_unban(uid: int, data: UnbanIn, user: User = Depends(require_perm("bans")),
                      session: Session = Depends(get_db)):
    def work():
        u = session.get(User, uid)
        if not u:
            raise HTTPException(404, "Пользователь не найден")
        u.is_banned = False
        u.ban_until = None
        u.ban_reason = ""
        _notify(session, u.id, "Аккаунт разблокирован. " + data.comment)
        session.commit()
        return {"ok": True}
    return await run_in_threadpool(work)


@app.get("/api/admin/promocodes")
async def admin_promocodes_list(user: User = Depends(require_perm("promocodes")),
                                session: Session = Depends(get_db)):
    def work():
        rows = session.query(Promocode).order_by(Promocode.id.desc()).all()
        return [{"id": p.id, "code": p.code, "type": p.type, "value": p.value,
                 "max_uses": p.max_uses, "used_count": p.used_count,
                 "created_at": p.created_at.isoformat() if p.created_at else None} for p in rows]
    return await run_in_threadpool(work)


class PromoCreateIn(BaseModel):
    code: str = Field(min_length=1, max_length=64)
    type: str
    value: int = Field(default=0, ge=0, le=100)
    max_uses: int = Field(default=0, ge=0, le=1_000_000)

    @field_validator("code")
    @classmethod
    def _check_code(cls, v):
        c = v.strip().upper()
        if not PROMO_CODE_RE.match(c):
            raise ValueError("Код может содержать только A-Z и 0-9")
        return c

    @field_validator("type")
    @classmethod
    def _check_type(cls, v):
        if v not in ("percent", "referral"):
            raise ValueError("Тип промокода: percent или referral")
        return v


@app.post("/api/admin/promocodes")
async def admin_promocodes_create(data: PromoCreateIn, user: User = Depends(require_perm("promocodes")),
                                  session: Session = Depends(get_db)):
    code = data.code

    def work():
        if session.query(Promocode).filter_by(code=code).first():
            raise HTTPException(400, "Такой код уже существует")
        session.add(Promocode(code=code, type=data.type, value=data.value, max_uses=data.max_uses))
        session.commit()
        return {"ok": True}
    return await run_in_threadpool(work)


@app.get("/api/admin/moderators")
async def admin_mods_list(user: User = Depends(require_perm("moderators")),
                          session: Session = Depends(get_db)):
    def work():
        rows = session.query(User).filter((User.is_admin == True) | (User.is_moderator == True)).all()
        return [user_public(u) for u in rows]
    return await run_in_threadpool(work)


class ModIn(BaseModel):
    identifier: str = Field(min_length=1, max_length=32)
    permissions: List[str] = Field(min_length=1)

    @field_validator("identifier")
    @classmethod
    def _check_ident(cls, v):
        ident = v.strip()
        if not ident.isdigit():
            raise ValueError("Идентификатор должен содержать только цифры (Site ID или Steam ID)")
        return ident

    @field_validator("permissions")
    @classmethod
    def _check_perms(cls, v):
        bad = [p for p in v if p not in ALL_PERMS]
        if bad:
            raise ValueError("Недопустимые права: " + ", ".join(str(p) for p in bad))
        return v


@app.post("/api/admin/moderators")
async def admin_mods_create(data: ModIn, user: User = Depends(require_perm("moderators")),
                            session: Session = Depends(get_db)):
    perms = [p for p in data.permissions if p in ALL_PERMS]
    ident = data.identifier

    def work():
        u = None
        if ident.isdigit():
            u = session.get(User, int(ident))
            if not u:
                u = session.query(User).filter_by(steam_id=ident).first()
        else:
            u = session.query(User).filter_by(steam_id=ident).first()
        if not u:
            raise HTTPException(404, "Пользователь не найден (Site ID или Steam ID)")
        if u.steam_id == OWNER_STEAM_ID:
            raise HTTPException(400, "Главный администратор уже имеет все права")
        u.is_moderator = True
        u.permissions = perms
        _notify(session, u.id, "Вам выданы права модератора: " + ", ".join(perms))
        session.commit()
        return {"ok": True}
    return await run_in_threadpool(work)


@app.delete("/api/admin/moderators/{uid}")
async def admin_mods_delete(uid: int, user: User = Depends(require_perm("moderators")),
                            session: Session = Depends(get_db)):
    def work():
        u = session.get(User, uid)
        if not u:
            raise HTTPException(404, "Пользователь не найден")
        if u.steam_id == OWNER_STEAM_ID:
            raise HTTPException(400, "Нельзя удалить главного администратора")
        u.is_moderator = False
        u.permissions = []
        _notify(session, u.id, "Права модератора отозваны")
        session.commit()
        return {"ok": True}
    return await run_in_threadpool(work)


@app.get("/api/admin/settings")
async def admin_settings_get(user: User = Depends(get_staff)):
    sess = SessionLocal()
    try:
        return {"support_link": _setting(sess, "support_link", DEFAULT_SUPPORT),
                "maintenance": _setting(sess, "maintenance", "0")}
    finally:
        sess.close()


class SettingsIn(BaseModel):
    support_link: Optional[str] = Field(default=None, max_length=200)
    maintenance: Optional[str] = None

    @field_validator("support_link")
    @classmethod
    def _check_support(cls, v):
        if v is not None and v != "" and not v.startswith("https://"):
            raise ValueError("Ссылка поддержки должна начинаться с https://")
        return v

    @field_validator("maintenance")
    @classmethod
    def _check_maintenance(cls, v):
        if v is not None and v not in ("0", "1"):
            raise ValueError("Параметр maintenance: только '0' или '1'")
        return v


@app.put("/api/admin/settings")
async def admin_settings_put(data: SettingsIn, user: User = Depends(require_perm("settings")),
                             session: Session = Depends(get_db)):
    def work():
        if data.support_link:
            row = session.get(Setting, "support_link") or Setting(key="support_link")
            row.value = data.support_link
            session.merge(row)
        if data.maintenance is not None:
            row = session.get(Setting, "maintenance") or Setting(key="maintenance")
            row.value = data.maintenance
            session.merge(row)
        session.commit()
        return {"ok": True}
    return await run_in_threadpool(work)


# ---------- Фоновые задачи ----------

def _upsert_price(skin_id, st, wear, steam):
    session = SessionLocal()
    try:
        row = session.query(Price).filter_by(skin_id=skin_id, stattrak=st, wear=wear).first()
        if row:
            row.steam_price = steam
            row.buy_price = int(steam * 0.85)
            row.sell_price = int(steam * 0.90)
            row.updated_at = datetime.utcnow()
        else:
            session.add(Price(skin_id=skin_id, stattrak=st, wear=wear, steam_price=steam,
                              buy_price=int(steam * 0.85), sell_price=int(steam * 0.90)))
        session.commit()
    finally:
        session.close()


async def parse_prices_job():
    session = SessionLocal()
    skins = session.query(Skin).all()
    session.close()
    async with httpx.AsyncClient(timeout=20) as client:
        for skin in skins:
            for st in (False, True):
                for wear in WEARS:
                    steam = await fetch_steam_price(client, build_hash(skin.market_hash_name, st, wear))
                    if steam is not None:
                        await run_in_threadpool(_upsert_price, skin.id, st, wear, steam)
                    await asyncio.sleep(0.25)


async def withdrawals_timeout_job():
    def work():
        session = SessionLocal()
        try:
            expired = session.query(Withdrawal).filter(
                Withdrawal.status.in_(["sending", "sent"]),
                Withdrawal.expires_at < datetime.utcnow()).all()
            for wd in expired:
                wd.status = "cancelled"
                wd.note = "Истёк таймаут 5 минут"
                u = session.get(User, wd.user_id)
                u.balance -= 100
                session.add(Transaction(user_id=u.id, amount=-100, type="penalty",
                                        description=f"Штраф по выводу #{wd.id}: истёк таймаут"))
                _notify(session, u.id, f"Вывод #{wd.id} отменён по таймауту. Штраф $1.00. Скины остались в инвентаре сайта.")
                session.commit()
        finally:
            session.close()
    await run_in_threadpool(work)


async def preorders_expire_job():
    def work():
        session = SessionLocal()
        try:
            expired = session.query(Preorder).filter(
                Preorder.status == "active", Preorder.expires_at < datetime.utcnow()).all()
            for po in expired:
                po.status = "cancelled"
                u = session.get(User, po.user_id)
                u.balance += po.price
                session.add(Transaction(user_id=u.id, amount=po.price, type="refund",
                                        description=f"Возврат по истёкшему предзаказу #{po.id}"))
                _notify(session, u.id, f"Предзаказ #{po.id} не выполнен за 14 дней, средства возвращены.")
                session.commit()
        finally:
            session.close()
    await run_in_threadpool(work)


async def users_check_job():
    session = SessionLocal()
    users = session.query(User).all()
    session.close()
    for i in range(0, len(users), 100):
        batch = users[i:i + 100]
        ids = ",".join(u.steam_id for u in batch)
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.get("https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v0002/",
                                     params={"key": STEAM_API_KEY, "steamids": ids})
            found = {p["steamid"] for p in r.json().get("response", {}).get("players", [])}
        except Exception:
            continue
        for u in batch:
            if u.steam_id not in found and u.steam_id != OWNER_STEAM_ID:
                session = SessionLocal()
                try:
                    for inst in session.query(Instance).filter_by(owner_id=u.id, status="with_user").all():
                        inst.status = "in_stock"
                        inst.owner_id = None
                    session.query(Notification).filter_by(user_id=u.id).delete()
                    session.query(Transaction).filter_by(user_id=u.id).delete()
                    session.delete(session.get(User, u.id))
                    session.commit()
                finally:
                    session.close()


@app.on_event("startup")
async def startup():
    scheduler.add_job(parse_prices_job, IntervalTrigger(hours=1),
                      next_run_time=datetime.now() + timedelta(seconds=30))
    scheduler.add_job(withdrawals_timeout_job, IntervalTrigger(seconds=30))
    scheduler.add_job(preorders_expire_job, IntervalTrigger(hours=1))
    scheduler.add_job(users_check_job, IntervalTrigger(days=3),
                      next_run_time=datetime.now() + timedelta(days=1))
    scheduler.start()
