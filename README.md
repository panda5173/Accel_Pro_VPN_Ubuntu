# AccelPro VPN Launcher

Unofficial desktop launcher and monitor for the **AccelPro Zero Trust Linux client**
on Debian / Ubuntu LTS. Not affiliated with or endorsed by Accelpro.

## Features
- One-click Connect / Disconnect; WebAuth sign-in in a compact window (browser-saved password + TOTP)
- Live tunnel statistics, throughput graph and connection uptime
- Link-loss detection (airplane mode / server silence) with alert and Reconnect
- Optional reachability tiles for up to 4 internal services (host + TCP port)
- Desktop notifications, system-tray icon, Dark / Light theme, start at login
- Password and TOTP are never stored; admin rights only for connect / disconnect

## Build the .deb
The AccelPro Linux client is proprietary and is **not** included in this repository.
Download it from your organisation's AccelPro user portal, then:

    sudo apt install dpkg-dev python3
    ./build-deb.sh /path/to/accelpro_client

## Install
    sudo apt install ./accelpro-vpn_1.2_amd64.deb

Open **AccelPro VPN** → Settings → enter the server URL and username from your administrator → Save Profile → Connect.

## Administrator defaults (optional)
Edit `config/defaults.json` before building (or `/etc/accelpro-vpn/defaults.json` after install):

    {
      "server": "https://vpn.example.com:9555",
      "username": "",
      "services": [ {"name": "File Server", "host": "10.0.0.10", "port": 445} ]
    }

Keep real server addresses out of public repositories.

## Files
| Path | Purpose |
|---|---|
| `/opt/accelpro-vpn/` | application, AccelPro client, root helper, vendored pystray |
| `/usr/bin/accelpro-vpn` | launcher (`--minimized`, `--version`) |
| `/etc/accelpro-vpn/defaults.json` | system defaults (conffile) |
| `~/.config/accelpro-vpn/settings.json` | per-user settings |
| `~/.local/state/accelpro-vpn/accelpro-vpn.log` | rotating log |

## Remove
    sudo apt remove accelpro-vpn     # keep /etc defaults
    sudo apt purge accelpro-vpn      # remove everything

## Licence
Add a LICENSE file of your choice before publishing. Bundled pystray is LGPL-3.0.
