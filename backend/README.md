# PAHEL FOUNDATION Backend

FastAPI + MongoDB API for mobile OTP sign-in, emergency response, blood requests, volunteers, plans, donations, subscriptions, notifications, and the admin panel.

## Run

```bash
cd backend
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python seed.py
uvicorn server:app --host 0.0.0.0 --port 8001 --reload
```

The API is available under `/api`. MongoDB uses GeoJSON `Point` values and `2dsphere` indexes for configurable 50 KM searches.

## Provider setup

APITXT credentials belong only in backend `.env`; the mobile app never receives them. The API calls `POST https://apitxt.com/api/sendOTP` with the configured key. If provider credentials are unavailable in development, OTP `123456` is returned as an explicitly labelled development fallback.

Razorpay, Firebase/Expo push, and WhatsApp are backend-only integrations. Blank credentials leave their flows safe and visible as unavailable/development fallbacks rather than falsely reporting delivery or payment success.

## Seeded admin

The single admin is created from `ADMIN_EMAIL` and `ADMIN_PASSWORD`. There is no admin signup route. Change the development defaults before real use.