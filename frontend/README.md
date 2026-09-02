# PAHEL FOUNDATION Mobile

Expo SDK 54 mobile application for PAHEL FOUNDATION. The configured workspace is `/app/frontend` so the existing Expo preview and Android build pipeline remain intact.

Features include mobile OTP sign-in, required emergency contact, permission onboarding, location refresh, SOS confirmation, nearby blood support, plans, donation history, notifications, volunteer application, and profile history.

```bash
yarn install
npx expo start
```

Backend URL is read from `EXPO_PUBLIC_BACKEND_URL`. Provider secrets never belong in this app. The attached foundation logo is used in the splash, auth, header, and profile surfaces.