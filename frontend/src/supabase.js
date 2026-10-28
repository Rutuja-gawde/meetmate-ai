import { createClient } from '@supabase/supabase-js'

const url = import.meta.env.VITE_SUPABASE_URL
const anonKey = import.meta.env.VITE_SUPABASE_ANON_KEY

export const supabaseConfigured = Boolean(url && anonKey)
export const supabase = supabaseConfigured ? createClient(url, anonKey) : null

// What MeetMate may do in the user's Google account (the narrowest scopes that work):
// add events to their calendar and send the minutes email as them.
export const GOOGLE_SCOPES =
  'https://www.googleapis.com/auth/calendar.events https://www.googleapis.com/auth/gmail.send'

// access_type=offline + prompt=consent make Google hand back a refresh token, which the
// backend needs so it can add calendar events / send email later, after the user has left.
export function signInWithGoogle() {
  return supabase.auth.signInWithOAuth({
    provider: 'google',
    options: {
      redirectTo: window.location.origin,
      scopes: GOOGLE_SCOPES,
      queryParams: { access_type: 'offline', prompt: 'consent' },
    },
  })
}

export function signOut() {
  return supabase.auth.signOut()
}
