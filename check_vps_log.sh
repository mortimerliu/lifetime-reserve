#!/bin/bash

SSH="ssh -i ~/.ssh/hetzner_lifetime_reserve root@204.168.135.198"
LOG_DIR="/root/lifetime-reserve/logs"

case "${1:-today}" in
  today)
    $SSH "cat $LOG_DIR/\$(date +%Y-%m-%d).log 2>/dev/null || echo 'No log for today'"
    ;;
  follow)
    $SSH "tail -f $LOG_DIR/\$(date +%Y-%m-%d).log"
    ;;
  all)
    $SSH "cat $LOG_DIR/*.log"
    ;;
  ls)
    $SSH "ls -lh $LOG_DIR/"
    ;;
  *)
    # Treat as a date (YYYY-MM-DD)
    if [[ "$1" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]]; then
      $SSH "cat $LOG_DIR/$1.log 2>/dev/null || echo 'No log for $1'"
    else
      echo "Usage: $0 [today|follow|all|ls|YYYY-MM-DD]"
      echo "  today      - today's log (default)"
      echo "  follow     - live stream today's log"
      echo "  all        - all logs concatenated"
      echo "  ls         - list log files"
      echo "  YYYY-MM-DD - specific date's log"
      exit 1
    fi
    ;;
esac
