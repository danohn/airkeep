# Hosted Airkeep privacy disclosures

Use this data inventory to prepare the public privacy notice and the App Store privacy answers. Add the service operator's name and privacy contact and confirm the deployed retention and backup settings before publication.

| Data | Purpose and handling |
| --- | --- |
| Email address and account identifier | Sign in, verify account ownership and reset passwords. Airkeep stores an Argon2id password hash. Twilio Email receives the recipient address and the one-time account link for delivery. |
| AMS humidity, temperature, index and observation time | Show readings and history and evaluate humidity alerts. Readings belong to the enrolled account and printer source. The LAN agent keeps the printer IP, serial, LAN access code and raw MQTT reports on the user's network. |
| Installation identifiers and notification tokens | Manage app and display connections and deliver opted-in alerts, Live Activity updates and Watch widget refresh hints via Apple Push Notification service. Hosted humidity notification text is generic. |
| Account and device security metadata | Verify optional sign-in methods, enforce rate limits, expire or revoke device sessions, and troubleshoot delivery. Airkeep stores passkey public keys, encrypted authenticator secrets, and hashed recovery codes. Hosted email and login limits use keyed hashes of the relevant address or IP. |

The App Store disclosures should include **Email Address**, **User ID**, **Device ID** and **Other Data Types**, linked to the account for app functionality. Review any additional data used by the final build or deployment before submission. [App Store privacy details](https://developer.apple.com/app-store/app-privacy-details/)

Hosted history and alert events are kept for seven days. The latest reading may remain visible with its original timestamp until the account, printer connection, or reading is replaced. The LAN agent keeps up to 24 hours of readings pending upload. Account settings let users delete their hosted account after confirming their password and, when enabled, an authenticator or recovery code. The service revokes connected grants and removes account data. The backup runbook specifies a 30-day archive expiry and a separate deletion ledger for recovery. [Hosted deployment and recovery](hosted-deployment.md)
