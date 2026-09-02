# PAHEL FOUNDATION

PAHEL FOUNDATION is a full-stack emergency response and community support system with three separated applications:

```text
backend/   FastAPI + MongoDB APIs, auth, geospatial SOS, payments/provider adapters
frontend/  Expo React Native mobile app (the configured mobile workspace)
admin/     React/Vite responsive administrator panel
```

## Quick start

### Backend

```bash
cd backend
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python seed.py
uvicorn server:app --host 0.0.0.0 --port 8001
```

### Mobile

```bash
cd frontend
yarn install
npx expo start
```

The app uses `EXPO_PUBLIC_BACKEND_URL` from the existing mobile environment. It requests location and notification access after sign-in, refreshes location on app open, and stores its JWT in secure storage.

### Admin

```bash
cd admin
npm install
cp .env.example .env
npm run dev
```

The single administrator is seeded from backend `ADMIN_EMAIL` and `ADMIN_PASSWORD`. There is no admin registration route.

## Provider configuration

APITXT is configured only in `backend/.env`; it is never bundled into the mobile app. Razorpay, Firebase/FCM, and WhatsApp are intentionally configurable and remain unavailable/development fallbacks until their provider credentials are added. The development OTP is explicitly returned as `123456` only when the provider is unavailable outside production.

The payment fallback records development donations using `mock_payment`; it must be replaced by verified Razorpay checkout before real money is accepted. Push delivery uses registered Expo tokens when available, while WhatsApp remains disabled unless enabled in backend configuration.

## Core API areas

- `/api/auth/*` mobile OTP and JWT sessions
- `/api/users/*` profile, emergency contact, location, and device tokens
- `/api/sos/*` emergency records, nearby search, history, cancellation
- `/api/blood-requests/*` requests and donor responses
- `/api/volunteers/*`, `/api/plans`, `/api/donations/*`, `/api/subscriptions/*`
- `/api/notifications`, `/api/settings/contact`
- `/api/admin/*` protected dashboard, moderation, plans, broadcasts, and settings

## Testing

Use the development admin and OTP credentials in `/app/memory/test_credentials.md`. MongoDB must be running for persistent records and geospatial indexes.