import AsyncStorage from "@react-native-async-storage/async-storage";
import { Stack, useRouter } from "expo-router";
import * as Linking from "expo-linking";
import * as Notifications from "expo-notifications";
import * as SplashScreen from "expo-splash-screen";
import { useEffect } from "react";
import { LogBox, Platform } from "react-native";

import { useIconFonts } from "@/src/hooks/use-icon-fonts";


// Disable logbox errors etc so that users can see the app
// and agent works as expected.
LogBox.ignoreAllLogs(true)

// Keep the native splash visible from cold start until icon fonts register.
// Required because @expo/vector-icons' componentDidMount fallback fires
// Font.loadAsync against a broken vendor path if any <Icon> mounts before
// the family is registered — which throws on Android Expo Go.
SplashScreen.preventAutoHideAsync();

// Push notification foreground behavior — must live at module scope so it is
// registered before any component mounts a listener.
if (Platform.OS !== "web") {
  Notifications.setNotificationHandler({
    handleNotification: async () => ({
      shouldShowAlert: true,
      shouldShowBanner: true,
      shouldShowList: true,
      shouldPlaySound: true,
      shouldSetBadge: false,
    }),
  });
}

// Android channel — created at module scope so it exists before the first push arrives.
if (Platform.OS === "android") {
  Notifications.setNotificationChannelAsync("default", {
    name: "Default",
    importance: Notifications.AndroidImportance.MAX,
    sound: "default",
    vibrationPattern: [0, 250, 250, 250],
    lightColor: "#9E2A2B",
  });
}

export default function RootLayout() {
  const [loaded, error] = useIconFonts();
  const router = useRouter();

  useEffect(() => {
    if (loaded || error) {
      SplashScreen.hideAsync();
    }
  }, [loaded, error]);

  useEffect(() => {
    if (Platform.OS === "web") return;

    const routeFor = (data: Record<string, any> | undefined) => {
      if (!data) return null;
      const url = data.deeplink || data.action_url;
      if (typeof url === "string" && url.length) return url;
      if (data.type === "SOS" && data.sosId) return `/?sos=${data.sosId}`;
      if (data.type === "BLOOD_REQUEST" && data.requestId) return `/?blood=${data.requestId}`;
      return null;
    };

    const openTarget = (data: Record<string, any> | undefined) => {
      const url = routeFor(data);
      if (!url) return;
      if (url.startsWith("http")) Linking.openURL(url);
      else router.push(url as any);
    };

    const tapSub = Notifications.addNotificationResponseReceivedListener((response) => {
      openTarget(response.notification.request.content.data as any);
    });

    Notifications.getLastNotificationResponseAsync().then((response) => {
      if (response) openTarget(response.notification.request.content.data as any);
    });

    // Weekly nudge if the user has permanently denied notifications.
    (async () => {
      try {
        const { status, canAskAgain } = await Notifications.getPermissionsAsync();
        if (status !== "denied" || canAskAgain) return;
        const lastNudge = await AsyncStorage.getItem("pushNudgeAt");
        const oneWeek = 7 * 24 * 60 * 60 * 1000;
        if (lastNudge && Date.now() - Number(lastNudge) <= oneWeek) return;
        await AsyncStorage.setItem("pushNudgeAt", String(Date.now()));
        Linking.openSettings();
      } catch {
        // permission read is best-effort; never crash the app on it
      }
    })();

    return () => {
      tapSub.remove();
    };
  }, [router]);

  // If the CDN is unreachable we fall through on error rather than wedging
  // the app — icons will tofu, but the app still boots.
  if (!loaded && !error) return null;

  return <Stack screenOptions={{ headerShown: false }} />;
}
