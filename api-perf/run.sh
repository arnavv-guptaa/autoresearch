#!/bin/bash
# Launch the autoresearch API performance optimization agent.
#
# Usage:
#   ./run.sh
#
# This starts a Claude Code session with all permissions pre-approved
# so the agent can run autonomously. It will:
#   1. Start a dev server on port 8005 from the worktree
#   2. Run a baseline benchmark
#   3. Loop forever: modify code → benchmark → keep/revert
#
# Monitor progress:
#   cat results.tsv
#   cat run.log
#   cd /Users/arnavgupta/Documents/GitHub/parallax-api-autoresearch && git log --oneline -10
#
# Stop: Ctrl+C

DIR="$(cd "$(dirname "$0")" && pwd)"

claude \
  --dangerously-skip-permissions \
  "Read ${DIR}/program.md and start the experiment loop. The worktree and branch are already set up. Skip the tag/confirm steps and just go. NEVER STOP."
