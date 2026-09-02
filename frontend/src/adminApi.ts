import { BACKEND_URL } from "@/src/api";
import { storage } from "@/src/utils/storage";

export const ADMIN_TOKEN_KEY = "pahel_admin_token";

export async function adminApi<T>(path: string, options: RequestInit = {}, token?: string): Promise<T> {
  const activeToken = token || await storage.secureGet<string | null>(ADMIN_TOKEN_KEY, null);
  const headers = new Headers(options.headers);
  headers.set("Content-Type", "application/json");
  if (activeToken) headers.set("Authorization", `Bearer ${activeToken}`);
  const response = await fetch(`${BACKEND_URL}/api${path}`, { ...options, headers });
  const body = await response.json().catch(() => ({ success: false, message: "Response unavailable" }));
  if (!response.ok || body.success === false) throw new Error(body.message || "Admin request failed");
  return body.data as T;
}