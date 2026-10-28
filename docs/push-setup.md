# APNs and background monitoring

Remote alerts need an Apple Developer Program team and an APNs token-signing key. A Developer ID Application certificate serves a different purpose. Keep the downloaded `.p8` files private and out of Git and chat.

## Configure APNs

1. In Apple Developer → Certificates, Identifiers & Profiles → Keys, create separate sandbox and production topic-specific APNs keys restricted to your configured iOS and Mac bundle IDs. Record the key IDs and team ID. Store the keys in a private directory with owner-only permissions. The Docker deployment mounts them read-only from its private runtime directory.
2. Enable Push Notifications and provision both app identifiers as described in [Apple setup](apple-setup.md).
3. Set the relay's APNs configuration in its ignored `.env`:

```dotenv
AIRKEEP_APNS_TEAM_ID=YOUR_TEAM_ID
AIRKEEP_APNS_KEY_ID=YOUR_KEY_ID
AIRKEEP_APNS_KEY_PATH=/private/path/AuthKey_YOUR_KEY_ID.p8
AIRKEEP_APNS_KEY_ENVIRONMENT=sandbox
AIRKEEP_APNS_PRODUCTION_KEY_ID=YOUR_PRODUCTION_KEY_ID
AIRKEEP_APNS_PRODUCTION_KEY_PATH=/private/path/AuthKey_PRODUCTION_KEY_ID.p8
AIRKEEP_IOS_TOPIC=io.airkeep
AIRKEEP_MAC_TOPIC=io.airkeep.mac
```

4. Restart the relay. In Settings → Relay, check Apple push setup. "Configuration found" means the relay has a readable P-256 key for this app's topic and push environment. It does not verify Apple's topic authorization or delivery. Advanced shows the read-only environment and app topic.
5. In Settings → Notifications, enable Receive alerts on this device. Airkeep asks for system permission only if it hasn't been requested. Denied permission must be changed in system settings. The switch changes only after the relay acknowledges it; a failed request keeps the confirmed state and offers Retry. System permission and relay registration are shown separately. Neither proves visible delivery.
6. In Settings → Humidity alerts, save and enable a rule with the AMS, threshold and hold period you want. Every enabled rule applies to every subscribed device. Other devices use the same shared rules but choose their own subscription. Setup doesn't send a test notification.

Turning delivery off removes this device's queued humidity notifications. It does not unregister the app from APNs or end Live Activities. New installations start with delivery off; existing relay registrations retain delivery during migration. Token refreshes preserve an opt-out.

## Delivery and monitoring sessions

The relay sends HTTP/2 requests with ES256 authorization tokens refreshed every 50 minutes. Each device registration carries its APNs environment and platform topic.

Live Activity updates use `<ios bundle id>.push-type.liveactivity`, `apns-push-type: liveactivity` and priority 5. The relay sends updates every five minutes. If the sensor reading is fresh, the display stays current for six minutes after the update, allowing one minute for delivery. Missing or old sensor readings are marked out of date immediately in that update. The original sample timestamp stays visible. A missed or delayed push can still cause the display to become stale. The dashboard and alert engine keep their separate sensor freshness checks.

Choose Start Live Activity in Settings → Live Activities on iPhone. Starting requires Live Activities permission, a fresh relay reading and configured iOS push credentials. The page shows update registration as pending, confirmed or failed with Retry. Ending works locally even if the relay is unreachable. Dynamic Island has no separate permission; its presentation is controlled by the system. On Mac, Settings explains the iPhone session rather than offering Start. Sessions last up to eight hours, show their end time and can be ended early with End Live Activity. The app registers the activity's update token, watches for token rotation and resumes watching existing sessions when reopened. Token rotation does not extend the session. The relay sends an end event at expiry. Airkeep doesn't use push-to-start tokens or broadcast channels.

The relay must stay running to check humidity limits while the app is closed. It needs outbound Internet access to APNs. Fetching readings also needs a network route to the relay, including when the phone is away from home. APNs is optional for local readings and history. The ESP32 uses the same relay API and never receives the APNs keys.

## What has been checked

Sandbox alert delivery was confirmed on the physical iPhone, and background Live Activity updates restored and retained Dynamic Island visibility after the relevance-score fix. The Mac sandbox test was accepted by APNs, but its banner wasn't independently captured.

Production delivery, long sessions, token rotation, sustained-threshold and recovery notifications, and session ending while the app is terminated still need their own device checks. An APNs HTTP 200 confirms acceptance by Apple, not that a person saw the notification.
