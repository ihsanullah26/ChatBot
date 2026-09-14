#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

# Run this in a SECOND terminal, after watchman_demo.sh is already
# running (the UI must be live on http://127.0.0.1:8000 first).

if command -v ngrok >/dev/null 2>&1; then
  echo "Starting ngrok tunnel to http://127.0.0.1:8000 ..."
  echo "Share the https://... URL printed by ngrok with your boss."
  exec ngrok http 8000
fi

if command -v cloudflared >/dev/null 2>&1; then
  echo "Starting Cloudflare quick tunnel to http://127.0.0.1:8000 ..."
  echo "Share the https://...trycloudflare.com URL printed by cloudflared with your boss."
  exec cloudflared tunnel --url http://127.0.0.1:8000
fi

echo "Neither ngrok nor cloudflared is installed."
echo "Install either one, then run this script again."
echo "ngrok: https://ngrok.com/download"
echo "Cloudflare Tunnel: https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/"
exit 1