import * as Notifications from "expo-notifications";
import { Platform } from "react-native";

import { api } from "@/src/api";

/**
 * Registers the device's native FCM/APNs token with the backend, which
 * forwards it to the Emergent push relay. Never throws — push registration
 * is best-effort and should not block auth/permission flows.
 *
 * Web is skipped entirely because the native module is unavailable there.
 */
export async function registerPushDevice(): Promise<{ status: string; token?: string }> {
  if (Platform.OS === "web") return { status: "unsupported" };
  try {
    const permission = await Notifications.getPermissionsAsync();
    let status = permission.status;
    if (status !== "granted") {
      const requested = await Notifications.requestPermissionsAsync();
      status = requested.status;
    }
    if (status !== "granted") return { status: "denied" };
    // getDevicePushTokenAsync returns the native FCM token on Android and APNs
    // token on iOS. It fails inside Expo Go (no google-services.json baked in),
    // so we swallow the error and let the app continue.
    const tokenResponse = await Notifications.getDevicePushTokenAsync();
    const token = String(tokenResponse.data);
    if (!token || token.length < 8) return { status: "no_token" };
    await api("/users/me/device", {
      method: "POST",
      body: JSON.stringify({ token, platform: Platform.OS }),
    });
    return { status: "registered", token };
  } catch {
    return { status: "unavailable" };
  }
}
