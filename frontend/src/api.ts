import Constants from "expo-constants";

import { storage } from "@/src/utils/storage";

const configuredUrl = Constants.expoConfig?.extra?.backendUrl;
// EXPO_PUBLIC_BACKEND_URL is the protected Expo workspace contract; EXPO_BACKEND_URL
// remains an explicit compatibility alias for externally-run mobile builds.
export const BACKEND_URL = String(configuredUrl || process.env.EXPO_PUBLIC_BACKEND_URL || process.env.EXPO_BACKEND_URL || "").replace(/\/$/, "");
export const TOKEN_KEY = "pahel_auth_token";

export async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const token = await storage.secureGet<string | null>(TOKEN_KEY, null);
  const headers = new Headers(options.headers);
  headers.set("Content-Type", "application/json");
  if (token) headers.set("Authorization", `Bearer ${token}`);
  const response = await fetch(`${BACKEND_URL}/api${path}`, { ...options, headers });
  const body = await response.json().catch(() => ({ success: false, message: "Network response was not readable" }));
  if (!response.ok || body.success === false) throw new Error(body.message || "Something went wrong");
  return body.data as T;
}

export async function saveToken(token: string) {
  await storage.secureSet(TOKEN_KEY, token);
}

export async function clearToken() {
  await storage.secureRemove(TOKEN_KEY);
}