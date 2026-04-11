#!/bin/bash
# Starts the bot inside a 'screen' session so it keeps running after SSH disconnect
# Usage: bash deploy_start.sh

cd ~/xauusdagent
source venv/bin/activate

# Kill any old session
screen -S btcbot -X quit 2>/dev/null || true

# Start fresh session
screen -dmS btcbot bash -c "
  cd ~/xauusdagent
  source venv/bin/activate
  while true; do
    echo '--- Bot starting at '$(date)' ---' >> logs/agent.log
    python agent.py >> logs/agent.log 2>&1
    echo '--- Bot crashed or stopped, restarting in 10s ---' >> logs/agent.log
    sleep 10
  done
"

echo ""
echo "Bot started in background screen session 'btcbot'"
echo ""
echo "Useful commands:"
echo "  View live logs:    tail -f ~/xauusdagent/logs/agent.log"
echo "  Attach to session: screen -r btcbot"
echo "  Detach (keep running): Ctrl+A then D"
echo "  Stop the bot:      screen -S btcbot -X quit"
echo "  Check running:     screen -ls"
