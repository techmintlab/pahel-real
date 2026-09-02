"""PAHEL FOUNDATION backend regression suite (iteration 4).

Covers auth, user profile, SOS (create/cancel/location/websocket), blood requests,
volunteers, plans, donations, subscriptions, notifications, settings, and admin flows.
"""
import asyncio
import json
import os
import uuid

import pytest
import requests
import websockets

BASE_URL = os.environ.get("EXPO_PUBLIC_BACKEND_URL") or os.environ.get("EXPO_BACKEND_URL")
assert BASE_URL, "Backend URL is required"
BASE_URL = BASE_URL.rstrip("/")
WS_BASE = BASE_URL.replace("https://", "wss://").replace("http://", "ws://")


def api(method, path, **kwargs):
    return requests.request(method, f"{BASE_URL}/api{path}", timeout=30, **kwargs)


def _new_user():
    mobile = f"9{uuid.uuid4().int % 1000000000:09d}"
    sent = api("POST", "/auth/send-otp", json={"mobile": mobile})
    assert sent.status_code == 200, sent.text
    data = sent.json()["data"]
    otp = data.get("development_otp", "123456")
    verified = api("POST", "/auth/verify-otp", json={"mobile": mobile, "otp": otp})
    assert verified.status_code == 200, verified.text
    token = verified.json()["data"]["token"]
    return mobile, token


# ---------- Health ----------
def test_health_envelope():
    response = api("GET", "/")
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True and body["data"]["version"] == "1.0.0"


# ---------- Auth ----------
def test_auth_send_and_verify_otp():
    mobile, token = _new_user()
    assert token
    me = api("GET", "/users/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["data"]["mobile"] == f"+91{mobile}"


def test_auth_invalid_mobile_rejected():
    r = api("POST", "/auth/send-otp", json={"mobile": "12345"})
    assert r.status_code == 422


# ---------- Profile / emergency contact / location / device ----------
def test_profile_emergency_contact_location_and_device():
    _, token = _new_user()
    headers = {"Authorization": f"Bearer {token}"}
    # patch name
    p = api("PATCH", "/users/me", headers=headers, json={"name": "TEST User"})
    assert p.status_code == 200 and p.json()["data"]["name"] == "TEST User"
    # emergency
    c = api("PUT", "/users/me/emergency-contact", headers=headers, json={"name": "TEST Contact", "mobile": "9876543210"})
    assert c.status_code == 200 and c.json()["data"]["mobile"] == "+919876543210"
    # location
    loc = api("POST", "/users/me/location", headers=headers, json={"latitude": 28.61, "longitude": 77.20})
    assert loc.status_code == 200 and loc.json()["data"]["coordinates"] == [77.20, 28.61]
    # device
    d = api("POST", "/users/me/device", headers=headers, json={"token": "ExponentPushToken[abc12345]", "platform": "expo"})
    assert d.status_code == 200
    # GET verifies persistence
    me = api("GET", "/users/me", headers=headers).json()["data"]
    assert me["emergency_contact"]["mobile"] == "+919876543210"
    assert me["location"]["latitude"] == 28.61


# ---------- SOS ----------
def _bootstrap_active_sos():
    _, token = _new_user()
    headers = {"Authorization": f"Bearer {token}"}
    api("PUT", "/users/me/emergency-contact", headers=headers, json={"name": "TEST", "mobile": "9999999999"})
    api("POST", "/users/me/location", headers=headers, json={"latitude": 28.61, "longitude": 77.20})
    sos = api("POST", "/sos/create", headers=headers).json()["data"]
    return token, headers, sos


def test_sos_create_returns_expected_shape_and_whatsapp_status():
    token, headers, sos = _bootstrap_active_sos()
    assert sos["status"] == "ACTIVE"
    assert isinstance(sos["nearby_users_notified"], int) and sos["nearby_users_notified"] >= 0
    # WhatsApp is enabled with live bridge; accept sent or failed but not disabled/unknown
    assert sos["whatsapp_status"] in {"sent", "failed"}, sos


def test_sos_location_update_owner_only_and_active_only():
    _, headers, sos = _bootstrap_active_sos()
    sos_id = sos["id"]
    # Owner update while ACTIVE -> 200
    r = api("POST", f"/sos/{sos_id}/location", headers=headers, json={"latitude": 28.62, "longitude": 77.21})
    assert r.status_code == 200, r.text

    # Non-owner receives 404
    _, other_token = _new_user()
    other_headers = {"Authorization": f"Bearer {other_token}"}
    r2 = api("POST", f"/sos/{sos_id}/location", headers=other_headers, json={"latitude": 28.62, "longitude": 77.21})
    assert r2.status_code == 404

    # Cancel -> 409 on next location update
    c = api("POST", f"/sos/{sos_id}/cancel", headers=headers)
    assert c.status_code == 200
    r3 = api("POST", f"/sos/{sos_id}/location", headers=headers, json={"latitude": 28.63, "longitude": 77.22})
    assert r3.status_code == 409


def test_sos_websocket_requires_valid_token_and_streams_updates():
    async def run():
        _, headers, sos = _bootstrap_active_sos()
        sos_id = sos["id"]
        token = headers["Authorization"].split()[1]
        ws_path = f"{WS_BASE}/api/ws/sos/{sos_id}"
        # Missing token -> connection closed with 1008
        with pytest.raises(Exception):
            async with websockets.connect(ws_path) as ws:
                await ws.recv()
        # Invalid token -> closed
        with pytest.raises(Exception):
            async with websockets.connect(f"{ws_path}?token=abc") as ws:
                await ws.recv()
        # Valid token -> snapshot, location broadcast on REST update, status broadcast on cancel
        async with websockets.connect(f"{ws_path}?token={token}") as ws:
            snap = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
            assert snap["type"] == "snapshot" and snap["sos_id"] == sos_id
            # Trigger a REST location update from another task
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None,
                lambda: api("POST", f"/sos/{sos_id}/location", headers=headers, json={"latitude": 28.65, "longitude": 77.23}),
            )
            frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
            assert frame["type"] == "location" and frame["latitude"] == 28.65
            # Cancel triggers a status broadcast
            await loop.run_in_executor(
                None,
                lambda: api("POST", f"/sos/{sos_id}/cancel", headers=headers),
            )
            # Consume until we see a 'status' frame (backend may or may not emit
            # additional intermediate frames)
            status_frame = None
            for _ in range(3):
                try:
                    payload = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
                except Exception:
                    break
                if payload.get("type") == "status":
                    status_frame = payload
                    break
            assert status_frame is not None, "expected a status frame after cancel"
            assert status_frame.get("status") in {"CANCELLED", "RESOLVED", "CANCELED"}
    asyncio.new_event_loop().run_until_complete(run())


# ---------- Blood ----------
def test_blood_request_full_flow():
    _, token = _new_user()
    headers = {"Authorization": f"Bearer {token}"}
    api("POST", "/users/me/location", headers=headers, json={"latitude": 28.61, "longitude": 77.20})
    payload = {"patient_name": "TEST Patient", "blood_group": "O+", "units": 2, "hospital": "TEST Hospital",
               "contact_name": "TEST Kin", "contact_mobile": "9876543210", "details": "urgent",
               "latitude": 28.61, "longitude": 77.20}
    created = api("POST", "/blood-requests", headers=headers, json=payload)
    assert created.status_code == 200
    rid = created.json()["data"]["id"]
    # Detail
    d = api("GET", f"/blood-requests/{rid}", headers=headers)
    assert d.status_code == 200 and d.json()["data"]["blood_group"] == "O+"
    # History includes it
    h = api("GET", "/blood-requests/history", headers=headers).json()["data"]
    assert any(item["id"] == rid for item in h)
    # Nearby list
    l = api("GET", "/blood-requests", headers=headers)
    assert l.status_code == 200
    # Donate
    _, donor = _new_user()
    donor_headers = {"Authorization": f"Bearer {donor}"}
    api("POST", "/users/me/location", headers=donor_headers, json={"latitude": 28.611, "longitude": 77.201})
    donate = api("POST", f"/blood-requests/{rid}/donate", headers=donor_headers)
    assert donate.status_code == 200


# ---------- Volunteers ----------
def test_volunteer_apply_and_admin_review_flips_user_status():
    _, token = _new_user()
    headers = {"Authorization": f"Bearer {token}"}
    r = api("POST", "/volunteers/apply", headers=headers, json={"full_name": "TEST Vol", "reason": "Want to help my community with PAHEL activities"})
    assert r.status_code == 200
    application_id = r.json()["data"]["id"]
    status = api("GET", "/volunteers/status", headers=headers)
    assert status.status_code == 200 and status.json()["data"]["status"] == "PENDING"
    # Admin approves
    admin = api("POST", "/admin/auth/login", json={"email": "admin@pahelfoundation.org", "password": "PahelAdmin#2026"}).json()["data"]["token"]
    ah = {"Authorization": f"Bearer {admin}"}
    approve = api("POST", f"/admin/volunteers/{application_id}/approve", headers=ah)
    assert approve.status_code == 200
    me = api("GET", "/users/me", headers=headers).json()["data"]
    assert me["volunteer_status"] == "APPROVED"


# ---------- Plans / Donations / Subscriptions ----------
def test_plans_donations_and_subscription_with_live_razorpay_secret():
    _, token = _new_user()
    headers = {"Authorization": f"Bearer {token}"}
    plans = api("GET", "/plans", headers=headers)
    assert plans.status_code == 200 and len(plans.json()["data"]) >= 2
    # Donation order
    order = api("POST", "/donations/create-order", headers=headers, json={"amount": 500, "purpose": "TEST"}).json()["data"]
    assert order["provider"] == "razorpay"
    # Invalid signature must fail (Razorpay secret set)
    bad = api("POST", "/donations/verify", headers=headers, json={"order_id": order["order_id"], "payment_id": "pay_1", "signature": "invalid"})
    assert bad.status_code == 400
    # Mock payment path is disabled when secret is set
    mock = api("POST", "/donations/verify", headers=headers, json={"order_id": order["order_id"], "payment_id": "mock_payment", "signature": ""})
    assert mock.status_code == 400
    # Subscription order + invalid verify
    plan_id = plans.json()["data"][0]["id"]
    sub_order = api("POST", "/subscriptions/create-order", headers=headers, params={"plan_id": plan_id}).json()["data"]
    assert sub_order["provider"] == "razorpay"
    # Invalid signature must be rejected now that HMAC is enforced
    bad_sub = api("POST", "/subscriptions/verify", headers=headers, json={"order_id": sub_order["order_id"], "payment_id": "pay_sub_1", "signature": "invalid"})
    assert bad_sub.status_code == 400, bad_sub.text
    # mock_payment path disabled when secret configured
    mock_sub = api("POST", "/subscriptions/verify", headers=headers, json={"order_id": sub_order["order_id"], "payment_id": "mock_payment", "signature": ""})
    assert mock_sub.status_code == 400, mock_sub.text
    # A correctly computed HMAC signature must succeed
    import hmac as _hmac, hashlib as _hashlib
    secret = os.environ.get("RAZORPAY_KEY_SECRET", "")
    assert secret, "RAZORPAY_KEY_SECRET must be set in backend env for signature test"
    payment_id = f"pay_test_{uuid.uuid4().hex[:12]}"
    signature = _hmac.new(secret.encode(), f"{sub_order['order_id']}|{payment_id}".encode(), _hashlib.sha256).hexdigest()
    good_sub = api("POST", "/subscriptions/verify", headers=headers, json={"order_id": sub_order["order_id"], "payment_id": payment_id, "signature": signature})
    assert good_sub.status_code == 200, good_sub.text
    body = good_sub.json()["data"]
    assert body["status"] == "ACTIVE" and body["payment_id"] == payment_id

    # Also verify a correctly-signed donation succeeds (regression for donations/verify)
    order2 = api("POST", "/donations/create-order", headers=headers, json={"amount": 250, "purpose": "TEST_signed"}).json()["data"]
    pay2 = f"pay_test_{uuid.uuid4().hex[:12]}"
    sig2 = _hmac.new(secret.encode(), f"{order2['order_id']}|{pay2}".encode(), _hashlib.sha256).hexdigest()
    good_don = api("POST", "/donations/verify", headers=headers, json={"order_id": order2["order_id"], "payment_id": pay2, "signature": sig2})
    assert good_don.status_code == 200, good_don.text


# ---------- Notifications ----------
def test_notifications_listing():
    _, token = _new_user()
    r = api("GET", "/notifications", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert "items" in r.json()["data"]


# ---------- Settings ----------
def test_settings_contact_defaults_present():
    _, token = _new_user()
    r = api("GET", "/settings/contact", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    body = r.json()["data"]
    assert "help_number" in body


# ---------- Admin ----------
def test_admin_login_dashboard_and_lists():
    admin = api("POST", "/admin/auth/login", json={"email": "admin@pahelfoundation.org", "password": "PahelAdmin#2026"})
    assert admin.status_code == 200
    token = admin.json()["data"]["token"]
    ah = {"Authorization": f"Bearer {token}"}
    for path in ["/admin/dashboard", "/admin/users", "/admin/sos", "/admin/blood-requests", "/admin/volunteers", "/admin/plans", "/admin/donations", "/admin/subscriptions", "/admin/settings"]:
        r = api("GET", path, headers=ah)
        assert r.status_code == 200, f"{path} failed with {r.status_code}: {r.text}"
    # broadcast
    b = api("POST", "/admin/notifications/broadcast", headers=ah, json={"title": "TEST Broadcast", "message": "TEST message body"})
    assert b.status_code == 200
    assert b.json()["data"]["recipients"] >= 0
    # settings PUT
    put = api("PUT", "/admin/settings/contact", headers=ah, json={"help_number": "+91 1800 000 0000", "help_email": "help@pahelfoundation.org"})
    assert put.status_code == 200


def test_admin_rejects_user_token():
    _, token = _new_user()
    r = api("GET", "/admin/dashboard", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403
