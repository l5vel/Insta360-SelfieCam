#!/bin/bash

# Exit immediately if a command exits with a non-zero status
set -e

SERVICE_NAME="insta360.service"
SERVICE_PATH="/home/base3/Insta360-SelfieCam/${SERVICE_NAME}"
SYSTEMD_PATH="/etc/systemd/system/${SERVICE_NAME}"

echo "=== Insta360 Service Installer ==="

# Check if the service file exists
if [ ! -f "$SERVICE_PATH" ]; then
    echo "Error: Service template not found at $SERVICE_PATH"
    exit 1
fi

echo "1. Copying service file to systemd directory..."
sudo cp "$SERVICE_PATH" "$SYSTEMD_PATH"

echo "2. Reloading systemd manager configuration..."
sudo systemctl daemon-reload

echo "3. Enabling the service to start on boot..."
sudo systemctl enable "$SERVICE_NAME"

echo "4. Starting the service..."
sudo systemctl start "$SERVICE_NAME"

echo "5. Checking service status..."
sudo systemctl status "$SERVICE_NAME"

echo "==========================================="
echo "Insta360 service has been installed, enabled, and started!"
echo "To check logs, run: journalctl -u $SERVICE_NAME -f"
echo "==========================================="
