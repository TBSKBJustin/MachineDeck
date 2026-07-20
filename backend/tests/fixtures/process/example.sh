#!/usr/bin/env bash
set -euo pipefail

counter=0
while true; do
    echo "MachineDeck process fixture log ${counter}"
    counter=$((counter + 1))
    /usr/bin/sleep 1
done
