# Deploying MeetMate (Vercel + Supabase + your GPU PC)

```
Browser ──► Vercel (website)
   │  sign in with Google
   ├────► Supabase (login + database)
   └────► <pc>.<tailnet>.ts.net ─ Tailscale Funnel ─► your PC: backend (FastAPI) + GPU pipeline
```
Vercel only serves the website. Whisper, pyannote and Ollama need your GPU, so the backend
keeps running on your PC and the website reaches it through a tunnel. Your PC must be on.

## 1. Supabase
1. Create a project at supabase.com.
2. SQL Editor → paste `supabase/schema.sql` → Run.
3. Project Settings → API: copy the **Project URL**, the **anon** key (website) and the
   **service_role** key (backend only, secret).
4. Authentication → URL Configuration: set **Site URL** to your Vercel address and add it, plus
   `http://localhost:5173`, to **Redirect URLs**.

## 2. Google sign-in (also gives calendar + email access)
1. console.cloud.google.com → create/select a project → enable **Google Calendar API** and **Gmail API**.
2. APIs & Services → OAuth consent screen: External, add the scopes
   `.../auth/calendar.events` and `.../auth/gmail.send`.
   While the app is in **Testing**, only the test users you list (max 100) can sign in and Google
   expires their refresh tokens after 7 days (they just sign in again). To open it to everyone,
   publish the app and complete Google's verification (these scopes are "sensitive").
3. Credentials → Create **OAuth client ID** → Web application. Authorized redirect URI:
   `https://<your-project>.supabase.co/auth/v1/callback`
4. Supabase → Authentication → Providers → Google: paste the client ID and secret, enable.
5. Put the same client ID and secret in `backend/.env` (`GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`).

## 3. Backend (your PC)
1. Copy `backend/.env.example` to `backend/.env` and fill it in. Make the encryption key with:
   `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`
   Keep a copy of that key somewhere safe: without it every user must reconnect their apps.
2. `cd backend && pip install -r requirements.txt` (use a venv; the old file was an unrelated freeze).
3. `uvicorn main:app --host 127.0.0.1 --port 8000`
4. `ALLOWED_ORIGINS` must contain your Vercel address, or the browser will block the calls.

## 4. Public address for the backend (Tailscale Funnel: free, no domain needed)
Vercel needs a **stable** address for your PC. Tailscale Funnel gives one for free:
`https://<your-pc-name>.<your-tailnet>.ts.net`.
1. Install Tailscale on the PC (tailscale.com/download) and sign in.
2. Run in PowerShell: `tailscale funnel --bg 8000`
   The first time, it prints a link: open it and approve HTTPS certificates and Funnel.
3. `tailscale funnel status` shows your public address. Open `<address>/health` in a browser:
   it must show "MeetMate AI backend is running" (the backend must be running first).
4. That address is your `VITE_API_URL` on Vercel. Keep it the same: do not rename the PC.
Funnel stays on after a restart, but the PC must be on, online and running the backend.
(A free ngrok account also works but is capped at 1 GB/month and 20,000 requests/month.)

## 5. Vercel
1. Import the GitHub repo. Set **Root Directory** to `frontend` (framework: Vite).
2. Environment variables: `VITE_SUPABASE_URL`, `VITE_SUPABASE_ANON_KEY`, `VITE_API_URL=https://<your-pc-name>.<your-tailnet>.ts.net`.
3. Deploy, then put the final Vercel address into Supabase (step 1.4) and `ALLOWED_ORIGINS` (step 3).

## Using it
- Users sign in with Google; calendar + email are connected at sign-in (they can untick the
  permissions on Google's screen, in which case those two steps show "Failed").
- **Connections** (top right): Notion (token + database) and Slack (incoming webhook) are pasted per user.
- Each user sees only their own meetings and connections.

## Before you push
- `.env`, `credentials.json` and `token.json` are ignored, but if they were ever committed, rotate
  those secrets and remove them from history. `credentials.json` / `token.json` are no longer used.
- The `SUPABASE_SERVICE_ROLE_KEY` and `TOKEN_ENCRYPTION_KEY` must never appear in the frontend or on GitHub.