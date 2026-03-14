#\!/bin/bash
# Waits for cloudflared quick tunnel URL, writes it to .env, restarts otlcbot.
# Called by otlcbot-tunnel-url.service after otlcbot-tunnel starts.

ENV_FILE="/home/parker/OTLCBot-2.0/.env"
MAX_WAIT=60   # seconds to wait for URL before giving up
ELAPSED=0

echo "$(date): Waiting for tunnel URL..."

while [ $ELAPSED -lt $MAX_WAIT ]; do
    URL=$(journalctl -u otlcbot-tunnel --no-pager --since "5 minutes ago"           | grep -oP "https://[a-z0-9-]+\.trycloudflare\.com" | tail -1)
    if [ -n "$URL" ]; then
        echo "$(date): Got tunnel URL: $URL"
        sed -i "s|^WEBAPP_URL=.*|WEBAPP_URL=$URL|" "$ENV_FILE"
        echo "$(date): Updated .env, restarting otlcbot..."
        systemctl restart otlcbot
        echo "$(date): Done."
        exit 0
    fi
    sleep 3
    ELAPSED=$((ELAPSED + 3))
done

echo "$(date): Timed out waiting for tunnel URL."
exit 1
