#!/bin/bash
# Installs the bot as a systemd service (survives reboots, auto-restarts on crash)
# Run AFTER deploy_setup.sh
# Usage: bash deploy_service.sh

cd ~/xauusdagent

# Install service
sudo cp btcagent.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable btcagent
sudo systemctl start btcagent

echo ""
sudo systemctl status btcagent --no-pager
echo ""
echo "=== Bot is now running as a system service ==="
echo ""
echo "Commands:"
echo "  Status:     sudo systemctl status btcagent"
echo "  Live logs:  sudo journalctl -u btcagent -f"
echo "  Stop:       sudo systemctl stop btcagent"
echo "  Restart:    sudo systemctl restart btcagent"
echo "  Disable:    sudo systemctl disable btcagent"
