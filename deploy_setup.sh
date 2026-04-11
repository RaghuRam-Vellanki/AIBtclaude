#!/bin/bash
# Run this ONCE on your cloud server after uploading the bot files
# Usage: bash deploy_setup.sh

set -e
echo "=== BTC Trading Agent — Server Setup ==="

# 1. Update system
sudo apt-get update -y && sudo apt-get upgrade -y

# 2. Install Python 3.11
sudo apt-get install -y python3.11 python3.11-venv python3-pip git screen

# 3. Create virtual environment
cd ~/xauusdagent
python3.11 -m venv venv
source venv/bin/activate

# 4. Install dependencies
pip install --upgrade pip
pip install -r requirements.txt

# 5. Create logs directory
mkdir -p logs

echo ""
echo "Setup complete!"
echo "Next: run   bash deploy_start.sh"
