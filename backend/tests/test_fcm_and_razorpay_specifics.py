"""Additional targeted tests for FCM push relay refactor and live Razorpay integration.

Covers the fine-grained assertions the review request cares about:
- Device register endpoint accepts token+platform and returns relay envelope.
- SOS and blood-request payloads expose push_status and persist in-app notifications for nearby users.
- Broadcast writes exactly one notification per active user.
- Volunteer approve enqueues a VOLUNTEER notification for the applicant.
- Razorpay order creation is real (order_id starts with 'order_', key_id + provider set).
- HMAC signature verification succeeds; a fake signature is rejected.
"""
import hashlib
import hmac
import os
import uuid

import requests

BASE_URL = (os.environ.get("EXPO_PUBLIC_BACKEND_URL") or os.environ.get("EXPO_BACKEND_URL", "")).rstrip("/")
assert BASE_URL, "Backend URL is required"


def api(method, path, **kwargs):
    return requests.request(method, f"{BASE_URL}/api{path}", timeout=30, **kwargs)


def _new_user(lat=28.61, lng=77.20):
    mobile = f"9{uuid.uuid4().int % 1000000000:09d}"
    api("POST", "/auth/send-otp", json={"mobile": mobile}).raise_for_status()
    token = api("POST", "/auth/verify-otp", json={"mobile": mobile, "otp": "123456"}).json()["data"]["token"]
    headers = {"Authorization": f"Bearer {token}"}
    api("PUT", "/users/me/emergency-contact", headers=headers, json={"name": "TEST", "mobile": "9999999999"})
    api("POST", "/users/me/location", headers=headers, json={"latitude": lat, "longitude": lng})
    return headers, token


def _admin_headers():
    token = api("POST", "/admin/auth/login", json={
        "email": "admin@pahelfoundation.org", "password": "PahelAdmin#2026"
    }).json()["data"]["token"]
    return {"Authorization": f"Bearer {token}"}


# ---------- Device registration ----------
def test_device_registration_accepts_fcm_platform_and_returns_relay_status():
    headers, _ = _new_user()
    r = api("POST", "/users/me/device", headers=headers, json={"token": "fcm_test_token_1234567890", "platform": "android"})
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert "relay" in data
    # firebase-admin is initialised in this deployment -> relay must report registered + firebase-admin.
    assert data["relay"]["status"] == "registered", data["relay"]
    assert data["relay"]["provider"] == "firebase-admin", data["relay"]
    me = api("GET", "/users/me", headers=headers).json()["data"]
    assert "fcm_test_token_1234567890" in me["push_tokens"]
    assert me.get("device_platform") == "android"


# ---------- SOS push_status + in-app notifications ----------
def test_sos_create_exposes_push_status_and_writes_notifications_for_nearby_users():
    # Nearby user (created first so they exist when we send SOS)
    nearby_headers, _ = _new_user(lat=28.611, lng=77.201)
    sender_headers, _ = _new_user(lat=28.61, lng=77.20)

    sos = api("POST", "/sos/create", headers=sender_headers).json()["data"]
    assert "push_status" in sos
    assert sos["push_status"] in {"sent", "unavailable", "no_tokens", "empty", "disabled", "failed"}
    assert sos["nearby_users_notified"] >= 1

    # Nearby user should now have an SOS notification
    notif = api("GET", "/notifications", headers=nearby_headers).json()["data"]["items"]
    assert any(n["type"] == "SOS" and n["related_id"] == sos["id"] for n in notif), notif


# ---------- Blood request in-app notifications ----------
def test_blood_request_creates_notifications_for_nearby_users():
    nearby_headers, _ = _new_user(lat=28.611, lng=77.201)
    sender_headers, _ = _new_user(lat=28.61, lng=77.20)
    payload = {"patient_name": "TEST", "blood_group": "O+", "units": 1, "hospital": "TEST Hosp",
               "contact_name": "TEST", "contact_mobile": "9876543210", "details": "test",
               "latitude": 28.61, "longitude": 77.20}
    created = api("POST", "/blood-requests", headers=sender_headers, json=payload).json()["data"]
    assert "push_status" in created
    notif = api("GET", "/notifications", headers=nearby_headers).json()["data"]["items"]
    assert any(n["type"] == "BLOOD_REQUEST" and n["related_id"] == created["id"] for n in notif)


# ---------- Broadcast writes 1 notification per active user + push envelope ----------
def test_broadcast_writes_one_notification_per_user_and_returns_push_envelope():
    user_headers, _ = _new_user()
    ah = _admin_headers()
    title = f"TEST_BR_{uuid.uuid4().hex[:6]}"
    r = api("POST", "/admin/notifications/broadcast", headers=ah,
            json={"title": title, "message": "test broadcast"})
    assert r.status_code == 200
    body = r.json()["data"]
    assert body["recipients"] >= 1
    assert "push" in body and body["push"]["status"] in {"sent", "failed", "unavailable", "no_tokens", "empty"}
    # Broadcast must go through firebase-admin now (not emergent relay / expo_legacy)
    assert body["push"].get("channel") == "firebase", body["push"]
    # Recipient sees exactly one notification with that title
    notif = api("GET", "/notifications", headers=user_headers).json()["data"]["items"]
    matching = [n for n in notif if n["title"] == title]
    assert len(matching) == 1, matching


# ---------- Volunteer approve writes a VOLUNTEER notification ----------
def test_volunteer_approve_writes_notification_for_applicant():
    headers, _ = _new_user()
    application_id = api("POST", "/volunteers/apply", headers=headers,
                         json={"full_name": "TEST Vol", "reason": "Testing volunteer approval notification path"}
                         ).json()["data"]["id"]
    ah = _admin_headers()
    approve = api("POST", f"/admin/volunteers/{application_id}/approve", headers=ah)
    assert approve.status_code == 200
    notif = api("GET", "/notifications", headers=headers).json()["data"]["items"]
    assert any(n["type"] == "VOLUNTEER" and n["related_id"] == application_id for n in notif)


# ---------- Razorpay live order + HMAC signature ----------
def test_donation_order_is_real_razorpay_and_hmac_signature_flow():
    headers, _ = _new_user()
    order = api("POST", "/donations/create-order", headers=headers,
                json={"amount": 100, "purpose": "TEST_signed"}).json()["data"]
    assert order["provider"] == "razorpay"
    assert order["is_development_fallback"] is False
    assert order["order_id"].startswith("order_") and not order["order_id"].startswith("order_dev_")
    assert order["key_id"] == "rzp_live_RuAmqyoj9yIDOP"

    # Fake signature rejected
    bad = api("POST", "/donations/verify", headers=headers,
              json={"order_id": order["order_id"], "payment_id": "pay_fake", "signature": "deadbeef"})
    assert bad.status_code == 400

    # Correct HMAC accepted
    secret = os.environ["RAZORPAY_KEY_SECRET"]
    payment_id = f"pay_test_{uuid.uuid4().hex[:10]}"
    sig = hmac.new(secret.encode(), f"{order['order_id']}|{payment_id}".encode(), hashlib.sha256).hexdigest()
    ok_resp = api("POST", "/donations/verify", headers=headers,
                  json={"order_id": order["order_id"], "payment_id": payment_id, "signature": sig})
    assert ok_resp.status_code == 200, ok_resp.text


def test_subscription_order_is_real_razorpay():
    headers, _ = _new_user()
    order = api("POST", "/subscriptions/create-order", headers=headers,
                params={"plan_id": "plan-monthly-support"}).json()["data"]
    assert order["provider"] == "razorpay"
    assert order["is_development_fallback"] is False
    assert order["order_id"].startswith("order_") and not order["order_id"].startswith("order_dev_")
    assert order["key_id"] == "rzp_live_RuAmqyoj9yIDOP"


# ---------- Firebase Admin SDK broadcast with fake tokens ----------
def test_broadcast_with_fake_fcm_tokens_uses_firebase_channel_and_reports_failed():
    """Register 2-3 fake FCM tokens then broadcast. Firebase will legitimately reject them,
    so push.channel must be 'firebase' and failed>0, sent==0, endpoint must NOT crash."""
    headers, _ = _new_user()
    for i in range(3):
        r = api("POST", "/users/me/device", headers=headers,
                json={"token": f"test-fcm-token-{uuid.uuid4().hex[:12]}", "platform": "android"})
        assert r.status_code == 200
        assert r.json()["data"]["relay"]["provider"] == "firebase-admin"

    ah = _admin_headers()
    br = api("POST", "/admin/notifications/broadcast", headers=ah,
             json={"title": f"TEST_FB_{uuid.uuid4().hex[:6]}", "message": "fake token firebase test"})
    assert br.status_code == 200, br.text
    push = br.json()["data"]["push"]
    assert push["channel"] == "firebase", push
    # Fake tokens rejected -> failed>0 and sent==0. Endpoint must not crash.
    assert push["status"] in {"sent", "failed"}
    assert push.get("failed", 0) >= 1, push
    assert push.get("sent", 0) == 0, push


# ---------- Emergent relay path must be fully removed ----------
def test_no_emergent_relay_references_in_server():
    """Confirm server.py no longer imports or calls integrations.emergentagent.com push relay."""
    with open("/app/backend/server.py") as f:
        src = f.read()
    assert "integrations.emergentagent.com" not in src, "Emergent push relay URL still referenced"
    # send_push must use firebase-admin
    assert "send_each_for_multicast" in src, "send_push not using firebase-admin messaging"
