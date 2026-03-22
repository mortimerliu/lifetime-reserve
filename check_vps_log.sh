#!/bin/bash

SSH="ssh -i ~/.ssh/hetzner_lifetime_reserve root@204.168.135.198"

case "${1:-tail}" in
  tail)
    $SSH "tail -50 /root/lifetime-reserve/reserve.log"
    ;;
  follow)
    $SSH "tail -f /root/lifetime-reserve/reserve.log"
    ;;
  all)
    $SSH "cat /root/lifetime-reserve/reserve.log"
    ;;
  *)
    echo "Usage: $0 [tail|follow|all]"
    echo "  tail   - last 50 lines (default)"
    echo "  follow - live stream"
    echo "  all    - full log"
    exit 1
    ;;
esac
