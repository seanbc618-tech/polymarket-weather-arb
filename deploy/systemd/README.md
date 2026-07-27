# systemd deployment templates

These units are conservative templates for a small VPS deployment. They assume:

- repository checkout: `/opt/polymarket-weather-arb`
- virtualenv: `/opt/polymarket-weather-arb/.venv`
- environment file: `/etc/polymarket-weather-arb.env`
- service user/group: `polymarket-weather`

Install example:

```bash
sudo useradd --system --home /opt/polymarket-weather-arb --shell /usr/sbin/nologin polymarket-weather
sudo cp deploy/systemd/polymarket-weather-*.service deploy/systemd/polymarket-weather-*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now polymarket-weather-backup.timer
sudo bash scripts/deploy_hk_vps.sh
```

Or manually:

```bash
sudo systemctl enable --now polymarket-weather-backup.timer
sudo systemctl enable --now polymarket-weather-autopilot.service
```

Monitor from your laptop through an SSH tunnel:

```bash
uv run polymarket-weather autopilot tunnel --host <vps-ip> --user root
# open http://127.0.0.1:8765/app?lang=zh
```

Keep `TRADING_DISABLED=true` in `/etc/polymarket-weather-arb.env` until rehearsal and `live-readiness` are clean.
The autopilot unit uses the canonical `autopilot start --full-auto` entry point;
`TRADING_DISABLED` remains the deployment cutover gate.

## Cloudflare Tunnel dashboard access

The production Dashboard must continue listening only on `127.0.0.1:8765`.
Do not open port 8765 in the VPS firewall. To use a private hostname such as
`weather.example.com`, put Cloudflare Tunnel and Cloudflare Access in front of
the existing loopback listener.

1. Add the domain to Cloudflare and point its nameservers to Cloudflare.
2. Install `cloudflared` using Cloudflare's current Linux package instructions.
3. Authenticate and create a named tunnel:

   ```bash
   cloudflared tunnel login
   cloudflared tunnel create polymarket-weather
   ```

4. Before publishing DNS, create a Cloudflare Zero Trust **Self-hosted** Access
   application for the exact hostname. Add an `Allow` policy containing only
   the intended email addresses. If using One-time PIN, enable that login method
   and still restrict the policy to explicit email addresses. Never use
   `Include Everyone` or `Login Methods: One-time PIN` by itself.
5. Create `/etc/cloudflared/config.yml`, replacing both placeholders:

   ```yaml
   tunnel: <TUNNEL-UUID>
   credentials-file: /etc/cloudflared/<TUNNEL-UUID>.json

   ingress:
     - hostname: weather.example.com
       service: http://127.0.0.1:8765
       originRequest:
         httpHostHeader: weather.example.com
     - service: http_status:404
   ```

6. Route DNS only after the Access application exists, then install the tunnel
   as a service:

   ```bash
   cloudflared tunnel route dns polymarket-weather weather.example.com
   sudo cloudflared --config /etc/cloudflared/config.yml service install
   sudo systemctl enable --now cloudflared
   ```

7. Set the exact application origin in `/etc/polymarket-weather-arb.env` and
   restart the existing Autopilot service:

   ```bash
   DASHBOARD_PUBLIC_ORIGIN=https://weather.example.com
   sudo systemctl restart polymarket-weather-autopilot.service
   ```

8. Verify the boundary:

   ```bash
   sudo ss -ltnp | grep 8765
   sudo systemctl status cloudflared polymarket-weather-autopilot.service
   ```

   Port 8765 must show only `127.0.0.1:8765`. An unauthenticated browser must
   see the Cloudflare Access login, while an allowed email can open `/app` and
   submit a CSRF-protected form. Restarting the app invalidates old form tokens;
   refresh an old browser tab before submitting again.

Official references:

- <https://developers.cloudflare.com/tunnel/advanced/local-management/create-local-tunnel/>
- <https://developers.cloudflare.com/cloudflare-one/access-controls/applications/choose-application-type/>
- <https://developers.cloudflare.com/cloudflare-one/integrations/identity-providers/one-time-pin/>
