#!/bin/sh

VICINAE=${VICINAE_BIN:-vicinae}
DICTIONARY='@local/local-mdict-dictionary:dictionary'
SELECTION_COMMAND='@local/local-mdict-dictionary:lookup-selected-text'

selection=$(timeout 0.8 xclip -o -selection primary 2>/dev/null || true)

if [ -n "$selection" ]; then
  exec "$VICINAE" cmd launch "$DICTIONARY" --query "$selection"
fi

exec "$VICINAE" cmd launch "$SELECTION_COMMAND"
