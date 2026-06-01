#!/bin/bash
cd "$(dirname "$0")"
. "$HOME/.cargo/env"
npm run tauri dev
