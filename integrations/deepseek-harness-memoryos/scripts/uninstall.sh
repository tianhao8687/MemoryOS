#!/usr/bin/env sh
set -eu

profile="${1:-memoryos}"
exec dsh plugin --profile "$profile" remove dsh-memoryos
