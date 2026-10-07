import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'
import fs from 'node:fs'

// If backend/certs exists (made by make_cert.py) the screen is served over HTTPS too.
const key = '../backend/certs/pos-key.pem', cert = '../backend/certs/pos.pem'
const https = fs.existsSync(key) && fs.existsSync(cert) ? { key: fs.readFileSync(key), cert: fs.readFileSync(cert) } : undefined

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: https ? { https } : {},
})
