import { supabase } from './supabase'

// In development the backend runs on its own address. In production set VITE_API_URL
// (for example https://api.yourdomain.com) in the Vercel project settings.
export const API_URL = (
  import.meta.env.VITE_API_URL ?? (import.meta.env.DEV ? 'http://localhost:8000' : '')
).replace(/\/$/, '')

// Always read the token fresh: Supabase renews it in the background.
export async function getAccessToken() {
  const { data } = await supabase.auth.getSession()
  return data.session?.access_token || null
}

export async function authHeaders() {
  const token = await getAccessToken()
  return token ? { Authorization: `Bearer ${token}` } : {}
}

// JSON request to the backend with the user's login attached. Throws Error(message).
export async function apiRequest(path, { method = 'GET', body, token } = {}) {
  const headers = token ? { Authorization: `Bearer ${token}` } : await authHeaders()
  if (body !== undefined) headers['Content-Type'] = 'application/json'

  let response
  try {
    response = await fetch(`${API_URL}${path}`, {
      method,
      headers,
      body: body !== undefined ? JSON.stringify(body) : undefined,
    })
  } catch {
    throw new Error('Could not reach the server. Check your connection and try again.')
  }
  if (response.status === 204) return null
  const data = await response.json().catch(() => ({}))
  if (!response.ok) throw new Error(data.detail || `Request failed (error ${response.status}).`)
  return data
}
