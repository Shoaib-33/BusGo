# backend/main.py
from fastapi import FastAPI, HTTPException, Request, Response, Cookie
from fastapi.middleware.cors import CORSMiddleware
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from typing import List, Optional
from datetime import datetime, timedelta
import json
from pathlib import Path
import uuid
import re
from html import unescape
import hashlib
import hmac
import secrets
import sqlite3

from .database import (
    create_booking, get_all_bookings, get_bookings_by_phone,
    get_booking_by_id, cancel_booking, delete_booking_permanently,
    generate_booking_id, get_booking_statistics, save_chat_message, get_chat_history,
    create_user, get_user_by_phone, get_user_by_login, get_user_by_session_token, create_auth_session,
    delete_auth_session, update_booking_payment, expire_payment_pending_bookings,
    get_all_users
)
from .payment_database import (
    verify_and_deduct, get_payment_for_booking,
    create_refund_request, approve_refund, list_refund_requests
)
from .rag_pipeline import get_answer, get_answer_with_sources, get_llm

app = FastAPI(title="Bus Ticket Booking System")

# Static files & templates
BASE_DIR = Path(__file__).parent.parent
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Load bus data
DATA_FILE = BASE_DIR / "data.json"
with open(DATA_FILE, "r", encoding="utf-8") as f:
    bus_data = json.load(f)

HOLIDAY_FILE = BASE_DIR / "holiday_calendar.json"
holiday_calendar = {"holidays": []}
holiday_calendar_mtime = None


def load_holiday_calendar():
    global holiday_calendar, holiday_calendar_mtime

    if not HOLIDAY_FILE.exists():
        holiday_calendar = {"holidays": []}
        holiday_calendar_mtime = None
        return holiday_calendar

    current_mtime = HOLIDAY_FILE.stat().st_mtime
    if holiday_calendar_mtime != current_mtime:
        with open(HOLIDAY_FILE, "r", encoding="utf-8") as f:
            holiday_calendar = json.load(f)
        holiday_calendar_mtime = current_mtime

    return holiday_calendar


load_holiday_calendar()

routes_data = bus_data.get("routes", [])


def enrich_provider_coverage():
    coverage_by_provider = {provider["name"]: set() for provider in bus_data["bus_providers"]}
    for route in routes_data:
        for schedule in route.get("provider_schedules", []):
            provider = schedule.get("provider")
            if provider in coverage_by_provider:
                coverage_by_provider[provider].update([route.get("from"), route.get("to")])

    for provider in bus_data["bus_providers"]:
        provider.setdefault("coverage_districts", sorted(d for d in coverage_by_provider[provider["name"]] if d))


enrich_provider_coverage()

# ==================== Models ====================

class BookingCreate(BaseModel):
    name: str = Field(..., min_length=2, max_length=100)
    phone: str = Field(..., min_length=11, max_length=15)
    bus_provider: str
    from_district: str
    to_district: str
    dropping_point: str
    travel_date: str
    num_passengers: int = Field(default=1, ge=1, le=10)
    departure_time: Optional[str] = None
    bus_type: Optional[str] = None
    service_details: Optional[str] = None
    selected_seats: Optional[List[str]] = None

class BookingResponse(BaseModel):
    booking_id: str
    name: str
    phone: str
    bus_provider: str
    from_district: str
    to_district: str
    dropping_point: str
    travel_date: str
    num_passengers: int
    fare: int
    total_amount: int
    departure_time: Optional[str] = None
    bus_type: Optional[str] = None
    service_details: Optional[str] = None
    seat_numbers: Optional[str] = None
    payment_status: Optional[str] = None
    payment_method: Optional[str] = None
    payment_transaction_id: Optional[str] = None
    payment_expires_at: Optional[str] = None
    paid_at: Optional[str] = None
    booking_date: str
    status: str

class QueryRequest(BaseModel):
    query: str
    phone: Optional[str] = None
    session_id: Optional[str] = None

class ChatSeatConfirmRequest(BaseModel):
    session_id: str
    selected_seats: List[str]

class SignupRequest(BaseModel):
    name: str = Field(..., min_length=2, max_length=100)
    phone: str = Field(..., min_length=11, max_length=15)
    password: str = Field(..., min_length=6, max_length=128)
    session_id: Optional[str] = None

class LoginRequest(BaseModel):
    phone: str = Field(..., min_length=3, max_length=100)
    password: str = Field(..., min_length=1, max_length=128)

class ChatSignupRequest(BaseModel):
    session_id: str
    password: str = Field(..., min_length=6, max_length=128)

class DemoPaymentRequest(BaseModel):
    booking_id: str
    provider: str
    phone: str = Field(..., min_length=11, max_length=15)
    amount: int = Field(..., ge=1)
    pin: str = Field(..., min_length=4, max_length=8)
    session_id: Optional[str] = None

# ==================== Helper Functions ====================

AUTH_COOKIE_NAME = "busgo_auth"
AUTH_SESSION_DAYS = 14


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        120000
    ).hex()
    return f"pbkdf2_sha256$120000${salt}${digest}"


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        algorithm, iterations, salt, expected = stored_hash.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt.encode("utf-8"),
            int(iterations)
        ).hex()
        return hmac.compare_digest(digest, expected)
    except Exception:
        return False


def public_user(user: Optional[dict]) -> Optional[dict]:
    if not user:
        return None
    return {
        "id": user.get("id"),
        "name": user.get("name"),
        "phone": user.get("phone"),
        "email": user.get("email"),
        "role": user.get("role", "user")
    }


def require_login(auth_token: Optional[str]) -> dict:
    user = get_current_user(auth_token)
    if not user:
        raise HTTPException(status_code=401, detail="Please log in first.")
    return user


def require_admin(auth_token: Optional[str]) -> dict:
    user = require_login(auth_token)
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required.")
    return user


def set_login_cookie(response: Response, user: dict) -> str:
    token = secrets.token_urlsafe(32)
    expires_at = datetime.now() + timedelta(days=AUTH_SESSION_DAYS)
    create_auth_session(token, int(user["id"]), expires_at.isoformat())
    response.set_cookie(
        AUTH_COOKIE_NAME,
        token,
        max_age=AUTH_SESSION_DAYS * 24 * 60 * 60,
        httponly=True,
        samesite="lax",
        path="/"
    )
    return token


def get_current_user(auth_token: Optional[str]) -> Optional[dict]:
    if not auth_token:
        return None
    return get_user_by_session_token(auth_token)


def payment_deadline() -> str:
    return (datetime.now() + timedelta(minutes=5)).isoformat()


def build_payment_payload(booking: dict) -> dict:
    return {
        "booking_id": booking.get("booking_id"),
        "amount": booking.get("total_amount"),
        "expires_at": booking.get("payment_expires_at"),
        "methods": ["bkash", "nagad"],
        "demo_note": "Demo payment only. No real money will be charged.",
    }


REFUND_REQUEST_MESSAGE_BN = "Apnar refund request admin panel e dewa hoyeche. Apni 48 hrs er moddhe refund peye jaben."


def create_refund_for_paid_booking(booking: Optional[dict]) -> Optional[dict]:
    if not booking or booking.get("payment_status") != "paid":
        return None
    payment = get_payment_for_booking(booking["booking_id"])
    return create_refund_request(booking["booking_id"], int(booking["total_amount"]), payment)


def cancellation_message_for_booking(booking: dict, conversation_text: str = "") -> str:
    booking_id = booking.get("booking_id")
    if booking.get("payment_status") == "paid":
        create_refund_for_paid_booking(booking)
        return f"Booking {booking_id} cancel kora hoyeche. {REFUND_REQUEST_MESSAGE_BN}"
    if is_banglish(conversation_text):
        return f"Booking {booking_id} cancel kora hoyeche."
    return f"Booking {booking_id} has been cancelled successfully."


def is_refund_followup(text: str) -> bool:
    lowered = (text or "").lower()
    markers = [
        "refund", "money back", "taka ferot", "ferot", "pabo kivabe",
        "pabo kibhabe", "kivabe pabo", "kibhabe pabo", "tk pabo", "taka pabo"
    ]
    return any(marker in lowered for marker in markers)


def refund_followup_message(booking: dict, conversation_text: str = "") -> str:
    booking_id = booking.get("booking_id")
    if booking.get("payment_status") == "paid":
        create_refund_for_paid_booking(booking)
        return f"Sir, {REFUND_REQUEST_MESSAGE_BN} Booking ID: {booking_id}."
    if is_banglish(conversation_text):
        return f"Sir, ei booking-er payment complete hoyni, tai refund-er moto kono taka deduct hoyni. Booking ID {booking_id} cancel hoye geche."
    return f"Sir, this booking was not paid, so no money was deducted and no refund is needed. Booking ID {booking_id} is cancelled."


def account_offer_payload(booking: dict) -> dict:
    return {
        "name": booking.get("name"),
        "phone": booking.get("phone"),
        "booking_id": booking.get("booking_id")
    }


def chat_account_signup_payload(session: dict) -> Optional[dict]:
    offer = session.get("pending_account_offer")
    if not offer:
        return None
    return {
        "name": offer.get("name"),
        "phone": offer.get("phone"),
        "booking_id": offer.get("booking_id")
    }


def wants_account_signup(text: str) -> bool:
    lowered = text.lower().strip()
    positive_markers = [
        "yes", "ok", "okay", "sure", "create", "signup", "sign up", "account",
        "khul", "khulte", "chai", "korun", "korte chai", "hmm", "ha", "haan"
    ]
    negative_markers = ["no", "na", "lagbe na", "dorkar nai", "not now", "pore"]
    if any(marker in lowered for marker in negative_markers):
        return False
    return any(marker in lowered for marker in positive_markers)


def rejects_account_signup(text: str) -> bool:
    lowered = text.lower().strip()
    return any(marker in lowered for marker in ["no", "na", "lagbe na", "dorkar nai", "not now", "pore"])

def clean_pdf_text(value) -> str:
    if value is None:
        return "-"
    text = unescape(str(value)).replace("\n", " ").replace("\r", " ").strip()
    return text.encode("latin-1", "replace").decode("latin-1")


def pdf_escape(value) -> str:
    return clean_pdf_text(value).replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def format_ticket_date(value: Optional[str]) -> str:
    if not value:
        return "-"
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).strftime("%d %b %Y")
    except Exception:
        return clean_pdf_text(value)


def format_bus_type_label(value: Optional[str]) -> str:
    normalized = normalize_bus_type(value)
    if normalized == "ac":
        return "AC"
    if normalized == "non_ac":
        return "Non-AC"
    return clean_pdf_text(value or "-")


def parse_service_details(value) -> dict:
    if not value:
        return {}
    if isinstance(value, dict):
        return value
    try:
        return json.loads(value)
    except Exception:
        return {}


SEAT_ROWS = 10
SEAT_LETTERS = ["A", "B", "C", "D"]
WINDOW_SEAT_LETTERS = {"A", "D"}
FEMALE_RESERVED_SEATS = {"A1", "B1", "C1", "D1"}


def normalize_departure_time(value: Optional[str]) -> str:
    return (value or "").strip()


def all_seat_labels() -> List[str]:
    return [f"{letter}{row}" for row in range(1, SEAT_ROWS + 1) for letter in SEAT_LETTERS]


def normalize_seat_label(value: str) -> Optional[str]:
    text = str(value or "").strip().upper().replace(" ", "")
    match = re.fullmatch(r"([ABCD])(\d{1,2})", text)
    if not match:
        return None
    label = f"{match.group(1)}{int(match.group(2))}"
    return label if label in all_seat_labels() else None


def parse_seat_numbers(value) -> List[str]:
    if not value:
        return []
    if isinstance(value, list):
        raw_values = value
    else:
        raw_values = re.split(r"[,;\s]+", str(value))
    seats = []
    for item in raw_values:
        label = normalize_seat_label(item)
        if label and label not in seats:
            seats.append(label)
    return seats


def service_matches_booking(booking: dict, provider: str, from_district: str, to_district: str, travel_date: str, bus_type: Optional[str], departure_time: Optional[str]) -> bool:
    status = str(booking.get("status", "")).lower()
    if status == "payment_pending":
        expires_at = booking.get("payment_expires_at")
        if expires_at:
            try:
                if datetime.fromisoformat(expires_at) < datetime.now():
                    return False
            except Exception:
                return False
    return (
        status in {"active", "payment_pending"}
        and str(booking.get("bus_provider", "")).lower() == provider.lower()
        and str(booking.get("from_district", "")).lower() == from_district.lower()
        and str(booking.get("to_district", "")).lower() == to_district.lower()
        and str(booking.get("travel_date", "")) == travel_date
        and normalize_bus_type(booking.get("bus_type")) == normalize_bus_type(bus_type)
        and normalize_departure_time(booking.get("departure_time")) == normalize_departure_time(departure_time)
    )


def get_booked_seats(provider: str, from_district: str, to_district: str, travel_date: str, bus_type: Optional[str], departure_time: Optional[str]) -> List[str]:
    expire_payment_pending_bookings()
    seats = []
    for booking in get_all_bookings():
        if service_matches_booking(booking, provider, from_district, to_district, travel_date, bus_type, departure_time):
            for seat in parse_seat_numbers(booking.get("seat_numbers")):
                if seat not in seats:
                    seats.append(seat)
    return seats


def build_seat_layout(booked_seats: List[str]):
    booked = set(booked_seats)
    rows = []
    for row in range(1, SEAT_ROWS + 1):
        row_seats = []
        for letter in SEAT_LETTERS:
            label = f"{letter}{row}"
            row_seats.append({
                "label": label,
                "row": row,
                "column": letter,
                "status": "booked" if label in booked else "available",
                "is_window": letter in WINDOW_SEAT_LETTERS,
                "is_female_reserved": label in FEMALE_RESERVED_SEATS,
            })
        rows.append(row_seats)
    return {
        "capacity": SEAT_ROWS * len(SEAT_LETTERS),
        "rows": rows,
        "booked_seats": booked_seats,
        "available_count": (SEAT_ROWS * len(SEAT_LETTERS)) - len(booked),
    }


def assign_seats(num_passengers: int, booked_seats: List[str], selected_seats: Optional[List[str]] = None):
    booked = set(booked_seats)
    if selected_seats:
        normalized = []
        for seat in selected_seats:
            label = normalize_seat_label(seat)
            if not label:
                return None, f"Seat '{seat}' is not valid."
            if label in normalized:
                return None, "Duplicate seat selection is not allowed."
            normalized.append(label)
        if len(normalized) != num_passengers:
            return None, f"Please select exactly {num_passengers} seat(s)."
        unavailable = [seat for seat in normalized if seat in booked]
        if unavailable:
            return None, f"Seat(s) already booked: {', '.join(unavailable)}."
        return normalized, None

    available = [seat for seat in all_seat_labels() if seat not in booked]
    if len(available) < num_passengers:
        return None, f"Only {len(available)} seat(s) are available for this service."
    return available[:num_passengers], None


def build_ticket_pdf(booking: dict) -> bytes:
    width, height = 595, 842
    content = []

    def rect(x, y, w, h, stroke="0.82 0.86 0.90", fill=None):
        if fill:
            content.append(f"{fill} rg {x} {y} {w} {h} re f")
        if stroke:
            content.append(f"{stroke} RG {x} {y} {w} {h} re S")

    def line(x1, y1, x2, y2, color="0.82 0.86 0.90"):
        content.append(f"{color} RG {x1} {y1} m {x2} {y2} l S")

    def text(x, y, value, size=11, color="0.12 0.16 0.22", font="F1"):
        content.append(f"BT {color} rg /{font} {size} Tf {x} {y} Td ({pdf_escape(value)}) Tj ET")

    def label_value(label, value, x, y, w=225):
        text(x, y + 15, label.upper(), 7.5, "0.42 0.48 0.56", "F2")
        text(x, y, value, 11, "0.10 0.13 0.18", "F1")
        line(x, y - 8, x + w, y - 8, "0.90 0.92 0.95")

    service_details = parse_service_details(booking.get("service_details"))
    holiday_context = service_details.get("holiday_context") or {}
    base_fare = service_details.get("base_fare")
    status = clean_pdf_text(booking.get("status", "active")).upper()

    rect(0, height - 118, width, 118, stroke=None, fill="0.08 0.13 0.20")
    text(48, height - 62, "BusGo", 27, "1 1 1", "F2")
    text(48, height - 85, "Printable Bus Ticket", 12, "0.74 0.82 0.92", "F1")
    text(430, height - 55, "BOOKING ID", 8, "0.74 0.82 0.92", "F2")
    text(430, height - 76, booking.get("booking_id"), 17, "1 1 1", "F2")

    rect(48, height - 158, 499, 36, stroke="0.22 0.28 0.36", fill="0.96 0.98 1")
    text(66, height - 143, f"Status: {status}", 12, "0.08 0.13 0.20", "F2")
    text(390, height - 143, f"Issued: {format_ticket_date(booking.get('booking_date'))}", 10, "0.38 0.44 0.52", "F1")

    rect(48, 165, 499, 510, stroke="0.82 0.86 0.90", fill="1 1 1")

    text(72, 633, "Passenger Details", 14, "0.08 0.13 0.20", "F2")
    label_value("Passenger Name", booking.get("name"), 72, 598)
    label_value("Phone Number", booking.get("phone"), 322, 598, 175)
    label_value("Passenger Count", f"{booking.get('num_passengers', '-')} passenger(s)", 72, 552)
    label_value("Seat Number(s)", booking.get("seat_numbers") or "-", 322, 552, 175)

    text(72, 494, "Journey Details", 14, "0.08 0.13 0.20", "F2")
    label_value("Bus Provider", booking.get("bus_provider"), 72, 459)
    label_value("Travel Date", format_ticket_date(booking.get("travel_date")), 322, 459, 175)
    label_value("Route", f"{booking.get('from_district')} to {booking.get('to_district')}", 72, 413)
    label_value("Dropping Point", booking.get("dropping_point"), 322, 413, 175)
    label_value("Bus Type", format_bus_type_label(booking.get("bus_type")), 72, 367)
    label_value("Departure Time", booking.get("departure_time") or "-", 322, 367, 175)

    text(72, 309, "Fare Summary", 14, "0.08 0.13 0.20", "F2")
    label_value("Fare Per Passenger", f"{booking.get('fare', 0)} Taka", 72, 274)
    label_value("Total Fare", f"{booking.get('total_amount', 0)} Taka", 322, 274, 175)
    if booking.get("payment_status"):
        label_value("Payment Status", str(booking.get("payment_status")).title(), 72, 228)
    if booking.get("payment_transaction_id"):
        label_value("Transaction ID", booking.get("payment_transaction_id"), 322, 228, 175)
    if base_fare and int(base_fare) != int(booking.get("fare") or 0):
        label_value("Base Fare", f"{base_fare} Taka", 72, 182)
    if holiday_context:
        holiday_name = holiday_context.get("name", "holiday calendar")
        surcharge = holiday_context.get("surcharge_percent")
        note = f"Includes {surcharge}% holiday fare adjustment for {holiday_name}." if surcharge else f"Includes holiday fare adjustment for {holiday_name}."
        text(72, 206, note, 9.5, "0.50 0.30 0.05", "F1")

    rect(48, 80, 499, 58, stroke="0.82 0.86 0.90", fill="0.97 0.98 0.99")
    if booking.get("status") == "cancelled":
        text(72, 113, "This ticket has been cancelled and is not valid for travel.", 10, "0.70 0.12 0.12", "F2")
    else:
        text(72, 113, "Please carry this ticket or the booking ID during travel.", 10, "0.18 0.24 0.31", "F1")
    text(72, 94, "For lookup or cancellation, use the booking ID with the passenger phone number.", 9, "0.42 0.48 0.56", "F1")

    stream = "\n".join(content).encode("latin-1", "replace")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {width} {height}] /Resources << /Font << /F1 4 0 R /F2 5 0 R >> >> /Contents 6 0 R >>".encode("ascii"),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>",
        b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\nstream\n" + stream + b"\nendstream",
    ]

    pdf = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, obj in enumerate(objects, start=1):
        offsets.append(len(pdf))
        pdf.extend(f"{index} 0 obj\n".encode("ascii"))
        pdf.extend(obj)
        pdf.extend(b"\nendobj\n")
    xref = len(pdf)
    pdf.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    pdf.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        pdf.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    pdf.extend(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode("ascii"))
    return bytes(pdf)

def normalize_bus_type(bus_type: Optional[str]) -> Optional[str]:
    if not bus_type:
        return None
    text = str(bus_type).lower().replace("-", " ").replace("_", " ")
    if "non" in text and "ac" in text:
        return "non_ac"
    if "ac" in text:
        return "ac"
    return text.strip() or None

def get_point_price(point: dict, bus_type: Optional[str] = None) -> int:
    normalized_type = normalize_bus_type(bus_type)
    if normalized_type == "ac":
        for key in ["ac_price", "price_ac", "ac_fare", "fare_ac"]:
            if key in point:
                return int(point[key])
    if normalized_type == "non_ac":
        for key in ["non_ac_price", "nonac_price", "price_non_ac", "non_ac_fare", "fare_non_ac"]:
            if key in point:
                return int(point[key])
    return int(point.get("price", point.get("fare", 0)) or 0)

def parse_date(date_text: Optional[str]):
    if not date_text:
        return None
    try:
        return datetime.strptime(date_text, "%Y-%m-%d").date()
    except ValueError:
        return None

def get_holiday_for_date(date_text: Optional[str], provider: Optional[str] = None):
    travel_date = parse_date(date_text)
    if not travel_date:
        return None

    calendar = load_holiday_calendar()
    for holiday in calendar.get("holidays", []):
        start = parse_date(holiday.get("start_date") or holiday.get("date"))
        end = parse_date(holiday.get("end_date") or holiday.get("date"))
        if not start or not end or not (start <= travel_date <= end):
            continue

        providers = holiday.get("providers") or holiday.get("providers_with_package") or []
        if provider and providers and provider.lower() not in {p.lower() for p in providers}:
            continue

        return holiday
    return None

def apply_holiday_fare(base_fare: int, travel_date: Optional[str], provider: Optional[str] = None):
    holiday = get_holiday_for_date(travel_date, provider)
    if not holiday:
        return base_fare, None

    surcharge_percent = (
        holiday.get("surcharge_percent")
        or holiday.get("fare_surcharge_percent")
        or holiday.get("extra_charge_percent")
    )
    multiplier = holiday.get("fare_multiplier")

    adjusted_fare = base_fare
    if surcharge_percent is not None:
        adjusted_fare = round(base_fare * (1 + float(surcharge_percent) / 100))
    elif multiplier is not None:
        adjusted_fare = round(base_fare * float(multiplier))

    return adjusted_fare, {
        "name": holiday.get("name", "Holiday"),
        "type": holiday.get("type"),
        "start_date": holiday.get("start_date") or holiday.get("date"),
        "end_date": holiday.get("end_date") or holiday.get("date"),
        "base_fare": base_fare,
        "adjusted_fare": adjusted_fare,
        "surcharge_percent": surcharge_percent,
        "fare_multiplier": multiplier,
        "note": holiday.get("note")
    }

def get_fare(district: str, dropping_point: str, bus_type: Optional[str] = None) -> int:
    for dist in bus_data["districts"]:
        if dist["name"].lower() == district.lower():
            for dp in dist["dropping_points"]:
                if dp["name"].lower() == dropping_point.lower():
                    return get_point_price(dp, bus_type)
    return 0

def provider_matches(candidate: str, requested: str) -> bool:
    candidate_lower = candidate.lower()
    requested_lower = requested.lower()
    return (
        candidate_lower == requested_lower
        or candidate_lower.startswith(requested_lower)
        or requested_lower in candidate_lower
    )


def get_provider_profile(provider_name: Optional[str]) -> dict:
    if not provider_name:
        return {}
    for provider in bus_data.get("bus_providers", []):
        if provider.get("name", "").lower() == str(provider_name).lower():
            return provider
    return {}


def bus_type_quality_score(bus_type: Optional[str]) -> int:
    normalized = normalize_bus_type(bus_type)
    if normalized == "Sleeper":
        return 5
    if normalized == "AC Volvo":
        return 4
    if normalized == "AC":
        return 3
    if normalized == "Non-AC":
        return 1
    return 0


def detect_service_preference(text: str) -> Optional[str]:
    text_lower = (text or "").lower()
    quality_markers = [
        "best quality", "best", "quality", "bhalo", "valo", "ভালো",
        "premium", "luxury", "comfortable", "comfort", "aram", "aramdayok",
        "shera", "sera", "top", "high rating", "rating"
    ]
    cheap_markers = [
        "cheap", "cheapest", "kom dam", "kom dame", "kom vara", "kom bhara",
        "low fare", "lowest fare", "budget", "shobcheye kom"
    ]
    if any(marker in text_lower for marker in quality_markers):
        return "quality"
    if any(marker in text_lower for marker in cheap_markers):
        return "cheap"
    return None


def detect_service_preference_with_llm(text: str) -> Optional[str]:
    if not text or not text.strip():
        return None

    prompt = f"""
Classify whether the user's latest bus-service preference is about quality/comfort or low price.

User message:
{text}

Return ONLY valid JSON:
{{"service_preference": "quality" | "cheap" | null}}

Rules:
- Understand English, Banglish, romanized Bangla, spelling variations, and typos.
- Use "quality" when the user wants a better, good, comfortable, premium, or higher-quality bus/service.
- Use "cheap" when the user wants lower fare, budget, cheapest, or lowest price.
- Return null when there is no clear quality or cheap preference.
"""
    try:
        response = get_llm().invoke(prompt)
        content = getattr(response, "content", str(response)).strip()
        content = re.sub(r"^```(?:json)?|```$", "", content, flags=re.IGNORECASE).strip()
        preference = json.loads(content).get("service_preference")
        if preference in {"quality", "cheap"}:
            return preference
    except Exception:
        return None
    return None


def parse_departure_time_choice(text: str, available_times: List[str]) -> Optional[str]:
    text_lower = (text or "").lower()
    normalized_available = {normalize_departure_time(time): time for time in available_times or []}

    for normalized, original in normalized_available.items():
        if normalized.lower() in text_lower or normalized.lstrip("0").lower() in text_lower:
            return original

    match = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", text_lower)
    if not match:
        return None

    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    period = match.group(3)
    candidates = []
    if period:
        if period == "pm" and hour < 12:
            hour += 12
        if period == "am" and hour == 12:
            hour = 0
        candidates.append(f"{hour:02d}:{minute:02d}")
    else:
        candidates.append(f"{hour:02d}:{minute:02d}")
        if hour <= 12:
            candidates.append(f"{hour + 12:02d}:{minute:02d}")

    for candidate in candidates:
        if candidate in normalized_available:
            return normalized_available[candidate]
    return None


def resolve_departure_time_with_llm(user_text: str, available_times: List[str], conversation_text: str) -> Optional[str]:
    if not available_times:
        return None

    prompt = f"""
You are BusGo's departure-time selector.

The user is choosing a bus departure time from this exact list:
{json.dumps(available_times, ensure_ascii=True)}

Conversation:
{conversation_text}

Latest user message:
{user_text}

Return ONLY valid JSON:
{{"selected_time": "HH:MM or null"}}

Rules:
- Select a time only from the provided list.
- Use natural language understanding for English, Banglish, and Bangla transliteration.
- Infer whether the latest user message selects one of the available times by exact time, approximate time, or time-of-day meaning.
- If the user asks a question about available times but does not choose one, return null.
- If unclear or multiple times match equally, return null.
- Do not invent a time.
"""
    try:
        response = get_llm().invoke(prompt)
        content = getattr(response, "content", str(response)).strip()
        content = re.sub(r"^```(?:json)?|```$", "", content, flags=re.IGNORECASE).strip()
        selected = json.loads(content).get("selected_time")
        if selected in available_times:
            return selected
        normalized_map = {normalize_departure_time(time): time for time in available_times}
        if selected and normalize_departure_time(selected) in normalized_map:
            return normalized_map[normalize_departure_time(selected)]
    except Exception:
        return None
    return None


def generate_departure_time_selection_response(
    draft: dict,
    user_text: str,
    available_times: List[str],
    conversation_text: str
) -> str:
    context = get_booking_context(draft)
    context["available_departure_times"] = available_times or []
    route_context = get_route_info_context(draft, user_text)

    prompt = f"""
You are BusGo's Bangladeshi bus-ticket assistant.

The user is currently choosing a departure time for this service:
{json.dumps(context, ensure_ascii=True)}

Full route context for answering follow-up questions:
{json.dumps(route_context, ensure_ascii=True)}

Conversation:
{conversation_text}

Latest user message:
{user_text}

Write the assistant's next reply.

Instructions:
- Match the user's language and style. If the user writes Banglish, reply in natural Banglish.
- If the user writes Banglish/romanized Bangla with Latin letters, reply in Latin-letter Banglish, not Bangla script.
- Keep a professional customer-service tone. Never call the user "bhai", "vai", "bro", or similar casual labels.
- Use "Sir" or "Madam" naturally. In Banglish, prefer phrases like "Sir, ..." or "Madam, ..." instead of casual address.
- Use only the service context and full route context above.
- If the user asks which bus/provider/type the shown times belong to, answer using the current service context.
- If the user asks whether another bus type/provider is available, answer from the full route context and list matching options when present.
- If the user asks about a time-of-day or schedule detail, answer from the available departure times and service details.
- If the user is still choosing the current service, ask them to choose or confirm one available departure time.
- Do NOT ask for name, phone, travel date, or passenger count yet.
- Do NOT say the ticket is booked or confirmed.
- Return only the assistant message.
"""
    try:
        response = get_llm().invoke(prompt)
        return getattr(response, "content", str(response)).strip()
    except Exception:
        times = ", ".join(available_times or [])
        provider = context.get("bus_provider") or context.get("provider") or "-"
        bus_type = context.get("bus_type") or "-"
        return (
            f"Sir, ei time gula {provider} {bus_type} service-er jonno: {times}. "
            "Ei time gula theke ekta select korun."
        )


def get_route_info_context(draft: dict, user_text: str = "") -> dict:
    context = get_booking_context(draft)
    from_district = draft.get("from_district") or context.get("from_district")
    to_district = draft.get("to_district") or context.get("to_district")
    requested_type = normalize_bus_type(detect_bus_type(user_text) or draft.get("bus_type"))

    options = []
    selected_options = []
    selected_matching_options = []
    if from_district and to_district:
        options = get_route_options(from_district, to_district)
        if draft.get("bus_provider"):
            selected_options = get_route_options(from_district, to_district, draft.get("bus_provider"))
        if requested_type:
            typed_options = [
                option for option in options
                if normalize_bus_type(option.get("bus_type")) == requested_type
            ]
            if typed_options:
                options = typed_options
            selected_typed_options = [
                option for option in selected_options
                if normalize_bus_type(option.get("bus_type")) == requested_type
            ]
            selected_matching_options = selected_typed_options
        else:
            selected_matching_options = selected_options

    return {
        "from_district": from_district,
        "to_district": to_district,
        "selected_provider": draft.get("bus_provider"),
        "requested_bus_type": requested_type,
        "service_preference": draft.get("service_preference"),
        "selected_provider_all_options": selected_options,
        "selected_provider_matching_options": selected_matching_options,
        "options": options
    }


def generate_route_options_info_message(draft: dict, user_question: str, conversation_text: str) -> str:
    context = get_route_info_context(draft, user_question)

    prompt = f"""
You are BusGo's Bangladeshi route-information assistant.

The user asked:
{user_question}

Structured route options:
{json.dumps(context, ensure_ascii=True)}

Conversation:
{conversation_text}

Write the assistant response.

Instructions:
- Match the user's language and style. If the user writes Banglish, reply in natural Banglish.
- If the user writes Banglish/romanized Bangla with Latin letters, reply in Latin-letter Banglish, not Bangla script.
- Keep a professional customer-service tone. Never call the user "bhai", "vai", "bro", or similar casual labels.
- Use only the structured route options above; do not invent providers, fares, or times.
- If the user asks which buses/services are available, list every matching item from options with provider, bus type, fare, and all departure times.
- If the user asks about the already selected/current provider, answer from selected_provider_matching_options when available; otherwise use selected_provider_all_options to explain what that provider offers.
- If selected_provider_matching_options is empty for the requested_bus_type but options has matches from other providers, clearly say the selected provider does not offer that requested type on this route, then list the matching alternatives from options.
- If requested_bus_type is present, answer for that bus type only.
- If service_preference is quality, mention which listed option is better quality/recommended based on rating/reviews/amenities from the options.
- If the user only asked for information, do not ask for name, phone, travel date, or passenger count.
- You may ask whether they want to choose one only after giving the complete list.
- Return only the assistant message.
"""
    try:
        response = get_llm().invoke(prompt)
        return getattr(response, "content", str(response)).strip()
    except Exception:
        options = context.get("options") or []
        if not options:
            return "Sorry, ei route-er jonno matching bus service khuje pelam na."
        lines = []
        for option in options:
            lines.append(
                f"{option['provider']} {option['bus_type']}: fare {option['fare']} Taka, "
                f"times {', '.join(option.get('departure_times') or [])}"
            )
        return "\n".join(lines)


def is_affirmative(text: str) -> bool:
    text_lower = (text or "").lower().strip()
    markers = [
        "yes", "yep", "yeah", "ok", "okay", "confirm", "book", "booking",
        "koro", "koren", "korun", "hobe", "chai", "nibo", "niben", "yes confirm"
    ]
    return any(marker in text_lower for marker in markers)


def is_negative(text: str) -> bool:
    text_lower = (text or "").lower().strip()
    markers = ["no", "na", "nah", "cancel", "lagbe na", "korbo na", "chai na"]
    return any(marker in text_lower for marker in markers)

def find_route(from_district: str, to_district: str) -> Optional[dict]:
    for route in routes_data:
        if route.get("from", "").lower() == from_district.lower() and route.get("to", "").lower() == to_district.lower():
            return route
    return None

def get_route_options(from_district: str, to_district: str, provider: Optional[str] = None):
    route = find_route(from_district, to_district)
    if not route:
        return []

    options = []
    for schedule in route.get("provider_schedules", []):
        schedule_provider = schedule.get("provider")
        if provider and not provider_matches(schedule_provider, provider):
            continue
        for service in schedule.get("services", []):
            options.append({
                "provider": schedule_provider,
                "bus_type": service.get("bus_type"),
                "fare": int(service.get("fare", 0) or 0),
                "departure_times": service.get("departure_times", []),
                "distance_km": route.get("distance_km"),
                "avg_duration_hours": route.get("avg_duration_hours"),
                "rating": float(get_provider_profile(schedule_provider).get("rating") or 0),
                "total_reviews": int(get_provider_profile(schedule_provider).get("total_reviews") or 0),
                "amenities_count": len(get_provider_profile(schedule_provider).get("amenities") or []),
            })
    return options

def choose_route_option(draft: dict):
    if not draft.get("from_district") or not draft.get("to_district"):
        return None
    options = get_route_options(
        draft["from_district"],
        draft["to_district"],
        draft.get("bus_provider")
    )
    if not options:
        return None

    requested_type = normalize_bus_type(draft.get("bus_type"))
    if requested_type:
        typed_options = [
            option for option in options
            if normalize_bus_type(option.get("bus_type")) == requested_type
        ]
        if typed_options:
            options = typed_options

    if draft.get("service_preference") == "quality":
        return max(
            options,
            key=lambda option: (
                option.get("rating", 0),
                option.get("amenities_count", 0),
                bus_type_quality_score(option.get("bus_type")),
                option.get("total_reviews", 0),
                -option["fare"],
            )
        )

    return min(options, key=lambda option: option["fare"])


def apply_preferred_route_option(draft: dict):
    if draft.get("service_preference") not in {"cheap", "quality"}:
        return draft
    if not draft.get("from_district") or not draft.get("to_district"):
        return draft

    selector = draft.copy()
    if not draft.get("provider_explicit"):
        selector["bus_provider"] = None

    option = choose_route_option(selector)
    if not option:
        return draft

    draft["bus_provider"] = option["provider"]
    draft["bus_type"] = option["bus_type"]
    draft["fare"] = option["fare"]
    return draft

def validate_route(provider: str, from_district: str, to_district: str) -> bool:
    return bool(get_route_options(from_district, to_district, provider))

def get_available_providers(from_district: str, to_district: str) -> List[str]:
    return sorted({option["provider"] for option in get_route_options(from_district, to_district)})

def get_dropping_points_by_district(district: str):
    for d in bus_data["districts"]:
        if d["name"].lower() == district.lower():
            points = []
            for dp in d["dropping_points"]:
                point = dp.copy()
                point["price"] = get_point_price(dp)
                points.append(point)
            return points
    return []

def get_district_names() -> List[str]:
    return [district["name"] for district in bus_data["districts"]]

def normalize_phone(phone: str) -> str:
    phone = phone.strip().replace(" ", "").replace("-", "")
    if phone.startswith("+88"):
        phone = phone[3:]
    return phone

def detect_phone(text: str) -> Optional[str]:
    compact = re.sub(r"[\s-]+", "", text)
    match = re.search(r"(\+?88)?01\d{9}", compact)
    return normalize_phone(match.group(0)) if match else None

def is_banglish(text: str) -> bool:
    text_lower = text.lower()
    markers = [
        "amar", "ami", "apni", "apnar", "theke", "e ", "er ", "jawar",
        "jabo", "jawa", "lagbe", "bhara", "vara", "koto", "kobe",
        "ticket lagbe", "ticket katbo", "dao", "den", "kivabe", "kibhabe",
        "pabo", "ferot", "krte", "korte", "hobe"
    ]
    return any(marker in text_lower for marker in markers)


def ensure_departure_time_visible(message: str, data: dict, conversation_text: str = "") -> str:
    departure_time = data.get("departure_time")
    if not departure_time:
        return message

    normalized_time = normalize_departure_time(departure_time)
    message_text = message or ""
    message_lower = message_text.lower()
    time_variants = {
        str(departure_time).lower(),
        normalized_time.lower(),
        normalized_time.lstrip("0").lower(),
    }
    if any(variant and variant in message_lower for variant in time_variants):
        return message_text

    provider = data.get("bus_provider") or data.get("provider") or "Bus"
    bus_type = data.get("bus_type") or ""
    from_district = data.get("from_district") or ""
    to_district = data.get("to_district") or ""
    travel_date = data.get("travel_date") or ""

    service_name = " ".join(part for part in [provider, bus_type] if part).strip()
    route = f"{from_district} to {to_district}".strip(" to")
    pieces = [piece for piece in [service_name, route, travel_date] if piece]
    prefix = "Service time"
    if is_banglish(conversation_text):
        line = f"{prefix}: {', '.join(pieces)} - {normalized_time}."
    else:
        line = f"{prefix}: {', '.join(pieces)} - {normalized_time}."

    if not message_text.strip():
        return line
    return message_text.rstrip() + "\n" + line

def detect_provider(text: str) -> Optional[str]:
    text_lower = text.lower()
    for provider in bus_data["bus_providers"]:
        provider_lower = provider["name"].lower()
        significant_words = [word for word in re.split(r"\W+", provider_lower) if len(word) > 2]
        if provider_lower in text_lower or any(re.search(rf"\b{re.escape(word)}\b", text_lower) for word in significant_words):
            return provider["name"]
    return None

def detect_route(text: str):
    text_lower = text.lower()
    districts = get_district_names()

    for source in districts:
        for destination in districts:
            if source.lower() == destination.lower():
                continue
            patterns = [
                rf"\bfrom\s+{re.escape(source.lower())}\s+to\s+{re.escape(destination.lower())}\b",
                rf"\b{re.escape(source.lower())}\s+to\s+{re.escape(destination.lower())}\b",
                rf"\bbetween\s+{re.escape(source.lower())}\s+and\s+{re.escape(destination.lower())}\b",
            ]
            if any(re.search(pattern, text_lower) for pattern in patterns):
                return source, destination

    return None, None

def detect_dropping_point(text: str, destination: Optional[str] = None):
    text_lower = text.lower()
    districts = [d for d in bus_data["districts"] if not destination or d["name"].lower() == destination.lower()]

    for district in districts:
        for point in district["dropping_points"]:
            if point["name"].lower() in text_lower:
                return point["name"]

    return None

def detect_passenger_count(text: str) -> Optional[int]:
    match = re.search(r"\b(\d{1,2})\s*(passengers?|people|persons?|seats?|tickets?)\b", text.lower())
    if not match:
        match = re.search(r"\b(\d{1,2})\s*(jon|জন|ta|ti)\b", text.lower())
    if match:
        count = int(match.group(1))
        if 1 <= count <= 10:
            return count
    return None

def detect_bus_type(text: str) -> Optional[str]:
    text_lower = text.lower()
    if re.search(r"\bnon[-\s]?ac\b", text_lower):
        return "Non-AC"
    if re.search(r"\bac\s+volvo\b", text_lower):
        return "AC Volvo"
    if re.search(r"\bsleeper\b", text_lower):
        return "Sleeper"
    if re.search(r"\bac\b", text_lower):
        return "AC"
    return None

def preserve_service_constraints(draft: dict, previous_draft: dict, latest_text: str):
    latest_type = detect_bus_type(latest_text)
    if latest_type:
        draft["bus_type"] = latest_type
    elif previous_draft.get("bus_type"):
        draft["bus_type"] = previous_draft["bus_type"]

    if previous_draft.get("departure_time") and not re.search(r"\b\d{1,2}:\d{2}\b", latest_text):
        draft["departure_time"] = previous_draft["departure_time"]

    return draft

def detect_travel_date(text: str) -> Optional[str]:
    text_lower = text.lower()
    if "tomorrow" in text_lower:
        return (datetime.now() + timedelta(days=1)).date().isoformat()
    if "today" in text_lower:
        return datetime.now().date().isoformat()

    month_names = {
        "jan": 1, "january": 1,
        "feb": 2, "february": 2,
        "mar": 3, "march": 3,
        "apr": 4, "april": 4,
        "may": 5,
        "jun": 6, "june": 6,
        "jul": 7, "july": 7,
        "aug": 8, "august": 8,
        "sep": 9, "sept": 9, "september": 9,
        "oct": 10, "october": 10,
        "nov": 11, "november": 11,
        "dec": 12, "december": 12,
    }
    month_match = re.search(
        r"\b(\d{1,2})\s+(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t|tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\s+(\d{2,4})\b",
        text_lower
    )
    if month_match:
        day = int(month_match.group(1))
        month = month_names[month_match.group(2)]
        year = int(month_match.group(3))
        if year < 100:
            year += 2000
        try:
            return datetime(year, month, day).date().isoformat()
        except ValueError:
            return None

    for pattern, fmt in [
        (r"\b(\d{4}-\d{2}-\d{2})\b", "%Y-%m-%d"),
        (r"\b(\d{1,2}/\d{1,2}/\d{4})\b", "%d/%m/%Y"),
    ]:
        match = re.search(pattern, text)
        if match:
            try:
                return datetime.strptime(match.group(1), fmt).date().isoformat()
            except ValueError:
                return None

    return None

def detect_name(text: str) -> Optional[str]:
    match = re.search(r"\b(?:my name is|name is|i am|i'm|nam|naam|name hocche|name hocce)\s+([A-Za-z][A-Za-z .'-]{1,80})", text, re.IGNORECASE)
    if not match:
        return None
    name = match.group(1).strip(" .")
    name = re.split(r"\b(?:phone|mobile|date|passenger|from|to|jabo|jon|ticket)\b", name, flags=re.IGNORECASE)[0].strip(" .")
    return name if is_valid_passenger_name(name) else None

def is_valid_passenger_name(value: str) -> bool:
    if not value:
        return False
    text = str(value).strip(" .,-")
    text_lower = text.lower()
    if not text or len(text) > 80:
        return False
    if re.search(r"\d", text):
        return False
    blocked_words = {
        "ami", "amar", "apni", "apnar", "jabo", "jaben", "jawa", "ticket",
        "phone", "mobile", "number", "jon", "passenger", "seat", "seats",
        "june", "july", "may", "october", "november", "december", "month"
    }
    words = [word for word in re.split(r"\s+", text_lower) if word]
    if not words or any(word in blocked_words for word in words):
        return False
    return bool(re.fullmatch(r"[A-Za-z][A-Za-z .'-]*", text))

def is_name_only_response(text: str) -> bool:
    return is_valid_passenger_name(text) and len(text.split()) <= 4

def detect_booking_lookup_intent_with_llm(conversation_text: str) -> bool:
    prompt = f"""
Decide whether the user's latest message is asking to look up/check existing bookings.

Return only JSON: {{"lookup_intent": true}} or {{"lookup_intent": false}}.

Guidance:
- True for English or Banglish requests like "check my booking", "amar mobile number e booking ase kina", "show my tickets", "amar booking gula dekhao".
- False for booking a new ticket, cancelling a ticket, route/fare questions, or provider policy questions.

Conversation:
{conversation_text}
"""
    try:
        response = get_llm().invoke(prompt)
        content = getattr(response, "content", str(response)).strip()
        content = re.sub(r"^```(?:json)?|```$", "", content, flags=re.IGNORECASE).strip()
        return bool(json.loads(content).get("lookup_intent"))
    except Exception:
        lookup_markers = [
            "check", "lookup", "look up", "show", "my booking", "bookings",
            "ticket check", "booking ase", "booking ache", "ase kina", "dekhao", "dekhte"
        ]
        text_lower = conversation_text.lower()
        return "booking" in text_lower and any(marker in text_lower for marker in lookup_markers)

def generate_booking_lookup_message(bookings: List[dict], phone: str, conversation_text: str) -> str:
    prompt = f"""
You are BusGo's assistant.

The backend looked up bookings for this phone number:
{phone}

Bookings found:
{json.dumps(bookings, ensure_ascii=True)}

Conversation:
{conversation_text}

Write the assistant response.

Instructions:
- Do not use a fixed template.
- Match the user's language and style. If the user writes Banglish, reply in natural Banglish.
- Keep a professional customer-service tone. Never call the user "bhai", "vai", "bro", or similar casual labels.
- Use "Sir" or "Madam" naturally. In Banglish, prefer phrases like "Sir, ..." or "Madam, ..." instead of "bhai".
- If no bookings are found, say that clearly and mention the phone number checked.
- If bookings are found, include booking ID, route, provider, travel date, departure time when present, passenger count, seat numbers when present, total fare, and status.
- Keep it concise.
- Do not invent anything.
- Return only the assistant message.
"""
    try:
        response = get_llm().invoke(prompt)
        message = getattr(response, "content", str(response)).strip()
        if bookings:
            for booking in bookings:
                message = ensure_departure_time_visible(message, booking, conversation_text)
        return message
    except Exception:
        if not bookings:
            return f"No bookings found for phone number {phone}."
        lines = [f"Bookings for {phone}:"]
        for booking in bookings:
            lines.append(
                f"{booking['booking_id']}: {booking['from_district']} to {booking['to_district']}, "
                f"{booking['bus_provider']}, {booking['travel_date']}, time: {booking.get('departure_time') or '-'}, "
                f"{booking['num_passengers']} passenger(s), seats: {booking.get('seat_numbers') or '-'}, {booking['total_amount']} Taka, "
                f"status: {booking['status']}"
            )
        return "\n".join(lines)

def format_ai_error(exc: Exception) -> str:
    error_text = str(exc)
    lower = error_text.lower()
    if "quota" in lower or "resourceexhausted" in lower or "429" in lower:
        return (
            "The AI assistant is temporarily unavailable because the Gemini API quota is exhausted. "
            "Please try again after the retry window or use a Google API key/project with available quota."
        )
    if "google_api_key" in lower or "google_api_key" in error_text or "GOOGLE_API_KEY" in error_text:
        return "GOOGLE_API_KEY is not set or is invalid. Please update the .env file and restart the server."
    return f"The AI assistant is temporarily unavailable: {error_text}"

def wants_holiday_context(text: str) -> bool:
    text_lower = text.lower()
    markers = [
        "eid", "eider", "holiday", "holidays", "festival", "puja",
        "special service", "surcharge", "chuti", "utsob"
    ]
    return any(marker in text_lower for marker in markers)

def remove_unrequested_holiday_text(answer: str, query: str, allow_holiday: bool = False) -> str:
    if allow_holiday or wants_holiday_context(query):
        return answer

    holiday_markers = [
        "eid", "eider", "eid-er", "holiday", "holidays", "festival",
        "surcharge", "special package", "holiday package", "chuti"
    ]
    kept_lines = []
    for line in answer.splitlines():
        if any(marker in line.lower() for marker in holiday_markers):
            continue
        kept_lines.append(line)

    cleaned = "\n".join(kept_lines).strip()
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned or answer

def apply_booking_slots(draft: dict, slots: dict):
    if not slots:
        return draft

    slot_map = {
        "source_district": "from_district",
        "from_district": "from_district",
        "destination_district": "to_district",
        "to_district": "to_district",
        "bus_provider": "bus_provider",
        "dropping_point": "dropping_point",
        "passenger_name": "name",
        "name": "name",
        "phone": "phone",
        "travel_date": "travel_date",
        "num_passengers": "num_passengers",
        "departure_time": "departure_time",
        "bus_type": "bus_type",
        "seat_type": "bus_type",
        "coach_type": "bus_type",
        "service_details": "service_details",
    }

    for source_key, draft_key in slot_map.items():
        value = slots.get(source_key)
        if value is None or value == "":
            continue
        if isinstance(value, str) and value.lower() in {"null", "none", "unknown"}:
            continue
        if draft_key == "name" and not is_valid_passenger_name(str(value)):
            continue
        draft[draft_key] = normalize_phone(value) if draft_key == "phone" else value

    if draft.get("num_passengers"):
        try:
            draft["num_passengers"] = int(draft["num_passengers"])
        except (TypeError, ValueError):
            draft["num_passengers"] = None

    if draft.get("from_district") and draft.get("to_district") and not draft.get("bus_provider"):
        providers = get_available_providers(draft["from_district"], draft["to_district"])
        if len(providers) == 1:
            draft["bus_provider"] = providers[0]

    if draft.get("to_district") and not draft.get("dropping_point"):
        points = get_dropping_points_by_district(draft["to_district"])
        if points:
            cheapest = min(points, key=lambda point: point["price"])
            draft["dropping_point"] = cheapest["name"]

    return draft

def extract_booking_slots_with_llm(conversation_text: str, draft: dict):
    route_data = {
        "districts": [
            {
                "name": district["name"],
                "dropping_points": district["dropping_points"]
            }
            for district in bus_data["districts"]
        ],
        "providers": [
            {
                "name": provider["name"],
                "coverage_districts": provider["coverage_districts"],
                "details": {
                    key: value
                    for key, value in provider.items()
                    if key not in {"name", "coverage_districts"}
                }
            }
            for provider in bus_data["bus_providers"]
        ],
        "routes": routes_data
    }

    prompt = f"""
Extract bus booking details from the conversation.

Return ONLY valid JSON with these keys:
source_district, destination_district, bus_provider, dropping_point,
passenger_name, phone, travel_date, num_passengers, bus_type, departure_time.

Rules:
- Use null for missing values.
- Use exact district, provider, and dropping point names from the allowed data.
- If the data contains service details such as departure_time, bus_type, seat_type, coach, AC, or Non-AC options, preserve those details when the user mentions them.
- If the conversation already has an AC preference and the latest user says "normal time", keep AC. "Normal time" means regular/non-Eid booking time, not Non-AC.
- Never change AC to Non-AC unless the user explicitly says Non-AC.
- If the user asks for the cheapest option and the conversation already identifies it, use that dropping point.
- Convert dates to YYYY-MM-DD when clear.
- Current date: {datetime.now().date().isoformat()}.
- If a date is ambiguous or impossible, use null.
- Do not invent passenger name, phone, date, or passenger count.
- Do not use the current date as the travel date unless the user explicitly says today.
- Do not assume 1 passenger. Use null unless the user says the passenger/ticket/seat count.
- If the latest user message only provides a phone number, extract only the phone number and keep other missing passenger fields null.
- Current draft values help with context, but passenger_name, phone, travel_date, and num_passengers must still be supported by the conversation.

Allowed data:
{json.dumps(route_data, ensure_ascii=True)}

Current draft:
{json.dumps(draft, ensure_ascii=True)}

Conversation:
{conversation_text}
"""

    try:
        response = get_llm().invoke(prompt)
        content = getattr(response, "content", str(response)).strip()
        content = re.sub(r"^```(?:json)?|```$", "", content, flags=re.IGNORECASE).strip()
        return json.loads(content)
    except Exception:
        return {}

def get_booking_planner_data():
    return {
        "districts": [
            {
                "name": district["name"],
                "dropping_points": district["dropping_points"]
            }
            for district in bus_data["districts"]
        ],
        "providers": [
            {
                "name": provider["name"],
                "coverage_districts": provider["coverage_districts"],
                "details": {
                    key: value
                    for key, value in provider.items()
                    if key not in {"name", "coverage_districts"}
                }
            }
            for provider in bus_data["bus_providers"]
        ],
        "routes": routes_data
    }

def normalize_planner_result(value: dict) -> dict:
    allowed_intents = {
        "booking", "booking_continue", "booking_info", "lookup",
        "cancel", "route_info", "policy_info", "smalltalk", "other"
    }
    required_fields = {
        "from_district", "to_district", "bus_provider", "dropping_point",
        "name", "phone", "travel_date", "num_passengers", "departure_time"
    }
    if not isinstance(value, dict):
        value = {}
    intent = str(value.get("intent") or "other").strip().lower()
    if intent not in allowed_intents:
        intent = "other"
    slots = value.get("slots") if isinstance(value.get("slots"), dict) else {}
    missing = [
        field for field in value.get("missing_fields", [])
        if field in required_fields
    ] if isinstance(value.get("missing_fields"), list) else []
    return {
        "intent": intent,
        "slots": slots,
        "missing_fields": missing,
        "next_action": str(value.get("next_action") or "").strip().lower(),
        "response_language": str(value.get("response_language") or "").strip().lower(),
    }

def fallback_planner_result(query_text: str, conversation_text: str, draft: dict) -> dict:
    text_lower = f"{conversation_text}\n{query_text}".lower()
    latest_lower = query_text.lower()
    booking_markers = [
        "book", "booking", "reserve", "ticket lagbe", "ticket katbo",
        "confirm ticket", "confirm booking", "book korte", "book krte"
    ]
    lookup_markers = [
        "check my booking", "my booking", "amar booking", "booking ase",
        "booking ache", "booking gula", "show my tickets", "ticket check"
    ]
    cancel_markers = ["cancel", "cancellation", "batil"]
    info_markers = [
        "price", "fare", "cost", "cheap", "cheapest", "rate", "policy",
        "refund", "contact", "route", "time", "kokhon", "schedule",
        "departure", "ac bus", "non-ac", "non ac"
    ]
    if any(marker in latest_lower for marker in cancel_markers):
        intent = "cancel"
    elif any(marker in text_lower for marker in lookup_markers):
        intent = "lookup"
    elif any(marker in text_lower for marker in booking_markers) or draft:
        intent = "booking_continue" if draft else "booking"
    elif any(marker in latest_lower for marker in info_markers):
        intent = "route_info"
    else:
        intent = "other"
    return {
        "intent": intent,
        "slots": {},
        "missing_fields": [],
        "next_action": "",
        "response_language": "banglish" if is_banglish(conversation_text) else "english",
    }

def plan_chat_turn_with_llm(query_text: str, conversation_text: str, draft: dict) -> dict:
    prompt = f"""
You are BusGo's conversation planner for a Bangladesh bus-ticket chatbot.

Return ONLY valid JSON with this exact shape:
{{
  "intent": "booking | booking_continue | booking_info | lookup | cancel | route_info | policy_info | smalltalk | other",
  "slots": {{
    "source_district": null,
    "destination_district": null,
    "bus_provider": null,
    "dropping_point": null,
    "passenger_name": null,
    "phone": null,
    "travel_date": null,
    "num_passengers": null,
    "bus_type": null,
    "departure_time": null
  }},
  "missing_fields": [],
  "next_action": "answer_info | ask_missing_fields | show_seat_selection | lookup_booking | cancel_booking | rag_answer | smalltalk",
  "response_language": "english | banglish"
}}

Rules:
- Understand English and Banglish/romanized Bangla.
- Use the full conversation and current draft to understand follow-ups.
- "Amar Dhaka theke Rangpur er ticket lagbe" is booking intent.
- "ok book it", "confirm", "ticket ta book koren" continues booking from previous route context.
- If the user asks price/fare/time/policy/contact only, use route_info or policy_info, not booking.
- If the user asks booking lookup/check/history, use lookup.
- If the user asks cancellation, use cancel.
- Extract any slots present in the conversation, even if phrased informally: "nam Shoaib", "mobile 013...", "3 jon", "porshudhin".
- Convert dates to YYYY-MM-DD when clear. Current date: {datetime.now().date().isoformat()}.
- Do not invent passenger name, phone, travel date, or passenger count.
- Do not assume 1 passenger.
- Do not use today's date unless the user explicitly says today.
- Use exact district, provider, and dropping point names from allowed data.
- Preserve AC/Non-AC and departure time when mentioned.
- If the user asks for cheapest/kom dam/cheap, choose the cheapest route service according to allowed data when possible.
- Route/provider/dropping point may be inferred from allowed data when the conversation supports that route or cheapest option.
- missing_fields should include only fields required before showing seat selection:
  from_district, to_district, bus_provider, dropping_point, departure_time, name, phone, travel_date, num_passengers.
- If the selected service has multiple departure_times and the user has not selected one, include departure_time in missing_fields before passenger details.
- Never silently choose the first departure time for a multi-time service; ask the user to choose or confirm the time first.
- Current draft values count as already known unless the latest user clearly corrects them.

Allowed data:
{json.dumps(get_booking_planner_data(), ensure_ascii=True)}

Current draft:
{json.dumps(draft or {}, ensure_ascii=True)}

Conversation:
{conversation_text}

Latest user message:
{query_text}
"""
    try:
        response = get_llm().invoke(prompt)
        content = getattr(response, "content", str(response)).strip()
        content = re.sub(r"^```(?:json)?|```$", "", content, flags=re.IGNORECASE).strip()
        return normalize_planner_result(json.loads(content))
    except Exception:
        return normalize_planner_result(fallback_planner_result(query_text, conversation_text, draft))

def detect_booking_intent_with_llm(conversation_text: str) -> bool:
    prompt = f"""
Decide whether the user's latest message is asking to actually book/reserve/buy/confirm a bus ticket now.

Return only JSON: {{"booking_intent": true}} or {{"booking_intent": false}}.

Guidance:
- True for English or Banglish requests like "book it", "reserve this ticket", "ticket lagbe", "ticket katbo", "amar Dhaka theke Rangpur e jawar ticket lagbe".
- True if the user first asks about a route/service and then says they want normal-time booking, confirm it, or continue booking.
- False for information-only requests like "ticket price koto", "fare koto", "cheap rate bolo", "policy ki", "route ache?", "contact number dao".
- If the user says "ticket lagbe" with a route, treat it as booking intent unless they explicitly ask only for price/fare/info.
- Use the conversation history to understand follow-ups like "ok book it".

Conversation:
{conversation_text}
"""
    try:
        response = get_llm().invoke(prompt)
        content = getattr(response, "content", str(response)).strip()
        content = re.sub(r"^```(?:json)?|```$", "", content, flags=re.IGNORECASE).strip()
        return bool(json.loads(content).get("booking_intent"))
    except Exception:
        return False

def update_booking_draft_from_text(draft: dict, text: str):
    preference = detect_service_preference(text) or detect_service_preference_with_llm(text)
    if preference:
        draft["service_preference"] = preference

    source, destination = detect_route(text)
    if source:
        draft["from_district"] = source
    if destination:
        draft["to_district"] = destination

    provider = detect_provider(text)
    if provider:
        draft["bus_provider"] = provider
        draft["provider_explicit"] = True

    bus_type = detect_bus_type(text)
    if bus_type:
        draft["bus_type"] = bus_type

    dropping_point = detect_dropping_point(text, draft.get("to_district"))
    if dropping_point:
        draft["dropping_point"] = dropping_point

    passenger_count = detect_passenger_count(text)
    if passenger_count:
        draft["num_passengers"] = passenger_count

    travel_date = detect_travel_date(text)
    if travel_date:
        draft["travel_date"] = travel_date

    name = detect_name(text)
    if name:
        draft["name"] = name

    phone = detect_phone(text)
    if phone:
        draft["phone"] = phone

    if "cheap" in text.lower() and draft.get("to_district") and not draft.get("dropping_point"):
        points = get_dropping_points_by_district(draft["to_district"])
        if points:
            cheapest = min(points, key=lambda point: point["price"])
            draft["dropping_point"] = cheapest["name"]

    cheap_markers = ["cheap", "cheapest", "kom price", "kom dame", "kom dam", "kom vara", "kom bhara", "shobcheye kom"]
    if (
        (draft.get("service_preference") in {"cheap", "quality"} or any(marker in text.lower() for marker in cheap_markers))
        and draft.get("from_district")
        and draft.get("to_district")
    ):
        apply_preferred_route_option(draft)

    if draft.get("from_district") and draft.get("to_district") and not draft.get("bus_provider"):
        providers = get_available_providers(draft["from_district"], draft["to_district"])
        if len(providers) == 1:
            draft["bus_provider"] = providers[0]

    option = choose_route_option(draft)
    if option:
        if not draft.get("bus_provider"):
            draft["bus_provider"] = option["provider"]
        if draft.get("bus_type") and not draft.get("fare"):
            draft["fare"] = option["fare"]

    return draft

def get_missing_booking_fields(draft: dict) -> List[str]:
    required_fields = [
        "from_district", "to_district", "bus_provider", "dropping_point",
        "name", "phone", "travel_date", "num_passengers"
    ]
    missing = []
    for field in required_fields:
        value = draft.get(field)
        if not value:
            missing.append(field)
            continue
        if field == "name" and not is_valid_passenger_name(str(value)):
            missing.append(field)
        if field == "phone" and not detect_phone(str(value)):
            missing.append(field)
        if field == "num_passengers":
            try:
                passengers = int(value)
                if passengers < 1 or passengers > 10:
                    missing.append(field)
            except (TypeError, ValueError):
                missing.append(field)
    return missing

def verify_booking_ready_with_llm(draft: dict, conversation_text: str) -> List[str]:
    prompt = f"""
You are checking whether BusGo is allowed to save a ticket booking.

Return ONLY valid JSON:
{{"missing_fields": ["field_name"]}}

Required fields:
from_district, to_district, bus_provider, dropping_point,
departure_time, name, phone, travel_date, num_passengers.

Booking draft:
{json.dumps(get_booking_context(draft), ensure_ascii=True)}

Conversation:
{conversation_text}

Rules:
- Be strict for passenger-provided fields: name, phone, travel_date, num_passengers.
- These passenger-provided fields are missing unless the user explicitly gave them in the conversation.
- Do not accept a default passenger count. If the user did not say how many passengers/tickets/seats/people, num_passengers is missing.
- Do not accept today's date, the current date, or any guessed date unless the user explicitly said today or gave that date.
- Route/provider/dropping point may be inferred from data when the draft contains them and the conversation supports the route or cheapest option.
- If the selected service has multiple departure_times and the user has not selected one, departure_time is missing.
- Never allow saving a multi-time service by silently selecting the first departure time.
- If everything is truly ready to save, return {{"missing_fields": []}}.
"""
    try:
        response = get_llm().invoke(prompt)
        content = getattr(response, "content", str(response)).strip()
        content = re.sub(r"^```(?:json)?|```$", "", content, flags=re.IGNORECASE).strip()
        missing = json.loads(content).get("missing_fields", [])
        return [field for field in missing if field in {
            "from_district", "to_district", "bus_provider", "dropping_point",
            "departure_time", "name", "phone", "travel_date", "num_passengers"
        }]
    except Exception:
        return get_missing_booking_fields(draft)

def get_booking_context(draft: dict):
    context = draft.copy()

    option = choose_route_option(draft)
    if option:
        fare, holiday_context = apply_holiday_fare(
            option["fare"],
            draft.get("travel_date"),
            draft.get("bus_provider") or option["provider"]
        )
        context["fare"] = fare
        context["base_fare"] = option["fare"]
        if holiday_context:
            context["holiday_context"] = holiday_context
        context["bus_provider"] = draft.get("bus_provider") or option["provider"]
        context["bus_type"] = draft.get("bus_type") or option["bus_type"]
        context["departure_times"] = option.get("departure_times", [])
        context["distance_km"] = option.get("distance_km")
        context["avg_duration_hours"] = option.get("avg_duration_hours")
        if draft.get("num_passengers"):
            context["total_amount"] = fare * int(draft["num_passengers"])
    elif draft.get("to_district") and draft.get("dropping_point"):
        fare = get_fare(draft["to_district"], draft["dropping_point"])
        context["fare"] = fare
        if draft.get("num_passengers"):
            context["total_amount"] = fare * int(draft["num_passengers"])

    if draft.get("from_district") and draft.get("to_district"):
        context["available_providers"] = get_available_providers(
            draft["from_district"],
            draft["to_district"]
        )

    return context

def get_seat_selection_payload(draft: dict):
    option = choose_route_option(draft)
    if not option:
        return None

    departure_time = draft.get("departure_time")
    if not departure_time and option.get("departure_times"):
        departure_time = option["departure_times"][0]

    bus_type = draft.get("bus_type") or option.get("bus_type")
    booked = get_booked_seats(
        option["provider"],
        draft["from_district"],
        draft["to_district"],
        draft["travel_date"],
        bus_type,
        departure_time
    )
    layout = build_seat_layout(booked)

    fare, holiday_context = apply_holiday_fare(
        option["fare"],
        draft.get("travel_date"),
        draft.get("bus_provider") or option["provider"]
    )

    return {
        "provider": option["provider"],
        "from_district": draft["from_district"],
        "to_district": draft["to_district"],
        "dropping_point": draft["dropping_point"],
        "travel_date": draft["travel_date"],
        "num_passengers": int(draft["num_passengers"]),
        "bus_type": bus_type,
        "departure_time": departure_time,
        "fare": fare,
        "total_amount": fare * int(draft["num_passengers"]),
        "holiday_context": holiday_context,
        "layout": layout,
    }

def generate_seat_selection_message(draft: dict, conversation_text: str) -> str:
    seat_context = get_seat_selection_payload(draft) or get_booking_context(draft)
    prompt = f"""
You are BusGo's bus-ticket booking assistant.

The booking details are ready, but the user must choose seats in the seat picker UI before the ticket is saved.

Booking/seat context:
{json.dumps(seat_context, ensure_ascii=True)}

Conversation:
{conversation_text}

Write one short assistant message.

Instructions:
- Do not use a fixed template.
- Match the user's language and style. If the user writes Banglish, reply in natural Banglish.
- Keep a professional customer-service tone. Never call the user "bhai", "vai", "bro", or similar casual labels.
- Use "Sir" or "Madam" naturally. In Banglish, prefer phrases like "Sir, ..." or "Madam, ..." instead of "bhai".
- Tell the user to select exactly the passenger count of seats from the seat picker and then confirm.
- Always mention the selected departure time when it is present in the booking/seat context, even if the user did not ask for time.
- Mention the route, bus, selected time, fare/total only if useful.
- Do not say the booking is confirmed yet.
- Return only the assistant message.
"""
    try:
        response = get_llm().invoke(prompt)
        message = getattr(response, "content", str(response)).strip()
        message = remove_unrequested_holiday_text(
            message,
            conversation_text,
            allow_holiday=bool(seat_context.get("holiday_context"))
        )
        return ensure_departure_time_visible(message, seat_context, conversation_text)
    except Exception:
        message = "Please select your seats from the seat picker, then confirm the booking."
        return ensure_departure_time_visible(message, seat_context, conversation_text)

def generate_booking_followup_message(draft: dict, missing_fields: List[str], conversation_text: str) -> str:
    booking_context = get_booking_context(draft)
    prompt = f"""
You are BusGo's bus-ticket booking assistant.

The backend has extracted a booking draft and knows these fields are still missing:
{json.dumps(missing_fields, ensure_ascii=True)}

Booking draft/context:
{json.dumps(booking_context, ensure_ascii=True)}

Conversation:
{conversation_text}

Write the next assistant message.

Instructions:
- Do not use a fixed template.
- Match the user's language and style. If the user writes Banglish, reply in natural Banglish.
- Keep a professional customer-service tone. Never call the user "bhai", "vai", "bro", or similar casual labels.
- Use "Sir" or "Madam" naturally. In Banglish, prefer phrases like "Sir, ..." or "Madam, ..." instead of "bhai".
- Sound conversational, not like a form.
- Ask for all missing fields, but do it naturally in one short message. Do not ask again for fields already present.
- If route, bus, dropping point, or fare are known, mention them briefly only if useful.
- The backend has NOT saved the booking yet. Before seat selection, never say request sent, booking confirmed, ticket confirmed, phone confirmation sent, or anything similar.
- A ticket can only be saved after all required fields are collected and the user selects seats from the seat picker UI.
- Do not mention Eid, holiday packages, or holiday surcharge unless the booking context contains holiday_context or the user explicitly asks about holidays.
- A dropping point is where the passenger gets down, not where they board. If it is known, state it professionally as information, for example "Sir, apnar dropping point Dhap." or "Sir, Dhap-e namte hobe." Do not ask "Dhap-e namte hobe, tai na?" unless the dropping point is genuinely uncertain.
- Do not ask the user to confirm a known dropping point. Ask only if there are multiple valid dropping points and the user has not chosen one.
- Do not confirm or imply the ticket is booked. The booking is not saved yet.
- Do not invent data.
- Keep it short.
- Return only the assistant message.
"""
    try:
        response = get_llm().invoke(prompt)
        message = getattr(response, "content", str(response)).strip()
        return remove_unrequested_holiday_text(
            message,
            conversation_text,
            allow_holiday=bool(booking_context.get("holiday_context"))
        )
    except Exception:
        return "I need a bit more information to complete the booking."


def should_show_service_recommendation(draft: dict, query_text: str) -> bool:
    if not draft.get("from_district") or not draft.get("to_district"):
        return False
    option = choose_route_option(draft)
    if not option:
        return False
    text_lower = (query_text or "").lower()
    service_markers = ["ticket lagbe", "ticket chai", "ticket dorkar", "bus er ticket", "bus ticket", "seat lagbe"]
    if not any(marker in text_lower for marker in service_markers) and not draft.get("service_preference"):
        return False
    passenger_fields = ["name", "phone", "travel_date", "num_passengers"]
    return any(not draft.get(field) for field in passenger_fields)


def generate_service_recommendation_message(draft: dict, conversation_text: str) -> str:
    context = get_booking_context(draft)
    option = choose_route_option(draft)
    if option:
        context["departure_times"] = option.get("departure_times", [])
        context["rating"] = option.get("rating")
        context["total_reviews"] = option.get("total_reviews")

    prompt = f"""
You are BusGo's Bangladeshi bus-ticket assistant.

The user seems interested in a ticket, but has not yet chosen a departure time or confirmed booking.

Service context:
{json.dumps(context, ensure_ascii=True)}

Conversation:
{conversation_text}

Write a short service recommendation message.

Instructions:
- Match the user's language. If the user writes Banglish, reply in natural Banglish.
- Keep a professional customer-service tone. Never call the user "bhai", "vai", "bro", or similar casual labels.
- Use "Sir" or "Madam" naturally. In Banglish, prefer phrases like "Sir, ..." or "Madam, ..." instead of casual address.
- Do NOT ask for name, phone, travel date, or passenger count yet.
- First mention the recommended bus/provider, bus type, route, fare per seat, and all available departure times.
- If service_preference is quality, say this is the better quality/recommended option.
- Ask the user to choose one departure time.
- Do not say the ticket is booked or confirmed.
- Return only the assistant message.
"""
    try:
        response = get_llm().invoke(prompt)
        return getattr(response, "content", str(response)).strip()
    except Exception:
        times = ", ".join(context.get("departure_times") or [])
        provider = context.get("bus_provider") or context.get("provider") or "-"
        bus_type = context.get("bus_type") or "-"
        fare = context.get("fare") or "-"
        return (
            f"Sir, {context.get('from_district')} theke {context.get('to_district')} er jonno "
            f"{provider} {bus_type} recommended. Fare {fare} Taka per seat. "
            f"Available times: {times}. Kon time ta niben?"
        )

def generate_booking_info_message(draft: dict, user_question: str, conversation_text: str) -> str:
    booking_context = get_booking_context(draft)
    prompt = f"""
You are BusGo's bus-ticket booking assistant.

The user is in the middle of a booking and asked this question:
{user_question}

Booking draft/context:
{json.dumps(booking_context, ensure_ascii=True)}

Conversation:
{conversation_text}

Write the assistant response.

Instructions:
- Answer only the user's question from the booking context.
- Match the user's language and style. If the user writes Banglish, reply in natural Banglish.
- Keep a professional customer-service tone. Never call the user "bhai", "vai", "bro", or similar casual labels.
- Use "Sir" or "Madam" naturally. In Banglish, prefer phrases like "Sir, ..." or "Madam, ..." instead of "bhai".
- If the user asks total fare, use total_amount from the booking context when available.
- If holiday_context exists, explain briefly that the date falls in that calendar window and the fare includes the configured surcharge.
- If holiday_context does not exist, do not mention Eid, holidays, holiday packages, or surcharges.
- Do not confirm the booking unless it is already saved.
- Return only the assistant message.
"""
    try:
        response = get_llm().invoke(prompt)
        message = getattr(response, "content", str(response)).strip()
        return remove_unrequested_holiday_text(
            message,
            user_question,
            allow_holiday=bool(booking_context.get("holiday_context"))
        )
    except Exception:
        if booking_context.get("total_amount"):
            return f"Total fare: {booking_context['total_amount']} Taka."
        return "I don't have enough booking details to calculate the total fare yet."

def create_booking_from_draft(draft: dict):
    if not validate_route(draft["bus_provider"], draft["from_district"], draft["to_district"]):
        return None, f"{draft['bus_provider']} does not operate from {draft['from_district']} to {draft['to_district']}."

    option = choose_route_option(draft)
    if not option:
        return None, f"No scheduled service was found for {draft['bus_provider']} from {draft['from_district']} to {draft['to_district']}."

    base_fare = option["fare"]
    fare, holiday_context = apply_holiday_fare(
        base_fare,
        draft.get("travel_date"),
        draft.get("bus_provider") or option["provider"]
    )
    if fare == 0:
        return None, f"Fare was not found for {draft['bus_provider']} from {draft['from_district']} to {draft['to_district']}."

    departure_time = draft.get("departure_time")
    if not departure_time and option.get("departure_times"):
        departure_time = option["departure_times"][0]

    seats, seat_error = assign_seats(
        int(draft["num_passengers"]),
        get_booked_seats(
            option["provider"],
            draft["from_district"],
            draft["to_district"],
            draft["travel_date"],
            draft.get("bus_type") or option.get("bus_type"),
            departure_time
        ),
        draft.get("selected_seats")
    )
    if seat_error:
        return None, seat_error

    booking_data = {
        "booking_id": generate_booking_id(),
        "name": draft["name"],
        "phone": normalize_phone(draft["phone"]),
        "bus_provider": option["provider"],
        "from_district": draft["from_district"],
        "to_district": draft["to_district"],
        "dropping_point": draft["dropping_point"],
        "travel_date": draft["travel_date"],
        "num_passengers": int(draft["num_passengers"]),
        "fare": fare,
        "total_amount": fare * int(draft["num_passengers"]),
        "departure_time": departure_time,
        "bus_type": draft.get("bus_type") or option.get("bus_type"),
        "seat_numbers": ", ".join(seats),
        "service_details": json.dumps({
            "distance_km": option.get("distance_km"),
            "avg_duration_hours": option.get("avg_duration_hours"),
            "available_departure_times": option.get("departure_times", []),
            "base_fare": base_fare,
            "holiday_context": holiday_context,
        }),
        "booking_date": datetime.now().isoformat(),
        "status": "payment_pending",
        "payment_status": "pending",
        "payment_expires_at": payment_deadline(),
    }
    return create_booking(booking_data), None

def generate_booking_confirmation_message(booking: dict, conversation_text: str, include_account_offer: bool = False) -> str:
    prompt = f"""
You are BusGo's bus-ticket booking assistant.

The backend has already saved this booking in the database:
{json.dumps(booking, ensure_ascii=True)}

Conversation:
{conversation_text}

Write the booking confirmation message.

Instructions:
- Do not use a fixed template.
- Match the user's language and style. If the user writes Banglish, reply in natural Banglish.
- Sound warm and conversational, not robotic.
- Keep a professional customer-service tone. Never call the user "bhai", "vai", "bro", or similar casual labels.
- Use "Sir" or "Madam" naturally. In Banglish, prefer phrases like "Sir, ..." or "Madam, ..." instead of "bhai".
- Clearly include the booking ID, route, bus provider, dropping point, travel date, passenger count, and total fare.
- Include bus type, departure time, and seat numbers when present in the saved booking.
- Mention holiday surcharge only if holiday_context is present in service_details.
- A dropping point is where the passenger gets down, not where they board. State it professionally as information, for example "Sir, apnar dropping point Dhap." or "Sir, Dhap-e namte hobe." Do not ask "tai na?" about a saved/known dropping point.
- Use the saved booking values exactly, especially booking ID, date, route, provider, fare, and passenger count.
- Tell the user to keep the booking ID because it is needed for lookup/cancellation.
- If include_account_offer is true, naturally ask whether the user wants to create an account so next time they do not need to type name and mobile again. Do not ask for name or phone for account creation because they are already known from the booking.
- include_account_offer: {str(include_account_offer).lower()}
- Do not invent anything.
- Return only the assistant message.
"""
    try:
        response = get_llm().invoke(prompt)
        message = getattr(response, "content", str(response)).strip()
        return ensure_departure_time_visible(message, booking, conversation_text)
    except Exception:
        message = f"Booking saved. Booking ID: {booking['booking_id']}"
        if include_account_offer:
            message += "\n\nApni ki account khulte chan? Account khulle next time name/mobile bar bar dite hobe na."
        return ensure_departure_time_visible(message, booking, conversation_text)

def generate_payment_pending_message(booking: dict, conversation_text: str) -> str:
    prompt = f"""
You are BusGo's bus-ticket booking assistant.

The backend has held seats for this booking, but payment is not completed yet:
{json.dumps(booking, ensure_ascii=True)}

Conversation:
{conversation_text}

Write a short assistant message.

Instructions:
- Match the user's language and style. If the user writes Banglish, reply in natural Banglish.
- Keep a professional customer-service tone. Use Sir/Madam naturally. Never use bhai/vai.
- Make clear that seats are held for 5 minutes, but the ticket is not active/confirmed until demo payment succeeds.
- Mention booking ID, route, provider, travel date, departure time when present, seats, total amount, and that the user can pay using bKash or Nagad demo payment.
- Do not say the ticket is confirmed yet.
- Return only the assistant message.
"""
    try:
        response = get_llm().invoke(prompt)
        message = getattr(response, "content", str(response)).strip()
        return ensure_departure_time_visible(message, booking, conversation_text)
    except Exception:
        message = (
            f"Sir, seats {booking.get('seat_numbers') or '-'} are held for booking {booking['booking_id']} "
            f"on {booking.get('travel_date') or '-'} at {booking.get('departure_time') or '-'} "
            f"for 5 minutes. Please complete demo bKash/Nagad payment of {booking['total_amount']} Taka "
            "to confirm the ticket."
        )
        return ensure_departure_time_visible(message, booking, conversation_text)

# ==================== Session Storage ====================
sessions = {}

# ==================== Page Routes (HTML) ====================

@app.get("/")
def index(request: Request):
    return templates.TemplateResponse(request, "index.html", {
        "request": request,
        "active": "book",
        "providers": bus_data["bus_providers"],
        "districts": bus_data["districts"]
    })

@app.get("/bookings-page")
def bookings_page(request: Request):
    return templates.TemplateResponse(request, "bookings.html", {
        "request": request,
        "active": "bookings"
    })

@app.get("/providers-page")
def providers_page(request: Request):
    return templates.TemplateResponse(request, "providers.html", {
        "request": request,
        "active": "providers",
        "providers": bus_data["bus_providers"]
    })

@app.get("/routes-page")
def routes_page(request: Request):
    return templates.TemplateResponse(request, "routes.html", {
        "request": request,
        "active": "routes",
        "districts": bus_data["districts"]
    })

@app.get("/assistant-page")
def assistant_page(request: Request):
    return templates.TemplateResponse(request, "assistant.html", {
        "request": request,
        "active": "assistant"
    })

@app.get("/login-page")
def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {
        "request": request,
        "active": "login"
    })

@app.get("/dashboard-page")
def dashboard_page(request: Request):
    return templates.TemplateResponse(request, "dashboard.html", {
        "request": request,
        "active": "dashboard"
    })

@app.get("/admin-page")
def admin_page(request: Request):
    return templates.TemplateResponse(request, "admin.html", {
        "request": request,
        "active": "admin"
    })

# ==================== API Endpoints ====================

@app.get("/auth/me")
def auth_me(auth_token: Optional[str] = Cookie(None, alias=AUTH_COOKIE_NAME)):
    user = get_current_user(auth_token)
    return {"authenticated": bool(user), "user": public_user(user)}

@app.post("/auth/signup")
def auth_signup(payload: SignupRequest, response: Response):
    phone = normalize_phone(payload.phone)
    if not re.fullmatch(r"01\d{9}", phone):
        raise HTTPException(status_code=400, detail="Enter a valid Bangladeshi phone number.")
    try:
        user = create_user(payload.name.strip(), phone, hash_password(payload.password))
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=409, detail="An account already exists with this phone number.")

    set_login_cookie(response, user)
    return {"message": "Account created successfully.", "user": public_user(user)}

@app.post("/auth/login")
def auth_login(payload: LoginRequest, response: Response):
    login_identifier = payload.phone.strip()
    user = get_user_by_login(login_identifier)
    if not user or not verify_password(payload.password, user.get("password_hash", "")):
        raise HTTPException(status_code=401, detail="Invalid login details or password.")

    set_login_cookie(response, user)
    return {"message": "Logged in successfully.", "user": public_user(user)}

@app.post("/auth/logout")
def auth_logout(response: Response, auth_token: Optional[str] = Cookie(None, alias=AUTH_COOKIE_NAME)):
    if auth_token:
        delete_auth_session(auth_token)
    response.delete_cookie(AUTH_COOKIE_NAME, path="/", samesite="lax")
    return {"message": "Logged out successfully."}

@app.get("/user/dashboard")
def user_dashboard(auth_token: Optional[str] = Cookie(None, alias=AUTH_COOKIE_NAME)):
    expire_payment_pending_bookings()
    user = get_current_user(auth_token)
    if not user:
        raise HTTPException(status_code=401, detail="Please log in to view your dashboard.")

    phone = user.get("phone")
    bookings = get_bookings_by_phone(phone)
    active_bookings = [booking for booking in bookings if booking.get("status") == "active"]
    cancelled_bookings = [booking for booking in bookings if booking.get("status") == "cancelled"]
    total_spent = sum(int(booking.get("total_amount") or 0) for booking in active_bookings)

    return {
        "user": public_user(user),
        "stats": {
            "total_bookings": len(bookings),
            "active_bookings": len(active_bookings),
            "cancelled_bookings": len(cancelled_bookings),
            "total_spent": total_spent
        },
        "bookings": bookings
    }

@app.get("/admin/dashboard")
def admin_dashboard(auth_token: Optional[str] = Cookie(None, alias=AUTH_COOKIE_NAME)):
    expire_payment_pending_bookings()
    admin = require_admin(auth_token)
    bookings = get_all_bookings()
    users = get_all_users()
    refunds = list_refund_requests()
    active_bookings = [booking for booking in bookings if booking.get("status") == "active"]
    pending_bookings = [booking for booking in bookings if booking.get("status") == "payment_pending"]
    cancelled_bookings = [booking for booking in bookings if booking.get("status") == "cancelled"]
    paid_bookings = [booking for booking in bookings if booking.get("payment_status") == "paid"]
    gross_ticket_sales = sum(int(booking.get("total_amount") or 0) for booking in paid_bookings)
    platform_revenue = round(gross_ticket_sales * 0.05, 2)
    active_routes = {
        f"{booking.get('from_district')}->{booking.get('to_district')}"
        for booking in active_bookings
        if booking.get("from_district") and booking.get("to_district")
    }
    pending_refunds = [refund for refund in refunds if refund.get("status") == "requested"]
    today = datetime.now().date().isoformat()
    today_bookings = [
        booking for booking in bookings
        if str(booking.get("booking_date") or "").startswith(today)
    ]
    return {
        "admin": public_user(admin),
        "stats": {
            "total_bookings": len(bookings),
            "today_bookings": len(today_bookings),
            "active_bookings": len(active_bookings),
            "pending_payment": len(pending_bookings),
            "cancelled_bookings": len(cancelled_bookings),
            "paid_bookings": len(paid_bookings),
            "gross_ticket_sales": gross_ticket_sales,
            "platform_revenue": platform_revenue,
            "commission_percent": 5,
            "active_routes": len(active_routes),
            "pending_refunds": len(pending_refunds),
            "total_users": len(users),
        },
        "bookings": bookings,
        "users": users,
        "refunds": refunds,
        "routes": routes_data,
        "operators": bus_data.get("bus_providers", []),
    }

@app.post("/chat/create-account")
def create_account_from_chat(payload: ChatSignupRequest, response: Response):
    session = sessions.get(payload.session_id)
    offer = session.get("pending_account_offer") if session else None
    if not offer:
        raise HTTPException(status_code=400, detail="No account signup is waiting in this chat.")

    name = (offer.get("name") or "").strip()
    phone = normalize_phone(offer.get("phone") or "")
    if not name or not re.fullmatch(r"01\d{9}", phone):
        raise HTTPException(status_code=400, detail="Booking details do not have enough account information.")

    try:
        user = create_user(name, phone, hash_password(payload.password))
    except sqlite3.IntegrityError:
        existing_user = get_user_by_phone(phone)
        if not existing_user:
            raise HTTPException(status_code=409, detail="Could not create the account.")
        raise HTTPException(status_code=409, detail="An account already exists with this phone number. Please log in instead.")

    session["pending_account_offer"] = None
    set_login_cookie(response, user)
    message = "Done, apnar BusGo account create hoye geche. Next time booking korte gele name ar mobile abar dite hobe na."
    save_chat_message(payload.session_id, "assistant", message, phone)
    return {"message": message, "user": public_user(user)}

@app.get("/demo-wallets")
def demo_wallets():
    return {
        "methods": ["bkash", "nagad"],
        "demo_pin_hint": "All seeded demo wallets use PIN 1234."
    }

@app.post("/payments/demo")
def demo_payment(payload: DemoPaymentRequest, auth_token: Optional[str] = Cookie(None, alias=AUTH_COOKIE_NAME)):
    expire_payment_pending_bookings()
    booking = get_booking_by_id(payload.booking_id)
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found.")
    if booking.get("status") != "payment_pending":
        raise HTTPException(status_code=400, detail="This booking is not waiting for payment.")
    expires_at = booking.get("payment_expires_at")
    if expires_at and datetime.fromisoformat(expires_at) < datetime.now():
        expire_payment_pending_bookings()
        raise HTTPException(status_code=400, detail="Payment time expired. Please book again.")
    if int(payload.amount) != int(booking.get("total_amount") or 0):
        raise HTTPException(status_code=400, detail=f"Payment amount must be exactly {booking['total_amount']} Taka.")

    phone = normalize_phone(payload.phone)
    payment, error = verify_and_deduct(
        payload.provider,
        phone,
        int(payload.amount),
        payload.pin,
        booking["booking_id"]
    )
    if error:
        raise HTTPException(status_code=400, detail=error)

    paid_booking = update_booking_payment(
        booking["booking_id"],
        payment["provider"],
        payment["transaction_id"]
    )
    current_user = get_current_user(auth_token)
    should_offer_account = not current_user and not get_user_by_phone(paid_booking["phone"])
    message = (
        f"Payment successful. Ticket {paid_booking['booking_id']} is now confirmed. "
        f"Transaction ID: {payment['transaction_id']}."
    )
    response_payload = {
        "message": message,
        "booking": paid_booking,
        "payment": payment,
    }
    if should_offer_account:
        if payload.session_id and payload.session_id in sessions:
            sessions[payload.session_id]["pending_account_offer"] = account_offer_payload(paid_booking)
        response_payload["account_offer"] = account_offer_payload(paid_booking)
    return response_payload

@app.post("/refunds/request/{booking_id}")
def request_refund(booking_id: str):
    booking = get_booking_by_id(booking_id)
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found.")
    if booking.get("payment_status") != "paid":
        raise HTTPException(status_code=400, detail="Only paid bookings can request a refund.")
    refund = create_refund_for_paid_booking(booking)
    return {"message": REFUND_REQUEST_MESSAGE_BN, "refund": refund}

@app.post("/refunds/approve/{refund_id}")
def approve_refund_endpoint(refund_id: str, auth_token: Optional[str] = Cookie(None, alias=AUTH_COOKIE_NAME)):
    require_admin(auth_token)
    refund, error = approve_refund(refund_id)
    if error:
        raise HTTPException(status_code=400, detail=error)
    return {"message": "Refund approved and demo wallet balance returned.", "refund": refund}

@app.get("/districts")
def get_districts():
    return {"districts": bus_data["districts"]}

@app.get("/providers")
def get_providers():
    return {"providers": bus_data["bus_providers"]}

@app.get("/providers/{provider_name}/policy")
def get_provider_policy(provider_name: str):
    provider_map = {
        "desh travel": "desh_travel.txt",
        "ena": "ena.txt",
        "ena paribahan": "ena.txt",
        "green line": "green line.txt",
        "greenline": "green line.txt",
        "hanif": "hanif.txt",
        "hanif enterprise": "hanif.txt",
        "shyamoli": "shyamoli.txt",
        "shyamoli paribahan": "shyamoli.txt",
        "soudia": "soudia.txt"
    }
    normalized = provider_name.lower().strip()
    if normalized not in provider_map:
        raise HTTPException(status_code=404, detail="Policy not available for this provider")
    file_name = provider_map[normalized]
    file_path = BASE_DIR / "attachment" / file_name
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Policy file not found")
    return {"provider": provider_name, "policy": content}

@app.get("/available-providers")
def available_providers(from_district: str, to_district: str):
    providers = get_available_providers(from_district, to_district)
    if not providers:
        return {"message": f"No providers between {from_district} and {to_district}", "providers": []}
    return {"providers": providers}

@app.get("/route-services")
def route_services(from_district: str, to_district: str, provider: Optional[str] = None):
    services = []
    for option in get_route_options(from_district, to_district, provider):
        for departure_time in option.get("departure_times") or [None]:
            services.append({
                "provider": option["provider"],
                "bus_type": option["bus_type"],
                "fare": option["fare"],
                "departure_time": departure_time,
                "distance_km": option.get("distance_km"),
                "avg_duration_hours": option.get("avg_duration_hours"),
            })
    if not services:
        return {"message": f"No scheduled services between {from_district} and {to_district}", "services": []}
    return {"services": services}

@app.get("/seat-layout")
def seat_layout(
    provider: str,
    from_district: str,
    to_district: str,
    travel_date: str,
    bus_type: Optional[str] = None,
    departure_time: Optional[str] = None
):
    if not validate_route(provider, from_district, to_district):
        raise HTTPException(status_code=400, detail="Provider does not operate on this route")
    booked = get_booked_seats(provider, from_district, to_district, travel_date, bus_type, departure_time)
    return build_seat_layout(booked)

@app.get("/holiday-calendar")
def get_holiday_calendar():
    return load_holiday_calendar()

@app.get("/dropping-points/{district}")
def dropping_points(district: str):
    points = get_dropping_points_by_district(district)
    if not points:
        return {"message": f"No dropping points found for {district}", "dropping_points": []}
    return {"dropping_points": points}

@app.post("/bookings", response_model=BookingResponse)
def create_booking_endpoint(booking: BookingCreate):
    provider_exists = any(p["name"].lower() == booking.bus_provider.lower() for p in bus_data["bus_providers"])
    if not provider_exists:
        raise HTTPException(status_code=400, detail=f"Bus provider '{booking.bus_provider}' not found")
    if not validate_route(booking.bus_provider, booking.from_district, booking.to_district):
        raise HTTPException(status_code=400, detail=f"{booking.bus_provider} does not operate on this route")
    option = choose_route_option({
        "from_district": booking.from_district,
        "to_district": booking.to_district,
        "bus_provider": booking.bus_provider,
        "bus_type": booking.bus_type,
    })
    if not option:
        raise HTTPException(status_code=400, detail=f"No scheduled service found for {booking.bus_provider} on this route")
    base_fare = option["fare"]
    fare, holiday_context = apply_holiday_fare(
        base_fare,
        booking.travel_date,
        booking.bus_provider or option["provider"]
    )
    if fare == 0:
        raise HTTPException(status_code=400, detail=f"Fare not found for {booking.bus_provider} on this route")
    departure_time = booking.departure_time or (option.get("departure_times") or [None])[0]
    seats, seat_error = assign_seats(
        booking.num_passengers,
        get_booked_seats(
            option["provider"],
            booking.from_district,
            booking.to_district,
            booking.travel_date,
            booking.bus_type or option.get("bus_type"),
            departure_time
        ),
        booking.selected_seats
    )
    if seat_error:
        raise HTTPException(status_code=400, detail=seat_error)
    new_booking_data = {
        "booking_id": generate_booking_id(),
        "name": booking.name,
        "phone": booking.phone.strip(),
        "bus_provider": option["provider"],
        "from_district": booking.from_district,
        "to_district": booking.to_district,
        "dropping_point": booking.dropping_point,
        "travel_date": booking.travel_date,
        "num_passengers": booking.num_passengers,
        "fare": fare,
        "total_amount": fare * booking.num_passengers,
        "departure_time": departure_time,
        "bus_type": booking.bus_type or option.get("bus_type"),
        "seat_numbers": ", ".join(seats),
        "service_details": booking.service_details or json.dumps({
            "distance_km": option.get("distance_km"),
            "avg_duration_hours": option.get("avg_duration_hours"),
            "available_departure_times": option.get("departure_times", []),
            "base_fare": base_fare,
            "holiday_context": holiday_context,
        }),
        "booking_date": datetime.now().isoformat(),
        "status": "payment_pending",
        "payment_status": "pending",
        "payment_expires_at": payment_deadline(),
    }
    saved_booking = create_booking(new_booking_data)
    return saved_booking

@app.get("/bookings")
def list_all_bookings():
    expire_payment_pending_bookings()
    return {"bookings": get_all_bookings()}

@app.get("/bookings/phone/{phone}")
def bookings_by_phone(phone: str):
    bookings = get_bookings_by_phone(phone.strip())
    if not bookings:
        raise HTTPException(status_code=404, detail="No bookings found for this phone number")
    return {"bookings": bookings}

@app.get("/bookings/{booking_id}")
def booking_details(booking_id: str):
    booking = get_booking_by_id(booking_id)
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")
    return booking

@app.get("/tickets/{booking_id}.pdf")
def download_ticket_pdf(booking_id: str):
    booking = get_booking_by_id(booking_id)
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")
    if booking.get("payment_status") != "paid" and booking.get("status") == "payment_pending":
        raise HTTPException(status_code=400, detail="Ticket PDF is available after payment.")
    pdf = build_ticket_pdf(booking)
    filename = f"BusGo-{booking_id}.pdf"
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )

@app.delete("/bookings/{booking_id}")
def delete_booking_endpoint(booking_id: str, permanent: Optional[bool] = False):
    if permanent:
        success = delete_booking_permanently(booking_id)
        if success:
            return {"message": f"Booking {booking_id} deleted permanently."}
        raise HTTPException(status_code=404, detail="Booking not found")
    else:
        booking = get_booking_by_id(booking_id)
        if not booking:
            raise HTTPException(status_code=404, detail="Booking not found")
        success = cancel_booking(booking_id)
        if success:
            return {"message": cancellation_message_for_booking(booking)}
        raise HTTPException(status_code=404, detail="Booking not found")

@app.post("/query/smart")
def query_smart(request: QueryRequest, auth_token: Optional[str] = Cookie(None, alias=AUTH_COOKIE_NAME)):
    session_id = request.session_id or str(uuid.uuid4())
    if session_id not in sessions:
        sessions[session_id] = {
            "awaiting_booking_id": False,
            "awaiting_phone_for_cancel": False,
            "awaiting_phone_for_lookup": False,
            "pending_bookings": [],
            "phone": None,
            "booking_draft": None,
            "awaiting_booking_field": None,
            "awaiting_seat_selection": False,
            "awaiting_departure_time_selection": False,
            "awaiting_booking_confirmation_after_time": False,
            "language_style": "english",
            "pending_account_offer": None,
            "pending_payment_booking_id": None,
            "last_cancelled_booking": None
        }
    session = sessions[session_id]
    query_text = request.query.strip()
    query_lower = query_text.lower()

    save_chat_message(session_id, "user", request.query, request.phone)
    chat_history_for_intent = get_chat_history(session_id, limit=10)
    conversation_for_intent = "\n".join(
        f"{item.get('role')}: {item.get('message')}"
        for item in chat_history_for_intent
    )
    current_user = get_current_user(auth_token)
    if current_user:
        session["phone"] = current_user.get("phone")

    if session.get("pending_account_offer"):
        if current_user:
            session["pending_account_offer"] = None
        elif wants_account_signup(query_text):
            message = (
                "Thik ache, account create korte shudhu ekta password din."
                if is_banglish(conversation_for_intent)
                else "Sure, enter a password and I will create the account with your booking name and phone."
            )
            save_chat_message(session_id, "assistant", message, session["pending_account_offer"].get("phone"))
            return {
                "message": message,
                "session_id": session_id,
                "account_signup": chat_account_signup_payload(session)
            }
        elif rejects_account_signup(query_text):
            session["pending_account_offer"] = None
            message = (
                "Thik ache, kono problem nei. Booking ID ta save kore rakhben."
                if is_banglish(conversation_for_intent)
                else "No problem. Please keep the booking ID for lookup or cancellation."
            )
            save_chat_message(session_id, "assistant", message, session.get("phone"))
            return {"message": message, "session_id": session_id}

    if is_refund_followup(query_text) and session.get("last_cancelled_booking"):
        booking = session["last_cancelled_booking"]
        message = refund_followup_message(booking, conversation_for_intent)
        save_chat_message(session_id, "assistant", message, booking.get("phone") or session.get("phone"))
        return {"message": message, "session_id": session_id}

    planner_draft = (session.get("booking_draft") or {}).copy()
    if current_user:
        if not planner_draft.get("name"):
            planner_draft["name"] = current_user.get("name")
        if not planner_draft.get("phone"):
            planner_draft["phone"] = current_user.get("phone")
    elif request.phone:
        if not planner_draft.get("phone"):
            planner_draft["phone"] = normalize_phone(request.phone)

    planner = plan_chat_turn_with_llm(query_text, conversation_for_intent, planner_draft)
    planner_slots = planner.get("slots") or {}
    planner_missing_fields = planner.get("missing_fields") or []
    planner_intent = planner.get("intent", "other")
    planner_next_action = planner.get("next_action", "")

    has_booking_intent = (
        planner_intent in {"booking", "booking_continue"}
        or planner_next_action in {"ask_missing_fields", "show_seat_selection"}
    )
    info_keywords = [
        "price", "fare", "cost", "cheap", "cheapest", "rate",
        "policy", "refund", "return", "contact", "information", "info", "details", "route",
        "time", "kokhon", "schedule", "departure", "ac bus", "non-ac", "non ac"
    ]
    has_info_intent = (
        planner_intent in {"booking_info", "route_info", "policy_info"}
        or any(k in query_lower for k in info_keywords)
    )
    has_lookup_intent = planner_intent == "lookup"
    has_cancel_intent = planner_intent == "cancel" or any(k in query_lower for k in ["cancel", "cancellation"])

    if session.get("awaiting_departure_time_selection") and session.get("booking_draft"):
        draft = session["booking_draft"]
        option = choose_route_option(draft)
        available_times = option.get("departure_times", []) if option else []
        chosen_time = parse_departure_time_choice(query_text, available_times)
        if not chosen_time:
            chosen_time = resolve_departure_time_with_llm(
                query_text,
                available_times,
                conversation_for_intent
            )

        if chosen_time:
            draft["departure_time"] = chosen_time
            session["booking_draft"] = draft
            session["awaiting_departure_time_selection"] = False
            session["awaiting_booking_confirmation_after_time"] = True
            context = get_booking_context(draft)
            provider = context.get("bus_provider") or draft.get("bus_provider")
            bus_type = context.get("bus_type") or draft.get("bus_type")
            fare = context.get("fare") or draft.get("fare")
            message = (
                f"Sir, {chosen_time} time select kora holo. {provider} {bus_type} fare {fare} Taka per seat. Ei time-e booking korte chan?"
                if is_banglish(conversation_for_intent)
                else f"Sir, {chosen_time} has been selected. {provider} {bus_type} fare is {fare} Taka per seat. Would you like to book this time?"
            )
            save_chat_message(session_id, "assistant", message, draft.get("phone"))
            return {"message": message, "session_id": session_id}

        info_or_service_change = (
            planner_intent in {"booking_info", "route_info"}
            or planner_next_action == "answer_info"
            or bool(planner_slots.get("bus_type"))
            or bool(planner_slots.get("bus_provider"))
        )
        if info_or_service_change:
            info_draft = draft.copy()
            apply_booking_slots(info_draft, planner_slots)
            update_booking_draft_from_text(info_draft, query_text)
            message = generate_route_options_info_message(info_draft, query_text, conversation_for_intent)
            session["booking_draft"] = info_draft
            session["awaiting_departure_time_selection"] = True
            session["awaiting_booking_confirmation_after_time"] = False
            session["awaiting_booking_field"] = None
            save_chat_message(session_id, "assistant", message, info_draft.get("phone"))
            return {"message": message, "session_id": session_id}

        message = generate_departure_time_selection_response(
            draft,
            query_text,
            available_times,
            conversation_for_intent
        )
        save_chat_message(session_id, "assistant", message, draft.get("phone"))
        return {"message": message, "session_id": session_id}

    if session.get("awaiting_booking_confirmation_after_time") and session.get("booking_draft"):
        draft = session["booking_draft"]
        if is_negative(query_text):
            session["awaiting_booking_confirmation_after_time"] = False
            session["awaiting_departure_time_selection"] = True
            message = (
                "Thik ache Sir, tahole onno ekta time select korun."
                if is_banglish(conversation_for_intent)
                else "Okay Sir, please choose another departure time."
            )
            save_chat_message(session_id, "assistant", message, draft.get("phone"))
            return {"message": message, "session_id": session_id}
        has_new_booking_slots = any(
            value not in {None, "", "null", "none", "unknown"}
            for value in planner_slots.values()
        )
        if is_affirmative(query_text) or has_new_booking_slots:
            previous_draft = draft.copy()
            apply_booking_slots(draft, planner_slots)
            preserve_service_constraints(draft, previous_draft, query_text)
            update_booking_draft_from_text(draft, query_text)
            preserve_service_constraints(draft, previous_draft, query_text)
            session["awaiting_booking_confirmation_after_time"] = False
            missing_fields = get_missing_booking_fields(draft)
            for field in planner_missing_fields:
                if field not in missing_fields:
                    missing_fields.append(field)
            for field in verify_booking_ready_with_llm(draft, conversation_for_intent):
                if field not in missing_fields:
                    missing_fields.append(field)
            if "departure_time" in missing_fields and not draft.get("departure_time"):
                session["booking_draft"] = draft
                session["awaiting_departure_time_selection"] = True
                session["awaiting_booking_field"] = None
                message = generate_service_recommendation_message(draft, conversation_for_intent)
                save_chat_message(session_id, "assistant", message, draft.get("phone"))
                return {"message": message, "session_id": session_id}
            if missing_fields:
                session["booking_draft"] = draft
                session["awaiting_booking_field"] = missing_fields[0]
                message = generate_booking_followup_message(draft, missing_fields, conversation_for_intent)
                save_chat_message(session_id, "assistant", message, draft.get("phone"))
                return {"message": message, "session_id": session_id}
            session["booking_draft"] = draft
            session["awaiting_booking_field"] = None
            session["awaiting_seat_selection"] = True
            seat_selection = get_seat_selection_payload(draft)
            message = generate_seat_selection_message(draft, conversation_for_intent)
            save_chat_message(session_id, "assistant", message, draft.get("phone"))
            return {"message": message, "session_id": session_id, "seat_selection": seat_selection}
        else:
            message = (
                "Sir, booking korte chaile yes bolun, na hole onno time select korun."
                if is_banglish(conversation_for_intent)
                else "Sir, say yes if you want to book this time, or choose another time."
            )
            save_chat_message(session_id, "assistant", message, draft.get("phone"))
            return {"message": message, "session_id": session_id}

    if session.get("awaiting_seat_selection") and session.get("booking_draft"):
        seats = parse_seat_numbers(query_text)
        if seats:
            draft = session["booking_draft"]
            draft["selected_seats"] = seats
            booking, error = create_booking_from_draft(draft)
            if error:
                message = error
                seat_selection = get_seat_selection_payload(draft)
                save_chat_message(session_id, "assistant", message, draft.get("phone"))
                return {"message": message, "session_id": session_id, "seat_selection": seat_selection}

            session["booking_draft"] = None
            session["awaiting_booking_field"] = None
            session["awaiting_seat_selection"] = False
            session["phone"] = booking["phone"]
            session["pending_payment_booking_id"] = booking["booking_id"]
            message = generate_payment_pending_message(booking, conversation_for_intent)
            save_chat_message(session_id, "assistant", message, booking["phone"])
            return {
                "message": message,
                "session_id": session_id,
                "payment_required": build_payment_payload(booking)
            }

        seat_selection = get_seat_selection_payload(session["booking_draft"])
        message = generate_seat_selection_message(session["booking_draft"], conversation_for_intent)
        save_chat_message(session_id, "assistant", message, session["booking_draft"].get("phone"))
        return {"message": message, "session_id": session_id, "seat_selection": seat_selection}

    if session.get("awaiting_phone_for_lookup"):
        phone = detect_phone(query_text)
        if phone:
            session["awaiting_phone_for_lookup"] = False
            session["phone"] = phone
            bookings = get_bookings_by_phone(phone)
            message = generate_booking_lookup_message(bookings, phone, conversation_for_intent)
            save_chat_message(session_id, "assistant", message, phone)
            return {"message": message, "session_id": session_id}

    if has_lookup_intent and not has_cancel_intent:
        phone = detect_phone(query_text) or request.phone or session.get("phone")
        if not phone:
            session["awaiting_phone_for_lookup"] = True
            message = (
                "Apnar booking check korte phone number ta din."
                if is_banglish(conversation_for_intent)
                else "Please provide the phone number you used for the booking."
            )
            save_chat_message(session_id, "assistant", message, None)
            return {"message": message, "session_id": session_id}

        phone = normalize_phone(phone)
        session["phone"] = phone
        bookings = get_bookings_by_phone(phone)
        message = generate_booking_lookup_message(bookings, phone, conversation_for_intent)
        save_chat_message(session_id, "assistant", message, phone)
        return {"message": message, "session_id": session_id}

    if session.get("booking_draft"):
        draft = session["booking_draft"]
        previous_draft = draft.copy()
        awaited_field = session.get("awaiting_booking_field")
        chat_history = get_chat_history(session_id, limit=10)
        conversation_text = "\n".join(f"{item.get('role')}: {item.get('message')}" for item in chat_history)
        apply_booking_slots(draft, planner_slots)
        preserve_service_constraints(draft, previous_draft, query_text)

        if has_info_intent and not has_booking_intent:
            update_booking_draft_from_text(draft, query_text)
            message = generate_booking_info_message(draft, request.query, conversation_text)
            save_chat_message(session_id, "assistant", message, draft.get("phone"))
            return {"message": message, "session_id": session_id}

        if awaited_field == "name":
            detected_name = detect_name(query_text)
            if detected_name:
                draft["name"] = detected_name
            elif is_name_only_response(query_text):
                draft["name"] = query_text
        elif awaited_field == "phone":
            detected_phone = detect_phone(query_text)
            if detected_phone:
                draft["phone"] = detected_phone
        elif awaited_field == "num_passengers":
            try:
                passengers = int(re.search(r"\d+", query_text).group(0))
                if 1 <= passengers <= 10:
                    draft["num_passengers"] = passengers
            except (AttributeError, ValueError):
                pass
        else:
            update_booking_draft_from_text(draft, query_text)

        update_booking_draft_from_text(draft, query_text)
        apply_preferred_route_option(draft)

        missing_fields = get_missing_booking_fields(draft)
        if should_show_service_recommendation(draft, query_text):
            session["booking_draft"] = draft
            session["awaiting_departure_time_selection"] = True
            session["awaiting_booking_confirmation_after_time"] = False
            session["awaiting_booking_field"] = None
            message = generate_service_recommendation_message(draft, conversation_text)
            save_chat_message(session_id, "assistant", message, draft.get("phone"))
            return {"message": message, "session_id": session_id}

        for field in planner_missing_fields:
            if field not in missing_fields:
                missing_fields.append(field)
        for field in verify_booking_ready_with_llm(draft, conversation_text):
            if field not in missing_fields:
                missing_fields.append(field)
        if "departure_time" in missing_fields and not draft.get("departure_time"):
            session["booking_draft"] = draft
            session["awaiting_departure_time_selection"] = True
            session["awaiting_booking_confirmation_after_time"] = False
            session["awaiting_booking_field"] = None
            message = generate_service_recommendation_message(draft, conversation_text)
            save_chat_message(session_id, "assistant", message, draft.get("phone"))
            return {"message": message, "session_id": session_id}
        if missing_fields:
            session["awaiting_booking_field"] = missing_fields[0]
            message = generate_booking_followup_message(draft, missing_fields, conversation_text)
            save_chat_message(session_id, "assistant", message, draft.get("phone"))
            return {"message": message, "session_id": session_id}

        session["booking_draft"] = draft
        session["awaiting_booking_field"] = None
        session["awaiting_seat_selection"] = True
        seat_selection = get_seat_selection_payload(draft)
        message = generate_seat_selection_message(draft, conversation_text)
        save_chat_message(session_id, "assistant", message, draft.get("phone"))
        return {"message": message, "session_id": session_id, "seat_selection": seat_selection}

    if session.get("awaiting_phone_for_cancel"):
        phone = query_text
        if phone.startswith("+88"):
            phone = phone[3:]
        session["phone"] = phone
        session["awaiting_phone_for_cancel"] = False
        bookings = get_bookings_by_phone(phone)
        active_bookings = [b for b in bookings if b['status'] == 'active']
        if not active_bookings:
            message = f"No active bookings found for phone number {phone}"
            save_chat_message(session_id, "assistant", message, phone)
            return {"message": message, "session_id": session_id}
        if len(active_bookings) == 1:
            booking = active_bookings[0]
            cancel_booking(booking['booking_id'])
            session["last_cancelled_booking"] = booking
            message = cancellation_message_for_booking(booking, conversation_for_intent)
            save_chat_message(session_id, "assistant", message, phone)
            return {"message": message, "session_id": session_id}
        session["awaiting_booking_id"] = True
        session["pending_bookings"] = active_bookings
        message = "You have multiple active bookings:\n"
        for b in active_bookings:
            message += f"{b['from_district']} to {b['to_district']} on {b['travel_date']} (ID: {b['booking_id']})\n"
        message += "\nPlease provide the Booking ID you want to cancel."
        save_chat_message(session_id, "assistant", message, phone)
        return {"message": message, "session_id": session_id}

    if session["awaiting_booking_id"]:
        booking_id = query_text
        booking = next((b for b in session["pending_bookings"] if b["booking_id"] == booking_id), None)
        if booking:
            cancel_booking(booking_id)
            session["awaiting_booking_id"] = False
            session["pending_bookings"] = []
            session["last_cancelled_booking"] = booking
            message = cancellation_message_for_booking(booking, conversation_for_intent)
            save_chat_message(session_id, "assistant", message, session.get("phone"))
            return {"message": message, "session_id": session_id}
        else:
            message = f"Booking ID {booking_id} not found. Please check again."
            save_chat_message(session_id, "assistant", message, session.get("phone"))
            return {"message": message, "session_id": session_id}

    if has_cancel_intent:
        phone = (request.phone or session.get("phone") or "").strip()
        if not phone:
            session["awaiting_phone_for_cancel"] = True
            message = "To cancel your booking, please provide your phone number."
            save_chat_message(session_id, "assistant", message, None)
            return {"message": message, "session_id": session_id}
        if phone.startswith("+88"):
            phone = phone[3:]
        session["phone"] = phone
        bookings = get_bookings_by_phone(phone)
        active_bookings = [b for b in bookings if b['status'] == 'active']
        if not active_bookings:
            message = f"No active bookings found for phone number {phone}"
            save_chat_message(session_id, "assistant", message, phone)
            return {"message": message, "session_id": session_id}
        if len(active_bookings) == 1:
            booking = active_bookings[0]
            cancel_booking(booking['booking_id'])
            session["last_cancelled_booking"] = booking
            message = cancellation_message_for_booking(booking, conversation_for_intent)
            save_chat_message(session_id, "assistant", message, phone)
            return {"message": message, "session_id": session_id}
        session["awaiting_booking_id"] = True
        session["pending_bookings"] = active_bookings
        message = "You have multiple active bookings:\n"
        for b in active_bookings:
            message += f"{b['from_district']} to {b['to_district']} on {b['travel_date']} (ID: {b['booking_id']})\n"
        message += "\nPlease provide the Booking ID you want to cancel."
        save_chat_message(session_id, "assistant", message, phone)
        return {"message": message, "session_id": session_id}

    if has_booking_intent and not has_cancel_intent:
        session["language_style"] = "banglish" if is_banglish(conversation_for_intent) else "english"
        draft = {
            "from_district": None,
            "to_district": None,
            "bus_provider": None,
            "dropping_point": None,
            "name": current_user.get("name") if current_user else None,
            "phone": current_user.get("phone") if current_user else (normalize_phone(request.phone) if request.phone else None),
            "travel_date": None,
            "num_passengers": None,
            "bus_type": None,
            "departure_time": None,
            "service_preference": None,
            "provider_explicit": False
        }

        chat_history = get_chat_history(session_id, limit=8)
        history_text = " ".join(item.get("message", "") for item in chat_history)
        update_booking_draft_from_text(draft, history_text)
        update_booking_draft_from_text(draft, query_text)
        conversation_text = "\n".join(f"{item.get('role')}: {item.get('message')}" for item in chat_history)
        previous_draft = draft.copy()
        apply_booking_slots(draft, planner_slots)
        preserve_service_constraints(draft, previous_draft, query_text)
        apply_preferred_route_option(draft)

        missing_fields = get_missing_booking_fields(draft)
        if should_show_service_recommendation(draft, query_text):
            session["booking_draft"] = draft
            session["awaiting_departure_time_selection"] = True
            session["awaiting_booking_confirmation_after_time"] = False
            session["awaiting_booking_field"] = None
            message = generate_service_recommendation_message(draft, conversation_text)
            save_chat_message(session_id, "assistant", message, draft.get("phone"))
            return {"message": message, "session_id": session_id}

        for field in planner_missing_fields:
            if field not in missing_fields:
                missing_fields.append(field)
        for field in verify_booking_ready_with_llm(draft, conversation_text):
            if field not in missing_fields:
                missing_fields.append(field)
        if "departure_time" in missing_fields and not draft.get("departure_time"):
            session["booking_draft"] = draft
            session["awaiting_departure_time_selection"] = True
            session["awaiting_booking_confirmation_after_time"] = False
            session["awaiting_booking_field"] = None
            message = generate_service_recommendation_message(draft, conversation_text)
            save_chat_message(session_id, "assistant", message, draft.get("phone"))
            return {"message": message, "session_id": session_id}
        if missing_fields:
            session["booking_draft"] = draft
            session["awaiting_booking_field"] = missing_fields[0]
            message = generate_booking_followup_message(draft, missing_fields, conversation_text)
            save_chat_message(session_id, "assistant", message, draft.get("phone"))
            return {"message": message, "session_id": session_id}

        session["booking_draft"] = draft
        session["awaiting_booking_field"] = None
        session["awaiting_seat_selection"] = True
        seat_selection = get_seat_selection_payload(draft)
        message = generate_seat_selection_message(draft, conversation_text)
        save_chat_message(session_id, "assistant", message, draft.get("phone"))
        return {"message": message, "session_id": session_id, "seat_selection": seat_selection}

    chat_history = get_chat_history(session_id, limit=8)
    try:
        answer = get_answer(request.query, chat_history=chat_history)
    except Exception as exc:
        answer = format_ai_error(exc)
    save_chat_message(session_id, "assistant", answer, session.get("phone"))
    return {"message": answer, "session_id": session_id}

@app.post("/chat/confirm-seat-booking")
def confirm_chat_seat_booking(request: ChatSeatConfirmRequest, auth_token: Optional[str] = Cookie(None, alias=AUTH_COOKIE_NAME)):
    session = sessions.get(request.session_id)
    if not session or not session.get("booking_draft") or not session.get("awaiting_seat_selection"):
        raise HTTPException(status_code=400, detail="No booking is waiting for seat selection")

    draft = session["booking_draft"]
    draft["selected_seats"] = request.selected_seats
    booking, error = create_booking_from_draft(draft)
    if error:
        seat_selection = get_seat_selection_payload(draft)
        return {
            "message": error,
            "session_id": request.session_id,
            "seat_selection": seat_selection
        }

    session["booking_draft"] = None
    session["awaiting_booking_field"] = None
    session["awaiting_seat_selection"] = False
    session["phone"] = booking["phone"]
    session["pending_payment_booking_id"] = booking["booking_id"]

    chat_history = get_chat_history(request.session_id, limit=10)
    conversation_text = "\n".join(f"{item.get('role')}: {item.get('message')}" for item in chat_history)
    message = generate_payment_pending_message(booking, conversation_text)
    save_chat_message(request.session_id, "assistant", message, booking["phone"])
    return {
        "message": message,
        "session_id": request.session_id,
        "payment_required": build_payment_payload(booking)
    }

@app.post("/query/detailed")
def query_rag_with_sources(request: QueryRequest):
    chat_history = get_chat_history(request.session_id, limit=8) if request.session_id else None
    try:
        return get_answer_with_sources(request.query, chat_history=chat_history)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=format_ai_error(exc))

@app.post("/chat/clear")
def clear_chat(session_id: str):
    if session_id in sessions:
        sessions[session_id] = {
            "awaiting_booking_id": False,
            "awaiting_phone_for_cancel": False,
            "awaiting_phone_for_lookup": False,
            "pending_bookings": [],
            "phone": None,
            "booking_draft": None,
            "awaiting_booking_field": None,
            "awaiting_seat_selection": False,
            "awaiting_departure_time_selection": False,
            "awaiting_booking_confirmation_after_time": False,
            "language_style": "english",
            "pending_account_offer": None,
            "pending_payment_booking_id": None,
            "last_cancelled_booking": None
        }
    return {"message": "Chat cleared"}

@app.get("/stats")
def stats():
    return get_booking_statistics()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
