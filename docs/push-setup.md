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

4. Restart the relay. In Humidity alerts, check Server push setup. "Configuration found" means the relay has a readable P-256 key for this app's topic and push environment. It does not verify Apple's topic authorization or delivery. Expand Server details for setup errors. Older relays show "Relay update needed" until they support this status check.
5. On each signed device, choose Enable notifications if permission hasn't been requested. Once allowed, the action becomes Notification settings. Airkeep refreshes system permission and relay registration when opened or made active, without asking for permission again. Denied permission must be changed in system settings. Relay registration confirms the current server has this device token; it does not confirm delivery.
6. Save an enabled humidity rule with the threshold and hold period you want. The rule applies to registered devices. Device permission and registration take effect immediately, independently of Save or Cancel. Setup doesn't send a test notification.

## Delivery and monitoring sessions

The relay sends HTTP/2 requests with ES256 authorization tokens refreshed every 50 minutes. Each device registration carries its APNs environment and platform topic.

Live Activity updates use `<ios bundle id>.push-type.liveactivity`, `apns-push-type: liveactivity` and priority 5. The content state matches the app's Codable model. Updates carry the sample's stale date, and the relay sends an end event at the requested expiry.

Start a monitoring session in the app. It registers that activity's update token, watches for token rotation and resumes watching existing sessions when reopened. Airkeep doesn't use push-to-start tokens or broadcast channels. Sessions last four hours.

The relay must stay running to check humidity limits while the app is closed. It needs outbound Internet access to APNs. Fetching readings also needs a network route to the relay, including when the phone is away from home. APNs is optional for local readings and history. The ESP32 uses the same relay API and never receives the APNs keys.

## What has been checked

Sandbox alert delivery was confirmed on the physical iPhone, and background Live Activity updates restored and retained Dynamic Island visibility after the relevance-score fix. The Mac sandbox test was accepted by APNs, but its banner wasn't independently captured.

Production delivery, long sessions, token rotation, sustained-threshold and recovery notifications, and session ending while the app is terminated still need their own device checks. An APNs HTTP 200 confirms acceptance by Apple, not that a person saw the notification.
