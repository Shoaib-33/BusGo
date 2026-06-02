import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional, List
import uuid
import os

PAYMENT_DATABASE_FILE = Path(os.environ.get("BUSGO_PAYMENT_DATABASE_FILE", "demo_payments.db"))


def get_payment_connection():
    conn = sqlite3.connect(PAYMENT_DATABASE_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def init_payment_database():
    conn = get_payment_connection()
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS wallet_accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            provider TEXT NOT NULL,
            phone TEXT NOT NULL,
            balance INTEGER NOT NULL,
            pin TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(provider, phone)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS payment_transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            transaction_id TEXT UNIQUE NOT NULL,
            booking_id TEXT NOT NULL,
            provider TEXT NOT NULL,
            phone TEXT NOT NULL,
            amount INTEGER NOT NULL,
            status TEXT NOT NULL,
            transaction_type TEXT NOT NULL DEFAULT 'payment',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS refund_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            refund_id TEXT UNIQUE NOT NULL,
            booking_id TEXT NOT NULL,
            payment_transaction_id TEXT,
            provider TEXT,
            phone TEXT,
            amount INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'requested',
            requested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            processed_at TEXT
        )
    """)

    seed_wallets = [
        ("bkash", "01309183295", 20000, "1234"),
        ("bkash", "01711111111", 15000, "1234"),
        ("nagad", "01309183295", 20000, "1234"),
        ("nagad", "01811111111", 15000, "1234"),
    ]
    cursor.executemany("""
        INSERT OR IGNORE INTO wallet_accounts (provider, phone, balance, pin)
        VALUES (?, ?, ?, ?)
    """, seed_wallets)

    conn.commit()
    conn.close()
    print("Demo payment database initialized")


init_payment_database()


def normalize_provider(provider: str) -> str:
    return str(provider or "").strip().lower()


def get_wallet(provider: str, phone: str) -> Optional[dict]:
    conn = get_payment_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT provider, phone, balance FROM wallet_accounts WHERE provider = ? AND phone = ?",
        (normalize_provider(provider), phone)
    )
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


def list_demo_wallets() -> List[dict]:
    conn = get_payment_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT provider, phone, balance FROM wallet_accounts ORDER BY provider, phone")
    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return rows


def list_refund_requests() -> List[dict]:
    conn = get_payment_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT refund_id, booking_id, amount, status, requested_at, processed_at
        FROM refund_requests
        ORDER BY requested_at DESC
    """)
    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return rows


def verify_and_deduct(provider: str, phone: str, amount: int, pin: str, booking_id: str):
    provider = normalize_provider(provider)
    conn = get_payment_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM wallet_accounts WHERE provider = ? AND phone = ?",
        (provider, phone)
    )
    account = cursor.fetchone()
    if not account:
        conn.close()
        return None, "No demo wallet found for this provider and phone number."
    if str(account["pin"]) != str(pin):
        conn.close()
        return None, "Invalid demo wallet PIN."
    if int(account["balance"]) < int(amount):
        conn.close()
        return None, "Insufficient demo wallet balance."

    transaction_id = "TXN-DEMO-" + uuid.uuid4().hex[:10].upper()
    cursor.execute(
        "UPDATE wallet_accounts SET balance = balance - ? WHERE id = ?",
        (int(amount), account["id"])
    )
    cursor.execute("""
        INSERT INTO payment_transactions (
            transaction_id, booking_id, provider, phone, amount, status, transaction_type
        ) VALUES (?, ?, ?, ?, ?, 'paid', 'payment')
    """, (transaction_id, booking_id, provider, phone, int(amount)))

    conn.commit()
    conn.close()
    return {
        "transaction_id": transaction_id,
        "provider": provider,
        "phone": phone,
        "amount": int(amount),
        "status": "paid",
    }, None


def get_payment_for_booking(booking_id: str) -> Optional[dict]:
    conn = get_payment_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT * FROM payment_transactions
        WHERE booking_id = ? AND transaction_type = 'payment' AND status = 'paid'
        ORDER BY created_at DESC
        LIMIT 1
    """, (booking_id,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


def get_refund_request_for_booking(booking_id: str) -> Optional[dict]:
    conn = get_payment_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT refund_id, booking_id, amount, status, requested_at, processed_at
        FROM refund_requests
        WHERE booking_id = ?
        ORDER BY requested_at DESC
        LIMIT 1
    """, (booking_id,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


def create_refund_request(booking_id: str, amount: int, payment: Optional[dict]):
    existing = get_refund_request_for_booking(booking_id)
    if existing and existing.get("status") in {"requested", "approved"}:
        return existing

    refund_id = "RF-DEMO-" + uuid.uuid4().hex[:10].upper()
    conn = get_payment_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO refund_requests (
            refund_id, booking_id, payment_transaction_id, provider, phone, amount, status
        ) VALUES (?, ?, ?, ?, ?, ?, 'requested')
    """, (
        refund_id,
        booking_id,
        payment.get("transaction_id") if payment else None,
        payment.get("provider") if payment else None,
        payment.get("phone") if payment else None,
        int(amount),
    ))
    conn.commit()
    conn.close()
    return {
        "refund_id": refund_id,
        "booking_id": booking_id,
        "amount": int(amount),
        "status": "requested",
    }


def approve_refund(refund_id: str):
    conn = get_payment_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM refund_requests WHERE refund_id = ?", (refund_id,))
    refund = cursor.fetchone()
    if not refund:
        conn.close()
        return None, "Refund request not found."
    if refund["status"] != "requested":
        conn.close()
        return None, "Refund request is not pending."
    if not refund["provider"] or not refund["phone"]:
        conn.close()
        return None, "Refund wallet details are missing."

    cursor.execute("""
        UPDATE wallet_accounts
        SET balance = balance + ?
        WHERE provider = ? AND phone = ?
    """, (int(refund["amount"]), refund["provider"], refund["phone"]))
    transaction_id = "RF-TXN-" + uuid.uuid4().hex[:10].upper()
    cursor.execute("""
        INSERT INTO payment_transactions (
            transaction_id, booking_id, provider, phone, amount, status, transaction_type
        ) VALUES (?, ?, ?, ?, ?, 'refunded', 'refund')
    """, (
        transaction_id,
        refund["booking_id"],
        refund["provider"],
        refund["phone"],
        int(refund["amount"]),
    ))
    cursor.execute("""
        UPDATE refund_requests
        SET status = 'approved', processed_at = ?
        WHERE refund_id = ?
    """, (datetime.now().isoformat(), refund_id))

    conn.commit()
    conn.close()
    return {"refund_id": refund_id, "transaction_id": transaction_id, "status": "approved"}, None
