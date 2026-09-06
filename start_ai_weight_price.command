#!/bin/sh
cd "$(dirname "$0")" || exit 1
python3 -m erp.ai_weight_price "$@"
