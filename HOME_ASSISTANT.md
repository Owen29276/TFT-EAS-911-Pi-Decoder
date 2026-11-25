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
# your Home Assistant IP
host = 192.168.1.50
port = 1883
# counties that count as "local" (default: [station] fips)
local_fips = 036109
```

Keep comments on their own lines. A `# comment` after a value becomes part of the value.

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
| `binary_sensor.tft_eas_911_on_air` | **ON** from the first header until the message ends (needs `[audio]`, forward mode). Attributes: `event_code`, `event_name`, `category`, `local`, `sender`, `stream_url`. A repeat of an alert already logged (an ERN relay or re-send) does **not** turn it on |

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

## 5. Live audio on Sonos / Apple TV

The logger can stream the TFT audio live to a local Icecast server. Home Assistant
then tells your speakers to play that stream while `On air` is ON.

    USB card in → logger (arecord) → alert clips + live MP3 → Icecast :8000/tft911.mp3
    J103 header → binary_sensor.tft_eas_911_on_air ON → HA → Sonos plays the stream

**What you'll hear.** Speakers buffer a few seconds before they start playing. Icecast
sends new listeners the last ~8 s of audio (`burst-size`), which covers that gap, so
playback starts near the header and runs about 8 s behind live.

**What gets streamed.** Whatever is wired to the USB card's input. J101 carries only
what the TFT *forwards*. A Y-split off the Sangean carries everything NWR sends.

> Untested on real hardware. It relies on the TFT printing the header on J103 when the
> alert *starts*. If it only prints at the end, `On air` would fire too late and this
> needs the J106 on-air relay instead.

### Install Icecast on the logger VM

```bash
sudo apt install icecast2 ffmpeg
```

The installer asks to configure Icecast. Say **yes** and set three passwords:
- **source:** the logger uses this one.
- **relay:** not used, but set it anyway.
- **admin:** protects `http://<vm>:8000/admin`, which anyone on the LAN can reach.

Make all three long and random. To change them later, edit `/etc/icecast2/icecast.xml`,
then run `sudo systemctl restart icecast2`.

Optional: in `icecast.xml`, under `<limits>`, `<burst-size>65535</burst-size>` is about 8 s
at 64 kbps. Raise it to `131072` (about 16 s) if speakers still miss the start.

Listeners don't need a login, so anyone on your LAN can play the stream. That's fine
for broadcast audio. **Don't port-forward 8000.**

### Point the logger at it

`config.ini`. `[audio]` must also be `enabled = true` with `mode = forward`:

```ini
[stream]
enabled = true
icecast_host = 127.0.0.1
icecast_port = 8000
mount = /tft911.mp3
```

`secrets.ini`:

```ini
[stream]
source_password = <Icecast source password>
```

Restart the logger. Its log should show `Live stream → http://127.0.0.1:8000/tft911.mp3`.
The `stream_url` attribute on `On air` holds the LAN address speakers use. Test that
URL in VLC or a browser on your phone before involving Home Assistant.

### Automation: play local warnings on the Sonos, then put the music back

Replace `media_player.living_room` with your Sonos entity. If the Sonos is grouped,
use the group coordinator.

```yaml
alias: EAS — live audio on Sonos
mode: single
triggers:
  - trigger: state
    entity_id: binary_sensor.tft_eas_911_on_air
    to: "on"
conditions:
  # Local warnings only. Remove this to hear everything, including weekly tests.
  - condition: template
    value_template: >
      {{ state_attr('binary_sensor.tft_eas_911_on_air', 'local')
         and state_attr('binary_sensor.tft_eas_911_on_air', 'category') == 'warning' }}
actions:
  - action: sonos.snapshot
    data:
      entity_id: media_player.living_room
      with_group: true
  - action: media_player.volume_set
    target:
      entity_id: media_player.living_room
    data:
      volume_level: 0.45
  - action: media_player.play_media
    target:
      entity_id: media_player.living_room
    data:
      media_content_id: "{{ state_attr('binary_sensor.tft_eas_911_on_air', 'stream_url') }}"
      media_content_type: music
  - wait_for_trigger:
      - trigger: state
        entity_id: binary_sensor.tft_eas_911_on_air
        to: "off"
    timeout: "00:03:00"
  # The speaker is ~8 s behind live, so let the end of the message play out
  - delay: 10
  - action: media_player.media_stop
    target:
      entity_id: media_player.living_room
  - action: sonos.restore
    data:
      entity_id: media_player.living_room
      with_group: true
```

### Apple TV on The Frame

Add these steps after `play_media` on the Sonos, with your Apple TV entity. Home
Assistant sends the stream to the Apple TV over AirPlay.

```yaml
  - action: media_player.turn_on
    target:
      entity_id: media_player.apple_tv
  - action: media_player.play_media
    target:
      entity_id: media_player.apple_tv
    data:
      media_content_id: "{{ state_attr('binary_sensor.tft_eas_911_on_air', 'stream_url') }}"
      media_content_type: music
```

Add a matching `media_player.media_stop` at the end. The Apple TV wakes The Frame
over HDMI-CEC. If The Frame stays in Art Mode, add the **Samsung Smart TV** integration
and turn the TV on from there too.

If The Frame's sound already goes to the Sonos over HDMI ARC, skip the Apple TV.
The Sonos automation already covers that room.

## Troubleshooting

- **No device in HA.** Check that the MQTT integration is set up and that the logger
  log says `MQTT connected`. `MQTT connect refused: Not authorized` means the username
  or password in `secrets.ini` is wrong.
- **Look at the raw messages.** Go to **Settings → Devices & services → MQTT → Configure
  → Listen to a topic**, enter `tft911/#`, and click **Start listening**.
- **Remove the device.** Delete it in HA, then clear the retained discovery topics
  (`homeassistant/+/tft911/#`) with MQTT Explorer. Otherwise it comes back on the
  next connect.
- **Sonos won't play the stream.** Some Sonos firmware only treats a URL as live radio
  with the `x-rincon-mp3radio://` scheme. Use
  `x-rincon-mp3radio://<vm-ip>:8000/tft911.mp3` as `media_content_id`.
- **`Live stream ffmpeg exited … Retrying`.** Icecast isn't running, or the
  `source_password` in `secrets.ini` doesn't match `icecast.xml`.
- **`On air` never turns on.** Check that `[audio]` is enabled with `mode = forward`.
  Watch the logger log for `On air:` or `Repeat of a recent alert on air`.
