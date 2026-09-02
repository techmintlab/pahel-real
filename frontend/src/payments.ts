import { Platform } from "react-native";

import { api } from "@/src/api";

type CheckoutModule = {
  open: (options: Record<string, unknown>) => Promise<{
    razorpay_payment_id: string;
    razorpay_order_id?: string;
    razorpay_signature: string;
  }>;
};

/**
 * The native Razorpay module is only available inside a real Android build.
 * Expo Go, Web, and iOS-in-preview return null so callers can fall back to
 * the backend's development mock_payment flow.
 */
function getNativeCheckout(): CheckoutModule | null {
  if (Platform.OS !== "android") return null;
  try {
    // eslint-disable-next-line @typescript-eslint/no-require-imports
    const mod = require("react-native-razorpay");
    const checkout = mod?.default ?? mod;
    if (checkout && typeof checkout.open === "function") return checkout as CheckoutModule;
    return null;
  } catch {
    return null;
  }
}

export function isNativeCheckoutAvailable(): boolean {
  return getNativeCheckout() !== null;
}

type OrderResponse = {
  order_id: string;
  amount: number;
  purpose: string;
  key_id?: string | null;
  is_development_fallback?: boolean;
};

export type PaymentSuccess = {
  order_id: string;
  payment_id: string;
  amount: number;
  receipt_number?: string;
};

async function verify(kind: "donation" | "subscription", body: Record<string, unknown>): Promise<any> {
  const path = kind === "donation" ? "/donations/verify" : "/subscriptions/verify";
  return await api<any>(path, { method: "POST", body: JSON.stringify(body) });
}

async function openCheckout(
  checkout: CheckoutModule,
  order: OrderResponse,
  prefill: { name?: string; contact?: string; email?: string },
  description: string,
): Promise<{ payment_id: string; signature: string }> {
  const result = await checkout.open({
    key: order.key_id,
    amount: order.amount * 100, // backend stores rupees; Razorpay expects paise
    currency: "INR",
    name: "PAHEL FOUNDATION",
    description,
    order_id: order.order_id,
    prefill: {
      name: prefill.name || "",
      email: prefill.email || "",
      contact: prefill.contact ? prefill.contact.replace(/\D/g, "").slice(-10) : "",
    },
    theme: { color: "#9E2A2B" },
  });
  return { payment_id: result.razorpay_payment_id, signature: result.razorpay_signature };
}

export async function donate(params: { amountRupees: number; purpose?: string; prefill?: { name?: string; contact?: string; email?: string } }): Promise<PaymentSuccess> {
  const order = await api<OrderResponse>("/donations/create-order", {
    method: "POST",
    body: JSON.stringify({ amount: params.amountRupees, purpose: params.purpose || "Foundation support" }),
  });
  const checkout = getNativeCheckout();
  if (!checkout || order.is_development_fallback) {
    // Preview / non-Android / no live keys: use the backend development mock.
    const verified = await verify("donation", { order_id: order.order_id, payment_id: "mock_payment", signature: "" });
    return { order_id: order.order_id, payment_id: "mock_payment", amount: order.amount, receipt_number: verified?.receipt_number };
  }
  const { payment_id, signature } = await openCheckout(checkout, order, params.prefill || {}, params.purpose || "Foundation donation");
  const verified = await verify("donation", { order_id: order.order_id, payment_id, signature });
  return { order_id: order.order_id, payment_id, amount: order.amount, receipt_number: verified?.receipt_number };
}

export async function subscribe(params: { planId: string; planName: string; prefill?: { name?: string; contact?: string; email?: string } }): Promise<PaymentSuccess> {
  const order = await api<OrderResponse>(`/subscriptions/create-order?plan_id=${encodeURIComponent(params.planId)}`, { method: "POST" });
  const checkout = getNativeCheckout();
  if (!checkout || order.is_development_fallback) {
    const verified = await verify("subscription", { order_id: order.order_id, payment_id: "mock_payment", signature: "" });
    return { order_id: order.order_id, payment_id: "mock_payment", amount: order.amount, receipt_number: verified?.id };
  }
  const { payment_id, signature } = await openCheckout(checkout, order, params.prefill || {}, params.planName);
  const verified = await verify("subscription", { order_id: order.order_id, payment_id, signature });
  return { order_id: order.order_id, payment_id, amount: order.amount, receipt_number: verified?.id };
}
