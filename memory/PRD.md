# PAHEL FOUNDATION — Product Requirements Document

## What the app is
A three-surface community-safety platform for PAHEL FOUNDATION:
1. **Expo mobile app** — the front line for members: OTP login, SOS with live location, blood requests, donations, plans, volunteering, notifications.
2. **FastAPI + MongoDB backend** — all business logic, geospatial queries (2dsphere), OTP, JWT, Razorpay signature verification, WhatsApp bridge, FCM/Expo push, SOS WebSocket hub.
3. **Admin panel** — currently mounted at Expo route `/admin` so it is testable in the shared preview; can be lifted to a standalone Vite/React web app when the user is ready to deploy separately.

## Live integrations
- **Razorpay (LIVE)** — Python `razorpay` SDK creates real orders (`order_...`) with `rzp_live_RuAmqyoj9yIDOP`; both donations and subscriptions require a valid HMAC-SHA256 signature on `/verify`. Mobile uses `react-native-razorpay@3.0.0` for the native Android checkout; Web/Expo Go fall back to the backend's `mock_payment` path.
- **WhatsApp bridge** — GET `whatsappmessage.parsiyacricket.com/send` called only for the SOS user's saved emergency contact.
- **APITXT OTP** — configured with the auth key; `development_otp=123456` remains as the preview fallback.
- **Push (FCM via Emergent relay + Firebase)** — `google-services.json` committed at `/app/frontend/google-services.json` and wired via `app.json → android.googleServicesFile`. Backend relays device tokens to `integrations.emergentagent.com/api/v1/push/users/register` and pushes via `/api/v1/push/trigger`. `EMERGENT_PUSH_KEY` stays as `placeholder` in preview and is auto-swapped at deployment time. All SOS, blood, volunteer, and admin-broadcast events also create in-app notification records so the mobile notifications list is populated immediately.

## Key flows
- **Auth**: `POST /api/auth/send-otp` → `POST /api/auth/verify-otp` → JWT. First-time users then set an emergency contact and grant location + notification permissions.
- **SOS**: `POST /api/sos/create` finds users within `SOS_RADIUS_KM` (default 50) via `2dsphere`, sends push notifications, WhatsApps only the sender's emergency contact, and records `nearby_users_notified` / `whatsapp_status` / `push_status`. `POST /api/sos/{id}/location` (owner-only, active-only) pushes the moving location; `POST /api/sos/{id}/cancel` closes it. Responders/admins listen on `wss://<host>/api/ws/sos/{id}?token=<jwt>` for `snapshot | location | status` frames.
- **Blood requests**: `POST /api/blood-requests` with GeoJSON storage; `GET /api/blood-requests` returns only requests within `BLOOD_RADIUS_KM`.
- **Donations / Subscriptions**: create-order → Razorpay checkout → verify. Backend rejects any request without a valid HMAC signature when Razorpay is configured.
- **Volunteers**: apply → admin `approve/reject` → user's `volunteer_status` flips to `APPROVED` and a badge shows on the profile.
- **Admin**: single seeded admin from env; dashboard, users, SOS, blood, volunteers, plans CRUD, donations, subscriptions, notification broadcast, contact settings.

## Business rules preserved from the master prompt
- One admin, seeded, no self-signup.
- Emergency contact is mandatory before SOS.
- Location + notification permissions are mandatory for emergency features.
- Nearby radius defaults to 50 KM for both SOS and blood.
- WhatsApp is only sent to the sender's emergency contact — never broadcast.
- Users can edit only their name and emergency contact; mobile number is immutable.
- Every SOS, donation, subscription, and volunteer decision is persisted as history.

## Health check
- Backend regression suite (`/app/backend/tests/test_pahel_regression.py`) — **14/14 pass** on the live preview URL.
- Admin panel `/admin` loads and every tab renders live data.
- No `shadow*` / stray-text-node RN Web warnings.
- WebSocket verified end-to-end through the preview ingress.

## Known follow-ups (not yet requested)
- Lift the admin panel back into a standalone `/app/admin` React web app for production separation.
- Real FCM (Firebase Admin SDK) once the user provides service-account JSON.
- Modularize `/app/frontend/app/index.tsx` (auth / home / blood / plans / profile) into `/app/frontend/src/screens/` files.
- SOS live-location marker on the mobile responder screen using the new WebSocket.
