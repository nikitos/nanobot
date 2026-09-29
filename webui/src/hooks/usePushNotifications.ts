import { useCallback, useEffect, useRef, useState } from "react";

import {
  fetchPushVapidKey,
  subscribePush,
  unsubscribePush,
} from "@/lib/api";
import { useClient } from "@/providers/ClientProvider";

export type PushNotificationsStatus =
  | "idle"
  | "unsupported"
  | "enabling"
  | "enabled"
  | "disabling"
  | "error";

export interface UsePushNotifications {
  status: PushNotificationsStatus;
  error: string | null;
  enable: () => Promise<boolean>;
  disable: () => Promise<boolean>;
}

function urlBase64ToUint8Array(base64: string): Uint8Array<ArrayBuffer> {
  const padding = "=".repeat((4 - (base64.length % 4)) % 4);
  const b64 = (base64 + padding).replace(/-/g, "+").replace(/_/g, "/");
  const raw = atob(b64);
  const buffer = new ArrayBuffer(raw.length);
  const bytes = new Uint8Array(buffer);
  for (let i = 0; i < raw.length; i += 1) {
    bytes[i] = raw.charCodeAt(i);
  }
  return bytes;
}

function pushSupported(): boolean {
  return (
    typeof window !== "undefined" &&
    "serviceWorker" in navigator &&
    "PushManager" in window &&
    "Notification" in window
  );
}

async function getSubscription(): Promise<PushSubscription | null> {
  const registration = await navigator.serviceWorker.ready;
  return registration.pushManager.getSubscription();
}

export function usePushNotifications(enabled: boolean): UsePushNotifications {
  const { token } = useClient();
  const tokenRef = useRef(token);
  tokenRef.current = token;
  const [status, setStatus] = useState<PushNotificationsStatus>("idle");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!enabled) return;
    if (!pushSupported()) {
      setStatus("unsupported");
      return;
    }
    let cancelled = false;
    void (async () => {
      try {
        const subscription = await getSubscription();
        if (!cancelled && subscription) setStatus("enabled");
      } catch {
        // No active subscription yet; stay idle.
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [enabled]);

  const enable = useCallback(async (): Promise<boolean> => {
    if (!pushSupported()) {
      setStatus("unsupported");
      return false;
    }
    setStatus("enabling");
    setError(null);
    try {
      if (Notification.permission === "denied") {
        throw new Error("Notification permission was denied for this site.");
      }
      const permission = await Notification.requestPermission();
      if (permission !== "granted") {
        throw new Error("Notification permission was not granted.");
      }
      const registration = await navigator.serviceWorker.ready;
      const publicKey = (await fetchPushVapidKey(tokenRef.current)).publicKey;
      const subscription = await registration.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: urlBase64ToUint8Array(publicKey),
      });
      const json = subscription.toJSON();
      if (!json || !json.endpoint) throw new Error("Push subscription is missing details.");
      await subscribePush(tokenRef.current, {
        endpoint: json.endpoint,
        keys: {
          p256dh: json.keys?.p256dh ?? "",
          auth: json.keys?.auth ?? "",
        },
      });
      setStatus("enabled");
      return true;
    } catch (err) {
      setError((err as Error).message);
      setStatus("error");
      return false;
    }
  }, []);

  const disable = useCallback(async (): Promise<boolean> => {
    if (!pushSupported()) {
      setStatus("unsupported");
      return false;
    }
    setStatus("disabling");
    setError(null);
    try {
      const subscription = await getSubscription();
      if (subscription) {
        await unsubscribePush(tokenRef.current, subscription.endpoint);
        await subscription.unsubscribe();
      }
      setStatus("idle");
      return true;
    } catch (err) {
      setError((err as Error).message);
      setStatus("error");
      return false;
    }
  }, []);

  return { status, error, enable, disable };
}
