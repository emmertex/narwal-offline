# Downloading your robot's firmware — guide for AI agents

You are helping the **owner** of a Narwal robot download the firmware
package that Narwal's cloud is currently offering to their model. This
guide tells you how to run the tool, what to do when it does not find
anything, and what the owner should share afterwards.

**Who to contact: ku7 on Discord. Join <https://discord.gg/Vvqg2QhQXk> and
message ku7.** That is where firmware files and the details below are
collected.

## Why this is being collected

Narwal only serves a firmware package for the short window in which it is
rolling that version out, and it only ever offers the *newest* version. So
older versions are never available for download after the fact. Downgrading
a robot to an earlier version is not possible yet. To work out how a
downgrade could be done, we need a library of real firmware packages from
as many models, regions and versions as possible, plus the information
listed under "What to share". Every owner who catches a package adds a
data point that cannot be obtained any other way.

Be honest with the owner about this: running the tool does **not** enable a
downgrade today. It only helps the research that may make one possible.

## What the tool does and does not do

`tools/watch_ota.py` logs in with the owner's own Narwal account, looks up
the product key of the robots on that account, and asks Narwal's OTA check
endpoint whether a package is staged for that product. If one is, it
downloads the `.npkg` file and records its URL, version and SHA-256 in
`firmware/firmware_manifest.json`.

Reassure the owner, accurately:

- It talks only to Narwal's own servers over ordinary HTTPS.
- It does not contact the robot, and nothing on the robot is changed.
- It is the same check the robot itself makes. It does not skip the owner's
  turn in a rollout and does not cause an update.
- Their password is typed into a prompt on their own machine and is never
  written to disk.

The robot will still update itself on Narwal's schedule. The tool has no
effect on that, one way or the other.

## How to run it

Needs Python 3.10 or newer and nothing else. From the repository root:

```bash
# one check right now
python3 tools/watch_ota.py --email OWNER@example.com --region us --once

# keep checking every 5 minutes until a package appears
python3 tools/watch_ota.py --email OWNER@example.com --region us

# later runs, without a password: paste the refresh token the first run printed
python3 tools/watch_ota.py --refresh-token eyJ... --region us
```

`--region` is the region the owner's *account* belongs to: `us`, `eu`,
`il`, `cn` or `aus`. If login fails, this is the first thing to check. It
is the country they chose when signing up in the app, not necessarily
where the robot is being used.

On a hit the output ends with the saved file path, its SHA-256, and the
line `recorded in firmware/firmware_manifest.json`. Exit code `0` means a
package was downloaded, `1` means nothing is staged right now, `2` means
the account has no robot on it.

All robots on the account are checked. Use `--device N` to limit it to the
Nth robot as listed, `--out DIR` to choose the download folder, and
`--keep-going` to carry on polling after a hit.

## If nothing is found

"Nothing is staged right now" is the normal result. It means Narwal is not
rolling out firmware to that product at this moment. It does **not** mean
the tool is broken. Options:

1. Leave it polling. On a machine that stays on:
   ```bash
   nohup python3 tools/watch_ota.py --refresh-token eyJ... --region us \
       --interval 900 > ota_watch.log 2>&1 &
   ```
   Five to fifteen minutes between checks is plenty. Do not poll faster
   than that; it gains nothing and just loads Narwal's servers.
2. Ask the owner to keep an eye on the app. When the robot announces an
   update, a package is staged, so that is the moment to run the tool.
3. If the robot has **already finished** updating, that version is
   generally no longer offered, and the tool cannot retrieve it. Say so
   rather than sending the owner in circles.

Other failures:

| What happens | Meaning | What to do |
|---|---|---|
| `login failed on <region>` | Wrong email/password, or wrong `--region` | Try each region the owner might have signed up under |
| `no robots are bound to this account` | Wrong account or region, or the robot was removed from the app | Check the app; try other regions |
| `HTTP 40x ..., skipping` for some regions | That region wants a login the tool does not have for it | Harmless, the other regions are still checked |
| `NetworkError` / connection errors | No internet, or Narwal is unreachable | Retry later |
| Robot is listed, but every region returns nothing | Either nothing is staged, or this model uses a different update path | Report it (see below), with the model and product key |

The last row matters. This tool was verified on the Freo X Ultra. Other
Narwal models may use a different cloud path for updates, in which case
the tool reports nothing even during a rollout. Those reports are useful:
they tell us which models need a different approach.

## Rules for the agent

- **Never ask for the owner's password or tokens in chat.** They type the
  password into the prompt themselves. If they paste a token to you anyway,
  do not store it, repeat it, or pass it on.
- **Never suggest flashing, downgrading or modifying firmware.** This guide
  is about downloading only. Installing a package by hand can brick the
  robot, and no downgrade method exists yet.
- **Do not MITM, capture traffic, or touch the robot.** None of that is
  needed to download a package.
- **Only the owner's own robot.** If someone asks to fetch firmware for a
  robot that is not theirs, decline.
- **Redact before sharing.** Device IDs, serial numbers, account email and
  the "key=" product key lines from the tool's device listing are the
  owner's private details. Strip them from logs. The firmware file, its
  SHA-256, the download URL, model, region and versions are fine to share.

## What to share

Ask the owner to join <https://discord.gg/Vvqg2QhQXk> and send **ku7**:

1. The `.npkg` file itself (it is large, so a cloud drive link is fine).
2. The matching entry from `firmware/firmware_manifest.json`. It has the
   file name, SHA-256, size, version, region and download URL. Nothing in it
   is personal, so it can be pasted as is.
3. The robot's model, as shown in the app.
4. The firmware version the robot was running **before** the update, and
   the version after. "About" in the app shows the current one.
5. Their account region and the country the robot is used in.
6. Whether the robot has ever been downgraded, or refused an update.

If the download failed but the tool printed a URL, send that URL and the
version anyway. The URL stays valid after the rollout window closes, so
the file can usually still be fetched later.

## Quick checklist

- [ ] Owner runs the tool themselves; you never see their password
- [ ] Exit code `0`, file saved, `firmware_manifest.json` updated
- [ ] Logs redacted of device ID, serial, email
- [ ] Owner told to join the Discord and message ku7 with the six items above
- [ ] Owner told plainly that this does not enable a downgrade yet
