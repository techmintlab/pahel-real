from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
import hashlib
import hmac
import logging
import os
import secrets
import uuid

import bcrypt
import jwt
import requests
from dotenv import load_dotenv
import razorpay
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect, status
from fastapi.responses import JSONResponse
from motor.motor_asyncio import AsyncIOMotorClient
from pydantic import BaseModel, Field, field_validator
from starlette.middleware.cors import CORSMiddleware

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("pahel")

MONGO_URL = os.getenv("MONGO_URL", "mongodb://localhost:27017")
DB_NAME = os.getenv("DB_NAME", "pahel_foundation")
JWT_SECRET = os.getenv("JWT_SECRET", "pahel-development-secret-change-me")
JWT_EXPIRE_MINUTES = int(os.getenv("JWT_EXPIRE_MINUTES", "10080"))
DEV_OTP = os.getenv("DEV_OTP", "123456")
APP_ENV = os.getenv("ENVIRONMENT", "development")
client = AsyncIOMotorClient(MONGO_URL, serverSelectionTimeoutMS=3000)
db = client[DB_NAME]
api_router = APIRouter(prefix="/api")
app = FastAPI(title="PAHEL FOUNDATION API", version="1.0.0")

if APP_ENV == "production":
    required_production = {"MONGO_URL": MONGO_URL, "DB_NAME": DB_NAME, "JWT_SECRET": JWT_SECRET, "ADMIN_EMAIL": os.getenv("ADMIN_EMAIL", ""), "ADMIN_PASSWORD": os.getenv("ADMIN_PASSWORD", "")}
    missing_production = [key for key, value in required_production.items() if not value or value in {"mongodb://localhost:27017", "pahel-development-secret-change-me"}]
    if missing_production:
        raise RuntimeError(f"Missing production configuration: {', '.join(missing_production)}")

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def clean_doc(value: Any) -> Any:
    """Convert Mongo documents into JSON-safe response data without leaking _id."""
    if isinstance(value, list):
        return [clean_doc(item) for item in value]
    if isinstance(value, dict):
        return {key: clean_doc(item) for key, item in value.items() if key != "_id"}
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    return value


def ok(message: str, data: Any = None) -> Dict[str, Any]:
    return {"success": True, "message": message, "data": clean_doc(data)}


def fail(message: str, code: int = 400) -> None:
    raise HTTPException(status_code=code, detail=message)


def normalize_mobile(value: str) -> str:
    digits = "".join(ch for ch in value if ch.isdigit())
    if digits.startswith("91") and len(digits) == 12:
        digits = digits[2:]
    if len(digits) != 10 or digits[0] not in "6789":
        fail("Enter a valid Indian mobile number", 422)
    return f"+91{digits}"


def hash_value(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def hash_password(value: str) -> str:
    return bcrypt.hashpw(value.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(value: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(value.encode("utf-8"), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def make_token(subject: str, role: str = "user") -> str:
    expires = datetime.now(timezone.utc) + timedelta(minutes=JWT_EXPIRE_MINUTES)
    return jwt.encode({"sub": subject, "role": role, "exp": expires}, JWT_SECRET, algorithm="HS256")


async def current_identity(request: Request) -> Dict[str, Any]:
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        fail("Please sign in to continue", 401)
    try:
        payload = jwt.decode(header.split(" ", 1)[1], JWT_SECRET, algorithms=["HS256"])
    except jwt.PyJWTError:
        fail("Your session has expired", 401)
    return payload


async def current_user(identity: Dict[str, Any] = Depends(current_identity)) -> Dict[str, Any]:
    if identity.get("role") != "user":
        fail("User access required", 403)
    user = await db.users.find_one({"id": identity.get("sub")}, {"_id": 0})
    if not user or user.get("status") == "INACTIVE":
        fail("User account is unavailable", 403)
    return user


async def admin_identity(identity: Dict[str, Any] = Depends(current_identity)) -> Dict[str, Any]:
    if identity.get("role") != "admin":
        fail("Administrator access required", 403)
    admin = await db.admins.find_one({"id": identity.get("sub")}, {"_id": 0})
    if not admin:
        fail("Administrator account is unavailable", 403)
    return admin


import asyncio


class SosLiveHub:
    """In-memory pub/sub for live SOS location broadcasts.

    Kept intentionally simple: one active-alert channel per SOS id. Responders
    connect via the WebSocket, senders push location updates through the REST
    endpoint, and this hub relays JSON payloads to every listener.
    """

    def __init__(self) -> None:
        self._connections: Dict[str, List[WebSocket]] = {}
        self._lock = asyncio.Lock()

    async def register(self, sos_id: str, websocket: WebSocket) -> None:
        async with self._lock:
            self._connections.setdefault(sos_id, []).append(websocket)

    async def unregister(self, sos_id: str, websocket: WebSocket) -> None:
        async with self._lock:
            listeners = self._connections.get(sos_id, [])
            if websocket in listeners:
                listeners.remove(websocket)
            if not listeners:
                self._connections.pop(sos_id, None)

    async def broadcast(self, sos_id: str, payload: Dict[str, Any]) -> None:
        async with self._lock:
            listeners = list(self._connections.get(sos_id, []))
        stale: List[WebSocket] = []
        for websocket in listeners:
            try:
                await websocket.send_json(payload)
            except Exception:
                stale.append(websocket)
        for websocket in stale:
            await self.unregister(sos_id, websocket)


sos_live = SosLiveHub()


async def send_apitxt_otp(mobile: str, code: str) -> Dict[str, Any]:
    auth_key = os.getenv("APITXT_AUTH_KEY", "")
    if not auth_key:
        return {"status": "fallback", "provider": "APITXT not configured"}
    try:
        response = requests.post(
            f"{os.getenv('APITXT_BASE_URL', 'https://apitxt.com').rstrip('/')}/api/sendOTP",
            data={"authkey": auth_key, "mobile": mobile.replace("+", ""), "otp": code, "channel": "sms"},
            timeout=10,
        )
        if response.ok:
            return {"status": "sent", "provider": "apitxt", "response": response.json() if response.content else {}}
        logger.warning("APITXT returned %s", response.status_code)
    except requests.RequestException as exc:
        logger.warning("APITXT unavailable: %s", exc)
    return {"status": "fallback", "provider": "APITXT unavailable"}


async def notify_tokens(tokens: List[str], title: str, message: str, data: Dict[str, Any]) -> Dict[str, Any]:
    """Legacy Expo-push fallback (kept for local dev where the Emergent relay is not configured)."""
    if not tokens:
        return {"status": "disabled", "sent": 0}
    sent = 0
    for token in tokens:
        try:
            response = requests.post(
                "https://exp.host/--/api/v2/push/send",
                json={"to": token, "title": title, "body": message, "data": data, "priority": "high"},
                timeout=8,
            )
            if response.ok:
                sent += 1
        except requests.RequestException:
            logger.warning("Push provider unavailable for token")
    return {"status": "sent" if sent else "unavailable", "sent": sent, "attempted": len(tokens)}


PUSH_BASE_URL = "https://integrations.emergentagent.com"


def push_relay_enabled() -> bool:
    key = os.getenv("EMERGENT_PUSH_KEY", "placeholder")
    return bool(key) and key != "placeholder"


async def push_register(user_id: str, platform: str, device_token: str) -> Dict[str, Any]:
    """Register a device token with the Emergent push relay (SuprSend → FCM/APNs).

    Runs as a no-op in preview where EMERGENT_PUSH_KEY is still the placeholder;
    the real key is injected at deployment time.
    """
    if not push_relay_enabled():
        return {"status": "development", "message": "Emergent push relay uses placeholder key"}
    try:
        response = requests.post(
            f"{PUSH_BASE_URL}/api/v1/push/users/register",
            headers={"X-Push-Key": os.getenv("EMERGENT_PUSH_KEY", "")},
            json={"user_id": user_id, "platform": platform, "device_token": device_token},
            timeout=10,
        )
        return {"status": "registered" if response.ok else "failed", "code": response.status_code}
    except requests.RequestException as exc:
        logger.warning("Emergent push register unavailable: %s", exc)
        return {"status": "failed", "message": "Provider unavailable"}


async def send_push(recipients: List[str], title: str, message: str, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Deliver a push to a list of user ids via the Emergent relay.

    Falls back to the Expo test relay (via push_tokens on the user document)
    when the Emergent key is still the placeholder so preview builds stay
    testable without touching production infrastructure.
    """
    recipients = [rid for rid in recipients if rid]
    if not recipients:
        return {"status": "empty", "sent": 0}
    data = {"title": title[:120], "message": message[:480]}
    if extra:
        data.update(extra)
    if not push_relay_enabled():
        users = await db.users.find({"id": {"$in": recipients}}, {"_id": 0, "push_tokens": 1}).to_list(len(recipients))
        tokens = [token for user in users for token in user.get("push_tokens", [])]
        legacy = await notify_tokens(tokens, title, message, extra or {})
        return {"status": legacy.get("status", "development"), "sent": legacy.get("sent", 0), "attempted": len(recipients), "channel": "expo_legacy"}
    sent = 0
    for start in range(0, len(recipients), 100):
        chunk = recipients[start:start + 100]
        try:
            response = requests.post(
                f"{PUSH_BASE_URL}/api/v1/push/trigger",
                headers={"X-Push-Key": os.getenv("EMERGENT_PUSH_KEY", "")},
                json={"recipients": chunk, "data": data},
                timeout=10,
            )
            if response.ok:
                sent += len(chunk)
            else:
                logger.warning("Emergent push relay returned %s: %s", response.status_code, response.text[:200])
        except requests.RequestException as exc:
            logger.warning("Emergent push relay unavailable: %s", exc)
    return {"status": "sent" if sent else "failed", "sent": sent, "attempted": len(recipients), "channel": "emergent"}


async def record_notifications(user_ids: List[str], notification_type: str, title: str, message: str, related_id: Optional[str] = None) -> int:
    """Persist an in-app notification record for every recipient."""
    docs = [
        {
            "id": str(uuid.uuid4()),
            "user_id": uid,
            "type": notification_type,
            "title": title,
            "message": message,
            "related_id": related_id,
            "is_read": False,
            "created_at": now_iso(),
        }
        for uid in user_ids
        if uid
    ]
    if docs:
        await db.notifications.insert_many(docs)
    return len(docs)


async def send_whatsapp_emergency(contact: str, message: str) -> Dict[str, Any]:
    if os.getenv("WHATSAPP_ENABLED", "false").lower() != "true":
        return {"status": "disabled", "message": "WhatsApp provider is not configured"}
    url = os.getenv("WHATSAPP_API_URL", "")
    if not url:
        return {"status": "disabled", "message": "WhatsApp provider URL missing"}
    # PAHEL uses a simple GET-based WhatsApp bridge: ?number=<10-digit>&message=<text>
    digits = "".join(ch for ch in contact if ch.isdigit())
    if digits.startswith("91") and len(digits) == 12:
        digits = digits[2:]
    try:
        query = urlencode({"number": digits, "message": message})
        response = requests.get(f"{url}?{query}", timeout=10)
        payload: Dict[str, Any] = {}
        try:
            payload = response.json()
        except ValueError:
            payload = {"raw": response.text[:200]}
        if response.ok and payload.get("success") is not False:
            return {"status": "sent", "provider_code": response.status_code, "response": payload}
        logger.warning("WhatsApp bridge returned %s: %s", response.status_code, payload)
        return {"status": "failed", "provider_code": response.status_code, "response": payload}
    except requests.RequestException as exc:
        logger.warning("WhatsApp unavailable: %s", exc)
        return {"status": "failed", "message": "Provider unavailable"}


class MobileInput(BaseModel):
    mobile: str


class VerifyOtpInput(BaseModel):
    mobile: str
    otp: str = Field(min_length=4, max_length=8)


class EmergencyContactInput(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    mobile: str
    relationship: str = Field(default="Emergency contact", max_length=40)


class LocationInput(BaseModel):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    accuracy: Optional[float] = None


class DeviceInput(BaseModel):
    token: str = Field(min_length=8)
    platform: str = Field(default="expo", max_length=20)


class BloodRequestInput(BaseModel):
    patient_name: str = Field(min_length=2, max_length=100)
    blood_group: str = Field(pattern=r"^(A|B|AB|O)[+-]$")
    units: int = Field(ge=1, le=20)
    hospital: str = Field(min_length=2, max_length=160)
    contact_name: str = Field(min_length=2, max_length=80)
    contact_mobile: str
    details: str = Field(default="", max_length=500)
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)


class VolunteerInput(BaseModel):
    full_name: str = Field(min_length=2, max_length=100)
    email: str = Field(default="", max_length=160)
    address: str = Field(default="", max_length=240)
    city: str = Field(default="", max_length=80)
    reason: str = Field(min_length=10, max_length=800)
    skills: str = Field(default="", max_length=300)
    availability: str = Field(default="", max_length=120)


class ProfileInput(BaseModel):
    name: str = Field(min_length=2, max_length=100)


class DonationOrderInput(BaseModel):
    amount: int = Field(ge=100, le=1000000)
    purpose: str = Field(default="Foundation support", max_length=100)


class PaymentVerifyInput(BaseModel):
    order_id: str
    payment_id: str
    signature: str = ""


class PlanInput(BaseModel):
    name: str = Field(min_length=2, max_length=100)
    description: str = Field(default="", max_length=500)
    price: int = Field(ge=0, le=1000000)
    billing_type: str = Field(pattern=r"^(MONTHLY|YEARLY)$")
    benefits: List[str] = Field(default_factory=list)
    eligibility: str = Field(default="Open to supporters", max_length=300)
    active: bool = True
    display_order: int = 0


class AdminLoginInput(BaseModel):
    email: str
    password: str


class BroadcastInput(BaseModel):
    title: str = Field(min_length=2, max_length=100)
    message: str = Field(min_length=2, max_length=500)


@api_router.get("/")
async def api_root() -> Dict[str, Any]:
    return ok("PAHEL FOUNDATION API is running", {"environment": APP_ENV, "version": "1.0.0"})


@api_router.post("/auth/send-otp")
async def send_otp(payload: MobileInput) -> Dict[str, Any]:
    mobile = normalize_mobile(payload.mobile)
    recent = await db.otp_sessions.find_one({"mobile": mobile, "created_at": {"$gte": (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()}}, {"_id": 0})
    if recent:
        fail("Please wait 30 seconds before requesting another OTP", 429)
    code = DEV_OTP if APP_ENV != "production" else f"{secrets.randbelow(1000000):06d}"
    provider = await send_apitxt_otp(mobile, code)
    await db.otp_sessions.insert_one({"id": str(uuid.uuid4()), "mobile": mobile, "otp_hash": hash_value(code), "attempts": 0, "created_at": now_iso(), "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()})
    data: Dict[str, Any] = {"mobile": mobile, "provider_status": provider["status"]}
    if provider["status"] == "fallback" and APP_ENV != "production":
        data["development_otp"] = DEV_OTP
        data["is_development_fallback"] = True
    return ok("OTP sent successfully", data)


@api_router.post("/auth/verify-otp")
async def verify_otp(payload: VerifyOtpInput) -> Dict[str, Any]:
    mobile = normalize_mobile(payload.mobile)
    session = await db.otp_sessions.find_one({"mobile": mobile}, sort=[("created_at", -1)])
    if not session:
        fail("Request an OTP first")
    if datetime.fromisoformat(session["expires_at"]) < datetime.now(timezone.utc) or session.get("attempts", 0) >= 5:
        fail("This OTP has expired. Request a new one")
    if not hmac.compare_digest(session["otp_hash"], hash_value(payload.otp)):
        await db.otp_sessions.update_one({"id": session["id"]}, {"$inc": {"attempts": 1}})
        fail("The OTP is incorrect")
    user = await db.users.find_one({"mobile": mobile}, {"_id": 0})
    is_new = not user
    if not user:
        user = {"id": str(uuid.uuid4()), "mobile": mobile, "name": "", "emergency_contact": None, "location": None, "push_tokens": [], "volunteer_status": "NONE", "status": "ACTIVE", "created_at": now_iso(), "updated_at": now_iso()}
        await db.users.insert_one(dict(user))
    await db.otp_sessions.delete_one({"id": session["id"]})
    return ok("Welcome to PAHEL FOUNDATION", {"token": make_token(user["id"]), "user": user, "is_new_user": is_new, "needs_emergency_contact": not user.get("emergency_contact")})


@api_router.get("/users/me")
async def get_me(user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    return ok("Profile loaded", user)


@api_router.patch("/users/me")
async def update_me(payload: ProfileInput, user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    await db.users.update_one({"id": user["id"]}, {"$set": {"name": payload.name.strip(), "updated_at": now_iso()}})
    user["name"] = payload.name.strip()
    return ok("Profile updated", user)


@api_router.put("/users/me/emergency-contact")
async def update_emergency_contact(payload: EmergencyContactInput, user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    contact = {"name": payload.name.strip(), "mobile": normalize_mobile(payload.mobile), "relationship": payload.relationship.strip()}
    await db.users.update_one({"id": user["id"]}, {"$set": {"emergency_contact": contact, "updated_at": now_iso()}})
    return ok("Emergency contact saved", contact)


@api_router.post("/users/me/location")
async def update_location(payload: LocationInput, user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    location = {"type": "Point", "coordinates": [payload.longitude, payload.latitude], "latitude": payload.latitude, "longitude": payload.longitude, "accuracy": payload.accuracy, "updated_at": now_iso()}
    await db.users.update_one({"id": user["id"]}, {"$set": {"location": location, "updated_at": now_iso()}})
    return ok("Location updated", location)


@api_router.post("/users/me/device")
async def register_device(payload: DeviceInput, user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    await db.users.update_one({"id": user["id"]}, {"$addToSet": {"push_tokens": payload.token}, "$set": {"device_platform": payload.platform, "updated_at": now_iso()}})
    relay = await push_register(user["id"], payload.platform, payload.token)
    return ok("Notification device registered", {"relay": relay})


@api_router.post("/sos/create")
async def create_sos(user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    if not user.get("emergency_contact"):
        fail("Add an emergency contact before sending SOS")
    if not user.get("location"):
        fail("Location is required before sending SOS")
    sos = {"id": str(uuid.uuid4()), "user_id": user["id"], "user_name": user.get("name") or "PAHEL member", "user_mobile": user["mobile"], "location": user["location"], "status": "ACTIVE", "nearby_users_notified": 0, "whatsapp_status": "pending", "created_at": now_iso(), "resolved_at": None}
    await db.sos_alerts.insert_one(dict(sos))
    location = user["location"]
    try:
        nearby = await db.users.find({"id": {"$ne": user["id"]}, "location": {"$near": {"$geometry": {"type": "Point", "coordinates": [location["longitude"], location["latitude"]]}, "$maxDistance": int(float(os.getenv("SOS_RADIUS_KM", "50")) * 1000)}}, "status": "ACTIVE"}, {"_id": 0, "id": 1}).to_list(500)
    except Exception as exc:
        logger.warning("Nearby SOS query unavailable: %s", exc)
        nearby = []
    nearby_ids = [item["id"] for item in nearby if item.get("id")]
    push_result = await send_push(
        nearby_ids,
        "Emergency Help Needed",
        f"{sos['user_name']} nearby needs urgent help. Tap to view the live location.",
        {"type": "SOS", "sosId": sos["id"], "action_url": f"/sos/{sos['id']}"},
    )
    await record_notifications(nearby_ids, "SOS", "Emergency Help Needed", f"{sos['user_name']} nearby needs urgent help.", sos["id"])
    whatsapp = await send_whatsapp_emergency(user["emergency_contact"]["mobile"], f"PAHEL FOUNDATION EMERGENCY ALERT\nName: {sos['user_name']}\nMobile: {sos['user_mobile']}\nLocation: https://www.openstreetmap.org/?mlat={location['latitude']}&mlon={location['longitude']}")
    await db.sos_alerts.update_one({"id": sos["id"]}, {"$set": {"nearby_users_notified": len(nearby_ids), "whatsapp_status": whatsapp.get("status", "unknown"), "push_status": push_result.get("status")}})
    sos["nearby_users_notified"] = len(nearby_ids)
    sos["whatsapp_status"] = whatsapp.get("status", "unknown")
    sos["push_status"] = push_result.get("status")
    return ok("SOS created successfully", sos)


@api_router.get("/sos/history")
async def sos_history(user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    records = await db.sos_alerts.find({"user_id": user["id"]}, {"_id": 0}).sort("created_at", -1).to_list(100)
    return ok("SOS history loaded", records)


@api_router.get("/sos/{sos_id}")
async def get_sos(sos_id: str, user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    record = await db.sos_alerts.find_one({"id": sos_id, "$or": [{"user_id": user["id"]}, {"status": "ACTIVE"}]}, {"_id": 0})
    if not record:
        fail("SOS alert not found", 404)
    return ok("SOS alert loaded", record)


@api_router.post("/sos/{sos_id}/cancel")
async def cancel_sos(sos_id: str, user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    result = await db.sos_alerts.update_one({"id": sos_id, "user_id": user["id"], "status": "ACTIVE"}, {"$set": {"status": "CANCELLED", "resolved_at": now_iso()}})
    if not result.modified_count:
        fail("Active SOS alert not found", 404)
    await sos_live.broadcast(sos_id, {"type": "status", "sos_id": sos_id, "status": "CANCELLED"})
    return ok("SOS cancelled")


@api_router.post("/sos/{sos_id}/location")
async def update_sos_location(sos_id: str, payload: LocationInput, user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    record = await db.sos_alerts.find_one({"id": sos_id, "user_id": user["id"]}, {"_id": 0})
    if not record:
        fail("SOS alert not found", 404)
    if record.get("status") != "ACTIVE":
        fail("Only active SOS alerts can update their location", 409)
    location = {"type": "Point", "coordinates": [payload.longitude, payload.latitude], "latitude": payload.latitude, "longitude": payload.longitude, "accuracy": payload.accuracy, "updated_at": now_iso()}
    await db.sos_alerts.update_one({"id": sos_id}, {"$set": {"location": location, "updated_at": now_iso()}})
    await db.users.update_one({"id": user["id"]}, {"$set": {"location": location, "updated_at": now_iso()}})
    await sos_live.broadcast(sos_id, {"type": "location", "sos_id": sos_id, "latitude": payload.latitude, "longitude": payload.longitude, "accuracy": payload.accuracy, "updated_at": location["updated_at"]})
    return ok("SOS location updated", location)


@api_router.get("/blood-requests")
async def nearby_blood_requests(user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    query: Dict[str, Any] = {"status": "OPEN"}
    if user.get("location"):
        loc = user["location"]
        query["location"] = {"$near": {"$geometry": {"type": "Point", "coordinates": [loc["longitude"], loc["latitude"]]}, "$maxDistance": int(float(os.getenv("BLOOD_RADIUS_KM", "50")) * 1000)}}
    requests_list = await db.blood_requests.find(query, {"_id": 0}).limit(100).to_list(100)
    return ok("Blood requests loaded", requests_list)


@api_router.post("/blood-requests")
async def create_blood_request(payload: BloodRequestInput, user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    location = {"type": "Point", "coordinates": [payload.longitude, payload.latitude], "latitude": payload.latitude, "longitude": payload.longitude}
    record = {"id": str(uuid.uuid4()), "requester_id": user["id"], "requester_name": user.get("name") or "PAHEL member", "patient_name": payload.patient_name, "blood_group": payload.blood_group, "units": payload.units, "hospital": payload.hospital, "contact_name": payload.contact_name, "contact_mobile": normalize_mobile(payload.contact_mobile), "details": payload.details, "location": location, "status": "OPEN", "donor_id": None, "created_at": now_iso()}
    await db.blood_requests.insert_one(dict(record))
    nearby_ids: List[str] = []
    try:
        nearby_users = await db.users.find({"id": {"$ne": user["id"]}, "location": {"$near": {"$geometry": {"type": "Point", "coordinates": [payload.longitude, payload.latitude]}, "$maxDistance": int(float(os.getenv("BLOOD_RADIUS_KM", "50")) * 1000)}}}, {"_id": 0, "id": 1}).limit(500).to_list(500)
        nearby_ids = [item["id"] for item in nearby_users if item.get("id")]
    except Exception as exc:
        logger.warning("Nearby blood query unavailable: %s", exc)
    push = await send_push(
        nearby_ids,
        f"Blood Donation Request · {payload.blood_group}",
        f"{payload.units} unit(s) needed at {payload.hospital}. Tap to see details.",
        {"type": "BLOOD_REQUEST", "requestId": record["id"], "action_url": f"/blood/{record['id']}"},
    )
    await record_notifications(nearby_ids, "BLOOD_REQUEST", f"Blood Donation Request · {payload.blood_group}", f"{payload.units} unit(s) needed at {payload.hospital}.", record["id"])
    return ok("Blood request created", {**record, "nearby_users_notified": len(nearby_ids), "push_status": push.get("status")})


@api_router.get("/blood-requests/history")
async def blood_history(user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    records = await db.blood_requests.find({"requester_id": user["id"]}, {"_id": 0}).sort("created_at", -1).to_list(100)
    return ok("Blood request history loaded", records)


@api_router.get("/blood-requests/{request_id}")
async def blood_detail(request_id: str, user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    record = await db.blood_requests.find_one({"id": request_id}, {"_id": 0})
    if not record:
        fail("Blood request not found", 404)
    return ok("Blood request loaded", record)


@api_router.post("/blood-requests/{request_id}/donate")
async def donate_blood(request_id: str, user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    record = await db.blood_requests.find_one({"id": request_id}, {"_id": 0})
    if not record:
        fail("Blood request not found", 404)
    response = {"id": str(uuid.uuid4()), "request_id": request_id, "donor_id": user["id"], "donor_name": user.get("name") or "PAHEL member", "created_at": now_iso()}
    await db.blood_responses.insert_one(dict(response))
    await db.blood_requests.update_one({"id": request_id, "status": "OPEN"}, {"$set": {"status": "DONOR_FOUND", "donor_id": user["id"]}})
    return ok("Thank you for offering to donate", response)


@api_router.post("/volunteers/apply")
async def apply_volunteer(payload: VolunteerInput, user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    existing = await db.volunteer_applications.find_one({"user_id": user["id"], "status": {"$in": ["PENDING", "APPROVED"]}}, {"_id": 0})
    if existing:
        return ok("Your volunteer application is already on file", existing)
    record = {"id": str(uuid.uuid4()), "user_id": user["id"], **payload.model_dump(), "status": "PENDING", "created_at": now_iso(), "reviewed_at": None}
    await db.volunteer_applications.insert_one(dict(record))
    await db.users.update_one({"id": user["id"]}, {"$set": {"volunteer_status": "PENDING"}})
    return ok("Your volunteer application has been sent for approval", record)


@api_router.get("/volunteers/status")
async def volunteer_status(user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    record = await db.volunteer_applications.find_one({"user_id": user["id"]}, {"_id": 0}, sort=[("created_at", -1)])
    return ok("Volunteer status loaded", record or {"status": user.get("volunteer_status", "NONE")})


@api_router.get("/plans")
async def get_plans(_: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    plans = await db.plans.find({"active": True}, {"_id": 0}).sort("display_order", 1).to_list(100)
    return ok("Plans loaded", plans)


def razorpay_client() -> Optional[razorpay.Client]:
    key_id = os.getenv("RAZORPAY_KEY_ID", "")
    key_secret = os.getenv("RAZORPAY_KEY_SECRET", "")
    if not key_id or not key_secret:
        return None
    return razorpay.Client(auth=(key_id, key_secret))


async def create_payment_order(user: Dict[str, Any], amount: int, purpose: str, kind: str) -> Dict[str, Any]:
    client_rp = razorpay_client()
    receipt = f"pf_{uuid.uuid4().hex[:16]}"
    if client_rp is not None:
        try:
            razorpay_order = client_rp.order.create({
                "amount": amount * 100,  # Razorpay expects paise
                "currency": "INR",
                "receipt": receipt,
                "notes": {"user_id": user["id"], "kind": kind, "purpose": purpose},
            })
            order_id = razorpay_order["id"]
        except Exception as exc:
            logger.warning("Razorpay order.create failed, falling back to development order: %s", exc)
            order_id = f"order_dev_{uuid.uuid4().hex[:14]}"
            client_rp = None
    else:
        order_id = f"order_dev_{uuid.uuid4().hex[:14]}"
    record = {"id": str(uuid.uuid4()), "order_id": order_id, "receipt": receipt, "user_id": user["id"], "amount": amount, "purpose": purpose, "kind": kind, "status": "CREATED", "created_at": now_iso()}
    await db.payment_orders.insert_one(dict(record))
    return {**record, "provider": "razorpay" if client_rp is not None else "development_fallback", "key_id": os.getenv("RAZORPAY_KEY_ID", "") if client_rp is not None else None, "is_development_fallback": client_rp is None}


@api_router.post("/donations/create-order")
async def donation_order(payload: DonationOrderInput, user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    return ok("Donation order created", await create_payment_order(user, payload.amount, payload.purpose, "DONATION"))


@api_router.post("/donations/verify")
async def verify_donation(payload: PaymentVerifyInput, user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    order = await db.payment_orders.find_one({"order_id": payload.order_id, "user_id": user["id"], "kind": "DONATION"}, {"_id": 0})
    if not order:
        fail("Payment order not found", 404)
    configured = bool(os.getenv("RAZORPAY_KEY_SECRET"))
    if configured:
        expected = hmac.new(os.getenv("RAZORPAY_KEY_SECRET", "").encode(), f"{payload.order_id}|{payload.payment_id}".encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, payload.signature):
            fail("Payment verification failed")
    elif payload.payment_id != "mock_payment":
        fail("Development payment requires mock_payment")
    receipt = f"PF-{datetime.now().year}-{secrets.randbelow(9000) + 1000}"
    donation = {"id": str(uuid.uuid4()), "user_id": user["id"], "amount": order["amount"], "purpose": order["purpose"], "payment_id": payload.payment_id, "receipt_number": receipt, "status": "SUCCESS", "created_at": now_iso()}
    await db.donations.insert_one(dict(donation))
    await db.payment_orders.update_one({"order_id": payload.order_id}, {"$set": {"status": "PAID", "payment_id": payload.payment_id}})
    return ok("Donation confirmed", donation)


@api_router.get("/donations/history")
async def donation_history(user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    return ok("Donation history loaded", await db.donations.find({"user_id": user["id"]}, {"_id": 0}).sort("created_at", -1).to_list(100))


@api_router.post("/subscriptions/create-order")
async def subscription_order(plan_id: str, user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    plan = await db.plans.find_one({"id": plan_id, "active": True}, {"_id": 0})
    if not plan:
        fail("Plan not found", 404)
    return ok("Subscription order created", await create_payment_order(user, plan["price"], plan["name"], "SUBSCRIPTION"))


@api_router.post("/subscriptions/verify")
async def verify_subscription(payload: PaymentVerifyInput, user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    order = await db.payment_orders.find_one({"order_id": payload.order_id, "user_id": user["id"], "kind": "SUBSCRIPTION"}, {"_id": 0})
    if not order:
        fail("Subscription order not found", 404)
    secret = os.getenv("RAZORPAY_KEY_SECRET", "")
    if secret:
        expected = hmac.new(secret.encode(), f"{payload.order_id}|{payload.payment_id}".encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, payload.signature):
            fail("Payment verification failed")
    elif payload.payment_id != "mock_payment":
        fail("Development payment requires mock_payment")
    subscription = {"id": str(uuid.uuid4()), "user_id": user["id"], "plan_name": order["purpose"], "amount": order["amount"], "status": "ACTIVE", "payment_id": payload.payment_id, "start_date": now_iso(), "expiry_date": (datetime.now(timezone.utc) + timedelta(days=365)).isoformat(), "created_at": now_iso()}
    await db.subscriptions.insert_one(dict(subscription))
    await db.payment_orders.update_one({"order_id": payload.order_id}, {"$set": {"status": "PAID", "payment_id": payload.payment_id}})
    return ok("Subscription activated", subscription)


@api_router.get("/subscriptions/history")
async def subscription_history(user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    return ok("Subscription history loaded", await db.subscriptions.find({"user_id": user["id"]}, {"_id": 0}).sort("created_at", -1).to_list(100))


@api_router.get("/notifications")
async def notifications(page: int = Query(1, ge=1), limit: int = Query(20, ge=1, le=50), user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    records = await db.notifications.find({"user_id": user["id"]}, {"_id": 0}).sort("created_at", -1).skip((page - 1) * limit).limit(limit).to_list(limit)
    return ok("Notifications loaded", {"items": records, "page": page, "limit": limit})


@api_router.post("/notifications/{notification_id}/read")
async def read_notification(notification_id: str, user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    await db.notifications.update_one({"id": notification_id, "user_id": user["id"]}, {"$set": {"is_read": True}})
    return ok("Notification marked as read")


@api_router.get("/settings/contact")
async def contact_settings(_: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    settings = await db.system_settings.find_one({"key": "contact"}, {"_id": 0})
    return ok("Contact settings loaded", settings or {"help_number": "", "emergency_number": "", "help_email": "", "support_message": "We are here to help."})


@api_router.post("/admin/auth/login")
async def admin_login(payload: AdminLoginInput) -> Dict[str, Any]:
    admin = await db.admins.find_one({"email": payload.email.lower().strip()}, {"_id": 0})
    if not admin or not verify_password(payload.password, admin["password_hash"]):
        fail("Invalid administrator credentials", 401)
    return ok("Administrator signed in", {"token": make_token(admin["id"], "admin"), "admin": {"id": admin["id"], "email": admin["email"]}})


@api_router.get("/admin/dashboard")
async def admin_dashboard(_: Dict[str, Any] = Depends(admin_identity)) -> Dict[str, Any]:
    counts = {name: await db[name].count_documents({}) for name in ["users", "sos_alerts", "blood_requests", "volunteer_applications", "donations", "subscriptions"]}
    counts["open_blood_requests"] = await db.blood_requests.count_documents({"status": "OPEN"})
    counts["pending_volunteers"] = await db.volunteer_applications.count_documents({"status": "PENDING"})
    counts["approved_volunteers"] = await db.volunteer_applications.count_documents({"status": "APPROVED"})
    donation_sum = await db.donations.aggregate([{"$match": {"status": "SUCCESS"}}, {"$group": {"_id": None, "total": {"$sum": "$amount"}}}]).to_list(1)
    counts["total_donations_amount"] = donation_sum[0]["total"] if donation_sum else 0
    return ok("Dashboard loaded", counts)


@api_router.get("/admin/users")
async def admin_users(page: int = Query(1, ge=1), limit: int = Query(25, ge=1, le=100), _: Dict[str, Any] = Depends(admin_identity)) -> Dict[str, Any]:
    items = await db.users.find({}, {"_id": 0}).sort("created_at", -1).skip((page - 1) * limit).limit(limit).to_list(limit)
    return ok("Users loaded", {"items": items, "page": page, "total": await db.users.count_documents({})})


@api_router.get("/admin/sos")
async def admin_sos(_: Dict[str, Any] = Depends(admin_identity)) -> Dict[str, Any]:
    return ok("SOS alerts loaded", await db.sos_alerts.find({}, {"_id": 0}).sort("created_at", -1).limit(200).to_list(200))


@api_router.get("/admin/blood-requests")
async def admin_blood(_: Dict[str, Any] = Depends(admin_identity)) -> Dict[str, Any]:
    return ok("Blood requests loaded", await db.blood_requests.find({}, {"_id": 0}).sort("created_at", -1).limit(200).to_list(200))


@api_router.get("/admin/volunteers")
async def admin_volunteers(_: Dict[str, Any] = Depends(admin_identity)) -> Dict[str, Any]:
    return ok("Volunteers loaded", await db.volunteer_applications.find({}, {"_id": 0}).sort("created_at", -1).limit(200).to_list(200))


async def review_volunteer(application_id: str, decision: str) -> Dict[str, Any]:
    application = await db.volunteer_applications.find_one({"id": application_id}, {"_id": 0})
    if not application:
        fail("Volunteer application not found", 404)
    await db.volunteer_applications.update_one({"id": application_id}, {"$set": {"status": decision, "reviewed_at": now_iso()}})
    await db.users.update_one({"id": application["user_id"]}, {"$set": {"volunteer_status": decision}})
    if decision in {"APPROVED", "REJECTED"}:
        title = "Volunteer application approved" if decision == "APPROVED" else "Volunteer application update"
        message = "You are now a PAHEL FOUNDATION volunteer." if decision == "APPROVED" else "Your volunteer application needs a fresh look. Please contact us."
        await send_push([application["user_id"]], title, message, {"type": "VOLUNTEER", "action_url": "/profile"})
        await record_notifications([application["user_id"]], "VOLUNTEER", title, message, application_id)
    return ok(f"Volunteer application {decision.lower()}")


@api_router.post("/admin/volunteers/{application_id}/approve")
async def approve_volunteer(application_id: str, _: Dict[str, Any] = Depends(admin_identity)) -> Dict[str, Any]:
    return await review_volunteer(application_id, "APPROVED")


@api_router.post("/admin/volunteers/{application_id}/reject")
async def reject_volunteer(application_id: str, _: Dict[str, Any] = Depends(admin_identity)) -> Dict[str, Any]:
    return await review_volunteer(application_id, "REJECTED")


@api_router.get("/admin/plans")
async def admin_plans(_: Dict[str, Any] = Depends(admin_identity)) -> Dict[str, Any]:
    return ok("Plans loaded", await db.plans.find({}, {"_id": 0}).sort("display_order", 1).to_list(100))


@api_router.post("/admin/plans")
async def admin_create_plan(payload: PlanInput, _: Dict[str, Any] = Depends(admin_identity)) -> Dict[str, Any]:
    plan = {"id": str(uuid.uuid4()), **payload.model_dump(), "created_at": now_iso(), "updated_at": now_iso()}
    await db.plans.insert_one(dict(plan))
    return ok("Plan created", plan)


@api_router.get("/admin/donations")
async def admin_donations(_: Dict[str, Any] = Depends(admin_identity)) -> Dict[str, Any]:
    return ok("Donations loaded", await db.donations.find({}, {"_id": 0}).sort("created_at", -1).limit(200).to_list(200))


@api_router.get("/admin/subscriptions")
async def admin_subscriptions(_: Dict[str, Any] = Depends(admin_identity)) -> Dict[str, Any]:
    return ok("Subscriptions loaded", await db.subscriptions.find({}, {"_id": 0}).sort("created_at", -1).limit(200).to_list(200))


@api_router.post("/admin/notifications/broadcast")
async def admin_broadcast(payload: BroadcastInput, _: Dict[str, Any] = Depends(admin_identity)) -> Dict[str, Any]:
    users = await db.users.find({"status": "ACTIVE"}, {"_id": 0, "id": 1}).to_list(10000)
    user_ids = [user["id"] for user in users if user.get("id")]
    await record_notifications(user_ids, "ANNOUNCEMENT", payload.title, payload.message)
    push = await send_push(user_ids, payload.title, payload.message, {"type": "ANNOUNCEMENT", "action_url": "/"})
    return ok("Notification broadcast created", {"recipients": len(user_ids), "push": push})


@api_router.get("/admin/settings")
async def admin_settings(_: Dict[str, Any] = Depends(admin_identity)) -> Dict[str, Any]:
    items = await db.system_settings.find({}, {"_id": 0}).to_list(50)
    return ok("Settings loaded", items)


@api_router.put("/admin/settings/contact")
async def admin_contact(payload: Dict[str, Any], _: Dict[str, Any] = Depends(admin_identity)) -> Dict[str, Any]:
    allowed = {key: str(value)[:300] for key, value in payload.items() if key in {"help_number", "emergency_number", "help_email", "whatsapp_number", "support_message", "emergency_instructions"}}
    await db.system_settings.update_one({"key": "contact"}, {"$set": {"key": "contact", **allowed, "updated_at": now_iso()}}, upsert=True)
    return ok("Contact settings saved", allowed)


app.include_router(api_router)


@app.websocket("/api/ws/sos/{sos_id}")
async def sos_websocket(websocket: WebSocket, sos_id: str) -> None:
    """Stream live SOS location updates to responders.

    Auth is JWT-based via `?token=`. The server verifies the token, ensures
    the caller can read the SOS (owner or another user while it is active),
    then pushes JSON payloads whenever the sender updates the location.
    """
    token = websocket.query_params.get("token", "")
    if not token:
        await websocket.close(code=1008)
        return
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
    except jwt.PyJWTError:
        await websocket.close(code=1008)
        return
    role = payload.get("role")
    sub = payload.get("sub")
    if role not in {"user", "admin"} or not sub:
        await websocket.close(code=1008)
        return
    record = await db.sos_alerts.find_one({"id": sos_id}, {"_id": 0})
    if not record:
        await websocket.close(code=1008)
        return
    if role != "admin" and record.get("user_id") != sub and record.get("status") != "ACTIVE":
        await websocket.close(code=1008)
        return
    await websocket.accept()
    await sos_live.register(sos_id, websocket)
    try:
        location = record.get("location") or {}
        await websocket.send_json({
            "type": "snapshot",
            "sos_id": sos_id,
            "status": record.get("status"),
            "latitude": location.get("latitude"),
            "longitude": location.get("longitude"),
            "updated_at": location.get("updated_at") or record.get("created_at"),
        })
        while True:
            # Keep the socket alive; ignore any client-sent frames.
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        await sos_live.unregister(sos_id, websocket)


@app.exception_handler(HTTPException)
async def http_error_handler(_: Request, exc: HTTPException) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"success": False, "message": str(exc.detail), "data": None})


@app.on_event("startup")
async def startup() -> None:
    await db.users.create_index("mobile", unique=True)
    await db.users.create_index([("location", "2dsphere")])
    await db.sos_alerts.create_index("created_at")
    await db.blood_requests.create_index([("location", "2dsphere")])
    await db.otp_sessions.create_index("created_at")
    email = os.getenv("ADMIN_EMAIL", "admin@pahelfoundation.org").lower().strip()
    password = os.getenv("ADMIN_PASSWORD", "PahelAdmin#2026")
    existing = await db.admins.find_one({"email": email}, {"_id": 0})
    if not existing:
        await db.admins.insert_one({"id": str(uuid.uuid4()), "email": email, "password_hash": hash_password(password), "created_at": now_iso()})
    plans = [
        {"id": "plan-monthly-support", "name": "Monthly Supporter", "description": "A simple monthly contribution for local relief work.", "price": 499, "billing_type": "MONTHLY", "benefits": ["Impact updates", "Supporter badge", "Priority help desk"], "eligibility": "Open to every supporter", "active": True, "display_order": 1, "created_at": now_iso(), "updated_at": now_iso()},
        {"id": "plan-yearly-support", "name": "Yearly Champion", "description": "Help PAHEL plan community support all year.", "price": 4999, "billing_type": "YEARLY", "benefits": ["Annual impact report", "Champion badge", "Volunteer orientation"], "eligibility": "18+ recommended", "active": True, "display_order": 2, "created_at": now_iso(), "updated_at": now_iso()},
    ]
    for plan in plans:
        await db.plans.update_one({"id": plan["id"]}, {"$setOnInsert": plan}, upsert=True)
    await db.system_settings.update_one({"key": "contact"}, {"$setOnInsert": {"key": "contact", "help_number": "+91 1800 123 4567", "emergency_number": "112", "help_email": "help@pahelfoundation.org", "whatsapp_number": "", "support_message": "PAHEL FOUNDATION is here to help.", "emergency_instructions": "Share your location and stay in a safe place."}}, upsert=True)
    logger.info("PAHEL FOUNDATION API ready")


@app.on_event("shutdown")
async def shutdown() -> None:
    client.close()