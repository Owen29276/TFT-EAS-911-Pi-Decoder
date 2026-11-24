# Home Assistant integration

The logger publishes to Home Assistant over MQTT using auto-discovery. Once it is
connected, a **TFT EAS 911** device shows up in HA with no YAML needed for the entities.

## 1. Install the MQTT broker (one time)

1. **Settings → Add-ons → Add-on Store → Mosquitto broker → Install → Start.**
   Turn on "Start on boot" and "Watchdog".
2. **Settings → People → Users → Add user.**
   - Name it `tft911` and give it a strong password.
   - It doesn't need admin rights.
   - The Mosquitto add-on accepts HA users as MQTT logins.
3. **Settings → Devices & services.** HA will show an **MQTT** integration it
   discovered automatically. Click **Configure → Submit**.

## 2. Point the logger at it

`config.ini`:

```ini
[mqtt]
enabled = true
host = 192.168.1.50        # your Home Assistant IP
port = 1883
local_fips = 036109        # counties that count as "local" (default: [station] fips)
```

`secrets.ini` (`cp secrets.ini.example secrets.ini && chmod 600 secrets.ini`):

```ini
[mqtt]
username = tft911
password = <the password from step 1>
```

Then run `sudo systemctl restart tft911-eas`. The logger log should show
`MQTT connected — publishing Home Assistant discovery`.

## 3. Entities

| Entity | What it is |
|---|---|
| `binary_sensor.tft_eas_911_local_warning` | **ON** while an unexpired *warning* covers one of your `local_fips` counties |
| `sensor.tft_eas_911_active_alerts` | Count of unexpired local, non-test alerts (list in attributes) |
| `sensor.tft_eas_911_last_alert` | e.g. "Tornado Warning". Attributes hold the full record: code, category, locations, expiry, header |
| `sensor.tft_eas_911_last_event_code` | e.g. `TOR` |
| `sensor.tft_eas_911_last_alert_time` | Timestamp |
| `sensor.tft_eas_911_transcript` | Whisper transcript. HA caps state at 255 chars, so the full text is in the `text` attribute |
| `sensor.tft_eas_911_last_weekly_test` | Timestamp of the last RWT received |
| `event.tft_eas_911_alert` | Fires on every new alert. `event_type` is `warning` / `watch` / `statement` / `test` / `admin` / `other` |

How the sensors behave:

- **Availability.** All entities go *unavailable* if the logger stops or the Pi loses
  power (MQTT last-will), so you also get a free health alarm.
- **Local matching.** Matching follows SAME rules: `000000` = whole US, `SS000` = whole
  state, and a leading `0` = whole county.
- **Expiry.** Watches and statements count as active alerts, but only warnings turn on
  `local_warning`. Expiry is re-checked every 30 s, so the sensor turns off on its own.

## 4. Example automations

Paste these into **Settings → Automations → ⋮ → Edit in YAML**. Change the
`notify.mobile_app_*` and `light.*` entity IDs to yours.

### Phone alert with the transcript for any local warning

```yaml
alias: EAS – local warning to phone
triggers:
  - trigger: state
    entity_id: event.tft_eas_911_alert
conditions:
  - condition: template
    value_template: >
      {{ trigger.to_state.attributes.event_type == 'warning'
         and trigger.to_state.attributes.local }}
actions:
  # Transcript arrives a few seconds after the alert — wait for it (max 3 min)
  - wait_for_trigger:
      - trigger: state
        entity_id: sensor.tft_eas_911_transcript
    timeout: "00:03:00"
  - action: notify.mobile_app_owens_iphone
    data:
      title: "⚠️ {{ trigger.to_state.attributes.event_name }}"
      message: >
        {{ state_attr('sensor.tft_eas_911_transcript', 'text')
           or trigger.to_state.attributes.eas_text }}
      data:
        push:
          interruption-level: critical
mode: queued
```

### Flash the lights red while a local warning is active

```yaml
alias: EAS – red lights during local warning
triggers:
  - trigger: state
    entity_id: binary_sensor.tft_eas_911_local_warning
    to: "on"
actions:
  - action: light.turn_on
    target:
      entity_id: light.living_room
    data:
      color_name: red
      brightness_pct: 100
      flash: long
mode: single
```

### Tell me if the EAS logger goes offline

```yaml
alias: EAS – logger offline
triggers:
  - trigger: state
    entity_id: sensor.tft_eas_911_last_event_code
    to: unavailable
    for: "00:05:00"
actions:
  - action: notify.mobile_app_owens_iphone
    data:
      title: EAS logger offline
      message: The TFT EAS 911 logger has been unreachable for 5 minutes.
```

### Missed Required Weekly Test

```yaml
alias: EAS – no RWT in 8 days
triggers:
  - trigger: time
    at: "09:00:00"
conditions:
  - condition: template
    value_template: >
      {% set last = states('sensor.tft_eas_911_last_weekly_test') %}
      {{ last in ['unknown', 'unavailable']
         or (now() - as_datetime(last)).days >= 8 }}
actions:
  - action: notify.mobile_app_owens_iphone
    data:
      title: EAS – missed weekly test
      message: "No RWT received since {{ states('sensor.tft_eas_911_last_weekly_test') }}."
```

## Troubleshooting

- **No device in HA.** Check that the MQTT integration is set up and that the logger
  log says `MQTT connected`. `MQTT connect refused: Not authorized` means the username
  or password in `secrets.ini` is wrong.
- **Look at the raw messages.** Go to **Settings → Devices & services → MQTT → Configure
  → Listen to a topic**, enter `tft911/#`, and click **Start listening**.
- **Remove the device.** Delete it in HA, then clear the retained discovery topics
  (`homeassistant/+/tft911/#`) with MQTT Explorer. Otherwise it comes back on the
  next connect.
