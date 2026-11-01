#!/usr/bin/env bun
// ABOUTME: CDK entry point. AWS hosts the sole supported production target.
// ABOUTME: Everything it assembles lives in lib/app.ts, so tests share it.

import { createPrrApp } from "../lib/app";

createPrrApp().synth();
